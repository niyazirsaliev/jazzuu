#!/usr/bin/env python3
"""archive_recording.py <file_id> [--force]
Own a PLAUD recording (audio + PLAUD transcript + summary) into the local archive.
Uses the PLAUD MCP streamable-HTTP transport.
Also importable: mcp_connect(), call(), archive_one(conn, sid, fid, force).
"""
import ipaddress, os, socket, sys, json, tempfile, time, sqlite3, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tenant as _tenant
import pipeline
import diarization
import recording_codes as _codes
from mcp_sse import sse_events as _shared_sse_events, sse_json_objects as _shared_sse_payloads

HERE = os.path.dirname(os.path.abspath(__file__))
TENANT = _tenant.load()
URL = TENANT.mcp_url
DB = TENANT.db_path
AUDIO = TENANT.audio_dir
try:
    CODE_PREFIX = _codes.prefix_for(TENANT.tenant_id)
except _codes.UnknownTenantError:
    CODE_PREFIX = None
# Every mutation of this archive shares connector.py's tenant-private gate.
# The standalone importer is retained for an intentional one-off only; it must
# never race the durable connector pipeline on the same SQLite/FTS files.
LOCK = os.path.join(os.path.dirname(os.path.abspath(DB)), '.connector.lock')

import plaud_mcp_auth as _auth

# Auth is resolved per request, not once at import. A tenant reads its
# caller token from a mounted file, so replacing that file rotates the
# credential under a running container.
# Back-compat: legacy callers and tests read module-level TOK.
TOK = TENANT.static_token or ''


def auth_token():
    """Current bearer token for whoever this process is speaking as."""
    if TENANT.uses_legacy_static_token:
        return TOK
    assert TENANT.caller_token_file, 'tenant mode requires PLAUD_MCP_TOKEN_FILE'
    return _auth.read_caller_token(TENANT.caller_token_file, TENANT.tenant_id)


# Ingest is patient on purpose: a 4h recording's metadata and transcript pages
# come back slowly, and a pass that gives up early archives nothing. Callers
# that answer to a deadline of their own — the healthcheck probe — pass their
# own, shorter timeout instead of changing this one for everybody.
REQUEST_TIMEOUT_S = 120
# The initialized notification is a fire-and-forget POST; it never waits on
# PLAUD itself, so it has always had a much shorter ceiling.
NOTIFY_TIMEOUT_S = 15

os.environ['TZ'] = os.environ.get('ARCHIVE_TIMEZONE', 'UTC')
# Below this length a recording plausibly has no transcript at all. Rows shorter than
# this are never treated as "damaged", so a blank one is not refetched forever.
MIN_TEXT_DURATION_MS = int(os.environ.get('MIN_TEXT_DURATION_MS', '15000'))
try: time.tzset()
except Exception: pass

def _sse_events(raw):
    """Yield each event's data payload from an SSE stream, in order.

    An event ends at a blank line, and its `data` field may be split across
    several `data:` lines that the spec says to join with newlines. The old
    parser did neither: it kept the last data LINE of the whole body, so a
    pretty-printed payload arrived as an unparseable fragment and every event
    before the final one — errors included — was discarded unread.
    """
    yield from _shared_sse_events(raw)

def _sse_payloads(raw):
    """The parseable JSON objects on a stream, skipping everything else.

    Pings, keep-alives and truncated frames share the wire with the response
    and must never take an ingest pass down; a fragment we cannot read is not
    evidence of anything, so it is dropped rather than guessed at.
    """
    yield from _shared_sse_payloads(raw)

