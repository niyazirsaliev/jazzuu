#!/usr/bin/env python3
"""connector.py — per-tenant PLAUD ingest daemon.

ONE of these runs per explicit tenant, including the owner. It is the only
process allowed to touch
that tenant's credentials and that tenant's archive, which is what keeps the
tenants isolated: separate container, separate caller token file, separate
archive directory, separate ASR caller token, and one pinned PLAUD MCP service
per tenant. It holds no PLAUD account credentials — that tenant's MCP service
owns those.

It does not reimplement ingest. `incremental.py` and `archive_recording.py`
already know how to page PLAUD, download audio and write the archive rows; this
module supplies per-tenant configuration and a schedule around them.

Modes:
  connector.py once        one sync pass, exit (used by the backfill run)
  connector.py loop        sync every SYNC_INTERVAL_S forever (the container)
  connector.py status      print non-secret readiness, exit 0/1 (healthcheck)
  connector.py probe       one live handshake with this tenant's MCP, exit 0/1

Backfill vs incremental is not a separate code path. The first pass sees an
empty archive, so every recording is "new" and gets archived; later passes see
only what PLAUD has added since. One code path, no special-casing.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import re
import signal
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline  # noqa: E402
import semantic_reconcile  # noqa: E402
import diarization  # noqa: E402
import stages  # noqa: E402
import control  # noqa: E402
import tenant as _tenant  # noqa: E402

TENANT = _tenant.load()

SYNC_INTERVAL_S = int(os.environ.get('SYNC_INTERVAL_S', '300'))
SOURCE_POLL_COOLDOWN_S = int(os.environ.get('SOURCE_POLL_COOLDOWN_S', '12'))
# Bound a single pass so a stuck download cannot wedge the loop forever.
MAX_PER_PASS = int(os.environ.get('MAX_PER_PASS', '0'))  # 0 = unlimited
RUN_ASR = os.environ.get('RUN_ASR', '1') not in ('0', 'false', 'no')
RUN_SUMMARIES = os.environ.get('RUN_SUMMARIES', '1') not in ('0', 'false', 'no')
ASR_ENGINE = os.environ.get('PIPELINE_ASR_ENGINE', 'mixed').strip() or 'mixed'
LOCK = os.path.join(TENANT.archive_dir, '.connector.lock')
CONTROL_SOCKET = os.environ.get('RECORDINGS_CONTROL_SOCKET', '/run/recordings/control.sock')
CONTROL_TOKEN_FILE = os.environ.get('RECORDINGS_CONTROL_TOKEN_FILE', '/creds/recordings-control-token')

# Docker kills the healthcheck at HEALTHCHECK --timeout and records only
# "unhealthy", so a probe that outlives it reports nothing an operator can act
# on — no failure class, no message, exactly the ambiguity the classifier
# exists to remove. Mirrors --timeout=15s in the Dockerfile; a test fails if
# the two drift apart.
HEALTHCHECK_TIMEOUT_S = 15
# Per-request inactivity ceiling. The wall-clock deadline below is authoritative
# because an SSE peer can keep a socket active indefinitely. Ingest keeps its
# own, much longer timeout: this only changes the live health probe.
PROBE_TIMEOUT_S = int(os.environ.get('PROBE_TIMEOUT_S', '4'))
# urllib's timeout is an inactivity timeout, not an end-to-end budget. An SSE
# peer can send keep-alives forever without timing out a blocking read, so the
# health process also owns a true wall-clock deadline that fires before Docker
# kills it and discards the classified result.
PROBE_STARTUP_MARGIN_S = 3
PROBE_DEADLINE_S = float(os.environ.get('PROBE_DEADLINE_S', '10'))
PROBE_DB_TIMEOUT_S = float(os.environ.get('PROBE_DB_TIMEOUT_S', '1'))
if not 0 < PROBE_DEADLINE_S <= HEALTHCHECK_TIMEOUT_S - PROBE_STARTUP_MARGIN_S:
    raise ValueError(
        'PROBE_DEADLINE_S must be positive and leave at least '
        f'{PROBE_STARTUP_MARGIN_S}s inside the Docker healthcheck window')
if not 0 < PROBE_DB_TIMEOUT_S < PROBE_DEADLINE_S:
    raise ValueError(
        'PROBE_DB_TIMEOUT_S must be positive and shorter than '
        'PROBE_DEADLINE_S')


class ProbeDeadlineExceeded(TimeoutError):
    """The live health probe exceeded its total wall-clock budget."""


@contextmanager
def _probe_wall_clock_deadline(seconds: float):
    """Interrupt blocking network reads at a real elapsed-time deadline.

    The connector healthcheck is a Linux process running on its main thread,
    where SIGALRM can interrupt a socket read even when SSE comments keep the
    socket's inactivity timer alive. Preserve any caller timer for test and
    library safety; the standalone health process normally has none.
    """
    if seconds <= 0:
        raise ValueError('probe wall-clock deadline must be positive')
    if not hasattr(signal, 'setitimer'):
        raise RuntimeError('hard probe wall-clock deadline is unavailable')

    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    restore_handler = (signal.SIG_DFL if previous_handler is None
                       else previous_handler)

    def deadline_reached(_signum, _frame):
        raise ProbeDeadlineExceeded(
            f'probe exceeded wall-clock deadline of {seconds:g}s')

    signal.signal(signal.SIGALRM, deadline_reached)
    previous_delay, previous_interval = signal.setitimer(
        signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, restore_handler)
        if previous_delay > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(signal.ITIMER_REAL,
                             max(1e-6, previous_delay - elapsed),
                             previous_interval)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS asr_attempts(
        id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
        last_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS recordings(
      id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
      duration_ms INTEGER, lang TEXT, asr_engine TEXT, asr_transcript TEXT,
      plaud_transcript TEXT, summary TEXT, audio_path TEXT, archived_at TEXT,
      plaud_meta_json TEXT, plaud_segments_json TEXT, summary_json TEXT,
      asr_meta_json TEXT, asr_alternative_transcript TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS recordings_fts USING fts5(
      id UNINDEXED, name, transcript);
CREATE TABLE IF NOT EXISTS recording_tombstones(
      recording_id TEXT PRIMARY KEY, recording_number TEXT, deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summary_attempts(
           id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
           last_at TEXT, last_error TEXT);
-- Review state for PLAUD's own transcripts; asr_backfill.ensure_reviews is
-- authoritative for it and owns enrollment. Created here so a tenant archive
-- is schema-complete before the first ASR pass, and so the viewer never has to
-- special-case a brand-new tenant.
CREATE TABLE IF NOT EXISTS plaud_reviews(
      id TEXT PRIMARY KEY, state TEXT NOT NULL, eligible_epoch INTEGER,
      selected_source TEXT, reason TEXT, candidates_json TEXT,
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
      claim_epoch INTEGER, claim_owner TEXT, updated_at TEXT);
"""


