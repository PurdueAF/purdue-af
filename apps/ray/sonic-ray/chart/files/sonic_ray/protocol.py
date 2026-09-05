"""KServe v2 inference protocol over HTTP, as Triton speaks it.

Two encodings of the same request:

- plain JSON: every tensor's ``data`` is a (possibly nested) list;
- the *binary tensor extension*: the body is a JSON header followed by the
  raw little-endian bytes of each tensor that declares
  ``parameters.binary_data_size``; the ``Inference-Header-Content-Length``
  header says where the JSON ends. This is what ``tritonclient.http`` sends
  by default, and what makes a 500-jet ParticleNet batch a few hundred KB
  instead of a few MB of decimal text.

Responses mirror that: an output is returned binary when the request asked
for it (``outputs[].parameters.binary_data`` or the request-level
``binary_data_output``), JSON otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

HEADER_LENGTH = "Inference-Header-Content-Length"

# The v2 datatype names and their numpy equivalents. BYTES is deliberately
# absent: none of the CMS models take strings, and its length-prefixed
# encoding is a protocol of its own.
DTYPES: dict[str, type[np.generic]] = {
    "BOOL": np.bool_,
    "UINT8": np.uint8,
    "UINT16": np.uint16,
    "UINT32": np.uint32,
    "UINT64": np.uint64,
    "INT8": np.int8,
    "INT16": np.int16,
    "INT32": np.int32,
    "INT64": np.int64,
    "FP16": np.float16,
    "FP32": np.float32,
    "FP64": np.float64,
}
NUMPY_TO_DTYPE: dict[type[np.generic], str] = {v: k for k, v in DTYPES.items()}


class ProtocolError(ValueError):
    """A request that is malformed at the protocol level (HTTP 400)."""


@dataclass
class OutputRequest:
    name: str
    binary: bool


@dataclass
class InferRequest:
    id: str
    inputs: dict[str, np.ndarray]
    # Empty means "every output, JSON-encoded", as in Triton.
    outputs: list[OutputRequest] = field(default_factory=list)
    binary_output_default: bool = False

    def wants_binary(self, output: str) -> bool:
        for requested in self.outputs:
            if requested.name == output:
                return requested.binary
        return self.binary_output_default


def parse_infer_request(body: bytes, header_length: str | None) -> InferRequest:
    """Decode an infer request body, plain-JSON or binary-extension."""
    if header_length is None:
        header, binary = body, b""
    else:
        try:
            n = int(header_length)
        except ValueError:
            raise ProtocolError(f"{HEADER_LENGTH} is not an integer") from None
        if n < 0 or n > len(body):
            raise ProtocolError(f"{HEADER_LENGTH} exceeds the body")
        header, binary = body[:n], body[n:]

    try:
        request = json.loads(header)
    except ValueError as exc:
        raise ProtocolError(f"request is not valid JSON: {exc}") from None
    if not isinstance(request, dict):
        raise ProtocolError("request must be a JSON object")
    inputs = request.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ProtocolError("request has no inputs")

    tensors: dict[str, np.ndarray] = {}
    offset = 0
    for spec in inputs:
        if not isinstance(spec, dict):
            raise ProtocolError("each input must be a JSON object")
        name, dtype, shape = _tensor_header(spec)
        parameters = spec.get("parameters") or {}
        size = parameters.get("binary_data_size")
        if size is not None:
            if header_length is None:
                raise ProtocolError(
                    f"input {name!r} declares binary_data_size "
                    f"but the request carries no {HEADER_LENGTH}"
                )
            end = offset + int(size)
            if end > len(binary):
                raise ProtocolError(f"binary data for input {name!r} is truncated")
            array = np.frombuffer(
                binary[offset:end], dtype=np.dtype(dtype).newbyteorder("<")
            )
            offset = end
        else:
            if "data" not in spec:
                raise ProtocolError(
                    f"input {name!r} has neither data nor binary_data_size"
                )
            array = np.asarray(spec["data"], dtype=dtype).ravel()
        expected = int(np.prod(shape)) if shape else 1
        if array.size != expected:
            raise ProtocolError(
                f"input {name!r}: shape {shape} holds {expected} elements, "
                f"got {array.size}"
            )
        tensors[name] = array.reshape(shape)
    if offset != len(binary):
        raise ProtocolError(
            f"{len(binary) - offset} trailing bytes after the last binary input"
        )

    request_parameters = request.get("parameters") or {}
    outputs = [
        OutputRequest(
            name=str(o["name"]),
            binary=bool((o.get("parameters") or {}).get("binary_data", False)),
        )
        for o in request.get("outputs") or []
        if isinstance(o, dict) and "name" in o
    ]
    return InferRequest(
        id=str(request.get("id", "")),
        inputs=tensors,
        outputs=outputs,
        binary_output_default=bool(request_parameters.get("binary_data_output", False)),
    )


def _tensor_header(spec: dict[str, Any]) -> tuple[str, type[np.generic], list[int]]:
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError("input without a name")
    datatype = spec.get("datatype")
    if datatype not in DTYPES:
        raise ProtocolError(f"input {name!r}: unsupported datatype {datatype!r}")
    shape = spec.get("shape")
    if not isinstance(shape, list) or not all(
        isinstance(d, int) and d >= 0 for d in shape
    ):
        raise ProtocolError(
            f"input {name!r}: shape must be a list of non-negative ints"
        )
    return name, DTYPES[datatype], shape


def encode_infer_response(
    request: InferRequest,
    model_name: str,
    model_version: str,
    outputs: dict[str, np.ndarray],
) -> tuple[bytes, dict[str, str]]:
    """→ (body, extra headers). Binary outputs are appended after the JSON
    header in the order they appear in it, with the header length announced
    the way the request announced its own."""
    header_outputs: list[dict[str, Any]] = []
    blobs: list[bytes] = []
    for name, array in outputs.items():
        dtype = numpy_dtype_name(array.dtype)
        entry: dict[str, Any] = {
            "name": name,
            "datatype": dtype,
            "shape": list(array.shape),
        }
        if request.wants_binary(name):
            blob = np.ascontiguousarray(
                array, dtype=array.dtype.newbyteorder("<")
            ).tobytes()
            entry["parameters"] = {"binary_data_size": len(blob)}
            blobs.append(blob)
        else:
            entry["data"] = array.ravel().tolist()
        header_outputs.append(entry)

    header = json.dumps(
        {
            "id": request.id,
            "model_name": model_name,
            "model_version": model_version,
            "outputs": header_outputs,
        },
        separators=(",", ":"),
    ).encode()
    if not blobs:
        return header, {"Content-Type": "application/json"}
    return header + b"".join(blobs), {
        "Content-Type": "application/octet-stream",
        HEADER_LENGTH: str(len(header)),
    }


def numpy_dtype_name(dtype: np.dtype) -> str:
    """The v2 name of a numpy dtype (``float32`` → ``FP32``)."""
    try:
        return NUMPY_TO_DTYPE[dtype.type]
    except KeyError:
        raise ProtocolError(f"no v2 datatype for numpy {dtype}") from None