def _response_from_events(raw, want_id):
    """The JSON-RPC response to OUR request, out of a whole stream.

    Two rules, both learned the hard way. Errors are classified from every
    event, not just the one that happens to come last: a service announcing
    pending_seed and then carrying on used to look like a clean empty answer.
    And the response is chosen by matching the id we sent, because a stream
    interleaves notifications and other traffic with it.
    """
    events = list(_sse_payloads(raw))
    generic = None
    for event in events:
        try:
            _auth.raise_for_jsonrpc_error(event, TENANT.tenant_id, URL)
        except _auth.PlaudMcpServiceError as exc:
            # A generic failure on an event that is not our response is only
            # fatal if nothing else answers us. A caller-auth or seed failure
            # is not caught here: those describe the session, not one message.
            generic = generic or exc

    for event in events:
        # Compared as text: JSON-RPC ids match by VALUE, and a server that
        # echoes our 9 as "9" is answering this request. Now that a stream
        # with no match is a hard failure, a type-strict comparison would turn
        # every reply from such a server into a failed call.
        if str(event.get('id')) == str(want_id) and ('result' in event or 'error' in event):
            _auth.raise_for_jsonrpc_error(event, TENANT.tenant_id, URL)
            return event

    if generic is not None:
        raise generic
    raise _auth.PlaudMcpServiceError(
        f'tenant {TENANT.tenant_id}: the PLAUD MCP service at {URL} sent no '
        f'JSON-RPC response for request id {want_id}. A stream that never '
        f'answers is a failed call, not an empty result.')

def _post(body, sid=None, timeout=None):
    h = {'Authorization': f'Bearer {auth_token()}', 'Content-Type': 'application/json',
         'Accept': 'application/json, text/event-stream'}
    if sid: h['Mcp-Session-Id'] = sid
    r = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=h, method='POST')
    try:
        with urllib.request.urlopen(r, timeout=timeout or REQUEST_TIMEOUT_S) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            s = resp.headers.get('Mcp-Session-Id', sid); ct = resp.headers.get('Content-Type', '')
    except urllib.error.HTTPError as exc:
        # 401/403 becomes a named caller-auth failure; anything else is
        # re-raised untouched and stays a transient.
        _auth.raise_for_http_error(exc, TENANT.tenant_id, URL)
        # Defensive: the classifier raises on every path today. If it ever
        # returned, execution would fall through with no response read at all
        # and blow up on an unbound local far from the real cause.
        raise
    if 'text/event-stream' in ct:
        # _response_from_events classifies every event it reads.
        o = _response_from_events(raw, body.get('id'))
    else:
        try:
            o = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise _auth.PlaudMcpServiceError(
                f'tenant {TENANT.tenant_id}: the PLAUD MCP service at {URL} '
                f'answered with a body that is not JSON') from None
        if not isinstance(o, dict):
            raise _auth.PlaudMcpServiceError(
                f'tenant {TENANT.tenant_id}: the PLAUD MCP service at {URL} '
                f'answered with {type(o).__name__}, not a JSON-RPC object')
        # A service that authenticated us but has no PLAUD account credentials
        # of its own answers 200 with a JSON-RPC error. Left unclassified it
        # reads as an empty listing, and the tenant looks like an account with
        # no files.
        _auth.raise_for_jsonrpc_error(o, TENANT.tenant_id, URL)
    return o, s

def mcp_connect(timeout=None):
    hello, sid = _post({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'archiver','version':'1'}}}, timeout=timeout)
    # notifications/initialized asserts "the handshake completed". Send it only
    # when initialize actually returned a result: an error reply already raised
    # above, and a reply with no result at all is not a session either. The
    # notification would tell the service a session exists that does not.
    if not isinstance(hello, dict) or not isinstance(hello.get('result'), dict):
        raise _auth.PlaudMcpServiceError(
            f'tenant {TENANT.tenant_id}: the PLAUD MCP service at {URL} '
            f'answered initialize with no result, so there is no session to '
            f'confirm; nothing was archived this pass.')
    # A stateless MCP server answers initialize with no Mcp-Session-Id. That is
    # legal, and the header must then be omitted entirely rather than sent as
    # None, which urllib rejects outright.
    h = {'Authorization':f'Bearer {auth_token()}','Content-Type':'application/json','Accept':'application/json, text/event-stream'}
    if sid:
        h['Mcp-Session-Id'] = sid
    # A caller working to a deadline (the healthcheck probe) budgets for BOTH
    # posts of this handshake, so its timeout has to bound this one too; it can
    # only ever shorten the standing ceiling, never raise it.
    notify_timeout = min(NOTIFY_TIMEOUT_S, timeout) if timeout else NOTIFY_TIMEOUT_S
    try:
        urllib.request.urlopen(urllib.request.Request(URL, data=json.dumps({'jsonrpc':'2.0','method':'notifications/initialized','params':{}}).encode(), headers=h, method='POST'), timeout=notify_timeout).read()
    except urllib.error.HTTPError as exc:
        # This notification is posted outside _post, so it needs the same
        # classification: a 401 here is our caller token, not a blip.
        _auth.raise_for_http_error(exc, TENANT.tenant_id, URL)
    return sid

