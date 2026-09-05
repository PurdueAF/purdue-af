"""The KServe v2 wire format: plain JSON and the binary tensor extension."""

import json

import numpy as np
import pytest
from sonic_ray import protocol
from sonic_ray.protocol import (
    HEADER_LENGTH,
    InferRequest,
    OutputRequest,
    ProtocolError,
    encode_infer_response,
    parse_infer_request,
)


def test_json_request_reshapes_flat_and_nested_data():
    body = json.dumps(
        {
            "id": "abc",
            "inputs": [
                {
                    "name": "a",
                    "datatype": "FP32",
                    "shape": [2, 2],
                    "data": [1, 2, 3, 4],
                },
                {"name": "b", "datatype": "INT64", "shape": [2, 1], "data": [[5], [6]]},
            ],
        }
    ).encode()
    request = parse_infer_request(body, None)
    assert request.id == "abc"
    np.testing.assert_array_equal(
        request.inputs["a"], np.array([[1, 2], [3, 4]], np.float32)
    )
    assert request.inputs["a"].dtype == np.float32
    np.testing.assert_array_equal(request.inputs["b"], np.array([[5], [6]], np.int64))
    assert request.outputs == []
    assert request.binary_output_default is False


def test_binary_request_is_what_tritonclient_http_sends():
    """JSON header, then the raw tensors in input order; the header length
    travels in Inference-Header-Content-Length."""
    a = np.arange(6, dtype=np.float32).reshape(2, 3)
    b = np.array([[1], [0]], dtype=np.int32)
    header = json.dumps(
        {
            "inputs": [
                {
                    "name": "a",
                    "datatype": "FP32",
                    "shape": [2, 3],
                    "parameters": {"binary_data_size": a.nbytes},
                },
                {
                    "name": "b",
                    "datatype": "INT32",
                    "shape": [2, 1],
                    "parameters": {"binary_data_size": b.nbytes},
                },
            ],
            "outputs": [{"name": "y", "parameters": {"binary_data": True}}],
        }
    ).encode()
    body = header + a.tobytes() + b.tobytes()
    request = parse_infer_request(body, str(len(header)))
    np.testing.assert_array_equal(request.inputs["a"], a)
    np.testing.assert_array_equal(request.inputs["b"], b)
    assert request.outputs == [OutputRequest("y", binary=True)]
    assert request.wants_binary("y") and not request.wants_binary("other")


def test_binary_and_json_inputs_may_mix():
    a = np.ones(4, dtype=np.float32)
    header = json.dumps(
        {
            "inputs": [
                {"name": "j", "datatype": "FP32", "shape": [2], "data": [1, 2]},
                {
                    "name": "a",
                    "datatype": "FP32",
                    "shape": [4],
                    "parameters": {"binary_data_size": a.nbytes},
                },
            ]
        }
    ).encode()
    request = parse_infer_request(header + a.tobytes(), str(len(header)))
    assert set(request.inputs) == {"j", "a"}


@pytest.mark.parametrize(
    "body, header_length, message",
    [
        (b"not json", None, "not valid JSON"),
        (b"[]", None, "JSON object"),
        (b'{"inputs": []}', None, "no inputs"),
        (
            b'{"inputs": [{"datatype": "FP32", "shape": [1], "data": [1]}]}',
            None,
            "without a name",
        ),
        (
            b'{"inputs": [{"name": "x", "datatype": "BYTES", "shape": [1], "data": ["a"]}]}',
            None,
            "unsupported datatype",
        ),
        (
            b'{"inputs": [{"name": "x", "datatype": "FP32", "shape": [-1], "data": [1]}]}',
            None,
            "non-negative",
        ),
        (
            b'{"inputs": [{"name": "x", "datatype": "FP32", "shape": [2], "data": [1]}]}',
            None,
            "holds 2 elements, got 1",
        ),
        (
            b'{"inputs": [{"name": "x", "datatype": "FP32", "shape": [1]}]}',
            None,
            "neither data nor binary_data_size",
        ),
        (
            b'{"inputs": [{"name": "x", "datatype": "FP32", "shape": [1], "parameters": {"binary_data_size": 4}}]}',
            None,
            "carries no Inference-Header",
        ),
        (
            b'{"inputs": [{"name": "x", "datatype": "FP32", "shape": [1], "parameters": {"binary_data_size": 4}}]}',
            "abc",
            "not an integer",
        ),
        (
            b'{"inputs": [{"name": "x", "datatype": "FP32", "shape": [1], "parameters": {"binary_data_size": 4}}]}',
            "9999",
            "exceeds the body",
        ),
    ],
)
def test_malformed_requests_are_protocol_errors(body, header_length, message):
    with pytest.raises(ProtocolError, match=message):
        parse_infer_request(body, header_length)


