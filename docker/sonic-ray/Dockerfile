# sonic-ray: Ray Serve + ONNX Runtime (CUDA) + the sonic_ray package.
#
# The base is the official Ray image for the Ray version the chart runs
# (apps/ray/sonic-ray/chart/values.yaml `ray.version` must equal the tag's
# version prefix — tests/manifests/test_ray.py checks). It carries CUDA 12.8
# and cuDNN 9, which is exactly what onnxruntime-gpu 1.26 links against, so
# no CUDA wheels are added on top: ORT dlopens the image's libraries.
FROM rayproject/ray:2.52.0-py312-cu128

# Built from the repository root, like the other images in docker/.
COPY --chown=ray:users docker/sonic-ray /tmp/sonic-ray
RUN pip install --no-cache-dir /tmp/sonic-ray && rm -rf /tmp/sonic-ray

# Fail the build, not the first replica, if the CUDA provider cannot be
# used: the provider must be compiled in, and every shared library it will
# dlopen at session creation must resolve on this image. No GPU is needed
# for either check.
RUN python -c "\
import ctypes, onnxruntime as ort; \
assert 'CUDAExecutionProvider' in ort.get_available_providers(), ort.get_available_providers(); \
[ctypes.CDLL(lib) for lib in ('libcudart.so.12', 'libcublas.so.12', 'libcublasLt.so.12', 'libcufft.so.11', 'libcurand.so.10', 'libcudnn.so.9')]; \
import sonic_ray.repository, sonic_ray.protocol; print('ok', ort.__version__)"

# Where the chart mounts the model repository; overridable per deployment.
ENV MODEL_REPOSITORY=/models \
    ONNX_EXECUTION_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider

# The base image already runs as the unprivileged `ray` user (uid 1000) with
# WORKDIR /home/ray and starts `ray start` through KubeRay's command; nothing
# to override.
