#!/usr/bin/env python3
"""pipeline.py — the durable job/state model the whole archive runs on.

There is exactly ONE schedule in this system: the PLAUD reconciliation poll.
The official PLAUD MCP has no reliable new-recording webhook, so discovering
what the account holds has to be asked for periodically. Everything AFTER that
— downloading, transcribing, summarising, deriving — is driven by rows in
`pipeline_jobs`, and a stage never waits for a clock.

The distinction this module exists to enforce:

    the QUEUE is the SQLite table. A WAKEUP is only a hint that the table is
    worth looking at sooner.

A direct function call, a threading Event, or an internal HTTP signal may all
wake a worker. Losing every one of them may delay work; it must never lose it,
because the row is already committed. That is what makes restart recovery,
duplicate wakeups and crashes between stages boring rather than dangerous.

Deliberately stdlib-only, exactly like the rest of the ingest side: no external
broker sits between a tenant's recordings and its archive.
"""
from __future__ import annotations

import os
import re
import socket
import time

import readiness as _readiness

# ---------------------------------------------------------------- stages ----
# The chain, in the order a recording travels it. Each stage is a separate
# durable job with its own retry budget, which is the whole point: a summary
# that keeps failing must never re-run ASR over the audio again.
STAGE_ASR = 'asr'
STAGE_DIARIZATION = 'diarization'
STAGE_SUMMARY = 'summary'
STAGE_SUMMARY_EN = 'summary_en'
STAGE_MINDMAP = 'mindmap'
STAGE_CARD = 'card'
STAGES = (STAGE_ASR, STAGE_DIARIZATION, STAGE_SUMMARY, STAGE_SUMMARY_EN, STAGE_MINDMAP, STAGE_CARD)

# What each stage hands to next when it succeeds. None ends the chain: the
# recording is ready.
NEXT_STAGE = {
    STAGE_ASR: STAGE_SUMMARY,
    STAGE_DIARIZATION: None,
    STAGE_SUMMARY: STAGE_MINDMAP,
    STAGE_SUMMARY_EN: None,
    STAGE_MINDMAP: STAGE_CARD,
    STAGE_CARD: None,
}

# ---------------------------------------------------------------- states ----
JOB_QUEUED = 'queued'
JOB_PROCESSING = 'processing'
JOB_RETRY_WAIT = 'retry_wait'
JOB_DONE = 'done'
JOB_FAILED = 'failed'

# A job that is neither finished nor abandoned is still owed work.
OPEN_STATES = (JOB_QUEUED, JOB_PROCESSING, JOB_RETRY_WAIT)


def log(msg: str) -> None:
    print(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} pipeline: {msg}', flush=True)


def now_epoch(now=None) -> int:
    return int(time.time() if now is None else now)


def stamp() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def normalize_summary_language(value: str) -> str:
    """Return a compact BCP-47-style language tag or reject it."""
    language = value.strip().lower() if isinstance(value, str) else ""
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,3}", language):
        raise ValueError("invalid summary language")
    return language


def ensure_summary_variants_schema(conn) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='recording_summary_variants'"
    ).fetchone()
    if row and "CHECK(language IN ('en'))" not in (row[0] or ""):
        return
    if row:
        conn.execute("DROP TABLE IF EXISTS recording_summary_variants_new")
        conn.execute("CREATE TABLE recording_summary_variants_new(recording_id TEXT NOT NULL,language TEXT NOT NULL,summary TEXT NOT NULL,summary_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(recording_id,language))")
        conn.execute("INSERT INTO recording_summary_variants_new SELECT * FROM recording_summary_variants")
        conn.execute("DROP TABLE recording_summary_variants")
        conn.execute("ALTER TABLE recording_summary_variants_new RENAME TO recording_summary_variants")
    else:
        conn.execute("CREATE TABLE recording_summary_variants(recording_id TEXT NOT NULL,language TEXT NOT NULL,summary TEXT NOT NULL,summary_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(recording_id,language))")


