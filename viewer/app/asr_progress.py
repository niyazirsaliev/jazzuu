"""Truthful long-ASR progress, derived read-only from `asr_segments`.

Long recordings are transcribed block by block: the archive writer materialises
one `asr_segments` row per block *before* processing starts, then flips each
row's `status` as it goes. That makes an honest progress number possible — we
count rows, we never estimate.

Deliberate properties:

* **Derived, never stored.** The archive mount is read-only and the schema is
  not ours; this module only ever SELECTs.
* **Defensive about the schema.** The same code serves the owner and the family
  tenants, whose DBs may predate `asr_segments` entirely or be mid-migration.
  A missing table or a missing column yields ``None``, never an exception —
  the list and detail views must keep working on old archives.
* **Silent when it cannot prove anything.** No rows, or a finished transcript
  (ASR *or* PLAUD), means no progress object at all.
* **Same classifier as the writer.** `asr_backfill.segment_progress` is
  authoritative; this module answers identically or it is lying to the reader.
  Status is the verdict, with exactly one guard: `complete` counts only once a
  result was really written (`text IS NOT NULL`) — an empty string is a
  finished silent window, not a missing one. `last_error` is history, not a
  verdict: a window retried after a failure reads pending, because it *is*
  pending, even though the row still carries the previous diagnosis.
* **Safe fields only.** `last_error`, `meta_json`, file paths and segment text
  never leave this module — only counts, a state and a Russian label.
* **No invented time.** There is no elapsed/remaining estimate anywhere,
  because nothing in the DB supports one.

Has no fastapi dependency on purpose, so the rules stay unit-testable without
the app's runtime stack (see tests/test_asr_progress.py).
"""

import sqlite3
import sys
from pathlib import Path

try:
    from . import readiness as _readiness
except ImportError:  # source-tree unit tests; image receives the same shared file
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'archive'))
    import readiness as _readiness

TABLE = "asr_segments"

# Everything the API is allowed to publish about an in-flight transcription.
SAFE_FIELDS = (
    "state", "total", "complete", "processing", "error", "pending",
    "percent", "label_ru",
)

# Columns we cannot derive an honest answer without. `text` is needed not for
# its contents but for its NULL-ness, which is what separates a window that was
# really written from a row merely labelled complete.
REQUIRED_COLUMNS = ("id", "status", "text")


def blocks_genitive_ru(n: int) -> str:
    """Russian genitive for "блок" as used after "из" — из 1 блока / из 9 блоков."""
    tail10, tail100 = n % 10, n % 100
    word = "блока" if tail10 == 1 and tail100 != 11 else "блоков"
    return f"{n} {word}"


def _label_ru(state: str, complete: int, total: int) -> str:
    if state == "error":
        # The archive writer retries failed blocks on its own schedule; saying
        # so is the only honest thing we can promise here.
        return "Ошибка блока · повторится автоматически"
    if state == "processing":
        return f"Расшифровывается · {complete} из {blocks_genitive_ru(total)}"
    return "Ожидает расшифровки"


def _columns(conn):
    """The `asr_segments` columns, or ``None`` when the table is unusable.

    PRAGMA table_info on an absent table returns no rows rather than raising,
    which covers the "archive has not been migrated yet" case.
    """
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
    except sqlite3.Error:
        return None
    if not all(column in columns for column in REQUIRED_COLUMNS):
        return None
    return columns


def _payload(total: int, complete: int, processing: int, error: int) -> dict:
    # Anything that is neither finished, running nor failed is still queued —
    # including unknown/NULL statuses and rows labelled complete that were never
    # written. The three buckets are mutually exclusive by construction (three
    # distinct status values, and `complete` only narrows one of them), so
    # counting the remainder keeps all four summing to `total`.
    pending = max(0, total - complete - processing - error)
    if processing:
        state = "processing"
    elif error:
        state = "error"
    else:
        state = "pending"
    return {
        "state": state,
        "total": total,
        "complete": complete,
        "processing": processing,
        "error": error,
        "pending": pending,
        "percent": complete * 100 // total if total else 0,
        "label_ru": _label_ru(state, complete, total),
    }