def test_truncated_and_trailing_binary_data_are_rejected():
    header = json.dumps(
        {
            "inputs": [
                {
                    "name": "x",
                    "datatype": "FP32",
                    "shape": [2],
                    "parameters": {"binary_data_size": 8},
                }
            ]
        }
    ).encode()
    with pytest.raises(ProtocolError, match="truncated"):
        parse_infer_request(header + b"\0" * 4, str(len(header)))
    with pytest.raises(ProtocolError, match="trailing bytes"):
        parse_infer_request(header + b"\0" * 12, str(len(header)))


def test_json_response_carries_data_inline():
    request = InferRequest(id="r1", inputs={})
    body, headers = encode_infer_response(
        request, "m", "1", {"y": np.array([[1.5, 2.5]], dtype=np.float32)}
    )
    assert headers == {"Content-Type": "application/json"}
    doc = json.loads(body)
    assert doc == {
        "id": "r1",
        "model_name": "m",
        "model_version": "1",
        "outputs": [
            {"name": "y", "datatype": "FP32", "shape": [1, 2], "data": [1.5, 2.5]}
        ],
    }


def test_binary_response_round_trips_through_the_request_parser_rules():
    """Outputs asked for in binary come back as raw little-endian bytes after
    the header, with the header length announced — the mirror image of the
    request encoding, which is what tritonclient.http decodes."""
    request = InferRequest(
        id="r2",
        inputs={},
        outputs=[OutputRequest("y", binary=True), OutputRequest("z", binary=False)],
    )
    y = np.arange(6, dtype=np.float32).reshape(2, 3)
    z = np.array([7], dtype=np.int64)
    body, headers = encode_infer_response(request, "m", "1", {"y": y, "z": z})
    assert headers["Content-Type"] == "application/octet-stream"
    n = int(headers[HEADER_LENGTH])
    doc = json.loads(body[:n])
    y_out, z_out = doc["outputs"]
    assert y_out == {
        "name": "y",
        "datatype": "FP32",
        "shape": [2, 3],
        "parameters": {"binary_data_size": y.nbytes},
    }
    assert z_out == {"name": "z", "datatype": "INT64", "shape": [1], "data": [7]}
    np.testing.assert_array_equal(
        np.frombuffer(body[n:], dtype=np.float32).reshape(2, 3), y
    )


def test_request_level_binary_default_applies_to_unlisted_outputs():
    request = InferRequest(id="", inputs={}, binary_output_default=True)
    body, headers = encode_infer_response(
        request, "m", "1", {"y": np.zeros(2, np.float16)}
    )
    assert HEADER_LENGTH in headers
    assert (
        json.loads(body[: int(headers[HEADER_LENGTH])])["outputs"][0]["datatype"]
        == "FP16"
    )


def test_every_v2_dtype_maps_both_ways():
    for name, dtype in protocol.DTYPES.items():
        assert protocol.numpy_dtype_name(np.dtype(dtype)) == name
    with pytest.raises(ProtocolError):
        protocol.numpy_dtype_name(np.dtype("U3"))
