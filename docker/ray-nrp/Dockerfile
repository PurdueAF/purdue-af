# Ray's image for jobs on NRP Nautilus: the AF's global Pixi environment
# (pixi/global), which Ray clusters on the AF run from /work, installed into
# the image, since NRP pods see no AF storage. A job's script then runs with the
# same Python, Ray and PyTorch at either site.
FROM ubuntu:24.04

COPY --from=ghcr.io/prefix-dev/pixi:0.83.0 /usr/local/bin/pixi /usr/local/bin/pixi

# wget: KubeRay's probes of a Ray pod run it.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/pixi-global
COPY pixi/global/pixi.toml pixi/global/pixi.lock ./
# The environment's GPU builds need CUDA, which a build machine does not have.
RUN CONDA_OVERRIDE_CUDA=12.4 pixi install --frozen \
    && rm -rf /root/.cache

# As raycluster.yaml runs Ray from an environment on the AF.
ENV CONDA_PREFIX=/opt/pixi-global/.pixi/envs/default
ENV PATH="${CONDA_PREFIX}/bin:${PATH}"

# The uid of Ray's own images, which Ubuntu's image gives its own user.
RUN userdel --remove ubuntu 2>/dev/null \
    && useradd --uid 1000 --user-group --create-home --shell /bin/bash ray
USER 1000
WORKDIR /home/ray
