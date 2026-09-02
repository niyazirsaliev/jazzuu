FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

WORKDIR /app

# Standard library only, deliberately: this service reads one SQLite file and
# answers JSON over HTTP. There is no `pip install` step, so the container that
# holds private recordings has no third-party code in it at all.
COPY recordings_mcp/ /app/recordings_mcp/
COPY semantic_search/ /app/semantic_search/
# recording_codes owns tenant numbering; pipeline owns the canonical durable
# readiness projection consumed by store.py. Both are stdlib-only shared code.
COPY archive/recording_codes.py /app/archive/recording_codes.py
COPY archive/pipeline.py /app/archive/pipeline.py
COPY archive/readiness.py /app/archive/readiness.py

# Unprivileged, and the archive is mounted read-only besides. This service has
# no reason to write anything, ever.
RUN useradd --uid 10000 --create-home --shell /usr/sbin/nologin recordings
USER recordings

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV RECORDINGS_MCP_HOST=0.0.0.0
ENV RECORDINGS_MCP_PORT=62390

EXPOSE 62390

# Liveness only: /healthz needs no credential and reports no content, so the
# probe cannot become a way to read the archive. Readiness (/readyz, counts and
# whether the DB opens) is for an operator, not for the restart loop — a
# briefly unreadable archive during a WAL checkpoint is not a reason to kill a
# healthy process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('RECORDINGS_MCP_PORT','62390')+'/healthz', timeout=4).status==200 else 1)"]

CMD ["python", "-m", "recordings_mcp.server"]
