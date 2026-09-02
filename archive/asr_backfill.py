#!/usr/bin/env python3
"""Fill missing archive transcripts through Tilmech streamable HTTP.

Designed for cron: quiet unless something happens, single-flight via lockfile,
one recording per run (ASR is slow), retry budget per file_id.

Recordings longer than ASR_SEGMENT_THRESHOLD_SECONDS (30 minutes) are
transcribed in ASR_SEGMENT_SECONDS windows, each committed to asr_segments as
it lands, so an interrupted long job resumes instead of starting over. After
ASR_SEGMENTS_PER_RUN windows (default 1), the worker yields when another
recording is pending. If the queue is otherwise empty it chains the next window
immediately, avoiding an idle cron interval without starving newly arrived
short work.

Usage:
  asr_backfill.py                 # pick oldest pending recording, transcribe
  asr_backfill.py <file_id> ...   # force specific ids (ignores retry budget)
  asr_backfill.py --engine kyrgyz <file_id>
  asr_backfill.py --all           # loop over every pending item in one run

Every run transcribes audio already present in its own archive through
`transcribe_url`.

Environment (paths): ARCHIVE_DB, ASR_LOCK_PATH and ASR_TOKEN_FILE override
individually; TENANT_ID / TENANT_ARCHIVE_DIR select a tenant archive wholesale.
Setting none of them is the owner layout, unchanged (see runtime_paths).

Environment (audio routing): ASR_AUDIO_URL_BASE publishes archive audio over HTTP.
ASR_AUDIO_PATH_PREFIX maps each local audio filename into an absolute path mounted
inside asr-mcp and sends it as a file:// URI. With neither set, local file URIs
keep their historical paths.

Environment (credentials): owner runs read ASR_MCP_TOKEN or .asr_token. A
tenant run reads ASR_MCP_TOKEN_<TENANT_ID> or its own token file and never a
bare ASR_MCP_TOKEN, which belongs to the owner (see runtime_token).
"""
import json
import os
import pathlib
from collections import namedtuple
import re
import socket
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from mcp_sse import last_sse_json
import pipeline

HERE = os.path.dirname(os.path.abspath(__file__))


def runtime_paths(env=None):
    """(db, lock, token_file) for this run, resolved from the environment.

    The owner deployment sets none of these and keeps archive.db, the lockfile
    and the ASR caller token beside these scripts, exactly as before.

    A tenant is a separate container with its own archive directory and its own
    caller token, and this script is also run standalone there (cron, a manual
    rerun) rather than only through connector.py, which passes its own
    connection. Hard-coding the owner paths made every one of those runs write
    the tenant's transcripts into the owner's archive, take the owner's lock,
    and authenticate with the owner's ASR token.

    So all three follow the archive being written: ARCHIVE_DB if given, else
    TENANT_ARCHIVE_DIR/archive.db, and lock and token default next to that DB
    rather than next to this file. TENANT_ID without an archive directory is
    refused outright — falling back to the owner archive is the one outcome no
    rerun can undo.
    """
    env = os.environ if env is None else env
    db = (env.get('ARCHIVE_DB') or '').strip()
    if not db:
        tenant_id = (env.get('TENANT_ID') or '').strip()
        archive_dir = (env.get('TENANT_ARCHIVE_DIR') or '').strip()
        if tenant_id and not archive_dir:
            raise SystemExit(
                f'asr_backfill: TENANT_ID={tenant_id} is set without '
                'TENANT_ARCHIVE_DIR or ARCHIVE_DB; refusing to fall back to '
                'the owner archive')
        db = os.path.join(archive_dir or HERE, 'archive.db')
    home = os.path.dirname(os.path.abspath(db)) or HERE
    lock = ((env.get('ASR_LOCK_PATH') or '').strip()
            or os.path.join(home, '.connector.lock'))
    token_file = ((env.get('ASR_TOKEN_FILE') or '').strip()
                  or os.path.join(home, '.asr_token'))
    return db, lock, token_file


def runtime_audio_dir(env=None, db=None) -> str:
    """Where this run's already-archived audio lives.

    Follows the archive being written, exactly as the lock and the token do:
    the tenant layout keeps audio in TENANT_ARCHIVE_DIR/audio (tenant.py), the
    owner layout beside these scripts. TENANT_AUDIO_DIR overrides both for a
    deployment that mounts the media somewhere else.
    """
    env = os.environ if env is None else env
    explicit = (env.get('TENANT_AUDIO_DIR') or '').strip()
    if explicit:
        return explicit
    db = db or runtime_paths(env)[0]
    return os.path.join(os.path.dirname(os.path.abspath(db)) or HERE, 'audio')


def scoped_token_var(tenant_id) -> str:
    """Name of the env var that holds the caller token for exactly this tenant.

    A scoped name cannot be inherited from another tenant's generic process
    environment by accident.
    """
    return f'ASR_MCP_TOKEN_{re.sub(r"[^A-Z0-9]", "_", (tenant_id or "").upper())}'


def runtime_token(env=None, token_file=None) -> str:
    """The ASR caller token this run is allowed to use, '' when it has none.

    A tenant run must never inherit a bare ASR_MCP_TOKEN. That variable is the
    default caller credential; inheriting it can authenticate against the wrong
    per-caller ACL. The file-based credential design prevents that silent
    cross-tenant failure.

    So in tenant mode exactly two sources count: ASR_MCP_TOKEN_<TENANT_ID>,
    which can only have been set for this tenant, and this tenant's own token
    file (ASR_TOKEN_FILE, else the one beside its archive). Neither present
    means no token and main() stops — a run that cannot prove whose account it
    is speaking for does not get to guess.
    """
    env = os.environ if env is None else env
    if token_file is None:
        token_file = runtime_paths(env)[2]
    tenant_id = (env.get('TENANT_ID') or '').strip()
    name = scoped_token_var(tenant_id) if tenant_id else 'ASR_MCP_TOKEN'
    token = (env.get(name) or '').strip()
    if token:
        return token
    if token_file and os.path.exists(token_file):
        try:
            return open(token_file, encoding='utf-8').read().strip()
        except OSError:
            return ''  # unreadable is indistinguishable from absent: fail closed
    return ''


DB, LOCK, TOKEN_FILE = runtime_paths()
AUDIO_DIR = runtime_audio_dir(db=DB)
# Each connector owns its media and uses its own ASR caller token.
TENANT_ID = (os.environ.get('TENANT_ID') or '').strip()
LOCAL_AUDIO = bool(TENANT_ID)
# Empty means asr-mcp reads the audio off the shared volume (a file:// URL).
# A deployment that cannot mount the tenant volume into asr-mcp publishes the
# same directory over HTTP and points this at it instead.
AUDIO_URL_BASE = (os.environ.get('ASR_AUDIO_URL_BASE') or '').strip()
# A shared asr-mcp volume can expose the archive under a different absolute
# directory. Map only the local filename into that mount; the archived source
# path is never allowed to choose a directory inside asr-mcp.
AUDIO_PATH_PREFIX = (os.environ.get('ASR_AUDIO_PATH_PREFIX') or '').strip()
if AUDIO_PATH_PREFIX and not os.path.isabs(AUDIO_PATH_PREFIX):
    raise ValueError('ASR_AUDIO_PATH_PREFIX must be an absolute path')
URL = os.environ.get('ASR_MCP_URL', 'http://127.0.0.1:62362/mcp')
TOKEN = runtime_token(token_file=TOKEN_FILE)
MAX_ATTEMPTS = int(os.environ.get('ASR_MAX_ATTEMPTS', '3'))
MAX_DURATION_MS = int(os.environ.get('ASR_MAX_DURATION_MS', str(6 * 3600 * 1000)))
MIN_DURATION_MS = int(os.environ.get('ASR_MIN_DURATION_MS', '3000'))
HTTP_TIMEOUT = int(os.environ.get('ASR_HTTP_TIMEOUT', '7200'))
# Past half an hour a single call is a long bet that loses everything when the
# connection or the service blinks; split it into resumable windows. Exactly
# 30 minutes still goes whole-file — only strictly longer audio is segmented,
# and the comparison happens in milliseconds (see needs_segmentation_ms), so a
# 30:00.001 recording is not rounded down onto the all-or-nothing path.
SEGMENT_THRESHOLD_SEC = int(os.environ.get('ASR_SEGMENT_THRESHOLD_SECONDS', '600'))
SEGMENT_SEC = int(os.environ.get('ASR_SEGMENT_SECONDS', '600'))
# asr-mcp refuses duration_sec above ASR_MAX_SEGMENT_SECONDS (1800 by default,
# app/segments.py normalize_segment_request) and that refusal is a permanent
# SegmentRequestError, not a retryable blip. Every window we plan is clamped to
# this cap: a window the server will never accept is nine wasted attempts and a
# recording that can never finish.
MAX_SEGMENT_SEC = int(os.environ.get('ASR_MAX_SEGMENT_SECONDS', '600'))
# Windows transcribed before the queue is reconsidered. One long recording must
# not lock out shorter work behind it. When no other recording is pending,
# CHAIN_IDLE_SEGMENTS lets the worker keep the warm service busy instead of
# waiting for another cron interval. 0 disables the bound.
SEGMENTS_PER_RUN = int(os.environ.get('ASR_SEGMENTS_PER_RUN', '1'))
CHAIN_IDLE_SEGMENTS = os.environ.get('ASR_CHAIN_IDLE_SEGMENTS', '1') != '0'
# A claim older than the longest a call could possibly still be in flight was
# left by a run that died (SIGKILL, OOM, container restart), so another run may
# take the window. Anything shorter risks two workers transcribing the same
# half hour; anything longer just delays recovery.
SEGMENT_LEASE_SEC = int(os.environ.get(
    'ASR_SEGMENT_LEASE_SECONDS', str(HTTP_TIMEOUT + 600)))