def log(msg: str) -> None:
    print(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} connector[{TENANT.tenant_id}]: {msg}',
          flush=True)


# What a caught exception is allowed to say out loud. Container logs and cron
# mail are readable by whoever can read the deployment, and a failure detail is
# whatever string the failing library chose — routinely the archived audio
# path, which describes both this tenant's identity and the host's layout, and
# occasionally an echoed Authorization header.
_FILE_URI_RE = re.compile(r'file://\S*')
_BEARER_RE = re.compile(r'(?i)\bbearer\s+\S+')


def _secret_values():
    """Caller token values, so a library that echoes one cannot log it.

    Read at use rather than cached: the token files are mounted read-only and
    rotated by replacing them, and a stale copy here would stop redacting the
    value that is actually in flight.
    """
    for path in (TENANT.asr_token_file, TENANT.caller_token_file):
        if not path:
            continue
        try:
            with open(path, encoding='utf-8') as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if len(value) >= 8:
            yield value


def safe_detail(err) -> str:
    """One failure detail, with local paths and credentials removed."""
    out = _BEARER_RE.sub('Bearer [redacted]', str(err or ''))
    out = _FILE_URI_RE.sub('[local-audio]', out)
    for value in _secret_values():
        out = out.replace(value, '[redacted]')
    # Longest first: the audio directory lives inside the archive directory, so
    # replacing the archive first would leave a bare "/audio/x.mp3" behind.
    for directory, label in sorted(
            ((TENANT.audio_dir, '[local-audio]'),
             (TENANT.archive_dir, '[archive]'),
             (os.path.dirname(TENANT.asr_token_file or '') or None, '[creds]'),
             (os.path.dirname(TENANT.caller_token_file or '') or None, '[creds]')),
            key=lambda pair: len(pair[0] or ''), reverse=True):
        if not directory:
            continue
        for form in {directory, os.path.realpath(directory)}:
            out = out.replace(form, label)
    return out


def safe_exception(exc) -> str:
    """Type and message, redacted — the shape every caught-error log uses."""
    return safe_detail(f'{type(exc).__name__}: {exc}')


