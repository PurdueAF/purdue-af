FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential cmake gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install A2RCHI CLI and services
RUN python -m pip install --upgrade pip && \
    git clone https://github.com/mit-submit/A2rchi.git /opt/A2rchi && \
    pip install -r /opt/A2rchi/requirements/requirements-base.txt && \
    pip install -r /opt/A2rchi/requirements/cpu-requirementsHEADER.txt && \
    pip install -e /opt/A2rchi

# Ensure the in-repo src layout is on the import path
ENV PYTHONPATH=/opt/A2rchi/src:${PYTHONPATH}

EXPOSE 7861

# Minimal runtime; the chart defines config and env.
CMD ["python", "/opt/A2rchi/src/bin/service_chat.py"]

