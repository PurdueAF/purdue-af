FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.20 /uv /uvx /bin/

WORKDIR /app

# Install locked dependencies first (cached layer unless the lock changes).
COPY docker/agentic-interface/pyproject.toml docker/agentic-interface/uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY docker/agentic-interface/context.py .
COPY docker/agentic-interface/auth.py .
COPY docker/agentic-interface/metrics.py .
COPY docker/agentic-interface/server.py .
COPY docker/agentic-interface/tools/ ./tools/

CMD ["python", "server.py"]