# The writer's classifier (asr_backfill.segment_progress), expressed in SQL:
#   status 'complete' AND text IS NOT NULL -> complete
#   status 'processing'                    -> processing
#   status 'error'                         -> error
#   anything else                          -> pending
# `last_error` is deliberately not consulted. The writer keeps the previous
# diagnosis on a row it has already re-queued, so reading it as current evidence
# is how a retry in the queue gets shown to the reader as a failure.
#
# Status is compared case- and padding-insensitively, which is a superset of the
# writer's exact match: a hand-edited " Processing " still reads as running
# rather than silently becoming pending.
_STATUS = "LOWER(TRIM(COALESCE(status,'')))"

# Written, not merely labelled. `text IS NOT NULL` is the writer's own test:
# record_segment() stores '' for a silent window precisely so it reads as
# finished, while a row that was never written keeps text NULL.
_WAS_WRITTEN = "text IS NOT NULL"

COUNT_SQL = (
    "SELECT id AS rec_id, COUNT(*) AS total,"
    f" SUM(CASE WHEN {_STATUS}='complete' AND {_WAS_WRITTEN}"
    "          THEN 1 ELSE 0 END) AS complete,"
    f" SUM(CASE WHEN {_STATUS}='processing' THEN 1 ELSE 0 END) AS processing,"
    f" SUM(CASE WHEN {_STATUS}='error' THEN 1 ELSE 0 END) AS error"
    f" FROM {TABLE} WHERE id IN (%s) GROUP BY id"
)


def progress_map(conn, rec_ids) -> dict:
    """`{recording id: safe progress dict}` for the ids that have blocks.

    Ids with no `asr_segments` rows are simply absent from the result, so the
    list endpoint can emit ``None`` for them. One query for the whole page.
    """
    ids = [i for i in (rec_ids or []) if i]
    columns = _columns(conn) if ids else None
    if not columns:
        return {}
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(COUNT_SQL % placeholders, ids).fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for row in rows:
        total = row["total"] or 0
        if total <= 0:
            continue
        out[row["rec_id"]] = _payload(
            total, row["complete"] or 0, row["processing"] or 0, row["error"] or 0
        )
    return out


def progress_for(conn, rec_id, has_final_transcript: bool):
    """Safe progress dict for one recording, or ``None``.

    ``None`` means "show nothing": the transcript is already done, the archive
    has no `asr_segments` table, the table is too old to be trustworthy, or
    this recording simply has no blocks.
    """
    if has_final_transcript:
        return None
    return progress_map(conn, [rec_id]).get(rec_id)


# ---------------------------------------------------------------------------
# Status: the same question for recordings that have no blocks at all.
#
# Block progress only ever existed for long recordings. A 14-second one, and a
# recording whose PLAUD text is being validated against local ASR, had nothing
# to report — the reader could not tell a finished transcript from one that had
# never started, nor which of the two transcripts they were looking at.
#
# Four states, and never a fifth: queued, processing, ready, error. `source`
# says which text is on screen ('local' | 'plaud' | None), which is the one
# thing a reader cannot work out for themselves. Candidate bodies, diagnoses
# and paths stay in the archive where they belong.

REVIEW_TABLE = "plaud_reviews"
ATTEMPTS_TABLE = "asr_attempts"
PIPELINE_TABLE = "pipeline_jobs"

QUEUED, PROCESSING, READY, ERROR = "queued", "processing", "ready", "error"

# The writer's review vocabulary (asr_backfill RV_*). Pinned by a parity test:
# a renamed state read as "unknown" would report every finished review as still
# waiting, forever.
REVIEW_QUEUED = "queued"
REVIEW_PROCESSING = "processing"
REVIEW_REVIEWED = "reviewed"
REVIEW_ERROR = "error"

# Mirrors asr_backfill.MAX_ATTEMPTS (ASR_MAX_ATTEMPTS). Past it the writer
# stops offering the recording, so "queued" would be a promise nothing keeps.
MAX_ATTEMPTS = 3

