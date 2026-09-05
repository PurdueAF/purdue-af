"""sonic-ray: the SuperSONIC model repository's ONNX models, served by Ray Serve.

Two modules:

- ``models`` — finds the ONNX models in a Triton-layout directory
  (``<model>/<version>/model.onnx``) and runs them with ONNX Runtime. No Ray,
  no HTTP; unit-testable with the CPU build of onnxruntime.
- ``serve_app`` — the Ray Serve deployment: a FastAPI ingress with plain JSON
  endpoints over the above. The only module that imports ``ray``.

Deliberately not Triton-compatible (no KServe v2 protocol, no config.pbtxt):
a client sends named arrays as JSON and gets named arrays back. Wire
compatibility with Triton clients is a later step, if wanted.

These files ship to the cluster as a ConfigMap rendered by the chart (see
templates/configmap.yaml) and land on PYTHONPATH in the stock Ray image; there
is no custom image. onnxruntime is installed by Ray Serve's runtime_env.
"""