class SourcePollRequest:
    """Coalesced source-discovery intent, separate from durable pipeline wakes."""
    def __init__(self, waker, *, monotonic=time.monotonic, cooldown_s=SOURCE_POLL_COOLDOWN_S):
        import threading
        self._waker, self._monotonic = waker, monotonic
        self._cooldown_s, self._last_accepted, self._pending = cooldown_s, None, False
        self._lock = threading.Lock()

    def request(self):
        with self._lock:
            if self._pending:
                return 'already_queued'
            now = self._monotonic()
            if self._last_accepted is not None and now - self._last_accepted < self._cooldown_s:
                return 'cooldown'
            self._pending = True
            self._last_accepted = now
        self._waker.wake()
        return 'queued'

    def consume(self):
        with self._lock:
            if not self._pending:
                return False
            self._pending = False
            return True


def ensure_layout(db_timeout_s: float = 60) -> None:
    """Archive dir, audio dir and a schema-complete DB must exist before ingest."""
    os.makedirs(TENANT.archive_dir, exist_ok=True)
    os.makedirs(TENANT.audio_dir, exist_ok=True)
    conn = sqlite3.connect(TENANT.db_path, timeout=db_timeout_s)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        # The durable job table, from its owning module rather than a copy of
        # its DDL here: this archive can be handed a job the moment it exists,
        # and two definitions of the same table is how they drift.
        pipeline.ensure_schema(conn)
        stages.ensure_schema(conn)
        control.ensure_schema(conn)
    finally:
        conn.close()


def sync_once() -> dict:
    """One ingest pass. Returns a small non-secret summary."""
    # Imported lazily and AFTER ensure_layout: archive_recording resolves the
    # tenant and pins its MCP target at import time.
    import archive_recording as A

    conn = sqlite3.connect(TENANT.db_path, timeout=60)
    try:
        A.init_db(conn)
        # Metadata is not a completed ingest for a tenant: ASR is only allowed
        # after a non-empty, tenant-contained audio file exists.  Keep rows from
        # an earlier `get_file` without a presigned URL in the discovery set so
        # a later poll can fetch the now-available URL and atomically enqueue
        # the first ASR job.  A plain `SELECT id` made those rows invisible
        # forever because reconciliation correctly refuses to enqueue no-audio.
        have = {
            rid for rid, audio_path in conn.execute(
                'SELECT id, audio_path FROM recordings')
            if pipeline.resolve_local_audio(TENANT.audio_dir, rid, audio_path)
        }
        tombstoned = {row[0] for row in conn.execute('SELECT recording_id FROM recording_tombstones')}
        sid = A.mcp_connect()

        # Page the account's file list; anything not already archived is new.
        page, size, new_ids = 1, 50, []
        while True:
            raw = A.call('list_files', {'page': page, 'page_size': size}, sid)
            payload = A._as_doc(raw)
            # _as_doc preserves the wire shape, so this may be a bare list on a
            # server that skips the envelope, or None on malformed JSON.
            if isinstance(payload, dict):
                items = payload.get('data') or []
            elif isinstance(payload, list):
                items = payload
            else:
                items = []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                fid = item.get('id')
                if fid and fid not in have and fid not in tombstoned:
                    new_ids.append(fid)
            # Discovery is remote input. Contain it here, once, rather than
            # letting a malformed id reach a filename or a primary key.
            new_ids = A.safe_recording_ids(new_ids)
            if len(items) < size:
                break
            page += 1

        total_seen = len(have) + len(new_ids)
        if MAX_PER_PASS:
            new_ids = new_ids[:MAX_PER_PASS]

        archived, failed = 0, 0
        for fid in new_ids:
            try:
                A.archive_one(conn, sid, fid, False)
                archived += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log(f'FAIL {fid}: {safe_exception(exc)}')
        return {'seen': total_seen, 'new': len(new_ids),
                'archived': archived, 'failed': failed}
    finally:
        conn.close()


def _tenant_stage_modules():
    """(asr_backfill, summary_backfill) bound to THIS tenant, or (None, None).

    Same reload dance the legacy passes use, and for the same reason: both
    modules resolve their archive, their MCP URL and their caller token at
    import time, and a long-lived container has to pick up a rotated token
    file. The token is passed as a FILE path, never exported as a value — a
    secret in os.environ shows up in every crash dump this process writes.
    """
    if not (TENANT.asr_url and TENANT.asr_token_file):
        return None, None
    if not os.path.exists(TENANT.asr_token_file):
        log('pipeline: ASR caller token file is not present yet')
        return None, None
    os.environ['ASR_MCP_URL'] = TENANT.asr_url
    os.environ['ASR_TOKEN_FILE'] = TENANT.asr_token_file
    import importlib
    import asr_backfill
    import summary_backfill
    importlib.reload(asr_backfill)
    importlib.reload(summary_backfill)
    if not asr_backfill.TOKEN:
        # Names only, never values. A tenant that reaches here is missing its
        # own credential; falling back to whatever the host exports would be
        # transcribing with somebody else's identity.
        log('pipeline: no ASR caller token resolved for this tenant')
        return None, None
    return asr_backfill, summary_backfill


