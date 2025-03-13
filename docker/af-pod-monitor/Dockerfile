FROM python:3.8-slim

WORKDIR /etc/

RUN pip install prometheus_client

COPY pod-metrics-exporter.py /etc/

RUN chmod +x  /etc/pod-metrics-exporter.py

CMD ["python", "/etc/pod-metrics-exporter.py"]