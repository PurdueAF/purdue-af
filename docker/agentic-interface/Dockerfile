FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /uvx /bin/

# Dedicated non-root user with a fixed UID so the Deployment's
# runAsNonRoot/runAsUser can reference it. No home dir: the root
# filesystem is read-only at runtime; HOME points at /tmp (an emptyDir
# in k8s) for anything that insists on a writable home.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin app

# Don't write .pyc files at runtime — the container filesystem is
# read-only, and without this Python would try (and fail) to cache
# bytecode on every import.
ENV PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

WORKDIR /app

# Install locked dependencies first (cached layer unless the lock changes).
COPY docker/agentic-interface/pyproject.toml docker/agentic-interface/uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY docker/agentic-interface/context.py .
COPY docker/agentic-interface/shared.py .
COPY docker/agentic-interface/auth.py .
COPY docker/agentic-interface/metrics.py .
COPY docker/agentic-interface/server.py .
COPY docker/agentic-interface/tools/ ./tools/

USER 1000

CMD ["python", "server.py"]
