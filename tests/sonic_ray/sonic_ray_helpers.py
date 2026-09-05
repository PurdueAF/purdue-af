"""Model builders and configs shared by the sonic-ray suite (uniquely named:
multiple modules called `conftest` cannot be imported from test code)."""

import numpy as np
import onnx
from onnx import TensorProto, helper

# Every model here is one config.pbtxt away from the repository layout the
# SuperSONIC model manager writes; the ParticleNet one is the real config of
# particleNetFromMiniAODAK8 with the model swapped for a tiny stand-in.
PARTICLENET_CONFIG = """\
name: "particleNetFromMiniAODAK8"
platform: "onnxruntime_onnx"
max_batch_size : 500
dynamic_batching {
   preferred_batch_size: [ 200 ]
}
input [
  {
    name: "pf_features"
    data_type: TYPE_FP32
    dims: [ 4, -1 ]
  },
  {
    name: "pf_mask"
    data_type: TYPE_FP32
    dims: [ 1, -1 ]
  }
]
output [
  {
    name: "output"
    data_type: TYPE_FP32
    dims: [ 4 ]
    label_filename: "particlenet_labels.txt"
  }
]
optimization {graph : {level : -1}}
"""

DEEPMET_CONFIG = """\
name: "deepmet"
platform: "tensorflow_graphdef"
max_batch_size: 100
input [
  { name: "input", data_type: TYPE_FP32, dims: [ 4500, 8 ] }
]
output [
  { name: "output/BiasAdd", data_type: TYPE_FP32, dims: [ 2 ] }
]
"""


def particlenet_like_model() -> onnx.ModelProto:
    """output[n, f] = Σ_p pf_features[n, f, p] * pf_mask[n, 0, p] — the shape
    contract of a ParticleNet (batch × features × particles, a dynamic
    particle count, a per-jet vector out) in two operators."""
    features = helper.make_tensor_value_info(
        "pf_features", TensorProto.FLOAT, ["N", 4, "P"]
    )
    mask = helper.make_tensor_value_info("pf_mask", TensorProto.FLOAT, ["N", 1, "P"])
    output = helper.make_tensor_value_info("output", TensorProto.FLOAT, ["N", 4])
    axes = helper.make_tensor("axes", TensorProto.INT64, [1], [2])
    graph = helper.make_graph(
        [
            helper.make_node("Mul", ["pf_features", "pf_mask"], ["masked"]),
            helper.make_node("ReduceSum", ["masked", "axes"], ["output"], keepdims=0),
        ],
        "particlenet_like",
        [features, mask],
        [output],
        initializer=[axes],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model


def doubler_model() -> onnx.ModelProto:
    """y = 2x on an int32 vector, unbatched: exercises a non-float dtype and
    max_batch_size 0."""
    x = helper.make_tensor_value_info("x", TensorProto.INT32, [3])
    y = helper.make_tensor_value_info("y", TensorProto.INT32, [3])
    graph = helper.make_graph(
        [helper.make_node("Add", ["x", "x"], ["y"])], "doubler", [x], [y]
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model


def reference_particlenet(features: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return (features * mask).sum(axis=2).astype(np.float32)
