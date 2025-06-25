FROM python:3.8-slim

WORKDIR /etc/

RUN pip install --no-cache-dir prometheus_client==0.22.1

COPY pod-metrics-exporter.py /etc/

RUN chmod +x  /etc/pod-metrics-exporter.py

CMD ["python", "/etc/pod-metrics-exporter.py"]