# PLAUD produces its own transcript asynchronously; give it a grace period
# before we spend GPU time on a recording that may get one for free.
MIN_AGE_MIN = int(os.environ.get('ASR_MIN_AGE_MIN', '60'))

# ---- reviewing PLAUD's own transcripts -------------------------------------
# A non-empty plaud_transcript used to be final purely by being non-empty:
# pending() skips the row, so a truncated or looping PLAUD transcript is what
# the reader gets forever. Review makes that acceptance explicit — the text
# stays exactly as it is and stays on screen, while a local ASR hypothesis is
# produced in the background and compared against it.
#
# The migration policy is the dangerous part, and it is deliberate:
#   * A recording archived within REVIEW_NEW_WINDOW_SEC is NEW work and is
#     eligible immediately (eligible_epoch NULL).
#   * Everything older is BACKLOG. It is enrolled newest-first and only
#     REVIEW_BACKLOG_PER_DAY rows become eligible per day, so importing years
#     of history cannot occupy the queue.
#   * Textless recordings always outrank both (see next_targets).
# Enrolling everything as eligible-now — which is what a NULL eligible_epoch
# would have meant for the whole archive — is the failure this encodes against.
REVIEW_BACKLOG_PER_DAY = max(1, int(os.environ.get('ASR_REVIEW_BACKLOG_PER_DAY', '5')))
REVIEW_NEW_WINDOW_SEC = int(
    os.environ.get('ASR_REVIEW_NEW_WINDOW_HOURS', '168')) * 3600
# Reviews attempted per run. Validation is never the reason a newly arrived
# recording waits, so a pass takes one and yields.
REVIEWS_PER_RUN = max(1, int(os.environ.get('ASR_REVIEWS_PER_RUN', '1')))

os.environ['TZ'] = os.environ.get('ARCHIVE_TIMEZONE', 'UTC')
try:
    time.tzset()
except Exception:
    pass


def log(msg):
    print(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} asr_backfill: {msg}', flush=True)


# ---------------- local audio ----------------

class LocalAudioMissing(RuntimeError):
    """This tenant has no usable archived audio for the recording.

    Deliberately not transient (see counts_against_budget): no amount of
    retrying makes a file appear, and a recording that stays eligible forever
    re-enters the queue on every pass and starves everything behind it. It is
    also never a reason to ask another service to resolve the recording id.
    """


# Diagnostics travel: cron mail, container logs, the retry ledger, and from
# there the API. None of those may carry a host path — it describes the
# deployment's filesystem to whoever can read a transcript.
_LOCAL_URI_RE = re.compile(r'file://\S*')


def redact(text) -> str:
    """One failure detail, with local filesystem locations removed."""
    out = _LOCAL_URI_RE.sub('[local-audio]', str(text or ''))
    for directory in {AUDIO_DIR, os.path.realpath(AUDIO_DIR)} if AUDIO_DIR else ():
        out = out.replace(directory, '[local-audio]')
    return out


def local_audio_path(conn, rid, audio_dir=None) -> str:
    """The real file this tenant archived for `rid`.

    Two candidates, in order: the audio_path the archiver recorded, and the
    conventional <id>.mp3. Both are resolved and required to sit INSIDE this
    tenant's audio directory — audio_path is archived metadata, not an
    instruction, and a row carrying somebody else's path (a restored backup, a
    hand-edited archive) must not turn a transcription into a read of an
    arbitrary file. A zero-byte file is a failed download, not audio.
    """
    directory = os.path.realpath(audio_dir or AUDIO_DIR)
    row = conn.execute(
        'SELECT audio_path FROM recordings WHERE id=?', (rid,)).fetchone()
    candidates = [(row[0] if row else '') or '', os.path.join(directory, f'{rid}.mp3')]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath([resolved, directory]) == directory
        except ValueError:
            inside = False
        if not inside:
            continue
        if os.path.isfile(resolved) and os.path.getsize(resolved) > 0:
            return resolved
    raise LocalAudioMissing(
        f'no archived audio for {rid} in this tenant archive; refusing to '
        f'resolve it through another account')


def audio_url(path) -> str:
    """The URL asr-mcp should fetch this local recording from.

    HTTP publishing remains the explicit override. Otherwise a configured
    shared-ASR mount receives only the local filename, never any directory from
    archived metadata; that makes `../../` paths unable to escape the mount.
    """
    filename = os.path.basename(path)
    if AUDIO_URL_BASE:
        return AUDIO_URL_BASE.rstrip('/') + '/' + urllib.parse.quote(filename)
    if AUDIO_PATH_PREFIX:
        return pathlib.Path(os.path.join(AUDIO_PATH_PREFIX, filename)).as_uri()
    return pathlib.Path(path).as_uri()


def asr_request(conn, rid, engine, start_sec=None, duration_sec=None):
    """Build one local-file transcription request.

    Every recording must exist in this tenant's archive before it reaches ASR.
    Missing audio fails closed instead of asking an account-bound remote service
    to resolve an identifier that may belong to another tenant.
    """
    args = {"url": audio_url(local_audio_path(conn, rid)), "engine": engine}
    if start_sec is not None and duration_sec is not None:
        args['start_sec'] = start_sec
        args['duration_sec'] = duration_sec
    return 'transcribe_url', args


# ---------------- MCP streamable-HTTP client ----------------

def _post(body, sid=None, timeout=120):
    h = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json',
         'Accept': 'application/json, text/event-stream'}
    if sid:
        h['Mcp-Session-Id'] = sid
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=h, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8', 'replace')
        s = resp.headers.get('Mcp-Session-Id', sid)
        ct = resp.headers.get('Content-Type', '')
    o = None
    if 'text/event-stream' in ct:
        o = last_sse_json(raw)
    else:
        o = json.loads(raw) if raw.strip() else {}
    return o, s


def mcp_connect():
    _, sid = _post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                    'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                               'clientInfo': {'name': 'archiver', 'version': '1'}}})
    h = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json',
         'Accept': 'application/json, text/event-stream', 'Mcp-Session-Id': sid}
    urllib.request.urlopen(urllib.request.Request(
        URL, data=json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized',
                              'params': {}}).encode(), headers=h, method='POST'),
        timeout=15).read()
    return sid


class McpToolError(RuntimeError):
    """An MCP tool returned an error result, never ASR response data."""


def call(name, args, sid, timeout=HTTP_TIMEOUT):
    o, _ = _post({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                  'params': {'name': name, 'arguments': args}}, sid, timeout=timeout)
    res = (o or {}).get('result') or {}
    # FastMCP represents tool failures inside a successful JSON-RPC envelope.
    # Its content can echo the request URL or validation detail, so never parse
    # or retain it as a transcript/error diagnostic.
    if res.get('isError'):
        raise McpToolError('ASR MCP tool call failed')
    if res.get('structuredContent'):
        return res['structuredContent']
    content = res.get('content') or []
    if content:
        txt = content[0].get('text', '')
        try:
            return json.loads(txt)
        except Exception:
            return {'text': txt}
    raise RuntimeError(f'unexpected mcp response: {json.dumps(o)[:300]}')


# ---------------- DB ----------------

