"""store.py — read-only queries over one tenant's processed archive.

Everything a bot can ever see is shaped here, which makes this the place the
leak rules live:

  * a recording is addressed ONLY by its permanent number, so PLAUD file ids,
    audio paths and the archive's location on disk never appear in a payload;
  * the ASR module's alternative hypothesis (`asr_alternative_transcript`) is
    a candidate, not a transcript, and is never served;
  * `asr_meta_json` is whitelisted down to four routing fields rather than
    passed through, because it is a debugging blob the ASR service owns;
  * every query is scoped to this process's series prefix, so a row that
    somehow belonged to another tenant would still not be served.

The database is opened read-only. With a live WAL, SQLite must open the primary
archive and readable WAL sidecars directly, so it sees committed frames without
copying an inconsistent pair. If that normal read-only open fails, the service
fails closed while a WAL exists. Only after a closed writer has checkpointed the
WAL away can it safely use SQLite's `immutable=1` fallback on a chmod/read-only
mount.
"""
from __future__ import annotations

import base64
import contextlib
import os
import re
import sqlite3
import sys

_ARCHIVE_PACKAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'archive')
if _ARCHIVE_PACKAGE not in sys.path:
    sys.path.insert(0, _ARCHIVE_PACKAGE)

import recording_codes  # noqa: E402
import pipeline  # noqa: E402

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# A page cursor is a position, and a position past this is a caller walking the
# archive for its own sake rather than reading it.
MAX_OFFSET = 10000
SUMMARY_PREVIEW_CHARS = 200
MAX_BRANCHES = 7
MAX_POINTS = 6

