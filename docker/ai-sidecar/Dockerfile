FROM python:3.11-slim

WORKDIR /app

COPY docker/ai-sidecar/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/ai-sidecar/server.py .
COPY docker/ai-sidecar/tools/ ./tools/

CMD ["python", "server.py"]