STATUS_FIELDS = ("state", "source", "engine", "label_ru")

# The durable queue is authoritative once present. These are deliberately only
# state/epoch fields: diagnostics, candidate text and filesystem paths stay in
# the archive even when a worker failed.
PIPELINE_STATES = frozenset({"queued", "processing", "retry_wait", "failed", "done"})

_STATUS_LABEL_RU = {
    (QUEUED, "plaud"): "Текст PLAUD · ожидает проверки",
    (PROCESSING, "plaud"): "Текст PLAUD · идёт проверка",
    (ERROR, "plaud"): "Текст PLAUD · проверка не удалась",
    (QUEUED, None): "Ожидает расшифровки",
    (PROCESSING, None): "Расшифровывается",
    (ERROR, None): "Ошибка расшифровки · повторится автоматически",
}


def _status_label_ru(state, source):
    if state == READY:
        return "Расшифровка Barston ASR" if source == "local" else "Текст PLAUD"
    # A local transcript being reworked is described like a plain one: the
    # PLAUD-specific copy is only honest while PLAUD text is what is on screen.
    key = source if source == "plaud" else None
    return _STATUS_LABEL_RU[(state, key)]


def _field(record, name, default=None):
    """One field of a record, whether it is a dict or a sqlite3.Row."""
    try:
        value = record[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _usable(conn, table, required):
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return False
    return bool(columns) and all(column in columns for column in required)


def _review_states(conn, ids):
    """`{id: (state, selected_source)}` for the ids that have been enrolled."""
    if not ids or not _usable(conn, REVIEW_TABLE, ("id", "state")):
        return {}
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            "SELECT id, LOWER(TRIM(COALESCE(state,''))),"
            " LOWER(TRIM(COALESCE(selected_source,'')))"
            f" FROM {REVIEW_TABLE} WHERE id IN ({placeholders})", ids).fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: (row[1], row[2] or None) for row in rows}


def _exhausted(conn, ids):
    """Ids the writer has stopped retrying."""
    if not ids or not _usable(conn, ATTEMPTS_TABLE, ("id", "attempts")):
        return set()
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT id FROM {ATTEMPTS_TABLE} WHERE COALESCE(attempts,0)>=?"
            f" AND id IN ({placeholders})", [MAX_ATTEMPTS] + list(ids)).fetchall()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows}


def _pipeline_jobs(conn, ids):
    """Safe whole-chain job facts keyed by recording id, or empty on old DBs."""
    required = ("recording_id", "stage", "state", "available_epoch", "progress_epoch")
    if not ids or not _usable(conn, PIPELINE_TABLE, required):
        return {}
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            "SELECT recording_id, LOWER(TRIM(COALESCE(stage,''))), "
            "LOWER(TRIM(COALESCE(state,''))), COALESCE(available_epoch,0), "
            "COALESCE(progress_epoch,0) "
            f"FROM {PIPELINE_TABLE} WHERE stage IN ('asr','summary','mindmap','card') "
            f"AND recording_id IN ({placeholders})", ids).fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for rid, stage, state, available, progress in rows:
        if state in PIPELINE_STATES:
            out.setdefault(rid, {})[stage] = (state, int(available or 0), int(progress or 0))
    return out


