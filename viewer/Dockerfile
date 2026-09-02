FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

WORKDIR /app

COPY viewer/requirements.txt viewer/requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY viewer/app/ ./app/
COPY semantic_search/ ./semantic_search/

# The viewer reads its archive/token mounts and writes only its disposable
# mind-map cache.  Seed the mounted cache path with matching ownership so a
# named volume remains writable without granting the web process root.
RUN useradd --uid 10000 --create-home --shell /usr/sbin/nologin recordings \
 && mkdir -p /cache/mindmaps \
 && chown -R 10000:10000 /app /cache

# Stamped by the deploy script so /api/health tells which code is live.
ARG BUILD_VERSION=dev
ENV BUILD_VERSION=$BUILD_VERSION
ENV PYTHONDONTWRITEBYTECODE=1

USER recordings

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=4s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
