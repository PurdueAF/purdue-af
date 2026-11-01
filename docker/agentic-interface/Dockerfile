FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

# The Deployment's runAsUser. No home: only /tmp, an emptyDir, is writable at runtime.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin app

ENV PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

WORKDIR /app

COPY docker/agentic-interface/pyproject.toml docker/agentic-interface/uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY docker/agentic-interface/config.py .
COPY docker/agentic-interface/context.py .
COPY docker/agentic-interface/errors.py .
COPY docker/agentic-interface/shared.py .
COPY docker/agentic-interface/auth.py .
COPY docker/agentic-interface/metrics.py .
COPY docker/agentic-interface/server.py .
COPY docker/agentic-interface/tools/ ./tools/
# Shared with the Hub's profile form so both report the same GPU numbers.
COPY apps/jupyterhub/jupyterhub/extraFiles/gpu_queries.py .
COPY .claude/skills/purdue-af-agentic-interface/SKILL.md .

USER 1000

RUN python -c "import server; print('ok:', server.SERVICE_PREFIX)"

CMD ["python", "server.py"]
