FROM python:3.8-slim
RUN pip install prometheus_client
COPY node_healthcheck.py /etc/
WORKDIR /etc/
CMD ["python", "/etc/node_healthcheck.py"]