def call(name, args, sid, timeout=None):
    """One tool call. Raises on every failure; returns text on every success.

    A reply with a result but no content is a real, empty answer — an account
    with no recordings, a recording with no note — and stays exactly as it
    was: the envelope comes back and the extractors read nothing out of it.
    A reply with NO result is not that. It used to take the same path and
    parse to an empty listing, which is indistinguishable from a healthy
    account that simply has nothing in it.
    """
    o, _ = _post({'jsonrpc':'2.0','id':9,'method':'tools/call','params':{'name':name,'arguments':args}}, sid, timeout=timeout)
    result = o.get('result')
    if not isinstance(result, dict):
        raise _auth.PlaudMcpServiceError(
            f'tenant {TENANT.tenant_id}: the PLAUD MCP service at {URL} '
            f'answered the {name} call with no result. That is a failed call, '
            f'not an empty one.')
    c = result.get('content') or []
    return c[0].get('text', '') if c else json.dumps(o)

def _as_doc(raw):
    """Parse an MCP payload, preserving whichever JSON shape it actually is.

    The two PLAUD endpoints in play disagree about their envelope:
      * the self-hosted proxy wraps everything in an object
        (`{source_list: [...]}` / `{note_list: [...]}`);
      * the official server (plaud 0.3.7) returns a purpose-built object for
        get_transcript (`{segments: [...], next_cursor: ...}`) and a BARE LIST
        for get_note.
    Returning the parsed value as-is lets each extractor decide, instead of
    silently flattening a list to {} and losing the content.
    """
    try:
        return json.loads(raw)
    except Exception:
        return None

def _segment_text(seg):
    """Render one transcript segment as '[speaker] content'."""
    if not isinstance(seg, dict):
        return ''
    content = (seg.get('content') or '').strip()
    if not content:
        return ''
    speaker = seg.get('speaker') or seg.get('original_speaker') or ''
    return (f'[{speaker}] ' if speaker else '') + content

def extract_plaud_transcript(gt_str):
    """Text of a PLAUD transcript, from either server's response shape."""
    d = _as_doc(gt_str)
    parts = []

    # Official server: {"block": "transaction", "segments": [{content, speaker}]}
    if isinstance(d, dict) and isinstance(d.get('segments'), list):
        for seg in d['segments']:
            text = _segment_text(seg)
            if text:
                parts.append(text)
        return '\n'.join(parts).strip()

    # Self-hosted proxy: {"source_list": [{data_type: transaction, data_content}]}
    if isinstance(d, dict):
        for src in (d.get('source_list') or []):
            if not isinstance(src, dict): continue
            if src.get('data_type') == 'transaction':
                try: arr = json.loads(src.get('data_content') or '[]')
                except Exception: arr = []
                for seg in arr:
                    text = _segment_text(seg)
                    if text:
                        parts.append(text)
    return '\n'.join(parts).strip()

