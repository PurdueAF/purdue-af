"""sonic-ray: Triton on Ray, with Ray Serve carrying the gRPC traffic.

One module, ``serve_app``: a Ray Serve deployment that forwards every unary
RPC of Triton's ``GRPCInferenceService`` to the Triton running beside it in
the same pod. Ray Serve's gRPC proxy speaks Triton's protocol because it is
handed Triton's own generated servicer (from the ``tritonclient`` package);
Triton does every bit of the inference. Nothing here parses a request.

The file ships to the cluster as a ConfigMap rendered by the chart (see
templates/configmap.yaml) and lands on PYTHONPATH in the stock Ray image;
there is no custom image.
"""
