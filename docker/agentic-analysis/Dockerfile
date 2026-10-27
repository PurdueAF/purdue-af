# Task image of workflows/agentic-analysis: the Flyte 2 runtime, git, pixi,
# and OpenCode with ripgrep and poppler.
# The workflow code itself is not baked in — Flyte ships it as a code bundle
# from the launcher.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/
COPY --from=ghcr.io/prefix-dev/pixi:0.83.0 /usr/local/bin/pixi /usr/local/bin/pixi

# ripgrep: OpenCode's search tools, which otherwise fetch it at run time.
# poppler-utils: PDF pages as PNG for the agents that read figures.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl ripgrep poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# OpenCode is one binary, published as an npm package per platform. The
# baseline build needs no AVX2, so it runs on every AF node.
ARG OPENCODE_VERSION="1.19.4"
RUN curl -fsSL --retry 3 --retry-delay 2 -o /tmp/opencode.tgz \
        "https://registry.npmjs.org/opencode-linux-x64-baseline/-/opencode-linux-x64-baseline-${OPENCODE_VERSION}.tgz" \
    && tar -xzf /tmp/opencode.tgz -C /tmp package/bin/opencode \
    && install -m 0755 /tmp/package/bin/opencode /usr/local/bin/opencode \
    && rm -rf /tmp/opencode.tgz /tmp/package \
    && opencode --version
ENV OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1

# A real home: the agents and pixi keep state and caches under it.
RUN useradd --uid 1000 --user-group --create-home --shell /bin/bash app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY docker/agentic-analysis/pyproject.toml docker/agentic-analysis/uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

USER 1000
ENV HOME=/home/app
# Flyte extracts the code bundle into the cwd; /app is root's.
WORKDIR /home/app
# Each runs as that user.
RUN opencode --version && pixi --version && python -c "import httpx"
