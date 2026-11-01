FROM python:3.12-slim

WORKDIR /etc/

RUN pip install --no-cache-dir prometheus_client==0.22.1

COPY pod-metrics-exporter.py /etc/

RUN chmod +x  /etc/pod-metrics-exporter.py

# -B: a __pycache__ here would ship in the image.
RUN python -B -c "import importlib.util as u; s = u.spec_from_file_location('m', 'pod-metrics-exporter.py'); m = u.module_from_spec(s); s.loader.exec_module(m); print('ok')"

CMD ["python", "/etc/pod-metrics-exporter.py"]