def drain_pipeline() -> dict:
    """Reconcile the queue against the disk, then run everything it owes.

    This is production orchestration. It is called from every pass, including
    passes where PLAUD discovery failed: a job whose audio is already on this
    disk owes nothing to the upstream account, and holding it because PLAUD is
    unreachable is the outage spreading to work that does not need it.

    The MCP session is opened lazily, so a pass with an empty queue costs one
    SELECT and no network at all.
    """
    conn = sqlite3.connect(TENANT.db_path, timeout=60)
    try:
        pipeline.ensure_schema(conn)
        # Re-read model presence every pass. Rolling provisioning may mount an
        # initially empty shared directory; recordings archived during that
        # window must be adopted without re-ingest once both models appear.
        diarization.configure_from_environment()
        adopted = pipeline.reconcile(
            conn, audio_dir=TENANT.audio_dir,
            diarization_available=diarization.available())
        asr_module, summary_module = _tenant_stage_modules()
        if summary_module is not None:
            # Local, idempotent migrations run on the normal connector path.
            # Existing category-bearing summaries project labels without a model
            # call; a bounded batch of valid legacy objects is then admitted to
            # the same forced summary replacement queue as reader requests.
            summary_module.ensure_attempts(conn)
            summary_module.enqueue_missing_category_summaries(conn)
            pipeline.admit_label_reclassification(conn, limit=summary_module.CATEGORY_BACKFILL_BATCH)
            conn.commit()
        sessions = {}

        def session(module):
            if module.__name__ not in sessions:
                sessions[module.__name__] = module.mcp_connect()
            return sessions[module.__name__]

        handlers = stages.build_handlers(
            asr_module=asr_module, summary_module=summary_module,
            session=session, audio_dir=TENANT.audio_dir)
        finished = pipeline.drain(
            conn, handlers,
            # asr_backfill classifies ASR failures better than the generic
            # rules can — it knows a segmented run that merely ran out of its
            # quantum is owed no strike — and it owns the redaction that keeps
            # host paths out of the ledger and out of the API behind it.
            transient=((lambda exc: not asr_module.counts_against_budget(exc))
                       if asr_module else None),
            redact=asr_module.redact if asr_module else None)
        semantic = {"replaced": 0, "purged": 0, "remaining": 0}
        try:
            semantic = semantic_reconcile.run_from_env(conn, TENANT.tenant_id)
        except Exception as exc:  # optional derived index; preserve lexical path
            log(f'semantic unavailable ({type(exc).__name__}); lexical search remains active')
        return {'adopted': len(adopted), 'finished': len(finished),
                'semantic': semantic}
    finally:
        conn.close()