_CURSOR_RE = re.compile(r'^r1:(\d{1,6})$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)

# What a bot may ask for, and nothing else. An unknown key is refused rather
# than ignored: a caller that thinks it is filtering and is not would read the
# result as "there are no others".
METADATA_FILTERS = frozenset({'date_from', 'date_to', 'min_duration_ms'})
SUMMARY_FILTERS = frozenset({'has_summary'})
TRANSCRIPT_FILTERS = frozenset({'has_transcript'})
ALLOWED_FILTERS = METADATA_FILTERS | SUMMARY_FILTERS | TRANSCRIPT_FILTERS

# The routing fields that describe WHICH engine ran and why. Everything else in
# asr_meta_json stays inside the ASR service.
_ASR_FIELDS = ('selected_engine', 'requested_engine', 'route_reason', 'language')


class StoreError(RuntimeError):
    """Base for failures a caller is allowed to hear about."""


class InvalidArgument(StoreError):
    """The request is malformed. Messages never quote SQL or a path."""


class NotFound(StoreError):
    """A well-formed number that this archive does not hold."""


class Unavailable(StoreError):
    """The archive cannot be read at all right now."""


# ---------------------------------------------------------------- connection

@contextlib.contextmanager
def open_db(config):
    """A read-only connection to this tenant's archive."""
    conn = _connect(config.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _require_numbering(conn):
    """Refuse to serve an archive whose rows have no numbers yet.

    Every tool here addresses recordings by number, so without the column
    there is nothing to answer with. Saying so plainly beats the generic
    internal error a missing column would otherwise produce, and points at the
    repair: the ingest side migrates and backfills on its next run.
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    if recording_codes.NUMBER_COLUMN not in columns:
        raise Unavailable(
            'this archive has not been numbered yet; it is served by number '
            'only, and the ingest side allocates numbers on its next run')


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise Unavailable('the recordings archive is not available')
    try:
        # This is the only mode allowed while a writer has a live WAL: SQLite
        # can merge committed WAL frames without us copying an inconsistent
        # DB/WAL pair. A read-only mount must expose readable sidecars.
        return _open_readonly(db_path)
    except sqlite3.OperationalError as exc:
        if os.path.exists(db_path + '-wal'):
            raise Unavailable('the recordings archive is not safely readable') from exc
        # A closed writer has checkpointed the WAL. immutable avoids SQLite
        # creating a shm sidecar on chmod/read-only mounts, and cannot discard
        # any committed WAL state because none exists.
        try:
            return _open_readonly(db_path, immutable=True)
        except sqlite3.Error as fallback:
            raise Unavailable('the recordings archive is not available') from fallback


def _open_readonly(path: str, immutable=False) -> sqlite3.Connection:
    uri = f'file:{path}?mode=ro' + ('&immutable=1' if immutable else '')
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        # Belt and braces: mode=ro already refuses archive writes, and this
        # also prevents an accidental write if SQLite changes URI semantics.
        conn.execute('PRAGMA query_only=1')
        conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


# ------------------------------------------------------------------ paging

def encode_cursor(offset: int) -> str:
    raw = f'r1:{int(offset)}'.encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_cursor(cursor) -> int:
    """Position behind an opaque cursor, or a refusal that explains nothing.

    The message deliberately does not describe the encoding: a cursor is ours
    to mint, and a caller trying to forge one gets no help doing it.
    """
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.strip():
        raise InvalidArgument('cursor is not valid')
    padded = cursor.strip() + '=' * (-len(cursor.strip()) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode('utf-8')
    except Exception:
        raise InvalidArgument('cursor is not valid') from None
    match = _CURSOR_RE.match(decoded)
    if not match:
        raise InvalidArgument('cursor is not valid')
    position = int(match.group(1))
    if position > MAX_OFFSET:
        raise InvalidArgument('cursor is not valid')
    return position


def clamp_limit(limit) -> int:
    """A page size within bounds, or a refusal.

    A limit that is nonsense is an error, while a limit that is merely greedy
    is clamped: the caller asked a legitimate question and gets an answer it
    can page through.
    """
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidArgument('limit must be a whole number')
    if limit < 1:
        raise InvalidArgument('limit must be at least 1')
    return min(limit, MAX_LIMIT)


# ------------------------------------------------------------------ helpers

def _plain_summary(text):
    """One-line plaintext preview of a Markdown summary."""
    if not text:
        return None
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'`{1,3}', '', text)
    text = re.sub(r'[*_]{1,3}', '', text)
    text = re.sub(r'^\s{0,3}#{1,6}\s*', '', text, flags=re.M)
    text = re.sub(r'^\s{0,3}>\s?', '', text, flags=re.M)
    text = re.sub(r'^\s{0,3}[-*+]\s+', '', text, flags=re.M)
    text = re.sub(r'^\s{0,3}\d+\.\s+', '', text, flags=re.M)
    text = re.sub(r'\s+', ' ', text).strip()
    return text or None


def _json_object(raw):
    if not raw:
        return None
    try:
        import json
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _text(value):
    return (value or '').strip() or None


def _series_clause(config):
    return f'{config.code_prefix}-%'


def _filter_sql(filters, allowed_filters=ALLOWED_FILTERS):
    """(sql, params) for a validated filter object."""
    if filters is None:
        return '', []
    if not isinstance(filters, dict):
        raise InvalidArgument('filters must be an object')
    unknown = sorted(set(filters) - allowed_filters)
    if unknown:
        raise InvalidArgument(
            f'unsupported filter {unknown[0]!r}; supported filters are '
            f'{", ".join(sorted(allowed_filters))}')

    clauses, params = [], []
    when = "COALESCE(NULLIF(start_at,''), NULLIF(created_at,''), '')"

    for key in ('date_from', 'date_to'):
        value = filters.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
            raise InvalidArgument(f'{key} must be a date such as 2026-01-31')
        if key == 'date_from':
            clauses.append(f'{when} >= ?')
            params.append(value.strip())
        else:
            clauses.append(f'{when} <= ?')
            params.append(value.strip() + ' 23:59:59')

    for key, column in (('has_summary', "COALESCE(summary,'') || COALESCE(summary_json,'')"),
                        ('has_transcript', "COALESCE(asr_transcript,'') || COALESCE(plaud_transcript,'')")):
        value = filters.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise InvalidArgument(f'{key} must be true or false')
        clauses.append(f"TRIM({column}) {'<>' if value else '='} ''")

    minimum = filters.get('min_duration_ms')
    if minimum is not None:
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise InvalidArgument('min_duration_ms must be a whole number of '
                                  'milliseconds')
        clauses.append('COALESCE(duration_ms,0) >= ?')
        params.append(minimum)

    return (' AND ' + ' AND '.join(clauses) if clauses else ''), params


# -------------------------------------------------------------------- tools

def _allowlist_sql(allowed_numbers):
    """Bounded SQL clause for an already-authorized recording allowlist."""
    if allowed_numbers is None or '*' in allowed_numbers:
        return '', []
    values = sorted(allowed_numbers)
    # Refuse pathological direct state edits rather than build unbounded SQL in
    # the read-only archive process.
    if len(values) > 100:
        raise Unavailable('the recordings archive is not available')
    if not values:
        return ' AND 0', []
    return ' AND recording_number IN (' + ','.join('?' for _ in values) + ')', values


def list_recordings(config, limit=None, cursor=None, filters=None, allowed_numbers=None,
                    allowed_filters=ALLOWED_FILTERS) -> dict:
    """One bounded page of this tenant's recordings, newest first."""
    limit = clamp_limit(limit)
    offset = decode_cursor(cursor)
    where, params = _filter_sql(filters, allowed_filters)
    allowed_where, allowed_params = _allowlist_sql(allowed_numbers)

    with open_db(config) as conn:
        _require_numbering(conn)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
        title_expr = ('semantic_title' if 'semantic_title' in columns
                      else 'NULL AS semantic_title')
        rows = conn.execute(
            f"SELECT recording_number, name, {title_expr}, start_at, created_at, duration_ms, "
            "lang, summary, "
            "TRIM(COALESCE(asr_transcript,'') || COALESCE(plaud_transcript,'')) "
            "  <> '' AS has_transcript, "
            "TRIM(COALESCE(summary,'') || COALESCE(summary_json,'')) "
            "  <> '' AS has_summary "
            "FROM recordings "
            "WHERE recording_number LIKE ?" + allowed_where + where + " "
            "ORDER BY COALESCE(NULLIF(start_at,''), NULLIF(created_at,''), '') "
            "  DESC, recording_number DESC "
            "LIMIT ? OFFSET ?",
            [_series_clause(config)] + allowed_params + params + [limit + 1, offset],
        ).fetchall()

    has_more = len(rows) > limit
    items = [{
        'number': row['recording_number'],
        'name': _text(row['semantic_title']) or row['name'] or 'Без названия',
        'title': _text(row['semantic_title']) or row['name'] or 'Без названия',
        'source_name': row['name'],
        'started_at': _text(row['start_at']) or _text(row['created_at']),
        'duration_ms': row['duration_ms'],
        'language': _text(row['lang']),
        'has_transcript': bool(row['has_transcript']),
        'has_summary': bool(row['has_summary']),
        'summary_preview': (_plain_summary(row['summary']) or '')[:SUMMARY_PREVIEW_CHARS] or None,
    } for row in rows[:limit]]
    return {
        'items': items,
        'limit': limit,
        'next_cursor': encode_cursor(offset + limit) if has_more else None,
    }


def search_recordings(config, query=None, limit=None, cursor=None, allowed_numbers=None) -> dict:
    """Full-text search over names and stored transcripts (FTS5)."""
    if not isinstance(query, str) or not query.strip():
        raise InvalidArgument('query must be a non-empty string')
    limit = clamp_limit(limit)
    offset = decode_cursor(cursor)
    allowed_where, allowed_params = _allowlist_sql(allowed_numbers)

    # A query is words, not an FTS expression. Quoting each term keeps
    # punctuation, operators and stray quotes from ever reaching the matcher.
    terms = _WORD_RE.findall(query)
    if not terms:
        return {'items': [], 'limit': limit, 'next_cursor': None}
    match = ' '.join(f'"{term}"' for term in terms)

    with open_db(config) as conn:
        _require_numbering(conn)
        try:
            rows = conn.execute(
                "SELECT r.recording_number AS number, r.name AS name, "
                "snippet(recordings_fts,-1,'[',']','…',12) AS snip "
                "FROM recordings_fts f JOIN recordings r ON r.id=f.id "
                "WHERE recordings_fts MATCH ? AND r.recording_number LIKE ?" +
                allowed_where.replace('recording_number', 'r.recording_number') + " "
                "ORDER BY rank LIMIT ? OFFSET ?",
                (match, _series_clause(config), *allowed_params, limit + 1, offset),
            ).fetchall()
        except sqlite3.OperationalError:
            # A query FTS5 still refuses is "no results", never a stack trace.
            rows = []

    has_more = len(rows) > limit
    items = [{
        'number': row['number'],
        'name': row['name'] or 'Без названия',
        'snippet': (row['snip'] or '').strip() or None,
    } for row in rows[:limit]]
    return {
        'items': items,
        'limit': limit,
        'next_cursor': encode_cursor(offset + limit) if has_more else None,
    }


def _row_for_number(conn, config, number):
    # Both lookup tools reach the archive through here, so the "not numbered
    # yet" refusal lives here too rather than at each call site.
    """(row, canonical code) for a caller-supplied number.

    parse_code refuses another series' prefix before the query runs, so a
    cross-tenant probe cannot tell "not yours" from "does not exist" by timing
    or by message.
    """
    try:
        code = recording_codes.parse_code(number, config.code_prefix)
    except recording_codes.RecordingCodeError as exc:
        raise InvalidArgument(str(exc)) from None
    _require_numbering(conn)
    row = conn.execute(
        'SELECT * FROM recordings WHERE recording_number=?', (code,)).fetchone()
    if row is None:
        raise NotFound(f'no recording {code} in this archive')
    return row, code


def _columns(row):
    return set(row.keys())


def get_recording(config, number=None) -> dict:
    """Everything a bot may read about one recording."""
    with open_db(config) as conn:
        row, code = _row_for_number(conn, config, number)
        columns = _columns(row)

        asr_text = _text(row['asr_transcript']) if 'asr_transcript' in columns else None
        plaud_text = _text(row['plaud_transcript']) if 'plaud_transcript' in columns else None
        summary_json = _json_object(row['summary_json']) if 'summary_json' in columns else None
        asr_meta = _json_object(row['asr_meta_json']) if 'asr_meta_json' in columns else None
        has_audio = os.path.exists(os.path.join(config.audio_dir, f'{row["id"]}.mp3'))
        readiness = pipeline.readiness(conn, row['id'])

    has_pipeline = any(readiness['stages'].values())
    if asr_text:
        source, transcript, status = 'asr', asr_text, (readiness['state'] if has_pipeline else 'ready')
    elif plaud_text:
        source, transcript, status = 'plaud', plaud_text, (readiness['state'] if has_pipeline else 'plaud_only')
    else:
        source, transcript, status = None, None, (readiness['state'] if has_pipeline else 'pending')

    route = {}
    if isinstance(asr_meta, dict):
        nested = asr_meta.get('route') if isinstance(asr_meta.get('route'), dict) else {}
        for field_name in _ASR_FIELDS:
            value = asr_meta.get(field_name, nested.get(field_name))
            if isinstance(value, (str, int, float, bool)):
                route[field_name] = value

    display_name = (_text(row['semantic_title']) if 'semantic_title' in columns else None) \
        or row['name'] or 'Без названия'
    return {
        'number': code,
        'name': row['name'] or 'Без названия',
        'title': display_name,
        'source_name': row['name'],
        'started_at': _text(row['start_at']) or _text(row['created_at']),
        'duration_ms': row['duration_ms'],
        'language': _text(row['lang']),
        'transcript': {'source': source, 'text': transcript},
        'summary': {
            'text': _text(row['summary']),
            'structured': summary_json,
        },
        'asr': {
            'status': status,
            'ready': readiness['ready'],
            'engine': route.get('selected_engine') or _text(row['asr_engine']),
            'requested_engine': route.get('requested_engine'),
            'route_reason': route.get('route_reason'),
            'language': route.get('language'),
        },
        'has_audio': has_audio,
        'has_mindmap': bool(summary_json),
    }


def get_mindmap(config, number=None) -> dict:
    """Structured mind-map data for one recording — never a file handle.

    The viewer renders a PNG of this on demand; a bot gets the structure it is
    drawn from. No viewer link is returned until a stable N-code viewer route
    exists, so this service never leaks the viewer's private PLAUD identifier.
    """
    with open_db(config) as conn:
        row, code = _row_for_number(conn, config, number)
        summary_json = _json_object(row['summary_json']) if 'summary_json' in _columns(row) else None

    columns = _columns(row)
    display_name = (_text(row['semantic_title']) if 'semantic_title' in columns else None) \
        or row['name'] or 'Без названия'
    source_name = row['name'] or 'Без названия'
    mindmap = build_mindmap(source_name, summary_json)
    # The viewer currently routes by its private PLAUD id. Never mint a link
    # that leaks it; a stable N-code route may opt in when actually deployed.
    app_url = None
    return {
        'number': code,
        'name': source_name,
        'title': display_name,
        'source_name': row['name'],
        'mindmap': mindmap,
        'reason': None if mindmap else
                  'no structured summary has been generated for this recording yet',
        'app_url': app_url,
    }


def build_mindmap(name, summary_json):
    """A root and its branches, out of the stored structured summary."""
    if not isinstance(summary_json, dict):
        return None

    branches = []

    def add(title, points):
        points = [p for p in (_point(item) for item in points or []) if p]
        if title and points:
            branches.append({'title': title, 'points': points[:MAX_POINTS]})

    for theme in _as_list(summary_json.get('themes'))[:MAX_BRANCHES]:
        if isinstance(theme, dict):
            detail = theme.get('summary') or theme.get('description') or ''
            points = [detail] if detail else _as_list(theme.get('points'))
            add(str(theme.get('title') or 'Тема'), points)
        elif isinstance(theme, str) and theme.strip():
            add('Тема', [theme])

    add('Решения', _as_list(summary_json.get('decisions')))
    add('Риски', _as_list(summary_json.get('risks')))
    add('Ключевые факты', _as_list(summary_json.get('key_facts')
                                   or summary_json.get('facts')))
    add('Задачи', _as_list(summary_json.get('action_items')))

    if not branches:
        return None
    return {
        'root': name,
        'overview': _text(str(summary_json.get('overview') or
                              summary_json.get('one_line_overview') or '')),
        'branches': branches[:MAX_BRANCHES],
    }


def _as_list(value):
    return value if isinstance(value, list) else []


def _point(item):
    if isinstance(item, str):
        return item.strip() or None
    if not isinstance(item, dict):
        return None
    text = (item.get('text') or item.get('task') or item.get('summary')
            or item.get('title') or item.get('value') or '')
    text = str(text).strip()
    if not text:
        return None
    detail = item.get('owner') or item.get('label') or item.get('due')
    return f'{text} — {detail}' if detail else text


def counts(config) -> dict:
    """Numbers only, for readiness. Never names, never paths."""
    with open_db(config) as conn:
        _require_numbering(conn)
        total = conn.execute(
            'SELECT COUNT(*) FROM recordings WHERE recording_number LIKE ?',
            (_series_clause(config),)).fetchone()[0]
        unnumbered = conn.execute(
            "SELECT COUNT(*) FROM recordings WHERE COALESCE(recording_number,'')=''"
        ).fetchone()[0]
    return {'recordings': total, 'unnumbered': unnumbered}