def fetch_plaud_transcript(fid, sid):
    """Full transcript for a file, following the official cursor pagination.

    The official get_transcript returns at most `limit` segments per call plus
    a `next_cursor`; a long recording can run to hundreds. Reading only the
    first page would silently truncate the transcript (and the search index)
    to its opening minutes, so follow the cursor to exhaustion.
    """
    chunks, cursor, seen = [], None, 0
    for _ in range(200):  # hard bound: never spin forever on a broken cursor
        args = {'file_id': fid}
        if cursor:
            args['cursor'] = cursor
        raw = call('get_transcript', args, sid)
        text = extract_plaud_transcript(raw)
        if text:
            chunks.append(text)
        d = _as_doc(raw)
        if not isinstance(d, dict):
            break
        cursor = d.get('next_cursor')
        seen += len(d.get('segments') or [])
        total = d.get('total')
        if not cursor or (isinstance(total, int) and seen >= total):
            break
    return '\n'.join(chunks).strip()

def extract_summary(gn_str):
    """Best summary note, from either server's response shape."""
    d = _as_doc(gn_str)
    # Official server returns a bare list of note dicts; the proxy wraps it.
    if isinstance(d, list):
        notes = d
    elif isinstance(d, dict):
        notes = d.get('note_list') or []
    else:
        notes = []
    notes = [n for n in notes if isinstance(n, dict)]
    for n in notes:
        if n.get('data_type') == 'auto_sum_note':
            return (n.get('data_content') or '').strip()
    for n in notes:
        if (n.get('data_content') or '').strip():
            return n['data_content'].strip()
    return ''

def code_prefix():
    """This deployment's recording-code prefix, or None if it has none.

    Numbering is a convenience laid on top of the archive; losing a recording
    is not an acceptable price for it. A tenant with no pinned prefix therefore
    archives normally and simply gets no codes — what it can never get is
    somebody else's series, because prefix_for() refuses to guess.
    """
    return CODE_PREFIX


