FROM python:3.11-slim

WORKDIR /app

COPY docker/agentic-interface/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/agentic-interface/context.py .
COPY docker/agentic-interface/server.py .
COPY docker/agentic-interface/tools/ ./tools/

CMD ["python", "server.py"]