def ensure_schema(conn) -> None:
    """Create the job table. Safe to run on every entry point, every pass.

    `seq` is AUTOINCREMENT rather than a plain rowid: FIFO here means "in the
    order work was durably committed", and a reused rowid after a delete would
    quietly put a new job ahead of older ones.
    """
    conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_jobs(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        recording_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        enqueued_epoch INTEGER NOT NULL,
        available_epoch INTEGER NOT NULL DEFAULT 0,
        claim_epoch INTEGER,
        claim_owner TEXT,
        progress_epoch INTEGER,
        force_local INTEGER NOT NULL DEFAULT 0,
        force_replace INTEGER NOT NULL DEFAULT 0,
        revival_attempts INTEGER NOT NULL DEFAULT 0,
        tool_attempts INTEGER NOT NULL DEFAULT 0,
        catalog_revision INTEGER,
        summary_language TEXT,
        updated_at TEXT,
        UNIQUE(recording_id, stage))''')
    # Archives created before viewer-initiated replacements have the durable
    # queue but not this intent bit.  It is deliberately on the canonical job,
    # not in a viewer sidecar: a restart must not forget that this ASR pass is a
    # requested fresh local run.
    columns = {row[1] for row in conn.execute('PRAGMA table_info(pipeline_jobs)')}
    if 'force_local' not in columns:
        conn.execute('ALTER TABLE pipeline_jobs ADD COLUMN force_local INTEGER NOT NULL DEFAULT 0')
    if 'force_replace' not in columns:
        conn.execute('ALTER TABLE pipeline_jobs ADD COLUMN force_replace INTEGER NOT NULL DEFAULT 0')
    if 'revival_attempts' not in columns:
        conn.execute('ALTER TABLE pipeline_jobs ADD COLUMN revival_attempts INTEGER NOT NULL DEFAULT 0')
    if 'tool_attempts' not in columns:
        conn.execute('ALTER TABLE pipeline_jobs ADD COLUMN tool_attempts INTEGER NOT NULL DEFAULT 0')
    if 'catalog_revision' not in columns:
        conn.execute('ALTER TABLE pipeline_jobs ADD COLUMN catalog_revision INTEGER')
    if 'summary_language' not in columns:
        conn.execute('ALTER TABLE pipeline_jobs ADD COLUMN summary_language TEXT')
    conn.commit()


def enqueue(conn, rid, stage, now=None, available_epoch=None,
            commit=True) -> bool:
    """Durably commit one stage of work. True when this call created it.

    Idempotent by construction — UNIQUE(recording_id, stage) plus DO NOTHING —
    because every caller in the system is allowed to be enthusiastic: ingest
    enqueues, a duplicate wakeup enqueues, and reconciliation enqueues whatever
    looks unfinished. Two rows for the same recording and stage would mean two
    workers doing the same transcription and racing each other's publication.

    A stage that already ran is not re-created either: `done` and `failed` rows
    stay in the table precisely so a re-enqueue is a no-op rather than an
    endless loop. Re-running one on purpose is `requeue`, which is explicit.

    `commit=False` leaves the row in the caller's open transaction. Ingest uses
    it so the recording row and its first job land together: a recording row
    with no job is work nothing in the system is looking for, and there is no
    schedule left that would notice.
    """
    seconds = now_epoch(now)
    cur = conn.execute(
        '''INSERT INTO pipeline_jobs(recording_id,stage,state,attempts,
             enqueued_epoch,available_epoch,updated_at)
           VALUES(?,?,?,0,?,?,?)
           ON CONFLICT(recording_id,stage) DO NOTHING''',
        (rid, stage, JOB_QUEUED, seconds,
         seconds if available_epoch is None else int(available_epoch), stamp()))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def asr_stage_satisfied(conn, rid) -> bool:
    """True when this archive already holds a published LOCAL transcript.

    The one guard that keeps a repair pass from re-transcribing an archive that
    is already finished. A PLAUD transcript deliberately does NOT satisfy the
    stage: it is provisional text that still has to be validated against local
    ASR, which is the entire reason the ASR job is enqueued for it.
    """
    try:
        row = conn.execute(
            "SELECT TRIM(COALESCE(asr_transcript,'')) FROM recordings WHERE id=?",
            (rid,)).fetchone()
    except Exception:  # noqa: BLE001 — an archive without the column has none
        return False
    return bool(row and row[0])


def enqueue_asr(conn, rid, now=None, commit=True) -> bool:
    """Enqueue transcription for one recording unless it is already published."""
    if asr_stage_satisfied(conn, rid):
        return False
    return enqueue(conn, rid, STAGE_ASR, now=now, commit=commit)


def request_retranscribe(conn, rid, now=None, commit=True) -> bool:
    """Durably request one fresh LOCAL-ASR replacement, idempotently.

    The existing transcript and all derived reader-visible values are not
    touched here.  This only resets the canonical ASR job after a terminal
    result; concurrent taps join an open claim instead of starting a second GPU
    run.  `force_local` is consumed by ``stages.asr_stage`` so PLAUD text can
    never divert an explicit retranscription into review mode.
    """
    seconds = now_epoch(now)
    row = conn.execute(
        'SELECT state FROM pipeline_jobs WHERE recording_id=? AND stage=?',
        (rid, STAGE_ASR)).fetchone()
    if row and row[0] in OPEN_STATES:
        return False
    if row:
        cur = conn.execute(
            '''UPDATE pipeline_jobs SET state=?, attempts=0, last_error=NULL,
                 enqueued_epoch=?, available_epoch=?, claim_epoch=NULL,
                 claim_owner=NULL, progress_epoch=NULL, force_local=1, force_replace=1,
                 updated_at=? WHERE recording_id=? AND stage=? AND state NOT IN (?,?,?)''',
            (JOB_QUEUED, seconds, seconds, stamp(), rid, STAGE_ASR, *OPEN_STATES))
    else:
        cur = conn.execute(
            '''INSERT INTO pipeline_jobs(recording_id,stage,state,attempts,
                 enqueued_epoch,available_epoch,force_local,force_replace,updated_at)
               VALUES(?,?,?,0,?,?,1,1,?)''',
            (rid, STAGE_ASR, JOB_QUEUED, seconds, seconds, stamp()))
    # Reset exactly once, in the same transaction as the explicit new ASR job.
    # Later SegmentPaused/retry claims only resume the freshly materialized plan.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asr_segments'").fetchone():
        conn.execute('DELETE FROM asr_segments WHERE id=?', (rid,))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def ensure_label_catalog_schema(conn) -> None:
    conn.execute('CREATE TABLE IF NOT EXISTS label_catalog_state('
                 'id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
    conn.execute('INSERT OR IGNORE INTO label_catalog_state(id,revision) VALUES(1,1)')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    if 'label_catalog_revision' not in columns:
        conn.execute('ALTER TABLE recordings ADD COLUMN label_catalog_revision INTEGER')


def label_catalog_revision(conn) -> int:
    ensure_label_catalog_schema(conn)
    return int(conn.execute('SELECT revision FROM label_catalog_state WHERE id=1').fetchone()[0])


def bump_label_catalog_revision(conn) -> int:
    ensure_label_catalog_schema(conn)
    conn.execute('UPDATE label_catalog_state SET revision=revision+1 WHERE id=1')
    return label_catalog_revision(conn)


def admit_label_reclassification(conn, limit=10) -> int:
    """Queue a bounded catalog generation; work survives restarts in SQLite."""
    ensure_schema(conn)
    revision, admitted = label_catalog_revision(conn), 0
    rows = conn.execute("""SELECT id FROM recordings
        WHERE LENGTH(TRIM(COALESCE(NULLIF(asr_transcript,''),plaud_transcript,'')))>=200
          AND COALESCE(label_catalog_revision,0) < ? ORDER BY id""", (revision,)).fetchall()
    for (rid,) in rows:
        if admitted >= max(1, int(limit)):
            break
        job = conn.execute('SELECT state FROM pipeline_jobs WHERE recording_id=? AND stage=?',
                           (rid, STAGE_SUMMARY)).fetchone()
        if job and job[0] in OPEN_STATES:
            continue
        if request_regenerate(conn, rid, catalog_revision=revision, commit=False):
            admitted += 1
    return admitted


def request_regenerate(conn, rid, now=None, commit=True, catalog_revision=None) -> bool:
    """Durably requeue summary → mindmap → card without touching published data.

    The summary stage carries the replacement generation bit.  Its successful
    completion atomically reopens each successor in order; a failed replacement
    leaves all prior reader-visible artifacts untouched.
    """
    seconds = now_epoch(now)
    row = conn.execute('SELECT state FROM pipeline_jobs WHERE recording_id=? AND stage=?',
                       (rid, STAGE_SUMMARY)).fetchone()
    if row and row[0] in OPEN_STATES:
        return False
    if row:
        cur = conn.execute('''UPDATE pipeline_jobs SET state=?, attempts=0, last_error=NULL,
             enqueued_epoch=?, available_epoch=?, claim_epoch=NULL, claim_owner=NULL,
             progress_epoch=NULL, force_local=0, force_replace=1, catalog_revision=?, updated_at=?
             WHERE recording_id=? AND stage=? AND state NOT IN (?,?,?)''',
             (JOB_QUEUED, seconds, seconds, catalog_revision, stamp(), rid, STAGE_SUMMARY, *OPEN_STATES))
    else:
        cur = conn.execute('''INSERT INTO pipeline_jobs(recording_id,stage,state,attempts,
             enqueued_epoch,available_epoch,force_local,force_replace,catalog_revision,updated_at)
             VALUES(?,?,?,0,?,?,0,1,?,?)''',
            (rid, STAGE_SUMMARY, JOB_QUEUED, seconds, seconds, catalog_revision, stamp()))
    if commit:
        conn.commit()
    return cur.rowcount == 1



def request_summary_language(conn, rid, language, now=None, commit=True) -> bool:
    """Idempotently queue one isolated lazy summary language variant."""
    language = normalize_summary_language(language)
    seconds = now_epoch(now)
    row = conn.execute("SELECT state,COALESCE(summary_language,'en') FROM pipeline_jobs WHERE recording_id=? AND stage=?", (rid, STAGE_SUMMARY_EN)).fetchone()
    if row and row[0] in OPEN_STATES:
        if row[1] != language:
            raise ValueError("another summary language is already queued")
        return False
    if row:
        cur = conn.execute("UPDATE pipeline_jobs SET state=?, attempts=0, last_error=NULL, enqueued_epoch=?, available_epoch=?, claim_epoch=NULL, claim_owner=NULL, progress_epoch=NULL, force_local=0, force_replace=0, summary_language=?, updated_at=? WHERE recording_id=? AND stage=? AND state NOT IN (?,?,?)", (JOB_QUEUED, seconds, seconds, language, stamp(), rid, STAGE_SUMMARY_EN, *OPEN_STATES))
    else:
        cur = conn.execute("INSERT INTO pipeline_jobs(recording_id,stage,state,attempts,enqueued_epoch,available_epoch,summary_language,updated_at) VALUES(?,?,?,0,?,?,?,?)", (rid, STAGE_SUMMARY_EN, JOB_QUEUED, seconds, seconds, language, stamp()))
    if commit:
        conn.commit()
    return cur.rowcount == 1

def request_diarize(conn, rid, now=None, commit=True) -> bool:
    """Requeue optional diarization without coupling it to ASR or summaries."""
    seconds = now_epoch(now)
    row = conn.execute('SELECT state FROM pipeline_jobs WHERE recording_id=? AND stage=?',
                       (rid, STAGE_DIARIZATION)).fetchone()
    if row and row[0] in OPEN_STATES:
        return False
    if row:
        cur = conn.execute('''UPDATE pipeline_jobs SET state=?, attempts=0, last_error=NULL,
            enqueued_epoch=?, available_epoch=?, claim_epoch=NULL, claim_owner=NULL,
            progress_epoch=NULL, force_local=0, force_replace=0, updated_at=? WHERE recording_id=? AND stage=?
            AND state NOT IN (?,?,?)''',
            (JOB_QUEUED, seconds, seconds, stamp(), rid, STAGE_DIARIZATION, *OPEN_STATES))
    else:
        cur = conn.execute('''INSERT INTO pipeline_jobs(recording_id,stage,state,attempts,
            enqueued_epoch,available_epoch,updated_at) VALUES(?,?,?,0,?,?,?)''',
            (rid, STAGE_DIARIZATION, JOB_QUEUED, seconds, seconds, stamp()))
    if commit:
        conn.commit()
    return cur.rowcount == 1


# ----------------------------------------------------------------- claims ----

# How long a claim is believed without other evidence. A stage call can be very
# long — a 30-minute ASR window against a busy service — so anything shorter
# risks two workers doing the same work, and anything longer just delays
# recovery after a SIGKILL. Overridable for deployments whose ASR is slower.
LEASE_SEC = int(os.environ.get('PIPELINE_LEASE_SECONDS', str(2 * 3600 + 600)))

JOB_COLUMNS =('seq', 'recording_id', 'stage', 'state', 'attempts',
               'last_error', 'enqueued_epoch', 'available_epoch',
               'claim_epoch', 'claim_owner', 'progress_epoch', 'force_local', 'force_replace',
               'revival_attempts', 'tool_attempts', 'catalog_revision', 'summary_language')


def claim_owner() -> str:
    """Who holds a claim: host and pid, so liveness is checkable later."""
    return f'{socket.gethostname()}:{os.getpid()}'


def _as_job(row):
    return dict(zip(JOB_COLUMNS, row)) if row else None


def claim_is_live(owner, claim_epoch, now=None, reclaim_foreign=False) -> bool:
    """True while some worker can still plausibly be running this job.

    The pid check makes recovery from a SIGKILL immediate instead of a
    lease-length wait: a claim whose process is gone from this host is dead the
    moment we look. For a claim made on another machine there is nothing to
    check, so the lease speaks for it.
    """
    if int(claim_epoch or 0) <= now_epoch(now) - LEASE_SEC:
        return False
    host, _, pid = (owner or '').partition(':')
    if not pid.isdigit() or host != socket.gethostname():
        # The general queue cannot prove a foreign worker is gone, so its lease
        # remains authoritative. The tenant connector has stronger evidence:
        # after acquiring its archive's single-flight lock, a claim from another
        # container hostname belongs to the retired instance it replaced.
        return not reclaim_foreign
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False  # the worker that claimed this job is gone
    except OSError:
        return True  # alive, just not ours to signal
    return True


def recover_stale_jobs(conn, now=None, reclaim_foreign=False) -> list[dict]:
    """Return jobs abandoned by a dead worker to the queue and describe them.

    This is the whole of restart recovery. Unfinished work is already committed
    as a row, so a worker that comes up after a crash does not need to be told
    what was in flight — it reads it. A crash is deliberately NOT a retry
    strike: an OOM kill or a redeploy says nothing about whether the audio can
    be transcribed, and spending the budget on it is how three unlucky restarts
    permanently retire a recording.
    """
    seconds = now_epoch(now)
    recovered = []
    rows = conn.execute(
        '''SELECT seq, recording_id, stage, COALESCE(claim_owner,''),
                  COALESCE(claim_epoch,0)
             FROM pipeline_jobs WHERE state=?''', (JOB_PROCESSING,)).fetchall()
    for seq, recording_id, stage, owner, claim_epoch in rows:
        if claim_is_live(owner, claim_epoch, now=seconds,
                         reclaim_foreign=reclaim_foreign):
            continue
        cur = conn.execute(
            '''UPDATE pipeline_jobs
                  SET state=?, claim_epoch=NULL, claim_owner=NULL,
                      available_epoch=?, updated_at=?
                WHERE seq=? AND state=?''',
            (JOB_QUEUED, seconds, stamp(), seq, JOB_PROCESSING))
        if cur.rowcount:
            recovered.append({'seq': seq, 'recording_id': recording_id,
                              'stage': stage, 'claim_owner': owner})
    conn.commit()
    return recovered


def recover_stale(conn, now=None, reclaim_foreign=False) -> int:
    """Compatibility count for callers that do not need recovered job IDs."""
    return len(recover_stale_jobs(
        conn, now=now, reclaim_foreign=reclaim_foreign))


def claim_next(conn, now=None, stages=None, exclude=()):
    """Take the oldest runnable job for this worker, or None.

    FIFO by `seq` — the order work was durably committed, which is the only
    order a reader can reason about. The UPDATE is a compare-and-swap against
    the exact row state the decision was made on, so two workers racing on the
    same job cannot both win: the loser sees rowcount 0 and looks again.

    `exclude` is how one drain pass refuses to pick a job it has already had a
    turn at. A stage that defers puts its job straight back on the queue, and
    without this the same pass would claim it again immediately and spin.
    """
    seconds = now_epoch(now)
    wanted = tuple(stages) if stages else STAGES
    placeholders = ','.join('?' * len(wanted))
    rows = conn.execute(
        f'''SELECT {','.join(JOB_COLUMNS)} FROM pipeline_jobs
             WHERE state IN (?,?) AND available_epoch<=? AND stage IN ({placeholders})
             ORDER BY seq ASC''',
        (JOB_QUEUED, JOB_RETRY_WAIT, seconds) + wanted).fetchall()
    for row in rows:
        job = _as_job(row)
        if job['seq'] in exclude:
            continue
        cur = conn.execute(
            '''UPDATE pipeline_jobs
                  SET state=?, claim_epoch=?, claim_owner=?, progress_epoch=?,
                      updated_at=?
                WHERE seq=? AND state=?''',
            (JOB_PROCESSING, seconds, claim_owner(), seconds, stamp(),
             job['seq'], job['state']))
        conn.commit()
        if cur.rowcount == 1:
            job.update(state=JOB_PROCESSING, claim_epoch=seconds,
                       claim_owner=claim_owner(), progress_epoch=seconds)
            return job
    return None


class ClaimLost(Exception):
    """This worker no longer owns the job it is working on.

    Raised at a checkpoint so a long ASR run can stop immediately instead of
    spending another half hour of GPU time on audio somebody else is already
    transcribing. Recognised by a marker, for the same reason StageDeferred is:
    a second copy of this module must not silently turn it back into a failure.
    """

    is_claim_lost = True


def is_claim_lost(exc) -> bool:
    return bool(getattr(exc, 'is_claim_lost', False))


# Every write below is fenced on the claim the caller made. A lease expiring
# does not prove the previous worker is gone — it proves we stopped believing
# it — and an ASR call can trickle bytes long past any inactivity timeout. So
# the old owner may still be alive and about to report on a job that has since
# been reissued. Keying these updates on `seq` alone let it mark the new
# owner's in-flight work done, spend its retry budget, or renew a lease it no
# longer held.
_FENCE = (" AND state=? AND COALESCE(claim_owner,'')=? "
          " AND COALESCE(claim_epoch,0)=?")


def _fence_args(job):
    return (JOB_PROCESSING, job.get('claim_owner') or '',
            int(job.get('claim_epoch') or 0))


def holds_claim(conn, job) -> bool:
    """True when the job is still processing under exactly this claim."""
    row = conn.execute(
        '''SELECT 1 FROM pipeline_jobs WHERE seq=?''' + _FENCE,
        (job['seq'],) + _fence_args(job)).fetchone()
    return row is not None


def heartbeat(conn, job, now=None) -> bool:
    """Say that this job is still making progress, durably. False if reissued.

    A long recording is transcribed in windows of at most half an hour, and
    each finished window calls this. Three things depend on it: the lease does
    not expire under a job that is genuinely working, the API can show a reader
    a last-progress time rather than an unexplained silence, and a worker whose
    claim was taken away finds out at the next window instead of at the end.
    """
    seconds = now_epoch(now)
    cur = conn.execute(
        '''UPDATE pipeline_jobs SET claim_epoch=?, progress_epoch=?, updated_at=?
            WHERE seq=?''' + _FENCE,
        (seconds, seconds, stamp(), job['seq']) + _fence_args(job))
    conn.commit()
    if cur.rowcount != 1:
        return False
    # The claim we now hold is the one we just wrote, so later fenced writes
    # have to compare against it rather than against the original.
    job['claim_epoch'] = seconds
    job['progress_epoch'] = seconds
    return True


def checkpoint(conn, job, now=None) -> None:
    """Heartbeat, or stop. The safe point for a long job to notice a takeover.

    Continuing after the claim is gone is the one outcome nobody can undo: two
    workers transcribing the same audio, then racing to publish two different
    transcripts for the same recording.
    """
    if not heartbeat(conn, job, now=now):
        raise ClaimLost(
            f'job {job["seq"]} ({job["stage"]} for {job["recording_id"]}) was '
            f'reissued to another worker; stopping')


# --------------------------------------------------------------- outcomes ----

MAX_ATTEMPTS = int(os.environ.get('PIPELINE_MAX_ATTEMPTS', '3'))
MAX_TOOL_ATTEMPTS = int(os.environ.get('PIPELINE_MAX_TOOL_ATTEMPTS', '8'))
MAX_REVIVALS = int(os.environ.get('PIPELINE_MAX_REVIVALS', '2'))
# First retry delay. Doubles per strike, so a stage that is genuinely broken
# stops hammering a service long before it exhausts its budget.
RETRY_BACKOFF_SEC = int(os.environ.get('PIPELINE_RETRY_BACKOFF_SECONDS', '60'))
RETRY_BACKOFF_MAX_SEC = int(
    os.environ.get('PIPELINE_RETRY_BACKOFF_MAX_SECONDS', str(30 * 60)))

# Failures that mean "the infrastructure went away mid-call", not "this input
# cannot be processed". They cost no retry budget: a redeploy or a busy
# afternoon must never permanently retire a recording nothing is wrong with.
# asr_backfill extends this tuple with its own ASR-specific entries rather than
# keeping a second copy of it.
TRANSIENT_MARKERS = (
    'unexpected mcp response: null',
    'connection reset',
    'connection refused',
    'connection aborted',
    'broken pipe',
    'timed out',
    'timeout',
    'remote end closed',
    'incompleteread',
    'temporary failure in name resolution',
    'bad gateway',
    'service unavailable',
    'gateway timeout',
    'too many requests',
    'internal server error',
)
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

# Only a number the text itself labels as a status counts. A bare three-digit
# match would read "decode failed at offset 500" as a server hiccup and hand an
# unreadable input an unlimited retry budget.
_HTTP_STATUS_RE = re.compile(
    r'\b(?:http(?:/\d(?:\.\d)?)?(?:\s+error)?|status(?:\s+code)?|code)'
    r'\s*[:=]?\s*(\d{3})\b')


def is_transient(err) -> bool:
    """True when the failure is about reaching a service, not about the input."""
    text = (str(err) or '').lower()
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return True
    return any(int(code) in TRANSIENT_HTTP_STATUSES
               for code in _HTTP_STATUS_RE.findall(text))


def backoff_sec(attempts) -> int:
    return min(RETRY_BACKOFF_MAX_SEC,
               RETRY_BACKOFF_SEC * (2 ** max(0, int(attempts))))


def complete(conn, job, now=None, next_stage=..., detail=None) -> bool:
    """Finish one stage and commit its successor in the SAME transaction.

    This is the join that removes the schedule from the middle of the pipeline.
    Before, a transcript was published and its summary waited for the next cron
    tick; now the successor row is part of the same commit, so a crash either
    leaves the stage unfinished (it is retried) or leaves the next stage queued
    (a worker picks it up). There is no third state in which a recording stops
    silently and nothing is looking for it.
    """
    successor = NEXT_STAGE.get(job['stage']) if next_stage is ... else next_stage
    seconds = now_epoch(now)
    try:
        cur = conn.execute(
            '''UPDATE pipeline_jobs
                  SET state=?, claim_epoch=NULL, claim_owner=NULL, force_local=0,
                      force_replace=0,
                      progress_epoch=?, last_error=?, updated_at=?
                WHERE seq=?''' + _FENCE,
            (JOB_DONE, seconds, detail, stamp(), job['seq']) + _fence_args(job))
        if cur.rowcount != 1:
            # Reissued while we worked. Marking it done here would advance the
            # chain on a stage the new owner has not finished — the summary
            # would then run against a transcript nobody published.
            conn.rollback()
            log(f'{job["stage"]} {job["recording_id"]}: claim lost before '
                f'completion; leaving the job to its current owner')
            return False
        if successor:
            conn.execute(
                '''INSERT INTO pipeline_jobs(recording_id,stage,state,attempts,
                     enqueued_epoch,available_epoch,force_local,force_replace,updated_at)
                   VALUES(?,?,?,0,?,?,?,?,?)
                   ON CONFLICT(recording_id,stage) DO UPDATE SET
                     state=CASE WHEN excluded.force_replace=1 THEN excluded.state
                                ELSE pipeline_jobs.state END,
                     attempts=CASE WHEN excluded.force_replace=1 THEN 0
                                   ELSE pipeline_jobs.attempts END,
                     last_error=CASE WHEN excluded.force_replace=1 THEN NULL
                                     ELSE pipeline_jobs.last_error END,
                     enqueued_epoch=CASE WHEN excluded.force_replace=1 THEN excluded.enqueued_epoch
                                         ELSE pipeline_jobs.enqueued_epoch END,
                     available_epoch=CASE WHEN excluded.force_replace=1 THEN excluded.available_epoch
                                           ELSE pipeline_jobs.available_epoch END,
                     claim_epoch=CASE WHEN excluded.force_replace=1 THEN NULL ELSE pipeline_jobs.claim_epoch END,
                     claim_owner=CASE WHEN excluded.force_replace=1 THEN NULL ELSE pipeline_jobs.claim_owner END,
                     progress_epoch=CASE WHEN excluded.force_replace=1 THEN NULL ELSE pipeline_jobs.progress_epoch END,
                     force_local=0,
                     force_replace=MAX(pipeline_jobs.force_replace, excluded.force_replace),
                     updated_at=CASE WHEN excluded.force_replace=1 THEN excluded.updated_at
                                     ELSE pipeline_jobs.updated_at END''',
                (job['recording_id'], successor, JOB_QUEUED, seconds, seconds,
                 0, int(bool(job.get('force_replace'))), stamp()))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 — a half-advanced chain is worse than none
        conn.rollback()
        raise


def fail(conn, job, err, now=None, transient=None, redact=None) -> bool:
    """Record why one stage did not finish, on that stage's own ledger.

    Only this stage is touched. A summary that keeps failing must never send
    the recording back through ASR: the audio is already transcribed, the
    transcript is already published, and re-running it would spend GPU hours to
    re-decide a review that has already been made.

    `transient` lets a caller that classifies better than the generic rules —
    asr_backfill knows a deliberately paused segmented run is not a failure at
    all — override the verdict.
    """
    conn.rollback()
    detail = err if isinstance(err, str) else f'{type(err).__name__}: {err}'
    if redact is not None:
        detail = redact(detail)
    if transient is None:
        transient = is_transient(err)
    seconds = now_epoch(now)
    # MCP tool envelopes deliberately discard server text because it may echo
    # a local URL or token. They still get a separate finite retry budget.
    tool_error = type(err).__name__ == 'McpToolError'
    tool_attempts = int(job.get('tool_attempts') or 0) + (1 if tool_error else 0)
    if tool_error:
        transient, attempts = True, int(job.get('attempts') or 0)
        state = JOB_FAILED if tool_attempts >= MAX_TOOL_ATTEMPTS else JOB_RETRY_WAIT
    else:
        attempts = int(job.get('attempts') or 0) + (0 if transient else 1)
        state = JOB_FAILED if attempts >= MAX_ATTEMPTS else JOB_RETRY_WAIT
    cur = conn.execute(
        '''UPDATE pipeline_jobs
              SET state=?, attempts=?, tool_attempts=?, last_error=?, available_epoch=?,
                  claim_epoch=NULL, claim_owner=NULL, updated_at=?
            WHERE seq=?''' + _FENCE,
        (state, attempts, tool_attempts, (detail or '')[:500], seconds + backoff_sec(tool_attempts if tool_error else attempts),
         stamp(), job['seq']) + _fence_args(job))
    conn.commit()
    if cur.rowcount != 1:
        # Somebody else owns this job now. Recording our failure against it
        # would spend THEIR retry budget for a call they never made.
        log(f'{job["stage"]} {job["recording_id"]}: claim lost; not charging '
            f'this failure to its current owner')
        return False
    return True


# ---------------------------------------------------------- reconciliation ----

# How many stranded recordings one repair pass may adopt. The poll runs every
# five minutes, so a bound here is a pacing decision, not a cap on the work:
# whatever is deferred is logged and adopted by the next pass. Importing years
# of history must not put the entire archive into the queue at once, because
# genuinely new recordings would then wait behind all of it.
RECONCILE_LIMIT = int(os.environ.get('PIPELINE_RECONCILE_LIMIT', '5'))


def resolve_local_audio(audio_dir, rid, recorded_path=None):
    """The real archived file for `rid`, or None.

    Two candidates, in order: the audio_path the archiver recorded, and the
    conventional <id>.mp3. Both are resolved and required to sit INSIDE this
    archive's audio directory — audio_path is archived metadata, not an
    instruction, and a row carrying somebody else's path (a restored backup, a
    hand-edited archive) must not turn a transcription into a read of an
    arbitrary file. A zero-byte file is a failed download, not audio.
    """
    if not audio_dir:
        return None
    directory = os.path.realpath(audio_dir)
    for candidate in ((recorded_path or ''), os.path.join(directory, f'{rid}.mp3')):
        if not candidate:
            continue
        resolved = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath([resolved, directory]) == directory
        except ValueError:
            continue
        if not inside:
            continue
        if os.path.isfile(resolved) and os.path.getsize(resolved) > 0:
            return resolved
    return None


def reconcile(conn, audio_dir=None, now=None, limit=None,
              diarization_available=False):
    """Repair the queue against what is actually on disk. Returns adopted ids.

    Two duties, both of which exist because nothing else in the system is
    scheduled:

    * A recording whose audio is archived but which has no ASR job is adopted.
      That is the interrupted-enqueue case — an older code path wrote the row,
      or a process died before committing the job — and without repair there is
      no cron that would ever look at it again.
    * A job whose worker died is returned to the queue (see recover_stale).

    Textless recordings are adopted first. They show the reader nothing at all,
    while a recording carrying a PLAUD transcript is already readable and is
    only waiting for that text to be validated.
    """
    ensure_schema(conn)
    recover_stale(conn, now=now)
    limit = RECONCILE_LIMIT if limit is None else limit
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    if 'id' not in columns:
        return []
    audio_path = 'r.audio_path' if 'audio_path' in columns else 'NULL'
    plaud = ("TRIM(COALESCE(r.plaud_transcript,''))" if 'plaud_transcript' in columns
             else "''")
    asr = ("TRIM(COALESCE(r.asr_transcript,''))" if 'asr_transcript' in columns
           else "''")
    order = ('COALESCE(r.archived_at, r.start_at, r.created_at)'
             if 'archived_at' in columns else 'r.id')
    # Failed ASR work whose only missing precondition is now present gets a
    # bounded fresh chance.  Terminal unreadable media still converges after
    # MAX_REVIVALS rather than being rediscovered forever.
    failed = conn.execute(f'''
        SELECT r.id, {audio_path} FROM recordings r JOIN pipeline_jobs j
          ON j.recording_id=r.id AND j.stage=?
         WHERE j.state=? AND {asr}='' AND COALESCE(j.revival_attempts,0)<?
         ORDER BY {order} DESC, r.id ASC''',
        (STAGE_ASR, JOB_FAILED, MAX_REVIVALS)).fetchall()
    adopted, stranded = [], 0
    for rid, recorded_path in failed:
        if len(adopted) >= limit:
            stranded += 1
            continue
        if not resolve_local_audio(audio_dir, rid, recorded_path):
            continue
        cur = conn.execute('''UPDATE pipeline_jobs SET state=?, attempts=0,
             tool_attempts=0, last_error=NULL, available_epoch=?, claim_epoch=NULL,
             claim_owner=NULL, revival_attempts=revival_attempts+1, updated_at=?
             WHERE recording_id=? AND stage=? AND state=?''',
             (JOB_QUEUED, now_epoch(now), stamp(), rid, STAGE_ASR, JOB_FAILED))
        if cur.rowcount:
            conn.commit()
            adopted.append(rid)
    rows = conn.execute(f'''
        SELECT r.id, {audio_path}
          FROM recordings r
          LEFT JOIN pipeline_jobs j ON j.recording_id=r.id AND j.stage=?
         WHERE j.seq IS NULL AND {asr}=''
         ORDER BY ({plaud}<>'') ASC, {order} DESC, r.id ASC''',
        (STAGE_ASR,)).fetchall()

    for rid, recorded_path in rows:
        if not resolve_local_audio(audio_dir, rid, recorded_path):
            continue  # nothing local to transcribe; the poll owns downloading
        if len(adopted) >= limit:
            stranded += 1
            continue
        if enqueue(conn, rid, STAGE_ASR, now=now):
            adopted.append(rid)
    if diarization_available:
        rows = conn.execute(f'''
            SELECT r.id, {audio_path}
              FROM recordings r
              LEFT JOIN pipeline_jobs j
                ON j.recording_id=r.id AND j.stage=?
             WHERE j.seq IS NULL
             ORDER BY {order} DESC, r.id ASC''',
            (STAGE_DIARIZATION,)).fetchall()
        for rid, recorded_path in rows:
            if not resolve_local_audio(audio_dir, rid, recorded_path):
                continue
            if len(adopted) >= limit:
                stranded += 1
                continue
            if enqueue(conn, rid, STAGE_DIARIZATION, now=now):
                if rid not in adopted:
                    adopted.append(rid)
    if adopted:
        log(f'reconcile adopted {len(adopted)} recording(s): {", ".join(adopted)}')
    if stranded:
        log(f'reconcile deferred {stranded} recording(s) to the next pass '
            f'(limit={limit} per pass)')
    return adopted


# ----------------------------------------------------------------- worker ----

# How many jobs one drain may run before handing control back. The same process
# also has to get back to polling PLAUD, and a four-hour recording must not hold
# the loop against every other tenant duty. Whatever is left stays queued.
DRAIN_MAX_JOBS = int(os.environ.get('PIPELINE_DRAIN_MAX_JOBS', '25'))

# A stage may hand the queue back without finishing and without failing: the
# successor of a stage is the string it returns, and STOP ends the chain here.
STOP = '__stop__'


class StageDeferred(Exception):
    """This stage made durable progress and is handing the queue back.

    A long recording transcribes one bounded window per turn, commits it, and
    raises this. Nothing failed: the job goes straight back to `queued` with its
    retry budget untouched, because a nine-window recording that needs nine
    turns must not look like nine failures. `available_in` lets a handler ask
    for a pause before its next turn.
    """

    # Recognised by this marker rather than by `isinstance`. Two copies of this
    # module — a stage package importing `pipeline` while the worker holds its
    # own instance — would otherwise define two unrelated classes, and every
    # deferral would quietly be recorded as a failure: a long recording would
    # burn its retry budget just for needing more than one turn. A duck-typed
    # check cannot degrade that way.
    is_stage_deferral = True

    def __init__(self, message, available_in=0):
        super().__init__(message)
        self.available_in = int(available_in or 0)


def is_deferral(exc) -> bool:
    return bool(getattr(exc, 'is_stage_deferral', False))


class Waker:
    """A wakeup hint, and nothing more.

    Deliberately not a queue. It carries no work, it holds no state a restart
    would need, and repeated wakes collapse into one pending signal — ingest,
    reconciliation and an internal signal all firing at once should cost one
    look at the table, not three. Losing every signal only delays work: the
    jobs are already committed rows.
    """

    def __init__(self):
        import threading
        self._event = threading.Event()

    def wake(self) -> None:
        self._event.set()

    def wait(self, timeout=None) -> bool:
        """True when a wake was pending. Consumes it."""
        fired = self._event.wait(timeout)
        if fired:
            self._event.clear()
        return fired


def defer(conn, job, exc, now=None) -> bool:
    """Put a deferred job back at the head of its own work, with no strike."""
    seconds = now_epoch(now)
    cur = conn.execute(
        '''UPDATE pipeline_jobs
              SET state=?, available_epoch=?, progress_epoch=?, last_error=?,
                  claim_epoch=NULL, claim_owner=NULL, updated_at=?
            WHERE seq=?''' + _FENCE,
        (JOB_QUEUED, seconds + int(getattr(exc, 'available_in', 0) or 0),
         seconds, str(exc)[:500], stamp(), job['seq']) + _fence_args(job))
    conn.commit()
    if cur.rowcount != 1:
        log(f'{job["stage"]} {job["recording_id"]}: claim lost; the current '
            f'owner keeps the job')
        return False
    return True


def drain(conn, handlers, now=None, max_jobs=None, stages=None,
          transient=None, redact=None):
    """Run everything the archive owes, right now. Returns the finished jobs.

    This is the whole worker. It is called directly by the ingest pass — that
    is the "immediate wake" — and by anything else that wants to be helpful. It
    reads its work from the table and nowhere else, so calling it twice, or
    never, changes only when work happens, not whether it happens.

    One failing recording never takes the queue down with it: its job goes to
    its own retry ledger and the loop moves to the next row.
    """
    ensure_schema(conn)
    recover_stale(conn, now=now)
    limit = DRAIN_MAX_JOBS if max_jobs is None else max_jobs
    finished = []
    # Jobs this pass has already had a turn at. A deferred job goes back on the
    # queue immediately — that is what makes it survive a crash — so without
    # this the loop would claim the same long recording forever and never reach
    # the recording behind it.
    had_a_turn = set()
    while len(finished) < limit:
        job = claim_next(conn, now=now, stages=stages, exclude=had_a_turn)
        if job is None:
            break
        had_a_turn.add(job['seq'])
        handler = handlers.get(job['stage'])
        if handler is None:
            # Never silently skipped: a stage nobody implements has to read as
            # a failed job, or the recording would report itself ready having
            # never been summarised.
            fail(conn, job, RuntimeError(
                f'no handler for stage {job["stage"]!r} in this deployment'),
                now=now, transient=False)
            continue
        try:
            successor = handler(conn, job)
        except Exception as exc:  # noqa: BLE001 — one recording, not the queue
            if is_deferral(exc):
                log(f'{job["stage"]} deferred {job["recording_id"]}: {exc}')
                defer(conn, job, exc, now=now)
                continue
            if is_claim_lost(exc):
                # Somebody else owns this job now. Record nothing at all: not a
                # completion, not a failure, not a strike. The fenced writes
                # would refuse anyway; stopping here also stops us burning the
                # rest of the drain budget on work we do not own.
                log(f'{job["stage"]} {job["recording_id"]}: {exc}')
                continue
            verdict = transient(exc) if transient else None
            detail = redact(f'{type(exc).__name__}: {exc}') if redact else None
            log(f'{job["stage"]} FAIL {job["recording_id"]}: '
                f'{detail or f"{type(exc).__name__}: {exc}"}')
            fail(conn, job, exc, now=now, transient=verdict, redact=redact)
            continue
        complete(conn, job, now=now,
                 next_stage=None if successor == STOP else
                 (NEXT_STAGE.get(job['stage']) if successor is None else successor))
        finished.append(job)
    return finished


# ---------------------------------------------------------- observability ----

READINESS_STAGES = _readiness.STAGES


def readiness(conn, rid):
    """Canonical durable reader state for the transcript-to-card chain."""
    try:
        rows = dict(conn.execute(
            'SELECT stage,state FROM pipeline_jobs WHERE recording_id=?',
            (rid,)).fetchall())
    except Exception:
        rows = {}  # pre-pipeline archives remain readable
    return _readiness.project(rows)


def is_ready(conn, rid) -> bool:
    """Compatibility boolean for the canonical :func:`readiness` projection."""
    return readiness(conn, rid)['ready']