def ensure_attempts(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS asr_attempts(
        id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
        last_at TEXT, last_error TEXT)''')
    ensure_segments(conn)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    for name in ('asr_meta_json', 'asr_alternative_transcript'):
        if name not in columns:
            conn.execute(f'ALTER TABLE recordings ADD COLUMN {name} TEXT')
    conn.commit()
    ensure_reviews(conn)


# Window lifecycle, as the UI sees it. A window is materialised PENDING before
# any GPU time is spent, flips to PROCESSING the instant its call is claimed,
# and lands on COMPLETE or ERROR. What counts as done is COMPLETE with a
# non-NULL text (see done_segments): a window whose audio really is silent
# commits an empty string and is finished, while a row that merely says
# complete without ever having been written stays work to do.
ST_PENDING = 'pending'
ST_PROCESSING = 'processing'
ST_COMPLETE = 'complete'
ST_ERROR = 'error'


def ensure_segments(conn):
    """Checkpoint table for windowed transcription of long recordings."""
    conn.execute('''CREATE TABLE IF NOT EXISTS asr_segments(
        id TEXT NOT NULL, seg_index INTEGER NOT NULL,
        start_sec INTEGER NOT NULL, duration_sec INTEGER NOT NULL,
        text TEXT, alternative_text TEXT, engine TEXT, meta_json TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT,
        status TEXT, claim_epoch INTEGER, claim_owner TEXT,
        PRIMARY KEY(id, seg_index))''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(asr_segments)')}
    if 'status' not in columns:
        # No DEFAULT: rows predating the column must stay NULL long enough for
        # the backfill below to label each one from what it actually holds.
        conn.execute('ALTER TABLE asr_segments ADD COLUMN status TEXT')
    if 'claim_epoch' not in columns:
        # Unix seconds, not the human updated_at string: lease arithmetic must
        # not depend on parsing a timestamp whose UTC offset can change.
        conn.execute('ALTER TABLE asr_segments ADD COLUMN claim_epoch INTEGER')
    if 'claim_owner' not in columns:
        conn.execute('ALTER TABLE asr_segments ADD COLUMN claim_owner TEXT')
    if 'alternative_text' not in columns:
        conn.execute('ALTER TABLE asr_segments ADD COLUMN alternative_text TEXT')
    conn.execute(
        "UPDATE asr_segments SET status=? WHERE COALESCE(status,'')=''"
        "  AND COALESCE(text,'')<>''", (ST_COMPLETE,))
    conn.execute(
        "UPDATE asr_segments SET status=? WHERE COALESCE(status,'')=''"
        "  AND COALESCE(last_error,'')<>''", (ST_ERROR,))
    conn.execute(
        "UPDATE asr_segments SET status=? WHERE COALESCE(status,'')=''",
        (ST_PENDING,))
    conn.commit()


def pending(conn, include_exhausted=False):
    q = '''SELECT r.id, r.name, r.duration_ms, r.archived_at, COALESCE(a.attempts,0)
           FROM recordings r LEFT JOIN asr_attempts a ON a.id=r.id
           WHERE COALESCE(r.plaud_transcript,'')=''
             AND COALESCE(r.asr_transcript,'')=''
           -- Finish shorter eligible work first. The previous oldest-first
           -- order let one 4h22m recording monopolise the single-flight queue
           -- for five hours, leaving two 32-minute recordings untouched.
           -- Long recordings still run once the bounded jobs are drained.
           ORDER BY COALESCE(r.duration_ms,0) ASC,
                    COALESCE(r.start_at, r.created_at) ASC'''
    now = time.time()
    out = []
    for rid, name, dur, archived_at, att in conn.execute(q):
        dur = dur or 0
        if not include_exhausted and att >= MAX_ATTEMPTS:
            continue
        if dur and dur < MIN_DURATION_MS:
            continue
        if dur > MAX_DURATION_MS:
            continue
        if MIN_AGE_MIN and archived_at:
            try:
                ts = time.mktime(time.strptime(archived_at[:19], '%Y-%m-%dT%H:%M:%S'))
                if now - ts < MIN_AGE_MIN * 60:
                    continue  # too fresh, PLAUD may still deliver a transcript
            except ValueError:
                pass
        out.append((rid, name, dur))
    return out


def has_competing_pending(conn, rid):
    """Whether another recording should receive the queue after this window."""
    return any(row[0] != rid for row in pending(conn))


# Errors that mean "the infrastructure went away mid-call", not "this audio
# cannot be transcribed". A 4-hour job killed by an asr-mcp restart must not
# burn one of the three attempts, or a couple of unlucky redeploys retire a
# recording permanently and no cron pass ever looks at it again.
TRANSIENT_ERROR_MARKERS = (
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
    # A run that stopped on purpose — its quantum ran out, or another run
    # already holds the window. Nothing failed, so nothing is owed a strike.
    'paused after',
    'held by another run',
)

# The same verdict by status code, for the many ways a failure reaches us
# without its reason phrase (urllib's "HTTP Error 500:", an asr-mcp detail
# string, a proxy's one-liner). 429 is the service asking us to slow down and
# 500 is it falling over mid-call: retrying is exactly right, and spending a
# strike on either is how a busy afternoon or one bad deploy permanently
# retires a recording nothing is wrong with. 502/503/504 are already named
# above by phrase and are listed here so both spellings agree.
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

# Only a number the text itself labels as a status counts. A bare three-digit
# match would read "decode failed at offset 500" as a server hiccup and hand
# an unreadable file an unlimited retry budget, which is the opposite failure:
# it re-enters the queue on every pass and starves everything behind it.
_HTTP_STATUS_RE = re.compile(
    r'\b(?:http(?:/\d(?:\.\d)?)?(?:\s+error)?|status(?:\s+code)?|code)'
    r'\s*[:=]?\s*(\d{3})\b')


def is_transient(err) -> bool:
    """True when the failure is about reaching the service, not the audio."""
    text = (str(err) or '').lower()
    if any(marker in text for marker in TRANSIENT_ERROR_MARKERS):
        return True
    return any(int(code) in TRANSIENT_HTTP_STATUSES
               for code in _HTTP_STATUS_RE.findall(text))


SEGMENT_INCOMPLETE_MARKER = 'segments incomplete'


class SegmentIncomplete(RuntimeError):
    """Some windows of a segmented recording are still missing.

    `transient` says whether the reason is worth a retry strike. An asr-mcp
    restart or an exhausted per-run quantum is not: the windows that did land
    are committed and the next run resumes. "decode failed" on the same window
    every time IS: without a strike such a recording is eligible forever and
    re-enters the queue on every single pass, which is how one unreadable file
    quietly starves every other recording behind it.
    """

    def __init__(self, message, transient=True):
        super().__init__(message)
        self.transient = transient


class SegmentPaused(SegmentIncomplete):
    """This run stopped on purpose with the recording still unfinished.

    The per-run quantum ran out, or another live run already holds the window.
    Nothing failed and nothing is owed a diagnosis: callers log it and leave
    the recording exactly as it was — pending, with its committed windows —
    rather than writing it into the retry ledger as an error.
    """


def is_partial(err) -> bool:
    """True when a run ended with checkpointed progress rather than a verdict."""
    return SEGMENT_INCOMPLETE_MARKER in (str(err) or '').lower()


def counts_against_budget(err) -> bool:
    """Whether this failure should consume one of MAX_ATTEMPTS.

    The exception object is authoritative when we have it; a caller that only
    kept the formatted string still classifies correctly, because a
    SegmentIncomplete message embeds the underlying failure detail.
    """
    if isinstance(err, SegmentIncomplete):
        return not err.transient
    return not is_transient(err)


def bump(conn, rid, err):
    """Record a failure against the recording's retry budget.

    Accepts the exception itself (preferred — its classification is exact) or a
    preformatted string. Transient infrastructure errors and deliberately
    paused segmented runs are logged but do NOT count towards MAX_ATTEMPTS;
    everything else does, including a segmented run that failed on a verdict
    about the audio.
    """
    # The caller reaches here from an except block, where a half-applied
    # transaction may still be open (see store()). Never let bump's commit be
    # the thing that persists it.
    conn.rollback()
    detail = redact(err if isinstance(err, str)
                    else f'{type(err).__name__}: {err}')
    increment = 1 if counts_against_budget(err) else 0
    conn.execute('''INSERT INTO asr_attempts(id,attempts,last_at,last_error)
        VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        attempts=asr_attempts.attempts+excluded.attempts, last_at=excluded.last_at,
        last_error=excluded.last_error''',
        (rid, increment, time.strftime('%Y-%m-%dT%H:%M:%S%z'), (detail or '')[:500]))
    conn.commit()


def assert_publication_claim(conn, job):
    """Atomically prove the pipeline claim before publishing any artifact.

    A no-op UPDATE is intentionally used instead of a preceding SELECT: the
    assertion and the following recordings/FTS/review-ledger changes share one
    SQLite transaction, so a reclaimed owner cannot publish the PLAUD-wins
    branch (which otherwise has no recordings UPDATE to fence on).
    """
    if job is None:
        return
    cur = conn.execute(
        '''UPDATE pipeline_jobs SET updated_at=updated_at WHERE seq=?
           AND state='processing' AND COALESCE(claim_owner,'')=?
           AND COALESCE(claim_epoch,0)=?''',
        (job['seq'], job.get('claim_owner') or '',
         int(job.get('claim_epoch') or 0)))
    if cur.rowcount != 1:
        raise pipeline.ClaimLost('pipeline claim lost before artifact publication')


_META_BODY_KEYS = {
    "text", "alternative_text", "alternative_transcript",
    "unresolved_hypotheses",
}


def public_asr_meta(value):
    """Keep routing evidence while removing transcript bodies from UI metadata."""
    if isinstance(value, dict):
        return {
            key: public_asr_meta(item)
            for key, item in value.items()
            if key not in _META_BODY_KEYS
        }
    if isinstance(value, list):
        return [public_asr_meta(item) for item in value]
    return value


def store(conn, rid, text, engine, lang, meta=None, alternative=None, job=None):
    """Publish a finished transcript: canonical row, search index and the
    retry ledger, all or nothing.

    Partial application is the one outcome that cannot be repaired later. The
    transcript alone makes pending() skip the recording forever, so a search
    index left empty here — the FTS delete applied, its insert not — is
    permanent invisibility, and the bump() that follows the failure would
    otherwise commit exactly that state.
    """
    name = conn.execute('SELECT name FROM recordings WHERE id=?', (rid,)).fetchone()
    name = (name[0] if name else '') or ''
    try:
        assert_publication_claim(conn, job)
        prior = conn.execute('SELECT asr_meta_json FROM recordings WHERE id=?', (rid,)).fetchone()
        try:
            previous_meta = json.loads((prior[0] if prior else '') or '{}')
        except (TypeError, ValueError):
            previous_meta = {}
        cleaned_meta = public_asr_meta(dict(meta or {}))
        merged_meta = cleaned_meta if isinstance(cleaned_meta, dict) else {}
        if isinstance(previous_meta, dict) and isinstance(previous_meta.get('diarization'), dict):
            merged_meta['diarization'] = previous_meta['diarization']
        args = [text, engine, lang or '', json.dumps(merged_meta, ensure_ascii=False), alternative or None, rid]
        cur = conn.execute('''UPDATE recordings SET asr_transcript=?, asr_engine=?,
                        lang=COALESCE(NULLIF(?,''), lang), asr_meta_json=?,
                        asr_alternative_transcript=? WHERE id=?''', args)
        if cur.rowcount != 1:
            raise RuntimeError('recording disappeared before transcript publication')
        conn.execute('DELETE FROM recordings_fts WHERE id=?', (rid,))
        conn.execute('INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)',
                     (rid, name, text))
        conn.execute('DELETE FROM asr_attempts WHERE id=?', (rid,))
        conn.commit()
    except Exception:  # noqa: BLE001 — undo everything, then let it surface
        conn.rollback()
        raise


# ---------------- segmented transcription ----------------

def duration_sec_from_ms(duration_ms) -> int:
    """Archive milliseconds → the seconds the ASR request is planned from.

    Rounds UP, on purpose, and is the only conversion any caller should use.
    Flooring loses the tail — the last 999 ms of a recording would simply never
    be transcribed — and it also drags 30:00.999 down onto the whole-file path
    that segmentation exists to keep long audio off.
    """
    return -(-int(duration_ms or 0) // 1000)


def min_segment_sec() -> int:
    """Shortest window ASR will accept, in whole seconds, rounded UP.

    A 3500 ms minimum floored to 3 s would let us plan a 3 s window that the
    service then rejects on every attempt.
    """
    return -(-MIN_DURATION_MS // 1000)


def needs_segmentation_ms(duration_ms) -> bool:
    """Segment strictly longer than the threshold, compared in milliseconds."""
    return int(duration_ms or 0) > SEGMENT_THRESHOLD_SEC * 1000


def needs_segmentation(duration_sec) -> bool:
    return needs_segmentation_ms(int(duration_sec or 0) * 1000)


def plan_segments(duration_sec, chunk_sec=None):
    """[(index, start_sec, duration_sec)] tiling the recording end to end.

    Every window obeys the server's two hard bounds: no longer than
    MAX_SEGMENT_SEC and no shorter than the ASR minimum. Both are permanent
    refusals on the asr-mcp side, so a plan that ignores them does not fail
    once — it fails identically on every retry until the recording gives up.
    """
    total = int(duration_sec or 0)
    # Clamped, not trusted: an ASR_SEGMENT_SECONDS above the server cap would
    # otherwise turn every single window into a guaranteed rejection.
    chunk = max(1, min(int(chunk_sec or SEGMENT_SEC), MAX_SEGMENT_SEC))
    if total <= 0:
        return []
    plan = []
    start = 0
    while start < total:
        plan.append([len(plan), start, min(chunk, total - start)])
        start += chunk
    # A tail shorter than the minimum ASR will accept can never succeed on its
    # own, and one unwinnable window blocks assembly of the whole recording.
    # Borrow from the window before it rather than merging into it: merging
    # produces a window longer than the cap, which the server rejects outright.
    minimum = min_segment_sec()
    if len(plan) > 1 and plan[-1][2] < minimum:
        borrow = min(minimum - plan[-1][2], max(0, plan[-2][2] - minimum))
        if borrow:
            plan[-2][2] -= borrow
            plan[-1][1] -= borrow
            plan[-1][2] += borrow
        if plan[-1][2] < minimum:
            # The neighbour had nothing to spare, so both are shorter than the
            # minimum and merging them stays far below the cap.
            plan[-2][2] += plan[-1][2]
            plan.pop()
    return [tuple(seg) for seg in plan]


def format_marker(seconds) -> str:
    """Offset into the recording as HH:MM:SS."""
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def done_segments(conn, rid, plan=None):
    """Committed windows for a recording, keyed by index.

    A window is done when this code committed a result for it: status complete
    AND a text column that was actually written (empty string included — a
    silent half hour is a finished half hour, and treating it as missing work
    is how a quiet window retries forever and blocks the whole assembly).

    A row merely labelled complete with nothing written, or holding text
    captured on boundaries the current plan no longer uses, is not progress:
    trusting either splices silence or old audio in under a wrong timestamp.
    """
    rows = conn.execute(
        '''SELECT seg_index, start_sec, duration_sec, text,
                  COALESCE(alternative_text,''), COALESCE(engine,''),
                  COALESCE(meta_json,'{}')
           FROM asr_segments
           WHERE id=? AND text IS NOT NULL AND status=?
           ORDER BY seg_index''', (rid, ST_COMPLETE)).fetchall()
    bounds = {index: (start, duration) for index, start, duration in (plan or [])}
    out = {}
    for index, start, duration, text, alternative, engine, meta_json in rows:
        if plan is not None and bounds.get(index) != (start, duration):
            continue
        try:
            meta = json.loads(meta_json)
        except ValueError:
            meta = {}
        # Rows written before alternative_text existed kept the body in meta.
        alternative = alternative or meta.pop('alternative', '')
        out[index] = {'index': index, 'start_sec': start, 'duration_sec': duration,
                      'text': text, 'alternative': alternative,
                      'engine': engine, 'meta': meta}
    return out


def materialize_plan(conn, rid, plan):
    """Make the stored rows equal the current plan, in one committed transaction.

    Until this runs, nothing exists for a window that has not succeeded yet, so
    a reader cannot tell a nine-window job at window one from a one-window job
    that is done. Every window of the plan lands as pending up front; windows
    already holding a committed result on exactly these boundaries keep it and
    read complete. Everything else — a stale claim from a killed run, an
    errored window, a checkpoint from a retuned window size — is reset to
    pending for this run, with its attempt count and last error kept as history.

    A window another run is working on right now — claimed within the lease —
    is left alone on its own boundaries: resetting it would hand the same half
    hour of GPU work to two processes at once.

    Rows outside the current plan are DELETED. A retune from four windows to
    two used to leave the two orphans behind, so progress read "2/4 complete"
    forever and the orphaned text described audio no window covers any more.
    """
    existing = {
        index: (start, duration, text, status, claim_epoch, owner)
        for index, start, duration, text, status, claim_epoch, owner
        in conn.execute(
            '''SELECT seg_index, start_sec, duration_sec, text,
                      COALESCE(status,''), COALESCE(claim_epoch,0),
                      COALESCE(claim_owner,'')
               FROM asr_segments WHERE id=?''', (rid,))}
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    try:
        planned = {index for index, _start, _duration in plan}
        for index in existing:
            if index not in planned:
                conn.execute(
                    'DELETE FROM asr_segments WHERE id=? AND seg_index=?',
                    (rid, index))
        for index, start, duration in plan:
            row = existing.get(index)
            on_plan = bool(row) and (row[0], row[1]) == (start, duration)
            if on_plan and row[2] is not None and row[3] == ST_COMPLETE:
                continue
            if (on_plan and row[3] == ST_PROCESSING
                    and not claim_expired(row[5], row[4])):
                continue
            conn.execute(
                '''INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,
                     text,engine,meta_json,attempts,status,updated_at,claim_epoch)
                   VALUES(?,?,?,?,NULL,NULL,NULL,0,?,?,NULL)
                   ON CONFLICT(id,seg_index) DO UPDATE SET
                     start_sec=excluded.start_sec, duration_sec=excluded.duration_sec,
                     text=NULL, engine=NULL, meta_json=NULL,
                     status=excluded.status, updated_at=excluded.updated_at,
                     claim_epoch=NULL''',
                (rid, index, start, duration, ST_PENDING, stamp))
        conn.commit()
    except Exception:  # noqa: BLE001 — a half-rewritten plan is worse than none
        conn.rollback()
        raise


def mark_processing(conn, rid, index, start, duration):
    """Claim one window unconditionally, and commit the claim.

    Committed separately from the result so a reader watching the DB sees the
    window in flight; a run killed here leaves the claim behind, and it expires
    after SEGMENT_LEASE_SEC (see claim_segment).
    """
    conn.execute(
        '''INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,
             attempts,status,updated_at,claim_epoch,claim_owner)
           VALUES(?,?,?,?,0,?,?,?,?)
           ON CONFLICT(id,seg_index) DO UPDATE SET
             start_sec=excluded.start_sec, duration_sec=excluded.duration_sec,
             status=excluded.status, updated_at=excluded.updated_at,
             claim_epoch=excluded.claim_epoch, claim_owner=excluded.claim_owner''',
        (rid, index, start, duration, ST_PROCESSING,
         time.strftime('%Y-%m-%dT%H:%M:%S%z'), int(time.time()), claim_owner()))
    conn.commit()


def claim_owner() -> str:
    """Who holds a claim: host and pid, so liveness is checkable later."""
    return f'{socket.gethostname()}:{os.getpid()}'


def release_processing_segments(conn, recovered_jobs) -> int:
    """Release only segments orphaned by a recovered ASR claim.

    A legacy bare recording id is accepted for compatibility, but can release
    only objectively dead segment claims.  A recovery record additionally lets
    startup release the matching former pipeline owner's claim; live foreign
    work is never stolen.
    """
    recovered_jobs = list(recovered_jobs)
    if not recovered_jobs:
        return 0
    ensure_segments(conn)
    released = 0
    for recovered in recovered_jobs:
        rid = recovered['recording_id'] if isinstance(recovered, dict) else recovered
        owner = (recovered.get('claim_owner', '') if isinstance(recovered, dict)
                 else claim_owner())
        rows = conn.execute('''SELECT seg_index, claim_owner, claim_epoch
                               FROM asr_segments WHERE id=? AND status=?''',
                            (rid, ST_PROCESSING)).fetchall()
        for index, segment_owner, segment_epoch in rows:
            if segment_owner != owner and claim_is_live(segment_owner, segment_epoch):
                continue
            cur = conn.execute('''UPDATE asr_segments SET status=?, claim_epoch=NULL,
                claim_owner=NULL, updated_at=? WHERE id=? AND seg_index=? AND status=?
                AND claim_owner=? AND claim_epoch=?''',
                (ST_PENDING, time.strftime('%Y-%m-%dT%H:%M:%S%z'), rid, index,
                 ST_PROCESSING, segment_owner, segment_epoch))
            released += cur.rowcount
    conn.commit()
    return released


def release_processing_reviews(conn, recording_ids) -> int:
    """Release orphaned review claims under the archive single-flight lock."""
    ensure_reviews(conn)
    released = 0
    for item in recording_ids:
        rid = item["recording_id"] if isinstance(item, dict) else item
        cur = conn.execute(
            """UPDATE plaud_reviews SET state=?, claim_epoch=NULL,
                       claim_owner=NULL, updated_at=?
                   WHERE id=? AND state=?""",
            (RV_QUEUED, time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             rid, RV_PROCESSING),
        )
        released += cur.rowcount
    conn.commit()
    return released


def claim_is_live(owner, claim_epoch) -> bool:
    """True while some run can still plausibly be working on this window.

    The pid check is what makes recovery from a SIGKILL immediate instead of a
    lease-length wait: a claim whose process is gone from this host is dead the
    moment we look. For a claim made on another machine there is nothing to
    check, so the lease speaks for it — no call can outlive HTTP_TIMEOUT.
    """
    if int(claim_epoch or 0) <= int(time.time()) - SEGMENT_LEASE_SEC:
        return False
    host, _, pid = (owner or '').partition(':')
    if not pid.isdigit() or host != socket.gethostname():
        return True  # another machine, still inside the lease
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False  # the run that claimed this window is gone
    except OSError:
        return True  # alive, just not ours to signal
    return True


def claim_expired(owner, claim_epoch) -> bool:
    """True when this run may take a window that someone claimed.

    Either nobody is working on it, or the claim is this process's own: a run
    is never concurrent with itself, so its own leftover claim — from a pass
    that unwound without clearing it — is not a reason to stall.
    """
    if owner and owner == claim_owner():
        return True
    return not claim_is_live(owner, claim_epoch)


def claim_segment(conn, rid, index, start, duration) -> bool:
    """Take one window for this run, atomically. True when it is ours.

    The UPDATE is a compare-and-swap against the exact row state the decision
    was made on, so two runs that reach the same window — different entry
    points, or a lockfile that guards only one of them — cannot both start the
    same half-hour of GPU work and then both write the row. The loser sees
    rowcount 0 and leaves the window alone.

    Claimable: pending, error, a row with no status yet, and an expired claim.
    Not claimable: a live claim held by another run, or a finished window.
    """
    row = conn.execute(
        '''SELECT COALESCE(status,''), COALESCE(claim_epoch,0),
                  COALESCE(claim_owner,'')
           FROM asr_segments WHERE id=? AND seg_index=?''',
        (rid, index)).fetchone()
    if row is None:
        return False  # materialize_plan writes every window before we get here
    status, claim_epoch, owner = row
    if status == ST_COMPLETE:
        return False
    if status == ST_PROCESSING and not claim_expired(owner, claim_epoch):
        return False
    cur = conn.execute(
        '''UPDATE asr_segments
              SET status=?, updated_at=?, claim_epoch=?, claim_owner=?,
                  start_sec=?, duration_sec=?
            WHERE id=? AND seg_index=?
              AND COALESCE(status,'')=? AND COALESCE(claim_epoch,0)=?
              AND COALESCE(claim_owner,'')=?''',
        (ST_PROCESSING, time.strftime('%Y-%m-%dT%H:%M:%S%z'),
         int(time.time()), claim_owner(), start, duration, rid, index,
         status, claim_epoch, owner))
    conn.commit()
    return cur.rowcount == 1


def record_segment(conn, rid, index, start, duration, text, engine, meta,
                   alternative=''):
    """Commit one finished window. Idempotent: a rerun overwrites in place.

    `text` may legitimately be empty — a window of silence transcribes to
    nothing — so it is stored as '' rather than NULL, which is what tells the
    next run this window is finished and not merely untouched.
    """
    conn.execute(
        '''INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,text,
             alternative_text,engine,meta_json,attempts,last_error,status,
             updated_at,claim_epoch)
           VALUES(?,?,?,?,?,?,?,?,0,NULL,?,?,NULL)
           ON CONFLICT(id,seg_index) DO UPDATE SET
             start_sec=excluded.start_sec, duration_sec=excluded.duration_sec,
             text=excluded.text, alternative_text=excluded.alternative_text,
             engine=excluded.engine,
             meta_json=excluded.meta_json, last_error=NULL,
             status=excluded.status, updated_at=excluded.updated_at,
             claim_epoch=NULL, claim_owner=NULL''',
        (rid, index, start, duration, text or '', alternative or None, engine,
         json.dumps(public_asr_meta(meta or {}), ensure_ascii=False), ST_COMPLETE,
         time.strftime('%Y-%m-%dT%H:%M:%S%z')))
    conn.commit()


def fail_segment(conn, rid, index, start, duration, err):
    """Record why one window is still missing, without touching global attempts."""
    conn.execute(
        '''INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,
             attempts,last_error,status,updated_at,claim_epoch)
           VALUES(?,?,?,?,1,?,?,?,NULL)
           ON CONFLICT(id,seg_index) DO UPDATE SET
             start_sec=excluded.start_sec, duration_sec=excluded.duration_sec,
             attempts=asr_segments.attempts+1, last_error=excluded.last_error,
             status=excluded.status, updated_at=excluded.updated_at,
             claim_epoch=NULL, claim_owner=NULL''',
        (rid, index, start, duration, (err or '')[:500], ST_ERROR,
         time.strftime('%Y-%m-%dT%H:%M:%S%z')))
    conn.commit()


def segment_progress(conn, rid):
    """How far along one recording is: counts only, never transcript text.

    Status is authoritative, with one guard: complete counts only when a result
    was really written. A window retried after an error reads pending — it is
    pending — even though its row still carries the previous diagnosis in
    last_error, which stays as history rather than contradicting the status.

    `stale` is a subset of `processing`: windows whose claiming run is gone —
    its pid no longer on this host, or its claim older than SEGMENT_LEASE_SEC.
    The four buckets still sum to total.
    """
    counts = {ST_COMPLETE: 0, ST_PROCESSING: 0, ST_ERROR: 0, ST_PENDING: 0}
    stale = 0
    rows = conn.execute(
        '''SELECT COALESCE(status,''), text IS NOT NULL,
                  COALESCE(claim_epoch,0), COALESCE(claim_owner,'')
           FROM asr_segments WHERE id=?''', (rid,)).fetchall()
    for status, has_text, claim_epoch, owner in rows:
        if status == ST_COMPLETE and has_text:
            counts[ST_COMPLETE] += 1
        elif status == ST_PROCESSING:
            counts[ST_PROCESSING] += 1
            if not claim_is_live(owner, claim_epoch):
                stale += 1
        elif status == ST_ERROR:
            counts[ST_ERROR] += 1
        else:
            counts[ST_PENDING] += 1
    total = len(rows)
    return {'id': rid, 'total': total,
            'complete': counts[ST_COMPLETE],
            'processing': counts[ST_PROCESSING],
            'stale': stale,
            'error': counts[ST_ERROR],
            'pending': counts[ST_PENDING],
            'percent': round(100.0 * counts[ST_COMPLETE] / total, 1) if total else 0.0}


def assemble(segments) -> str:
    """One transcript, in order, each window labelled with its offset."""
    parts = []
    for seg in sorted(segments, key=lambda s: s['index']):
        text = (seg.get('text') or '').strip()
        if text:
            parts.append(f'[{format_marker(seg["start_sec"])}]\n{text}')
    return '\n\n'.join(parts)


def _result_fields(res, engine, allow_empty=False):
    """(text, engine_used, lang, meta, alternative) from one ASR response.

    `allow_empty` separates the two things an empty string can mean. For a
    whole-file call it is a non-answer worth retrying. For one window of a long
    recording it is usually the truth — half an hour of a four-hour meeting
    really can be silence — and asr-mcp says so by returning a normal result
    rather than status=error. Retrying that forever is how one quiet window
    blocks assembly of the entire recording.
    """
    if res.get('status') == 'error':
        raise RuntimeError(res.get('detail', 'unknown asr error'))
    text = (res.get('text') or '').strip()
    if not text and not allow_empty:
        raise RuntimeError('empty transcript')
    meta = res.get('meta') or {}
    engine_used = res.get('engine_used') or engine
    lang = meta.get('language') or meta.get('lang') or meta.get('detected_lang') or ''
    nested = meta.get('alternative') if isinstance(meta.get('alternative'), dict) else {}
    alternative = (res.get('alternative_transcript') or
                   res.get('alternative_text') or
                   meta.get('alternative_transcript') or
                   nested.get('text') or '')
    return text, engine_used, lang, meta, alternative


def transcribe_segmented(conn, sid, rid, engine, duration_sec, publish=None,
                         on_window=None):
    """Transcribe a long recording window by window, committing as it goes.

    Does at most SEGMENTS_PER_RUN windows, stops at the first failing window
    rather than hammering a service that has just refused, and raises
    SegmentIncomplete so the recording stays pending with its finished windows
    intact. The exception says whether the reason deserves a retry strike.

    `publish` replaces the final `store` call and nothing else. A review needs
    to hear the whole recording before it can judge PLAUD's transcript against
    it, and storing the assembled hypothesis would publish the challenger as
    the selected transcript before anything had compared the two. The windows
    themselves are committed either way, so a review of a four-hour recording
    resumes from its checkpoints exactly as a plain transcription does.
    """
    ensure_segments(conn)
    plan = plan_segments(duration_sec)
    # Publish the whole plan before the first call, so anything watching knows
    # the total — and which windows are still owed — from second zero.
    materialize_plan(conn, rid, plan)
    # Only a checkpoint captured on exactly this window can be trusted: a
    # retuned ASR_SEGMENT_SECONDS would otherwise file old audio under a new
    # timestamp and silently corrupt the assembled transcript.
    done = done_segments(conn, rid, plan)
    t0 = time.time()
    processed = 0
    log(f'plan {rid} segments={len(plan)} done={len(done)}')
    for index, start, duration in plan:
        if index in done:
            continue
        if SEGMENTS_PER_RUN and processed >= SEGMENTS_PER_RUN:
            # A committed window is the fairness boundary. Yield if anything
            # else is waiting; otherwise keep the warm service busy instead of
            # paying another cron interval before the next checkpointed window.
            competitor = has_competing_pending(conn, rid)
            if competitor or not CHAIN_IDLE_SEGMENTS:
                missing = len(plan) - len(done)
                log(f'segment PAUSE {rid} after {processed} this run, '
                    f'{missing}/{len(plan)} still owed competitor={competitor}')
                raise SegmentPaused(
                    f'{missing}/{len(plan)} {SEGMENT_INCOMPLETE_MARKER} for {rid}, '
                    f'paused after {processed} segment(s) this run')
            log(f'segment CONTINUE {rid}: queue idle after {processed} this run')
        if not claim_segment(conn, rid, index, start, duration):
            missing = len(plan) - len(done)
            log(f'segment SKIP {rid} #{index} at {format_marker(start)}: '
                'claimed by another run')
            raise SegmentPaused(
                f'{missing}/{len(plan)} {SEGMENT_INCOMPLETE_MARKER} for {rid}, '
                f'segment #{index} held by another run')
        try:
            tool, args = asr_request(conn, rid, engine, start, duration)
            res = call(tool, args, sid)
            text, engine_used, lang, meta, alternative = _result_fields(
                res, engine, allow_empty=True)
        except Exception as exc:  # noqa: BLE001 — checkpoint, then hand up
            detail = redact(f'{type(exc).__name__}: {exc}')
            fail_segment(conn, rid, index, start, duration, detail)
            missing = len(plan) - len(done_segments(conn, rid))
            log(f'segment FAIL {rid} #{index} at {format_marker(start)}: {detail}')
            raise SegmentIncomplete(
                f'{missing}/{len(plan)} {SEGMENT_INCOMPLETE_MARKER} for {rid}, '
                f'stopped at {format_marker(start)}: {detail}',
                transient=is_transient(detail)) from exc
        record_segment(conn, rid, index, start, duration,
                       text, engine_used,
                       {'lang': lang, 'provider': meta}, alternative)
        done[index] = {'index': index, 'start_sec': start, 'duration_sec': duration,
                       'text': text, 'alternative': alternative,
                       'engine': engine_used,
                       'meta': {'lang': lang, 'provider': public_asr_meta(meta)}}
        processed += 1
        log(f'segment OK {rid} #{index + 1}/{len(plan)} at {format_marker(start)} '
            f'chars={len(text)}')
        # The checkpoint. Committed work is safe from here, so this is the
        # moment to ask whether we are still the worker that owes it. A caller
        # that has lost its claim raises out of this and stops, rather than
        # spending another half hour of GPU on audio somebody else is already
        # transcribing.
        if on_window is not None:
            on_window(index, len(plan))

    segments = [done[index] for index, _start, _duration in plan]
    text = assemble(segments)
    if not text.strip():
        # Every window came back silent. Nothing to publish, and storing '' would
        # leave the recording eligible forever; treat it like the whole-file path
        # does, as a verdict that spends the retry budget and then stops.
        raise RuntimeError('empty transcript')
    engine_used = next((s['engine'] for s in segments if s['engine']), engine)
    lang = next((s['meta'].get('lang') for s in segments if s['meta'].get('lang')), '')
    alternative = assemble([dict(s, text=s.get('alternative') or '')
                            for s in segments])
    unresolved_count = sum(
        int(value)
        for value in (
            (s['meta'].get('provider') or {}).get('unresolved_count', 0)
            for s in segments
        )
        if isinstance(value, (int, float)) and value > 0
    )
    meta = {
        'segmented': True,
        'segment_sec': SEGMENT_SEC,
        'segment_count': len(segments),
        'duration_sec': int(duration_sec),
        'unresolved_count': unresolved_count,
        'segments': [{'index': s['index'], 'start_sec': s['start_sec'],
                      'duration_sec': s['duration_sec'],
                      'marker': format_marker(s['start_sec']),
                      'engine': s['engine'],
                      'meta': s['meta'].get('provider') or {}} for s in segments],
    }
    # asr_segments rows are deliberately left in place: they are the record of
    # which window produced which text, and make a re-assembly free.
    (publish or store)(conn, rid, text, engine_used, lang, meta, alternative)
    log(f'OK {rid} segmented engine={engine_used} lang={lang or "?"} '
        f'segments={len(segments)} chars={len(text)} took={int(time.time()-t0)}s')


def transcribe(conn, sid, rid, engine, duration_sec=0, publish=None,
               on_window=None):
    if needs_segmentation(duration_sec):
        return transcribe_segmented(conn, sid, rid, engine, int(duration_sec),
                                    publish=publish, on_window=on_window)
    t0 = time.time()
    tool, args = asr_request(conn, rid, engine)
    res = call(tool, args, sid)
    text, engine_used, lang, meta, alternative = _result_fields(res, engine)
    (publish or store)(conn, rid, text, engine_used, lang, meta, alternative)
    log(f'OK {rid} engine={engine_used} lang={lang or "?"} chars={len(text)} '
        f'took={int(time.time()-t0)}s')


# ---------------- reviewing PLAUD's own transcripts ----------------

# Review lifecycle, and what the viewer reports for it: a row is QUEUED from
# enrollment until a run claims it, PROCESSING while the local hypothesis is
# being produced, and then REVIEWED — whichever candidate won — or ERROR.
# REVIEWED is durable and terminal: a recording where PLAUD kept the selection
# has been reviewed, and re-reviewing it every pass forever is exactly the
# behaviour this table exists to prevent.
RV_QUEUED = 'queued'
RV_PROCESSING = 'processing'
RV_REVIEWED = 'reviewed'
RV_ERROR = 'error'

# What one review decided. `text` is the transcript that must now be displayed
# and indexed; `candidates` keeps every hypothesis considered, scored, so a
# verdict can be second-guessed later without re-running ASR.
Verdict = namedtuple(
    'Verdict',
    'source reason candidates text engine lang meta alternative')
Verdict.__new__.__defaults__ = ('plaud', '', (), '', '', '', None, '')


def ensure_reviews_table(conn):
    """The review ledger itself, with no enrollment policy attached.

    Split out from ensure_reviews because the two have different callers and
    very different costs. The pipeline hands us one recording that already has
    a committed validation job and needs somewhere to record its verdict; it
    must not pay for — or trigger — a migration scan of the whole archive to
    get it.
    """
    conn.execute('''CREATE TABLE IF NOT EXISTS plaud_reviews(
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        -- NULL means eligible now, and is reserved for NEW work. Backlog rows
        -- always carry an explicit instant (see enroll_reviews): a NULL here
        -- for a historical row would make the whole archive eligible at once.
        eligible_epoch INTEGER,
        selected_source TEXT, reason TEXT, candidates_json TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        claim_epoch INTEGER, claim_owner TEXT, updated_at TEXT)''')
    conn.commit()


def ensure_reviews(conn, now=None):
    """Create the review ledger and enroll whatever is not in it yet.

    Runs from ensure_attempts, which every entry point already calls, so an
    existing archive migrates on the next ordinary pass with no manual SQL.
    """
    ensure_reviews_table(conn)
    enroll_reviews(conn, now=now)


def _epoch(stamp):
    """An archive timestamp as unix seconds, or None when unreadable."""
    try:
        return time.mktime(time.strptime(str(stamp or '')[:19],
                                         '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return None


def _recording_columns(conn):
    return {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}


def review_eligible_duration_ms(duration_ms) -> bool:
    """Whether a recording is the right size for whole-file validation.

    Validation deliberately never takes the segmented path. A four-hour
    recording would occupy the single-flight queue for nine windows to
    second-guess text the reader already has, which is precisely the starvation
    the priority rules exist to prevent. Unknown durations are allowed: an
    archive that never recorded one is not evidence of a long recording.
    """
    duration_ms = int(duration_ms or 0)
    if not duration_ms:
        return True
    return MIN_DURATION_MS <= duration_ms <= SEGMENT_THRESHOLD_SEC * 1000


def enroll_reviews(conn, now=None) -> int:
    """Add review rows for non-empty PLAUD transcripts that have none yet.

    Idempotent and bounded. Returns how many rows were enrolled. Nothing here
    touches `recordings`: an enrolled recording keeps displaying exactly the
    text it displayed before, which is the whole contract of a background
    review.
    """
    columns = _recording_columns(conn)
    if 'plaud_transcript' not in columns:
        return 0  # not an archive this migration has anything to say about
    now = time.time() if now is None else now
    archived = 'r.archived_at' if 'archived_at' in columns else "''"
    duration = 'COALESCE(r.duration_ms,0)' if 'duration_ms' in columns else '0'
    asr = ("TRIM(COALESCE(r.asr_transcript,''))" if 'asr_transcript' in columns
           else "''")
    order = ('COALESCE(r.start_at, r.created_at)'
             if {'start_at', 'created_at'} <= columns else 'r.id')
    rows = conn.execute(f'''
        SELECT r.id, COALESCE({archived},''), {duration}
          FROM recordings r LEFT JOIN plaud_reviews v ON v.id=r.id
         WHERE v.id IS NULL
           AND TRIM(COALESCE(r.plaud_transcript,''))<>''
           AND {asr}=''
         ORDER BY {order} DESC, r.id DESC''').fetchall()
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    fresh, backlog = [], []
    for rid, archived_at, duration_ms in rows:
        if not review_eligible_duration_ms(duration_ms):
            continue
        age = _epoch(archived_at)
        if age is not None and now - age <= REVIEW_NEW_WINDOW_SEC:
            fresh.append(rid)
        else:
            # No readable timestamp is treated as history, not as new work:
            # guessing the other way is how a migration floods the queue.
            backlog.append(rid)
    enrolled = [(rid, None) for rid in fresh]
    enrolled += [(rid, int(now) + (rank // REVIEW_BACKLOG_PER_DAY) * 86400)
                 for rank, rid in enumerate(backlog)]
    if not enrolled:
        return 0
    conn.executemany(
        '''INSERT INTO plaud_reviews(id,state,eligible_epoch,updated_at)
           VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING''',
        [(rid, RV_QUEUED, eligible, stamp) for rid, eligible in enrolled])
    conn.commit()
    return len(enrolled)


def review_pending(conn, now=None, include_exhausted=False):
    """[(id, name, duration_ms)] ready for validation, best first.

    New work (eligible_epoch NULL) before backlog, backlog by the day it became
    eligible, then newest recording first. A row whose PLAUD text has since
    been emptied, or which ran out of retry budget, is not offered; one left
    PROCESSING by a run that died is, as soon as its claim expires.
    """
    columns = _recording_columns(conn)
    if 'plaud_transcript' not in columns:
        return []
    now = time.time() if now is None else now
    name = 'r.name' if 'name' in columns else "''"
    duration = 'COALESCE(r.duration_ms,0)' if 'duration_ms' in columns else '0'
    order = ('COALESCE(r.start_at, r.created_at)'
             if {'start_at', 'created_at'} <= columns else 'r.id')
    try:
        rows = conn.execute(f'''
            SELECT r.id, COALESCE({name},''), {duration}, v.state,
                   COALESCE(v.attempts,0), v.eligible_epoch,
                   COALESCE(v.claim_epoch,0), COALESCE(v.claim_owner,'')
              FROM plaud_reviews v JOIN recordings r ON r.id=v.id
             WHERE v.state IN (?,?,?)
               AND TRIM(COALESCE(r.plaud_transcript,''))<>''
             ORDER BY (v.eligible_epoch IS NOT NULL) ASC,
                      v.eligible_epoch ASC, {order} DESC, r.id DESC''',
            (RV_QUEUED, RV_ERROR, RV_PROCESSING)).fetchall()
    except sqlite3.OperationalError:
        return []  # archive predates the review ledger; nothing to offer yet
    out = []
    for rid, name, duration_ms, state, attempts, eligible, claimed, owner in rows:
        if not include_exhausted and attempts >= MAX_ATTEMPTS:
            continue
        if eligible is not None and eligible > now:
            continue
        # claim_is_live, not claim_expired: a review is one short call, so a
        # run's OWN claim means it is working on this row right now and must
        # not pick it twice in the same pass. A claim left by a dead run is
        # still recovered at once — its pid is gone from this host.
        if state == RV_PROCESSING and claim_is_live(owner, claimed):
            continue
        out.append((rid, name, duration_ms or 0))
    return out


def next_targets(conn, now=None):
    """('transcribe'|'review', targets) — what this run should do.

    Textless recordings always win. They show the reader nothing at all, while
    a review candidate is already displaying its PLAUD text, so letting a
    backlog of validations delay them would trade a visible gap for an
    invisible improvement.
    """
    textless = pending(conn)
    if textless:
        return 'transcribe', textless
    return 'review', review_pending(conn, now=now)[:REVIEWS_PER_RUN]


def claim_review(conn, rid) -> bool:
    """Take one review for this run, atomically. True when it is ours.

    Same compare-and-swap as claim_segment, and the same lease: a run killed
    mid-review leaves PROCESSING behind, and the row must come back rather than
    sit unreviewed forever.
    """
    row = conn.execute(
        '''SELECT state, COALESCE(claim_epoch,0), COALESCE(claim_owner,'')
             FROM plaud_reviews WHERE id=?''', (rid,)).fetchone()
    if row is None:
        return False
    state, claimed, owner = row
    if state == RV_REVIEWED:
        return False
    if state == RV_PROCESSING and claim_is_live(owner, claimed):
        return False
    cur = conn.execute(
        '''UPDATE plaud_reviews
              SET state=?, claim_epoch=?, claim_owner=?, updated_at=?
            WHERE id=? AND state=? AND COALESCE(claim_epoch,0)=?
              AND COALESCE(claim_owner,'')=?''',
        (RV_PROCESSING, int(time.time()), claim_owner(),
         time.strftime('%Y-%m-%dT%H:%M:%S%z'), rid, state, claimed, owner))
    conn.commit()
    return cur.rowcount == 1


def fail_review(conn, rid, err):
    """Record why one review did not finish, on the review's own ledger.

    Transient infrastructure failures cost no budget, exactly as they do for
    transcription: an asr-mcp restart must not retire a validation candidate.
    """
    conn.rollback()
    detail = redact(err if isinstance(err, str)
                    else f'{type(err).__name__}: {err}')
    increment = 1 if counts_against_budget(err) else 0
    conn.execute(
        '''INSERT INTO plaud_reviews(id,state,attempts,last_error,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             state=excluded.state,
             attempts=plaud_reviews.attempts+?,
             last_error=excluded.last_error, updated_at=excluded.updated_at,
             claim_epoch=NULL, claim_owner=NULL''',
        (rid, RV_ERROR, increment, detail[:500],
         time.strftime('%Y-%m-%dT%H:%M:%S%z'), increment))
    conn.commit()


def release_review(conn, rid):
    """Release this process's review claim when its pipeline attempt fails."""
    conn.execute('''UPDATE plaud_reviews SET state=?, claim_epoch=NULL,
                    claim_owner=NULL, updated_at=? WHERE id=? AND state=?
                    AND claim_owner=?''',
                 (RV_QUEUED, time.strftime('%Y-%m-%dT%H:%M:%S%z'), rid,
                  RV_PROCESSING, claim_owner()))
    conn.commit()


def record_review(conn, rid, verdict, job=None):
    """Publish one verdict: selection, candidates, review state and the search
    index, all or nothing.

    The PLAUD transcript is a source artifact and is never written here. When
    PLAUD wins, `recordings` is left untouched on purpose: asr_transcript is
    what the viewer prefers, so writing the rejected local hypothesis into it
    would display the candidate the review just turned down.

    Partial application is the outcome that cannot be repaired later — a
    verdict recorded without its index entry is a recording that is silently
    unsearchable, and one indexed without its verdict is re-reviewed forever.
    """
    row = conn.execute(
        "SELECT COALESCE(name,'') FROM recordings WHERE id=?", (rid,)).fetchone()
    name = (row[0] if row else '') or ''
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    try:
        assert_publication_claim(conn, job)
        if verdict.source == 'local':
            cur = conn.execute(
                '''UPDATE recordings SET asr_transcript=?, asr_engine=?,
                     lang=COALESCE(NULLIF(?,''), lang), asr_meta_json=?,
                     asr_alternative_transcript=? WHERE id=?''',
                (verdict.text, verdict.engine, verdict.lang or '',
                 json.dumps(verdict.meta or {}, ensure_ascii=False),
                 verdict.alternative or None, rid))
            if cur.rowcount != 1:
                raise RuntimeError('recording disappeared before review publication')
        conn.execute('DELETE FROM recordings_fts WHERE id=?', (rid,))
        conn.execute(
            'INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)',
            (rid, name, verdict.text))
        conn.execute(
            '''INSERT INTO plaud_reviews(id,state,eligible_epoch,selected_source,
                 reason,candidates_json,attempts,last_error,claim_epoch,
                 claim_owner,updated_at)
               VALUES(?,?,NULL,?,?,?,0,NULL,NULL,NULL,?)
               ON CONFLICT(id) DO UPDATE SET
                 state=excluded.state, eligible_epoch=NULL,
                 selected_source=excluded.selected_source,
                 reason=excluded.reason,
                 candidates_json=excluded.candidates_json,
                 attempts=0, last_error=NULL, claim_epoch=NULL,
                 claim_owner=NULL, updated_at=excluded.updated_at''',
            (rid, RV_REVIEWED, verdict.source, verdict.reason,
             json.dumps(list(verdict.candidates or ()), ensure_ascii=False),
             stamp))
        conn.execute('DELETE FROM asr_attempts WHERE id=?', (rid,))
        conn.commit()
    except Exception:  # noqa: BLE001 — undo everything, then let it surface
        conn.rollback()
        raise


# ---- deterministic quality comparison --------------------------------------
# Not a grade for a transcript: a test for the two ways a non-empty transcript
# is nonetheless not the recording. Everything here is pure and deterministic,
# because a verdict that cannot be reproduced from the stored candidates is not
# reviewable by a human later.
_WORD_RE = re.compile(r'\w+', re.UNICODE)
# Characters ordinary transcribed speech is made of. What is left over is
# mojibake and control junk — the shape a broken decode arrives in.
_JUNK_RE = re.compile(r'[^\w\s.,!?;:%№()\[\]«»"\'\-–—…/+&@]', re.UNICODE)
# Share of the transcript one repeated word may occupy before it reads as a
# decoder loop rather than speech. Russian filler tops out well under this.
REVIEW_LOOP_SHARE = 0.25
# Words below which a transcript cannot claim to cover a whole recording.
REVIEW_SUBSTANTIAL_WORDS = int(os.environ.get('ASR_REVIEW_SUBSTANTIAL_WORDS', '40'))
# How much better the local hypothesis must score before it replaces text the
# reader is already reading. Ties, and anything inside the margin, keep PLAUD.
REVIEW_MARGIN = float(os.environ.get('ASR_REVIEW_MARGIN', '0.15'))
# A PLAUD transcript shorter than this fraction of an equally clean local one
# is a fragment of the recording, not a rendering of it.
REVIEW_TRUNCATION_RATIO = float(os.environ.get('ASR_REVIEW_TRUNCATION_RATIO', '0.4'))


def quality_score(text) -> float:
    """0.0..1.0 — how much this text reads like a transcript of speech.

    Three independent signals, none of which needs a reference transcript:
    legibility (mojibake and control junk), variety (a decoder loop repeats one
    token), and substance (a handful of words cannot be a whole recording).
    """
    text = (text or '').strip()
    if not text:
        return 0.0
    words = _WORD_RE.findall(text.lower())
    if not words:
        return 0.0
    legibility = max(0.0, 1.0 - len(_JUNK_RE.findall(text)) / len(text))
    dominant = max(words.count(word) for word in set(words)) / len(words)
    variety = max(0.0, 1.0 - min(1.0, dominant / REVIEW_LOOP_SHARE))
    substance = min(1.0, len(words) / max(1, REVIEW_SUBSTANTIAL_WORDS))
    return round(0.40 * legibility + 0.25 * variety + 0.35 * substance, 4)


def score_candidate(source, engine, text, selected=False) -> dict:
    """One hypothesis, with everything needed to re-judge it later."""
    text = (text or '').strip()
    return {'source': source, 'engine': engine or '', 'chars': len(text),
            'words': len(_WORD_RE.findall(text)), 'score': quality_score(text),
            'selected': bool(selected), 'text': text}


def review_verdict(plaud_text, local_text, alternative='', engine='', lang='',
                   meta=None, alternative_engine='') -> Verdict:
    """Compare the archived PLAUD transcript against the local hypotheses.

    Conservative on purpose. PLAUD wins ties, wins inside the margin, and wins
    whenever the local side has nothing to offer: the reader is already reading
    the PLAUD text, so replacing it is a change that has to be earned. Local
    wins only on a clear quality gap, or when PLAUD is a fragment of an equally
    clean local transcript.

    auto mode returns two local hypotheses. The better one is the challenger;
    the other is kept as the alternative, whichever way the verdict goes.
    """
    candidates = [score_candidate('plaud', 'plaud', plaud_text)]
    local = score_candidate('local', engine, local_text)
    runner_up = score_candidate('local-alternative', alternative_engine,
                                alternative)
    unresolved = bool((meta or {}).get('unresolved_count'))
    if unresolved:
        # A counterfactual assembled across an unresolved window is evidence,
        # not a publication candidate. Keep it in the private review ledger
        # but never let fluent-looking hallucination outrank the selected path.
        runner_up['eligible'] = False
    else:
        runner_up['eligible'] = True
    local['eligible'] = True
    candidates += [local, runner_up]
    plaud = candidates[0]
    eligible = [candidate for candidate in (local, runner_up)
                if candidate.get('eligible')]
    challenger = max(eligible, key=lambda candidate:
                     (candidate['score'], candidate['chars']))
    other = runner_up if challenger is local else local

    if not challenger['chars']:
        source, reason = 'plaud', 'local-empty'
    elif not plaud['chars']:
        source, reason = 'local', 'plaud-empty'
    elif challenger['score'] >= plaud['score'] + REVIEW_MARGIN:
        source, reason = 'local', 'local-clearly-better'
    elif (plaud['chars'] < REVIEW_TRUNCATION_RATIO * challenger['chars']
          and challenger['score'] >= plaud['score']):
        source, reason = 'local', 'plaud-truncated'
    else:
        source, reason = 'plaud', 'plaud-adequate'

    winner = plaud if source == 'plaud' else challenger
    winner['selected'] = True
    cleaned_provider = public_asr_meta(dict(meta or {}))
    provider = cleaned_provider if isinstance(cleaned_provider, dict) else {}
    provider['review'] = {
        'selected_source': source, 'reason': reason,
        # Metadata only: the bodies live in the review ledger, and this object
        # is served to the viewer.
        'candidates': [{k: v for k, v in c.items() if k != 'text'}
                       for c in candidates],
    }
    return Verdict(
        source=source, reason=reason, candidates=candidates,
        text=winner['text'],
        engine=challenger['engine'] if source == 'local' else '',
        lang=lang if source == 'local' else '',
        meta=provider if source == 'local' else None,
        alternative=other['text'] if source == 'local' else '')


def ensure_review_row(conn, rid, now=None) -> bool:
    """Make sure `rid` has a review ledger row, eligible now.

    Enrollment (enroll_reviews) is a bounded MIGRATION of history. A recording
    the pipeline has just handed us is not history: its PLAUD text arrived
    minutes ago, the validation job is already committed, and making it wait
    for a backlog drip would be exactly the scheduled gap the pipeline exists
    to remove.
    """
    ensure_reviews_table(conn)
    cur = conn.execute(
        '''INSERT INTO plaud_reviews(id,state,eligible_epoch,updated_at)
           VALUES(?,?,NULL,?) ON CONFLICT(id) DO NOTHING''',
        (rid, RV_QUEUED, time.strftime('%Y-%m-%dT%H:%M:%S%z')))
    conn.commit()
    return cur.rowcount == 1


def _capture_local(conn, sid, rid, engine, duration_sec, on_window=None):
    """Run local ASR for a review WITHOUT publishing it as the transcript.

    A review has to hear the whole recording before it can say the PLAUD text
    is a fragment of it, and past half an hour that means the windowed path —
    the same committed, resumable windows ordinary transcription uses. What it
    must not do is `store` the result: that would publish the challenger as the
    selected transcript before anything had compared the two.
    """
    captured = {}

    def capture(_conn, _rid, text, engine_used, lang, meta=None, alternative=None):
        captured.update(text=text, engine=engine_used, lang=lang,
                        meta=meta or {}, alternative=alternative or '')

    transcribe(conn, sid, rid, engine, duration_sec, publish=capture,
               on_window=on_window)
    return captured


def review_one(conn, sid, rid, engine='auto', duration_sec=0, on_window=None, job=None):
    """Validate one non-empty PLAUD transcript against local ASR.

    Claims the row (so a reader sees `processing`), produces the local
    hypotheses through whichever tool this deployment is allowed to use, and
    publishes one verdict. Returns the Verdict, or None when another run holds
    the row.

    A long recording is validated from its windowed transcription, so a
    four-hour meeting is compared against the whole of itself rather than
    against a single call that would time out.
    """
    if not claim_review(conn, rid):
        log(f'review SKIP {rid}: held by another run')
        return None
    row = conn.execute(
        "SELECT COALESCE(plaud_transcript,'') FROM recordings WHERE id=?",
        (rid,)).fetchone()
    plaud_text = (row[0] if row else '') or ''
    t0 = time.time()
    if needs_segmentation(duration_sec):
        captured = _capture_local(conn, sid, rid, engine, duration_sec,
                                  on_window=on_window)
        text = captured['text']
        engine_used, lang = captured['engine'], captured['lang']
        meta, alternative = captured['meta'], captured['alternative']
    else:
        tool, args = asr_request(conn, rid, engine)
        res = call(tool, args, sid)
        text, engine_used, lang, meta, alternative = _result_fields(res, engine)
    nested = meta.get('alternative') if isinstance(meta.get('alternative'), dict) else {}
    verdict = review_verdict(
        plaud_text, text, alternative=alternative, engine=engine_used,
        lang=lang, meta=meta, alternative_engine=nested.get('engine', ''))
    record_review(conn, rid, verdict, job=job)
    log(f'review OK {rid} selected={verdict.source} reason={verdict.reason} '
        f'chars={len(verdict.text)} took={int(time.time() - t0)}s')
    return verdict


def review_next(conn, sid, engine='auto', now=None):
    """Review this run's bounded share of the validation queue.

    One failure is that recording's alone: the retry ledger is the review's own
    (fail_review), the PLAUD text stays exactly where it is, and the pass moves
    on rather than taking the whole queue down with it.
    """
    reviewed = []
    for rid, _name, _duration in review_pending(conn, now=now)[:REVIEWS_PER_RUN]:
        try:
            if review_one(conn, sid, rid, engine) is not None:
                reviewed.append(rid)
        except Exception as exc:  # noqa: BLE001
            fail_review(conn, rid, exc)
            log(f'review FAIL {rid}: {redact(f"{type(exc).__name__}: {exc}")}')
    return reviewed


def explicit_targets(conn, ids):
    """Forced ids, with the duration that decides segmented vs single call.

    Assuming 0 here would send a four-hour recording down the single-call path
    purely because it was named on the command line. An id the archive has
    never heard of is rejected outright rather than defaulted to 0: it has no
    duration to plan from, and transcribing it would write a transcript and an
    FTS row for a recording that does not exist.
    """
    targets = []
    for rid in ids:
        row = conn.execute(
            "SELECT COALESCE(name,''), COALESCE(duration_ms,0) "
            'FROM recordings WHERE id=?', (rid,)).fetchone()
        if not row:
            log(f'SKIP {rid}: no such recording in this archive')
            continue
        targets.append((rid, row[0], row[1]))
    return targets


def main():
    argv = sys.argv[1:]
    engine = 'auto'
    if '--engine' in argv:
        i = argv.index('--engine')
        engine = argv[i + 1]
        del argv[i:i + 2]
    do_all = '--all' in argv
    argv = [a for a in argv if not a.startswith('--')]

    if not TOKEN:
        # Names only, never values: this line goes to cron mail and container
        # logs. A tenant that reaches here is missing its own credential and
        # stops, rather than falling back to whatever the host environment has.
        tenant_id = os.environ.get('TENANT_ID', '').strip()
        variable = scoped_token_var(tenant_id) if tenant_id else 'ASR_MCP_TOKEN'
        log(f'FATAL no ASR caller token ({variable} or {TOKEN_FILE})')
        sys.exit(1)

    # single-flight: ASR runs can take hours, cron fires every 30 min
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    try:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        log('REFUSED archive connector lock is held')
        return 75  # EX_TEMPFAIL: never report a bypass attempt as success

    conn = sqlite3.connect(DB, timeout=60)
    ensure_attempts(conn)
    kind = 'transcribe'
    if argv:
        # Forced ids are unchanged: they transcribe, whatever the archive
        # already holds for them.
        targets = explicit_targets(conn, argv)
    else:
        kind, targets = next_targets(conn)
        if not do_all and kind == 'transcribe':
            targets = targets[:1]
    if not targets:
        conn.close()
        os.close(lock_fd)
        return 0

    sid = mcp_connect()
    if kind == 'review':
        # Nothing is textless; spend this pass validating PLAUD's own text.
        review_next(conn, sid, engine)
        conn.close()
        os.close(lock_fd)
        return 0
    for rid, _name, dur in targets:
        try:
            duration_sec = duration_sec_from_ms(dur)
            log(f'start {rid} duration={duration_sec or "?"}s engine={engine}'
                + (f' segments={len(plan_segments(duration_sec))}'
                   if needs_segmentation(duration_sec) else ''))
            transcribe(conn, sid, rid, engine, duration_sec)
        except SegmentPaused as exc:
            # Caught before SegmentIncomplete, which it subclasses. Nothing
            # failed: this run spent its quantum, or another run holds the
            # window. The recording is left exactly as it was — pending, with
            # its committed windows — and never touches the retry ledger, so a
            # nine-window plan cannot accumulate strikes just by taking nine
            # runs to finish.
            log(f'PAUSED {rid}: {exc}')
        except SegmentIncomplete as exc:
            bump(conn, rid, exc)
            log(f'INCOMPLETE {rid}: {exc}')
        except Exception as exc:  # noqa: BLE001
            bump(conn, rid, exc)
            log(f'FAIL {rid}: {type(exc).__name__}: {exc}')
    conn.close()
    os.close(lock_fd)
    return 0


if __name__ == '__main__':
    sys.exit(main())
