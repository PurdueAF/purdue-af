# Task image of the self-repair workflow (workflows/self-repair): the Flyte 2
# runtime, git, and opencode as the coding agent. The workflow code itself is
# not baked in — Flyte ships it as a code bundle from the launcher.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

# opencode is an npm package around a platform binary; node comes from the
# official image of the same Debian release rather than apt, which lags.
COPY --from=node:24-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ARG OPENCODE_VERSION="1.18.31"
# hadolint ignore=DL3016  # pinned via the ARG above
RUN npm install -g --no-fund --no-audit "opencode-ai@${OPENCODE_VERSION}" \
    && opencode --version \
    && rm -rf /root/.npm

# A real home: opencode keeps its state under ~/.local/share/opencode.
RUN useradd --uid 1000 --user-group --create-home --shell /bin/bash app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY docker/self-repair/pyproject.toml docker/self-repair/uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

USER 1000
ENV HOME=/home/app
# Flyte extracts the code bundle into the cwd; /app is root's.
WORKDIR /home/app