def init_db(conn):
    conn.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS recordings(
      id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
      duration_ms INTEGER, lang TEXT, asr_engine TEXT, asr_transcript TEXT,
      plaud_transcript TEXT, summary TEXT, audio_path TEXT, archived_at TEXT,
      plaud_meta_json TEXT, summary_json TEXT, semantic_title TEXT, asr_meta_json TEXT,
      asr_alternative_transcript TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS recordings_fts USING fts5(
      id UNINDEXED, name, transcript);
    -- Review state for PLAUD's own transcripts. asr_backfill.ensure_reviews is
    -- authoritative for this table (and owns enrollment); it is created here so
    -- an owner archive opens complete, exactly as asr_attempts does for the
    -- tenant layout in connector.SCHEMA.
    CREATE TABLE IF NOT EXISTS plaud_reviews(
      id TEXT PRIMARY KEY, state TEXT NOT NULL, eligible_epoch INTEGER,
      selected_source TEXT, reason TEXT, candidates_json TEXT,
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
      claim_epoch INTEGER, claim_owner TEXT, updated_at TEXT);
    -- Per-stage retry ledgers. connector.SCHEMA already creates these for the
    -- tenant layout; creating them here too means a FRESH archive can take its
    -- first pipeline job immediately, instead of depending on whichever
    -- backfill entry point happened to run first to migrate the table in.
    CREATE TABLE IF NOT EXISTS asr_attempts(
      id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
      last_at TEXT, last_error TEXT);
    CREATE TABLE IF NOT EXISTS summary_attempts(
      id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
      last_at TEXT, last_error TEXT);
    ''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    for name in ('summary_json', 'semantic_title', 'asr_meta_json', 'asr_alternative_transcript'):
        if name not in columns:
            conn.execute(f'ALTER TABLE recordings ADD COLUMN {name} TEXT')
    conn.commit()
    # The durable job table. Ingest commits a job in the same transaction as the
    # recording row, so it has to exist before the first archive_one of a fresh
    # archive — not after some later stage happens to run.
    pipeline.ensure_schema(conn)
    # Codes: migrate the column, then number whatever is still unnumbered. Both
    # halves are idempotent, so this runs on every start and doubles as the
    # repair path for a row archived while the process was killed mid-pass.
    _codes.ensure_schema(conn)
    prefix = code_prefix()
    if prefix:
        _codes.backfill(conn, prefix)

def _sync_fts(conn, rid, name, transcript):
    conn.execute('DELETE FROM recordings_fts WHERE id=?', (rid,))
    conn.execute('INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)', (rid, name or '', transcript or ''))

def _needs_text_repair(conn, fid, duration_ms=None):
    """True if a row exists with audio but no text, and text is plausible.

    A row written by a buggy or partial run (empty transcript AND empty
    summary) is indistinguishable from a fully archived one to the
    "already archived" short-circuit, so such a row would be skipped forever
    and the recording would stay silently blank. Re-fetching it is cheap
    relative to leaving it permanently empty.

    Very short recordings genuinely have no transcript, so they are not
    treated as damaged; otherwise every pass would refetch them for nothing.
    """
    row = conn.execute(
        "SELECT COALESCE(plaud_transcript,''), COALESCE(summary,''), "
        "COALESCE(asr_transcript,''), COALESCE(duration_ms,0) "
        "FROM recordings WHERE id=?", (fid,)).fetchone()
    if not row:
        return False
    plaud, summary, asr, dur = row
    if plaud.strip() or summary.strip() or asr.strip():
        return False
    dur = duration_ms if duration_ms is not None else dur
    return bool(dur) and dur >= MIN_TEXT_DURATION_MS


# A PLAUD file id is remote input. It arrives from list_files on an account
# this process does not control, and it is then interpolated into a filename,
# a primary key and an FTS row. The viewer already constrains the same value to
# [A-Za-z0-9_-]+ on its /audio and /api routes; matching that here means an id
# the archive accepts is an id the viewer can serve, and an id that is not a
# path fragment in any filesystem: no separator, no dot segment, no NUL, no
# percent-encoding to unwrap, no leading dash to be read as a flag.
SAFE_RECORDING_ID = __import__('re').compile(r'\A[A-Za-z0-9_][A-Za-z0-9_-]{0,127}\Z')


class UnsafeRecordingId(ValueError):
    """A discovered id that must not be turned into a path or a row.

    Deliberately fatal for that one recording rather than sanitised into
    something adjacent: silently rewriting `../../escaped` to `escaped` would
    archive a real recording under an id nothing else in the system agrees
    with, and the next pass would archive it again under the same wrong name.
    """


def is_safe_recording_id(fid) -> bool:
    return bool(isinstance(fid, str) and SAFE_RECORDING_ID.match(fid))


def require_safe_recording_id(fid) -> str:
    """The id, or a refusal — before any filesystem or database use.

    Checked at the boundary because after this point it has already been joined
    onto the audio directory, and a temp file created in the DERIVED directory
    escapes with it: containment after the join is not containment.
    """
    if not is_safe_recording_id(fid):
        raise UnsafeRecordingId(
            'refusing a PLAUD file id that is not a plain identifier; it would '
            'be used as a filename and a primary key')
    return fid


def safe_recording_ids(ids):
    """The ids from one discovery page that are safe to archive.

    One malformed id must not end the pass — the other recordings in that page
    are real work — so the bad one is dropped with a note and the rest proceed.
    The id itself is never logged: it is unvalidated remote input, and this line
    goes to container logs and cron mail.
    """
    out = []
    for fid in ids or []:
        if is_safe_recording_id(fid):
            out.append(fid)
        else:
            print(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} skip: discovery '
                  f'returned a file id that is not a plain identifier')
    return out


