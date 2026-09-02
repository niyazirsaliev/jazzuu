#!/usr/bin/env python3
"""recording_codes.py — permanent, human-friendly codes for archived recordings.

A recording's id is a PLAUD file id: fine for machines, useless for a person
saying "look at this one" out loud. Each archived recording therefore also
carries a short code, unique within its tenant's archive:

    tenant-alpha  A-0042
    tenant-beta   B-0042

Two properties are the whole point of this module.

*The code is stored, never derived.* Nothing here counts rows. A number
computed at read time — "the 68th row" — silently renumbers the entire archive
the first time a recording is deleted, which is exactly the moment a written-
down code becomes a lie. The number is allocated once, written to the row, and
from then on a rename, a re-sync or a neighbour's deletion cannot move it.

*Allocation is race-safe and idempotent.* Ingest passes overlap (a 5-minute
cron against a 4-hour recording), so allocation runs inside a BEGIN IMMEDIATE
transaction and is guarded by a UNIQUE index. Asking twice for the same
recording returns the same code and writes nothing the second time.

The counter lives in `recording_code_seq` and only ever moves forward, so a
deleted recording's number is retired rather than handed to the next arrival.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

# Four digits is the display width, not a limit: a five-digit archive keeps
# counting rather than wrapping onto a code someone already wrote down.
CODE_WIDTH = 4
MAX_DIGITS = 6

_PREFIX_RE = re.compile(r'^[A-Z][A-Z0-9]{0,7}$')
_CANONICAL_RE = re.compile(r'^([A-Za-z][A-Za-z0-9]{0,7})-(\d{1,%d})$' % MAX_DIGITS)
_SHORTHAND_RE = re.compile(r'^(\d{1,%d})$' % MAX_DIGITS)

SEQ_TABLE = 'recording_code_seq'
NUMBER_COLUMN = 'recording_number'

# How often a blocked allocation retries before giving up. SQLite's own busy
# timeout does most of the waiting; this covers the UNIQUE-index backstop.
_ATTEMPTS = 8
_BACKOFF_S = 0.05


class RecordingCodeError(Exception):
    """Base for every failure this module reports."""


class UnknownTenantError(RecordingCodeError):
    """No prefix is pinned for this tenant, so nothing may be allocated.

    Deliberately fatal. A process that cannot say whose archive it is holding
    must not write codes into it.
    """


class InvalidCodeError(RecordingCodeError):
    """The text is not a code this archive can hold.

    Covers both malformed input and another tenant's prefix. The message names
    only what the caller already sent — never which tenant owns the prefix, and
    never whether such a recording exists elsewhere.
    """


class UnknownRecordingError(RecordingCodeError):
    """Asked to number a row that is not in this archive."""


def _prefixes(env) -> dict[str, str]:
    raw = env.get('RECORDING_CODE_PREFIXES_JSON', '').strip()
    try:
        configured = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise UnknownTenantError(
            'recording codes: RECORDING_CODE_PREFIXES_JSON must be a valid JSON object') from exc
    if not isinstance(configured, dict) or not configured:
        raise UnknownTenantError(
            'recording codes: RECORDING_CODE_PREFIXES_JSON must be a non-empty JSON object')
    clean: dict[str, str] = {}
    for tenant, prefix in configured.items():
        if not isinstance(tenant, str) or not tenant.strip() or not isinstance(prefix, str):
            raise UnknownTenantError('recording codes: tenant IDs and prefixes must be strings')
        tenant, prefix = tenant.strip(), prefix.strip().upper()
        if tenant in clean or not _PREFIX_RE.fullmatch(prefix):
            raise UnknownTenantError(
                'recording codes: prefixes must be unique 1-8 character uppercase codes')
        clean[tenant] = prefix
    if len(set(clean.values())) != len(clean):
        raise UnknownTenantError('recording codes: each tenant must have a unique prefix')
    return clean


def prefix_for(tenant_id, env=None) -> str:
    """The configured code prefix for a tenant id, or a fatal error."""
    if not isinstance(tenant_id, str):
        raise UnknownTenantError(
            'recording codes: a tenant id must be a string, not '
            f'{type(tenant_id).__name__}')
    prefixes = _prefixes(os.environ if env is None else env)
    prefix = prefixes.get(tenant_id.strip())
    if not prefix:
        raise UnknownTenantError(
            f'recording codes: no code prefix is pinned for tenant '
            f'{tenant_id.strip() or "<empty>"!r}; the configured tenant must '
            f'be one of {", ".join(sorted(prefixes))}')
    return prefix


def format_code(prefix: str, number: int) -> str:
    """Render a stored number as its canonical code."""
    return f'{prefix}-{int(number):0{CODE_WIDTH}d}'


def _require_prefix(prefix) -> str:
    if not isinstance(prefix, str) or not _PREFIX_RE.fullmatch(prefix.strip().upper()):
        raise UnknownTenantError(
            'recording codes: this process has no configured code prefix, so a '
            'code cannot be resolved in any scope')
    return prefix.strip().upper()


def parse_code(text, prefix) -> str:
    """Normalise caller-supplied text into a canonical code for THIS scope.

    Accepts the canonical form in any case (`n-0068`), a short canonical form
    (`N-68`), and — only because this process serves exactly one tenant — the
    bare number a person actually says out loud (`68`). The scope's prefix is
    supplied by configuration and is never taken from the text: a code carrying
    somebody else's prefix is refused here, before any lookup happens, so the
    refusal cannot depend on whether that recording exists.
    """
    scope = _require_prefix(prefix)
    if isinstance(text, bool) or isinstance(text, float):
        raise InvalidCodeError(_bad_code_message(text, scope))
    if isinstance(text, int):
        text = str(text)
    if not isinstance(text, str):
        raise InvalidCodeError(_bad_code_message(text, scope))
    candidate = text.strip()

    match = _CANONICAL_RE.match(candidate)
    if match:
        given, digits = match.group(1).upper(), match.group(2)
        number = int(digits)
        if given != scope or number < 1:
            raise InvalidCodeError(_bad_code_message(candidate, scope))
        return format_code(scope, number)

    match = _SHORTHAND_RE.match(candidate)
    if match:
        number = int(match.group(1))
        if number < 1:
            raise InvalidCodeError(_bad_code_message(candidate, scope))
        return format_code(scope, number)

    raise InvalidCodeError(_bad_code_message(candidate, scope))


def _bad_code_message(text, scope: str) -> str:
    shown = text if isinstance(text, str) else repr(text)
    return (f'{shown!s} is not a valid recording code in this archive; '
            f'expected {scope}-0001 or the number on its own')


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add the code column, its uniqueness rule and the counter. Idempotent.

    Safe to call on every start, including on an archive that predates codes:
    existing rows keep a NULL number (SQLite treats NULLs as distinct, so the
    unique index tolerates any number of not-yet-numbered rows) and `backfill`
    fills them in afterwards.
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    if not columns:
        raise UnknownRecordingError(
            'recording codes: this database has no recordings table')
    if NUMBER_COLUMN not in columns:
        conn.execute(f'ALTER TABLE recordings ADD COLUMN {NUMBER_COLUMN} TEXT')
    conn.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS idx_recordings_{NUMBER_COLUMN} '
        f'ON recordings({NUMBER_COLUMN})')
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS {SEQ_TABLE}('
        '  prefix TEXT PRIMARY KEY,'
        '  next_number INTEGER NOT NULL)')
    conn.commit()


def current_number(conn: sqlite3.Connection, rec_id: str):
    """The code already stored for a recording, or None."""
    row = conn.execute(
        f'SELECT {NUMBER_COLUMN} FROM recordings WHERE id=?', (rec_id,)
    ).fetchone()
    if row is None:
        return None
    return (row[0] or '').strip() or None


def allocate_number(conn: sqlite3.Connection, prefix: str, rec_id: str) -> str:
    """The permanent code for a recording, allocating one if it has none.

    Idempotent and safe against a concurrent ingest pass in another process.
    Runs in its own BEGIN IMMEDIATE transaction, so the caller must not have
    one open: call it after committing the row it belongs to. The row is only
    ever numbered while the writer lock is held, which is what keeps two passes
    from handing out the same code.
    """
    code, _ = _allocate(conn, _require_prefix(prefix), rec_id)
    return code


def _allocate(conn: sqlite3.Connection, prefix: str, rec_id: str):
    """(code, newly_allocated) for one recording."""
    last_error = None
    for attempt in range(_ATTEMPTS):
        try:
            conn.commit()  # nothing of ours may be inside the write transaction
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            last_error = exc
            time.sleep(_BACKOFF_S * (attempt + 1))
            continue
        try:
            row = conn.execute(
                f'SELECT {NUMBER_COLUMN} FROM recordings WHERE id=?', (rec_id,)
            ).fetchone()
            if row is None:
                raise UnknownRecordingError(
                    'recording codes: cannot allocate a code for a recording '
                    'that is not in this archive')
            existing = (row[0] or '').strip()
            if existing:
                conn.rollback()
                return existing, False

            # Reserved only once the row is known to need one, so a repeat call
            # never burns a number and leaves a hole in the series.
            number = _reserve(conn, prefix)
            code = format_code(prefix, number)
            updated = conn.execute(
                f'UPDATE recordings SET {NUMBER_COLUMN}=? WHERE id=? '
                f"AND COALESCE({NUMBER_COLUMN},'')=''", (code, rec_id)).rowcount
            if updated != 1:
                # Someone numbered it between our read and our write, which the
                # writer lock should make impossible; rather than trust that,
                # drop everything and re-read.
                conn.rollback()
                continue
            conn.commit()
            return code, True
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            # IntegrityError: the UNIQUE index refused the code, so the counter
            # disagrees with reality. Rolling back discards the reservation too,
            # and the next pass recomputes it from the rows themselves.
            last_error = exc
            conn.rollback()
            time.sleep(_BACKOFF_S * (attempt + 1))
        except Exception:
            conn.rollback()
            raise

    raise RecordingCodeError(
        'recording codes: could not allocate a code after '
        f'{_ATTEMPTS} attempts ({type(last_error).__name__ if last_error else "no error"})')


def _reserve(conn: sqlite3.Connection, prefix: str) -> int:
    """Take the next number in a prefix's series, and advance the counter.

    The counter is authoritative, but it is cross-checked against the highest
    code actually stored so that an archive numbered before this table existed
    (or repaired by hand) cannot be handed a code that is already in use.
    """
    row = conn.execute(
        f'SELECT next_number FROM {SEQ_TABLE} WHERE prefix=?', (prefix,)
    ).fetchone()
    seq_next = int(row[0]) if row and row[0] else 1
    highest = conn.execute(
        f'SELECT MAX(CAST(SUBSTR({NUMBER_COLUMN}, ?) AS INTEGER)) FROM recordings '
        f'WHERE {NUMBER_COLUMN} LIKE ?', (len(prefix) + 2, f'{prefix}-%')
    ).fetchone()[0] or 0
    number = max(seq_next, int(highest) + 1, 1)
    conn.execute(
        f'INSERT INTO {SEQ_TABLE}(prefix,next_number) VALUES(?,?) '
        f'ON CONFLICT(prefix) DO UPDATE SET next_number='
        f'MAX({SEQ_TABLE}.next_number, excluded.next_number)',
        (prefix, number + 1))
    return number


def unnumbered_ids(conn: sqlite3.Connection):
    """Rows still without a code, in the order they will be numbered.

    Ordered by when the recording happened, falling back to when it was created
    and then to when it was archived, with the id as a final tie-break. The
    ordering is a pure function of stored data, so two processes — or the same
    process run twice — walk the archive identically.
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    keys = [name for name in ('start_at', 'created_at', 'archived_at')
            if name in columns]
    order = ', '.join(f"NULLIF({name},'')" for name in keys)
    sort_key = f"COALESCE({order}, '')" if keys else "''"
    rows = conn.execute(
        f'SELECT id FROM recordings '
        f"WHERE COALESCE({NUMBER_COLUMN},'')='' "
        f'ORDER BY {sort_key} ASC, id ASC').fetchall()
    return [row[0] for row in rows]


def backfill(conn: sqlite3.Connection, prefix: str) -> int:
    """Give every unnumbered recording a code. Returns how many THIS run wrote.

    Idempotent by construction: a row that already has a code is skipped, so a
    second run assigns nothing and reassigns nothing, and two processes running
    it at once still number each row exactly once.
    """
    scope = _require_prefix(prefix)
    assigned = 0
    for rec_id in unnumbered_ids(conn):
        try:
            _, created = _allocate(conn, scope, rec_id)
        except UnknownRecordingError:
            continue  # deleted under us mid-run; nothing to number
        if created:
            assigned += 1
    return assigned


def resolve(conn: sqlite3.Connection, text, prefix: str):
    """The recording id behind a caller-supplied code, or None if there is none.

    Raises InvalidCodeError before touching the database when the text is not a
    code for this scope, so a cross-tenant code can never be distinguished from
    a missing one by timing or by message.
    """
    code = parse_code(text, prefix)
    row = conn.execute(
        f'SELECT id FROM recordings WHERE {NUMBER_COLUMN}=?', (code,)).fetchone()
    return row[0] if row else None
