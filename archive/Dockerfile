FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

# ffprobe/ffmpeg: asr_backfill inspects durations and slices representative
# samples for long recordings before routing them to the ASR engine.
RUN printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260301T000000Z bookworm main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20260301T000000Z bookworm-security main\n' > /etc/apt/sources.list \
 && rm -f /etc/apt/sources.list.d/* \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y --no-install-recommends ffmpeg=7:5.1.8-0+deb12u1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY archive/requirements-diarization.txt archive/requirements-diarization.lock ./
RUN pip install --no-cache-dir --require-hashes --requirement requirements-diarization.lock

# The ingest side remains dependency-light; semantic compute is remote HTTP.
COPY archive/*.py ./
COPY semantic_search/ ./semantic_search/

# Run as an unprivileged account. The tenant's archive dir and caller token
# files on the host are owned by this uid, so no other tenant can read them.
# The token files are mounted read-only: nothing here writes a credential.
RUN useradd --uid 10000 --create-home --shell /usr/sbin/nologin connector
USER connector

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=$BUILD_VERSION
ENV PYTHONUNBUFFERED=1

# The healthcheck performs one authenticated MCP handshake. This is stricter
# than a local file check: a missing, empty, wrong, or revoked caller token, an
# unseeded tenant service, and a service that answers with a JSON-RPC error all
# report unhealthy with a specific probe result naming who has to fix it.
#
# A transient (unreachable service, timeout) also reports unhealthy. That is a
# deliberate choice, not an oversight: the alternative is a connector that has
# archived nothing for minutes while still reporting healthy, which is the one
# state no operator would ever go looking at. The probe's JSON says which
# failure it was, so a transient is never mistaken for a credential problem.
#
# --timeout below includes Python startup and printing the result. The probe
# owns a shorter hard wall-clock deadline (connector.PROBE_DEADLINE_S), including
# SQLite layout work and network reads, and leaves an explicit startup/output
# margin. A test fails if that contract or this Docker timeout drifts.
HEALTHCHECK --interval=60s --timeout=15s --start-period=10s --retries=3 \
  CMD ["python", "/app/connector.py", "probe"]

CMD ["python", "/app/connector.py", "loop"]