def contained(path, directory) -> bool:
    """True when `path` really is inside `directory` once resolved."""
    resolved, root = os.path.realpath(path), os.path.realpath(directory)
    try:
        return os.path.commonpath([resolved, root]) == root
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_audio_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    allowed = {
        item.strip().lower().rstrip('.')
        for item in os.environ.get('PLAUD_AUDIO_DOWNLOAD_HOSTS', '').split(',')
        if item.strip()
    }
    if parsed.scheme != 'https' or not host or parsed.username or parsed.password:
        raise ValueError('audio download requires an HTTPS URL without credentials')
    if parsed.port not in (None, 443):
        raise ValueError('audio download requires HTTPS port 443')
    if host not in allowed:
        raise ValueError('audio download host is not allow-listed')
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('audio download host resolves outside the public internet')
    return parsed.geturl()


def _download_to_file(url, destination):
    url = _validated_audio_url(url)
    limit = int(os.environ.get('PLAUD_AUDIO_MAX_BYTES', str(8 << 30)))
    request = urllib.request.Request(url, headers={'User-Agent': 'Jazzuu/1'})
    total = 0
    with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=REQUEST_TIMEOUT_S) as response, open(destination, 'wb') as output:
        declared = response.headers.get('Content-Length')
        if declared and int(declared) > limit:
            raise ValueError('audio download is too large')
        while chunk := response.read(1 << 20):
            total += len(chunk)
            if total > limit:
                raise ValueError('audio download is too large')
            output.write(chunk)
    if total <= 0:
        raise RuntimeError('downloaded audio is empty')