def run_asr() -> None:
    """Transcribe anything PLAUD did not transcribe itself, via asr-mcp.

    COMPATIBILITY ONLY. Production no longer depends on this: the durable
    pipeline owns transcription, and drain_pipeline has already run by the time
    this is called, so on a healthy archive it finds nothing. It stays as a
    manual/emergency entry point for an operator working on one archive.
    """
    if not (TENANT.asr_url and TENANT.asr_token_file):
        return
    if not os.path.exists(TENANT.asr_token_file):
        log('ASR skipped: caller token file not present yet')
        return
    token = open(TENANT.asr_token_file, encoding='utf-8').read().strip()
    if not token:
        log('ASR skipped: caller token file is empty')
        return

    # asr_backfill reads its config from the environment at import time. Point
    # it at this tenant's archive and token FILE rather than exporting the
    # token itself: a bare ASR_MCP_TOKEN in the environment is the owner's
    # credential by convention (deploy/connector forbids it as a tenant env
    # var), asr_backfill refuses to inherit one in tenant mode, and a secret
    # left in os.environ shows up in every crash dump this process writes.
    os.environ['ASR_MCP_URL'] = TENANT.asr_url
    os.environ['ASR_TOKEN_FILE'] = TENANT.asr_token_file
    import importlib
    import asr_backfill
    importlib.reload(asr_backfill)
    if not asr_backfill.TOKEN:
        # The file was readable a moment ago, so this is a misconfiguration
        # (wrong TENANT_ID scoping, a file that emptied under us) rather than a
        # transient. Names only — never log what the token is.
        log('ASR skipped: no caller token resolved for this tenant')
        return
    try:
        conn = sqlite3.connect(TENANT.db_path, timeout=60)
        asr_backfill.ensure_attempts(conn)
        # Textless recordings first, always: they show the reader nothing,
        # while a validation candidate is already displaying its PLAUD text.
        # Validation only ever runs on a pass that had nothing else to do.
        kind, targets = asr_backfill.next_targets(conn)
        if not targets:
            conn.close()
            return
        log(f'ASR: {len(targets)} {kind} pending')
        sid = asr_backfill.mcp_connect()
        if kind == 'review':
            asr_backfill.review_next(conn, sid, engine=ASR_ENGINE)
            conn.close()
            return
        for rid, _name, dur in targets:
            try:
                # The duration decides whole-file versus windowed transcription.
                # Dropping it here sent every tenant recording — including the
                # 4h22m one — down the all-or-nothing path, which is exactly
                # what segmentation exists to avoid.
                asr_backfill.transcribe(
                    conn, sid, rid, ASR_ENGINE,
                    asr_backfill.duration_sec_from_ms(dur))
            except asr_backfill.SegmentPaused as exc:
                # Caught before SegmentIncomplete, which it subclasses. The run
                # did its bounded share of a long recording's plan and stopped
                # on purpose; the retry ledger stays untouched, or a recording
                # that simply needs nine passes would look like nine failures.
                log(f'ASR paused {rid}: {safe_detail(exc)}; resuming next pass')
                break
            except asr_backfill.SegmentIncomplete as exc:
                # A long recording checkpointed part of its plan. End the pass
                # here: staying would spend hours of this container's only
                # thread on one file while PLAUD sync falls behind.
                asr_backfill.bump(conn, rid, exc)
                log(f'ASR checkpoint {rid}: {safe_detail(exc)}; resuming next pass')
                break
            except Exception as exc:  # noqa: BLE001
                asr_backfill.bump(conn, rid, exc)
                log(f'ASR FAIL {rid}: {safe_exception(exc)}')
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log(f'ASR pass failed: {safe_exception(exc)}')


def run_summaries() -> None:
    """Generate one pending structured summary after transcript persistence."""
    if not (TENANT.asr_url and TENANT.asr_token_file):
        return
    if not os.path.exists(TENANT.asr_token_file):
        log('summary skipped: caller token file not present yet')
        return
    os.environ['ASR_MCP_URL'] = TENANT.asr_url
    os.environ['ASR_TOKEN_FILE'] = TENANT.asr_token_file
    import importlib
    import summary_backfill
    importlib.reload(summary_backfill)
    if not summary_backfill.TOKEN:
        log('summary skipped: no caller token resolved for this tenant')
        return
    try:
        summary_backfill.main([])
    except Exception as exc:  # noqa: BLE001
        # Discovery, ASR, and summary have independent retry ledgers. A summary
        # outage must not stop the next ingest pass.
        log(f'summary pass failed: {safe_exception(exc)}')


def pass_once() -> None:
    t0 = time.time()
    try:
        result = sync_once()
        log(f'sync seen={result["seen"]} new={result["new"]} '
            f'archived={result["archived"]} failed={result["failed"]} '
            f'took={int(time.time() - t0)}s')
    finally:
        # The durable pipeline runs on EVERY pass, discovery or no discovery.
        # It is not behind a flag, because a stage the archive owes is not
        # something a deployment gets to switch off silently; and it runs here,
        # inside the finally, because jobs whose audio is already local do not
        # depend on PLAUD being reachable. Any discovery exception is re-raised
        # after this block so the loop still exposes the upstream failure.
        try:
            outcome = drain_pipeline()
            if outcome['adopted'] or outcome['finished']:
                log(f'pipeline adopted={outcome["adopted"]} '
                    f'finished={outcome["finished"]}')
        except Exception as exc:  # noqa: BLE001 — never kill the pass
            log(f'pipeline pass failed: {safe_exception(exc)}')
        # `run_asr` and `run_summaries` remain explicit manual compatibility
        # entry points only. Calling them from production passes would create a
        # second scheduler and can repeat work the durable queue already owns.


def auth_status() -> dict:
    """Non-secret readiness of this tenant's caller credential.

    Deterministic and local: it answers "could this connector authenticate at
    all", not "is the service up". Reports the token file PATH, which is what
    an operator needs, and never the token, which ends up in healthcheck output
    and `docker inspect`.
    """
    if TENANT.uses_legacy_static_token:
        return {'ok': bool(TENANT.static_token), 'mode': 'static-token'}
    out = {'mode': 'mcp-caller-token', 'token_file': TENANT.caller_token_file}
    try:
        import plaud_mcp_auth
        plaud_mcp_auth.read_caller_token(TENANT.caller_token_file,
                                         TENANT.tenant_id)
    except Exception as exc:  # noqa: BLE001
        out['ok'] = False
        out['error'] = str(exc)
        return out
    out['ok'] = True
    return out


