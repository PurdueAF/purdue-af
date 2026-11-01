FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY docker/supersonic-model-manager/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY docker/supersonic-model-manager/model_manager /app/model_manager

# The chart's runAsUser and fix-ownership init container use this uid.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
USER 10001

# The dashboard and its logo ship in the image, and the ASGI routes are wired.
RUN python -c "\
from model_manager.main import app, STATIC_DIR; \
routes = {getattr(r, 'path', None) for r in app.routes}; \
assert {'/', '/healthz', '/api/state'} <= routes, routes; \
assert (STATIC_DIR / 'index.html').is_file(); \
assert (STATIC_DIR / 'img' / 'supersonic-mark.svg').is_file(); \
print('ok:', len(routes), 'routes')"

EXPOSE 8080

CMD ["uvicorn", "model_manager.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
