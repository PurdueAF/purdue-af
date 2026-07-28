FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Built from the repository root, like the other images in docker/.
COPY docker/supersonic-model-manager/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY docker/supersonic-model-manager/model_manager /app/model_manager

# Runs unprivileged; the PVC must be group- or world-writable, or use
# podSecurityContext.fsGroup in the chart (default) to fix ownership.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8080

CMD ["uvicorn", "model_manager.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