def disabled_stages() -> list:
    """Required stages this deployment has been told to switch off.

    RUN_ASR / RUN_SUMMARIES predate the durable pipeline, when they meant "skip
    the cron pass". They no longer gate anything the pipeline owes — the drain
    is unconditional — so the only thing left to do with them is say loudly
    that somebody asked for a state this system will not enter.
    """
    off = []
    if not RUN_ASR:
        off.append('RUN_ASR')
    if not RUN_SUMMARIES:
        off.append('RUN_SUMMARIES')
    return off


def queue_health() -> dict:
    """Counts from the durable queue. Counts only — never a diagnosis.

    A stage failure detail is whatever string the failing library chose, and it
    routinely carries the archived audio path. This output is printed by a
    Docker healthcheck and readable through `docker inspect`, so what travels
    is how many jobs are in each state and nothing about any one of them.
    """
    counts = {state: 0 for state in
              (pipeline.JOB_QUEUED, pipeline.JOB_PROCESSING,
               pipeline.JOB_RETRY_WAIT, pipeline.JOB_DONE, pipeline.JOB_FAILED)}
    # Diarization is a reader-requested enhancement, never a prerequisite for
    # archive, ASR, summary, or derived material. Keep its truth visible but do
    # not let an unavailable optional engine declare the required pipeline bad.
    counts['diarization'] = {state: 0 for state in counts}
    if not os.path.exists(TENANT.db_path):
        return counts
    try:
        conn = sqlite3.connect(f'file:{TENANT.db_path}?mode=ro', uri=True)
    except sqlite3.Error:
        return counts
    try:
        rows = conn.execute(
            'SELECT stage, state, COUNT(*) FROM pipeline_jobs GROUP BY stage, state').fetchall()
    except sqlite3.Error:
        return counts  # archive predates the queue; nothing to report yet
    finally:
        conn.close()
    for stage, state, count in rows:
        if state in counts:
            if stage == pipeline.STAGE_DIARIZATION:
                counts['diarization'][state] = count
            else:
                counts[state] += count
    return counts


def status() -> dict:
    out = {'tenant': TENANT.tenant_id, 'archive_dir': TENANT.archive_dir,
           'mcp_url': TENANT.mcp_url, 'db_exists': os.path.exists(TENANT.db_path)}
    out['auth'] = auth_status()
    out['stages'] = {'disabled': disabled_stages()}
    out['pipeline'] = queue_health()
    # A connector that cannot present a caller token archives nothing at all,
    # so it is not healthy no matter how good the rest of the layout looks.
    # And one more way to be unhealthy that used to be invisible: a job that
    # has exhausted its retries is work nothing in the system will ever pick up
    # again. Queued and in-flight jobs are NOT a fault — that is the pipeline
    # working — so they are reported without touching health.
    #
    # Status remains available even for a deployment main() refuses to start,
    # but must report it unhealthy. Otherwise Docker's only readiness signal
    # says healthy while the required ASR/summary work is explicitly disabled.
    out['ok'] = bool(out['auth'].get('ok')
                     and not out['stages']['disabled']
                     and not out['pipeline'][pipeline.JOB_FAILED])
    if out['db_exists']:
        conn = sqlite3.connect(f'file:{TENANT.db_path}?mode=ro', uri=True)
        try:
            out['recordings'] = conn.execute(
                'SELECT COUNT(*) FROM recordings').fetchone()[0]
            out['with_text'] = conn.execute(
                "SELECT COUNT(*) FROM recordings WHERE "
                "COALESCE(asr_transcript,'')<>'' OR "
                "COALESCE(plaud_transcript,'')<>''").fetchone()[0]
        finally:
            conn.close()
    return out


