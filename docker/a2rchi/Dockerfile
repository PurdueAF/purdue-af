FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install A2RCHI CLI and services
RUN git clone https://github.com/mit-submit/A2rchi.git /opt/A2rchi && \
    pip install -e /opt/A2rchi && \
    pip install uvicorn[standard]

EXPOSE 7861

# Minimal runtime; the chart defines config and env.
# Allow overriding the ASGI app module via A2RCHI_APP_MODULE if needed.
ENV A2RCHI_APP_MODULE=a2rchi.apps.chat_app.main:app
CMD ["sh", "-c", "uvicorn ${A2RCHI_APP_MODULE} --host 0.0.0.0 --port 7861"]

