FROM python:3.8-slim
RUN pip install prometheus_client
COPY metrics_server.py /etc/
WORKDIR /etc/
CMD ["python", "/etc/metrics_server.py"]