def probe() -> dict:
    """One live handshake with this tenant's MCP service, classified.

    `status` deliberately touches no network, so it cannot tell a healthy
    service from a refused token. This does — and it reports WHICH failure it
    hit, because they have different owners: the caller token is ours to
    replace, the service's PLAUD account is somebody's to seed, a JSON-RPC
    error is the service's own to fix, and a transient is nobody's.

    'mcp-error' is not 'transient'. A service that answers a well-formed
    request with a JSON-RPC error is up and refusing; waiting for it to pass on
    its own is exactly the wrong advice, and the retry that a transient invites
    would never succeed.

    Every request carries PROBE_TIMEOUT_S rather than the ingest ceiling: this
    runs under a Docker healthcheck that is killed at HEALTHCHECK_TIMEOUT_S,
    and a probe that outlives its own healthcheck reports nothing at all.

    Any transient makes the container unhealthy. That is deliberate: with
    --retries=3 at --interval=60s, staying healthy through three minutes of an
    unreachable service would mean a connector that has archived nothing for
    three minutes looks fine, and the one signal the deployment has is the one
    that never fires. An outage is visible either way; a silent one is not.
    """
    out = {'tenant': TENANT.tenant_id, 'mcp_url': TENANT.mcp_url}
    import plaud_mcp_auth

    try:
        with _probe_wall_clock_deadline(PROBE_DEADLINE_S):
            # Layout can block on SQLite for far longer than Docker's health
            # window. It belongs inside the same hard budget as network I/O.
            ensure_layout(db_timeout_s=PROBE_DB_TIMEOUT_S)
            import archive_recording as A
            sid = A.mcp_connect(timeout=PROBE_TIMEOUT_S)
            # initialize only proves the MCP transport and caller token. A service
            # with pending_seed account credentials can still initialize cleanly,
            # so exercise one harmless account-backed tool before declaring ready.
            A.call('list_files', {'page': 1, 'page_size': 1}, sid,
                   timeout=PROBE_TIMEOUT_S)
    except plaud_mcp_auth.PlaudAuthConfigError as exc:
        return {**out, 'ok': False, 'failure': 'auth-config', 'error': str(exc)}
    except plaud_mcp_auth.PlaudCallerAuthError as exc:
        return {**out, 'ok': False, 'failure': 'caller-auth', 'error': str(exc)}
    except plaud_mcp_auth.PlaudServiceAccountNotSeededError as exc:
        return {**out, 'ok': False,
                'failure': 'service-account-not-seeded', 'error': str(exc)}
    except plaud_mcp_auth.PlaudMcpError as exc:
        # Caught after the three named credential failures, so it only ever
        # sees what they did not: an unknown tool, a malformed or missing
        # response, an upstream failure the service reported as a tool error.
        # Deliberately the BASE class, so a future sibling lands here rather
        # than in the transient bucket by default.
        return {**out, 'ok': False, 'failure': 'mcp-error', 'error': str(exc)}
    except Exception as exc:  # noqa: BLE001
        # Everything else is the service being unreachable or unwell. Named
        # 'transient' so nobody spends an afternoon on the credentials.
        return {**out, 'ok': False, 'failure': 'transient',
                'error': f'{type(exc).__name__}: {exc}'}
    return {**out, 'ok': True}


