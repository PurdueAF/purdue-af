"""sonic-ray: the SuperSONIC model repository served by Ray Serve.

Three modules, in dependency order:

- ``repository`` — reads a Triton-layout model repository (``<model>/
  config.pbtxt`` + ``<model>/<version>/model.onnx``) and runs the ONNX models
  in it with ONNX Runtime. No Ray, no HTTP.
- ``protocol`` — the KServe v2 (a.k.a. Triton) HTTP wire format: JSON bodies,
  the binary tensor extension, dtype names. No Ray, no HTTP framework.
- ``serve_app`` — the Ray Serve deployment: a FastAPI ingress exposing the
  two above on the v2 endpoints. The only module that imports ``ray``.

The split is what keeps the first two unit-testable on a laptop with the CPU
build of onnxruntime; the deployment itself is a thin layer over them.

These files ship to the cluster as a ConfigMap rendered by the chart (see
templates/configmap.yaml) and land on PYTHONPATH in the stock Ray image; there
is no custom image. onnxruntime is installed by Ray Serve's runtime_env.
"""