def download_audio(url, path):
    """Fetch audio to a temp file beside `path`, then rename it into place.

    Writing the canonical name directly is what made a dropped connection
    indistinguishable from a finished download: every later reader — the repair
    pass, local ASR, the viewer's has_audio — treats a present, non-empty
    `<id>.mp3` as "this recording is archived", so half a file would be
    transcribed as though it were the recording.

    The temp file shares the directory on purpose. os.replace is only atomic
    within one filesystem; a temp in /tmp would make this a copy with a visible
    half-written window, which is the failure being fixed. Contents are fsynced
    before the rename and the directory after it, so a power loss cannot leave
    the name pointing at bytes that never reached the disk. A failure at any
    point removes the temp file: a partial download is not left behind under
    any name.
    """
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}.', suffix='.part', dir=directory)
    os.close(handle)
    try:
        _download_to_file(url, tmp)
        with open(tmp, 'rb+') as fh:
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def archive_one(conn, sid, fid, force=False):
    # First statement in the function on purpose. Everything below joins this
    # value onto the audio directory or writes it into the archive, so it has
    # to be contained before any of that, not after.
    fid = require_safe_recording_id(fid)
    row = conn.execute('SELECT id,audio_path FROM recordings WHERE id=?', (fid,)).fetchone()
    audio_path = os.path.join(AUDIO, f'{fid}.mp3')
    # Belt and braces against a future edit to the id rule: the path that is
    # about to be written must resolve inside this tenant's audio directory.
    if not contained(audio_path, AUDIO):
        raise UnsafeRecordingId(
            'refusing an audio path that resolves outside the tenant archive')
    have_audio = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
    if row and have_audio and not force and not _needs_text_repair(conn, fid):
        # Nothing to fetch — but this is exactly the path an interrupted pass
        # comes back through, so the queue still has to be made correct. The
        # enqueue is idempotent and refuses a recording whose local transcript
        # is already published, so a settled archive stays settled.
        if pipeline.enqueue_asr(conn, fid):
            print(f'requeued {fid} (archived audio with no ASR job)')
        print(f'skip {fid} (already archived)')
        return {'id': fid, 'skipped': True}
    gf = call('get_file', {'file_id': fid}, sid)
    meta = _as_doc(gf)
    if not isinstance(meta, dict) or not meta:
        raise RuntimeError(f'get_file returned no usable metadata for {fid}')
    name = meta.get('name', ''); start_at = meta.get('start_at', ''); created_at = meta.get('created_at', '')
    duration_ms = meta.get('duration', 0) or 0
    pu = meta.get('presigned_url')
    # strip presigned_url from stored meta (expires; keep meta lean but audio pointer stays via audio_path)
    meta_store = dict(meta); meta_store.pop('presigned_url', None)
    kb = 0
    if pu and (force or not have_audio):
        download_audio(pu, audio_path)
    if os.path.exists(audio_path): kb = os.path.getsize(audio_path) // 1024
    plaud_transcript = fetch_plaud_transcript(fid, sid)
    summary = extract_summary(call('get_note', {'file_id': fid}, sid))
    archived_at = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    # The recording row, its search index entry and its first pipeline job are
    # ONE transaction. A row without a job is work nothing is looking for —
    # there is no downstream schedule left that would find it — and a job
    # without a row points at a recording that does not exist. The only two
    # outcomes allowed here are "both" and "neither"; a crash in between is
    # covered by the next poll re-archiving an id it still cannot see.
    try:
        conn.execute('''INSERT INTO recordings
          (id,name,start_at,created_at,duration_ms,lang,asr_engine,asr_transcript,plaud_transcript,summary,audio_path,archived_at,plaud_meta_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,start_at=excluded.start_at,created_at=excluded.created_at,
            duration_ms=excluded.duration_ms,plaud_transcript=excluded.plaud_transcript,
            summary=COALESCE(NULLIF(recordings.summary,''),excluded.summary),
            audio_path=excluded.audio_path,archived_at=excluded.archived_at,
            plaud_meta_json=excluded.plaud_meta_json''',
          (fid, name, start_at, created_at, duration_ms, None, None, None,
           plaud_transcript, summary, audio_path, archived_at, json.dumps(meta_store, ensure_ascii=False)))
        # FTS must reflect what the viewer shows: ASR wins, PLAUD is the fallback.
        # Read back the ASR text so a re-archive (--force / metadata refresh) never
        # wipes an ASR transcript out of the search index. A PLAUD transcript that
        # arrived with the recording is published here and is immediately readable
        # and searchable — provisionally. It does not finish the ASR stage.
        prev = conn.execute('SELECT asr_transcript FROM recordings WHERE id=?', (fid,)).fetchone()
        asr_prev = (prev[0] if prev else None) or ''
        fts_transcript = asr_prev.strip() or plaud_transcript
        _sync_fts(conn, fid, name, fts_transcript)
        # Every ASR worker reads a tenant-local file. Do not enqueue until this
        # pass has one; reconciliation adopts rows whose audio arrives later.
        if pipeline.resolve_local_audio(AUDIO, fid, audio_path):
            pipeline.enqueue_asr(conn, fid, commit=False)
            if diarization.available():
                pipeline.enqueue(conn, fid, pipeline.STAGE_DIARIZATION, commit=False)
        conn.commit()
    except Exception:  # noqa: BLE001 — a row with no job is the one bad outcome
        conn.rollback()
        raise
    # Allocated after the row is committed, and never inside this pass's
    # transaction: allocation takes the writer lock itself so two overlapping
    # ingest passes cannot hand out the same code. A crash in between leaves
    # the row unnumbered, which the backfill in init_db repairs on the next run.
    prefix = code_prefix()
    number = _codes.allocate_number(conn, prefix, fid) if prefix else None
    print(f'archived {fid} {duration_ms//1000}s audio={kb}KB plaud_transcript={len(plaud_transcript)}chars summary={len(summary)}chars'
          + (f' number={number}' if number else ''))
    return {'id': fid, 'number': number, 'duration_ms': duration_ms, 'kb': kb,
            'plaud_chars': len(plaud_transcript), 'summary_chars': len(summary)}

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    force = '--force' in sys.argv
    if not args:
        print('usage: archive_recording.py <file_id> [--force]'); sys.exit(1)
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print('REFUSED archive connector lock is held')
        return 75
    try:
        conn = sqlite3.connect(DB)
        try:
            init_db(conn)
            sid = mcp_connect()
            for fid in args:
                archive_one(conn, sid, fid, force)
        finally:
            conn.close()
    finally:
        os.close(lock_fd)
    return 0

if __name__ == '__main__':
    sys.exit(main())
