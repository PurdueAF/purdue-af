FROM python:3.8-slim

WORKDIR /app

RUN pip install prometheus_client

COPY pod-metrics-exporter.py /app/

RUN chmod +x  /app/pod-metrics-exporter.py