def _pipeline_status(record, job):
    """Render a queue fact without exposing its diagnostic ledger."""
    # One canonical projection shared with connector and Recordings MCP.
    projection = _readiness.project(
        {stage: facts[0] for stage, facts in job.items()})
    state = projection['state']
    active = projection.get('stage')
    available_epoch = progress_epoch = 0
    if active and state in _readiness.OPEN_STATES:
        _stage_state, available_epoch, progress_epoch = job[active]
    displayed = ("local" if _field(record, "has_local_transcript", False)
                 else ("plaud" if _field(record, "has_plaud_transcript", False)
                       else None))
    public = "completed" if state == "done" else state
    labels = {
        "queued": "Текст PLAUD · ожидает проверки" if displayed == "plaud"
                  else "Ожидает расшифровки",
        "processing": "Текст PLAUD · идёт проверка" if displayed == "plaud"
                      else "Расшифровывается",
        "retry_wait": "Проверка PLAUD повторится автоматически" if displayed == "plaud"
                      else "Расшифровка повторится автоматически",
        "failed": "Проверка PLAUD не удалась" if displayed == "plaud"
                  else "Расшифровка не удалась",
        "completed": ("Расшифровка Barston ASR" if displayed == "local"
                      else "Текст PLAUD" if displayed == "plaud"
                      else "Расшифровка завершена"),
    }
    out = {"state": public, "source": displayed,
           "engine": str(_field(record, "engine", "") or ""),
           "label_ru": labels[public]}
    if public == "retry_wait":
        out["next_retry_epoch"] = available_epoch
    elif public == "processing" and progress_epoch:
        out["progress_epoch"] = progress_epoch
    return out


def _status(record, review, blocks, exhausted, pipeline_job=None):
    if pipeline_job is not None:
        return _pipeline_status(record, pipeline_job)
    local = bool(_field(record, "has_local_transcript", False))
    plaud = bool(_field(record, "has_plaud_transcript", False))
    displayed = "local" if local else ("plaud" if plaud else None)
    source = displayed

    if review:
        # An enrolled recording is the review's to describe: it is the only
        # work in flight for that row, and it is what decides the source.
        review_state, selected = review
        if review_state == REVIEW_REVIEWED:
            state, source = READY, (selected or displayed)
        elif review_state == REVIEW_PROCESSING:
            state = PROCESSING
        elif review_state == REVIEW_ERROR:
            state = ERROR
        else:
            state = QUEUED
    elif blocks:
        # Coarser than the block badge on purpose, and deliberately not in
        # conflict with it: the badge says what each window is doing right now
        # ("pending" between cron runs), while this says where the recording is
        # in the pipeline. A recording with some windows already committed has
        # started, so reporting it as merely queued would be a worse answer
        # than the badge sitting next to it.
        # Same precedence as the badge — processing, then error — with one
        # addition at the end: windows already committed and nothing in flight
        # is still a recording under way, not one that has yet to start.
        if blocks["total"] and blocks["complete"] == blocks["total"]:
            state = READY
        elif blocks["processing"]:
            state = PROCESSING
        elif blocks["error"]:
            state = ERROR
        elif blocks["complete"]:
            state = PROCESSING
        else:
            state = QUEUED
    elif displayed:
        state = READY
    elif exhausted:
        state = ERROR
    else:
        state = QUEUED

    if state == READY and not source:
        # Every block landed but nothing is published yet: the writer commits
        # the assembled transcript last, and calling that ready shows the
        # reader a transcript tab with nothing in it.
        state = PROCESSING
    return {
        "state": state,
        "source": source,
        "engine": str(_field(record, "engine", "") or ""),
        "label_ru": _status_label_ru(state, source),
    }


def status_map(conn, records) -> dict:
    """`{recording id: safe status dict}` for a page, in three queries.

    `records` carries what the API already knows — id, whether a local and a
    PLAUD transcript exist, and the engine — as booleans rather than text, so
    a page of 200 never copies transcripts out of SQLite to answer yes/no.
    """
    records = [r for r in (records or []) if _field(r, "id")]
    ids = [_field(r, "id") for r in records]
    if not ids:
        return {}
    reviews = _review_states(conn, ids)
    blocks = progress_map(conn, ids)
    exhausted = _exhausted(conn, ids)
    jobs = _pipeline_jobs(conn, ids)
    return {rec_id: _status(record, reviews.get(rec_id), blocks.get(rec_id),
                            rec_id in exhausted, jobs.get(rec_id))
            for record, rec_id in zip(records, ids)}


def status_for(conn, record):
    """Safe status dict for one recording, or ``None`` for an unusable record."""
    return status_map(conn, [record]).get(_field(record, "id"))