def main() -> int:
    diarization.configure_from_environment()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'once'

    if mode == 'status':
        ensure_layout()
        report = status()
        print(json.dumps(report, indent=2))
        # The Docker healthcheck reads this exit code. Unusable caller auth is
        # a hard failure: the container is running and archiving nothing.
        return 0 if report.get('ok') else 1

    if mode == 'probe':
        report = probe()
        print(json.dumps(report, indent=2))
        return 0 if report.get('ok') else 1

    if TENANT.uses_legacy_static_token:
        log('refusing to run: explicit TENANT_ID configuration is required')
        return 2

    # Fail loudly rather than silently archiving into a dead end. These flags
    # used to switch off transcription and summarisation for real, while the
    # healthcheck went on reporting the tenant healthy: recordings accumulated
    # with no text and nothing ever said so. The pipeline no longer honours
    # them, so a deployment that sets one is asking for a state this system
    # will not enter, and starting anyway would be the silent disabling all
    # over again — just with a different mechanism.
    disabled = disabled_stages()
    if disabled:
        log(f'refusing to run: {", ".join(disabled)}=0 would disable a stage '
            f'this connector is responsible for. Downstream work is owned by '
            f'the durable pipeline and cannot be switched off; unset it, or '
            f'stop the container if you mean to pause this tenant.')
        return 2

    ensure_layout()

    # Single-flight per tenant. Two connectors on the same archive would fetch
    # every recording twice and race each other's writes.
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log('another connector holds the lock for this tenant; exiting')
        return 0

    # Docker gives each replacement container a new hostname. Once this process
    # holds the tenant-private single-flight lock, no prior connector instance
    # can still own the archive, so its in-flight claims are orphans now, not in
    # two hours when the generic cross-host lease expires.
    recovery_conn = sqlite3.connect(TENANT.db_path, timeout=60)
    try:
        # Keep ordinary stale-owner cleanup; the lock-backed sweep below is
        # stronger and also reclaims an identical host:pid prior incarnation.
        pipeline.recover_stale_jobs(recovery_conn, reclaim_foreign=True)
        # Holding the archive-wide single-flight lock proves that *no*
        # processing claim can be legitimate, including same hostname:pid from
        # a container's previous pid-1 incarnation.
        recovered_jobs = []
        for row in recovery_conn.execute(
                "SELECT seq,recording_id,stage,claim_owner FROM pipeline_jobs WHERE state=?",
                (pipeline.JOB_PROCESSING,)).fetchall():
            seq, rid, stage, owner = row
            cur = recovery_conn.execute('''UPDATE pipeline_jobs SET state=?, claim_epoch=NULL,
                claim_owner=NULL, available_epoch=?, updated_at=? WHERE seq=? AND state=?''',
                (pipeline.JOB_QUEUED, pipeline.now_epoch(), pipeline.stamp(), seq,
                 pipeline.JOB_PROCESSING))
            if cur.rowcount:
                recovered_jobs.append({'seq': seq, 'recording_id': rid, 'stage': stage,
                                       'claim_owner': owner})
        recovery_conn.commit()
        recovered_asr = [job for job in recovered_jobs if job['stage'] == pipeline.STAGE_ASR]
        # A worker can die after deferring its pipeline job but before its
        # segment claim is cleared. In that state the job is already queued, so
        # foreign-job recovery has nothing to change, yet every next pass skips
        # the window as "claimed by another run". Under the tenant single-flight
        # lock, a non-processing ASR job cannot have a legitimate processing
        # segment. Restore that invariant for every runnable ASR job.
        runnable_asr = [row[0] for row in recovery_conn.execute(
            '''SELECT recording_id FROM pipeline_jobs
                 WHERE stage=? AND state IN (?,?)''',
            (pipeline.STAGE_ASR, pipeline.JOB_QUEUED,
             pipeline.JOB_RETRY_WAIT)).fetchall()]
        releasable_asr = recovered_asr + runnable_asr
        released_segments = 0
        released_reviews = 0
        if releasable_asr:
            import asr_backfill
            released_segments = asr_backfill.release_processing_segments(
                recovery_conn, releasable_asr)
            released_reviews = asr_backfill.release_processing_reviews(
                recovery_conn, releasable_asr)
    finally:
        recovery_conn.close()
    if recovered_jobs or released_segments or released_reviews:
        log(f'pipeline recovered={len(recovered_jobs)} claims, '
            f'released_segments={released_segments}, and '
            f'released_reviews={released_reviews} from prior connector instance')

    # The socket is private control-plane only: user intent commits to the
    # canonical table, wakes this event, and this loop drains immediately. The
    # periodic timeout remains solely PLAUD discovery; it is never downstream
    # polling. A missing/unsafe credential fails startup closed.
    waker = pipeline.Waker()
    source_requests = SourcePollRequest(waker)
    try:
        control_server = control.ControlServer(
            TENANT.db_path, CONTROL_SOCKET, CONTROL_TOKEN_FILE, waker=waker,
            source_poll=source_requests.request,
            audio_dir=TENANT.audio_dir, cache_dirs=tuple(path for path in (
                os.environ.get("MINDMAP_DIR", ""), os.environ.get("SUMMARY_CARD_DIR", "")) if path))
        control_server.start()
    except (control.ControlUnavailable, OSError) as exc:
        log(f'refusing to run: control service credential/socket unavailable ({type(exc).__name__})')
        return 2
    try:
        if mode == 'once':
            pass_once()
            return 0

        if mode == 'loop':
            # Discovery has one monotonic cadence.  A control wake is allowed
            # to drain committed downstream work promptly, but it must neither
            # manufacture a PLAUD poll nor keep moving the next real poll out.
            discovery_deadline = time.monotonic()
            while True:
                # This distinct source intent wins over a concurrent downstream
                # wake but never mutates the periodic discovery deadline.
                if source_requests.consume():
                    try:
                        pass_once()
                    except Exception as exc:  # noqa: BLE001
                        log(f'manual source pass failed: {safe_exception(exc)}')
                    continue
                remaining = max(0, discovery_deadline - time.monotonic())
                if waker.wait(remaining):
                    try:
                        outcome = drain_pipeline()
                        if outcome['adopted'] or outcome['finished']:
                            log(f'pipeline wake adopted={outcome["adopted"]} finished={outcome["finished"]}')
                    except Exception as exc:  # noqa: BLE001
                        log(f'pipeline wake failed: {safe_exception(exc)}')
                    continue
                try:
                    pass_once()
                except Exception as exc:  # noqa: BLE001
                    log(f'pass failed: {safe_exception(exc)}')
                # Schedule from the completed discovery pass.  Wakes consumed
                # above never touch this deadline, including sustained taps.
                discovery_deadline = time.monotonic() + SYNC_INTERVAL_S

        log(f'unknown mode {mode!r}')
        return 2
    finally:
        control_server.stop()


if __name__ == '__main__':
    sys.exit(main())
