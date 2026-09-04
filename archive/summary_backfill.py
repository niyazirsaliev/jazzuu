#!/usr/bin/env python3
"""Backfill markdown and structured summaries through asr-mcp.

Cron-friendly defaults: a non-blocking single-flight lock, bounded retries, and
one recording per run. Use --all to process every eligible recording.
"""
import json
import os
import re
import sqlite3
import sys
import time
HERE = os.path.dirname(os.path.abspath(__file__))
import urllib.request
sys.path.insert(0, HERE)
from mcp_sse import last_sse_json
import pipeline


def runtime_paths(env=None):
    env = os.environ if env is None else env
    db = (env.get("ARCHIVE_DB") or "").strip()
    if not db:
        tenant_id = (env.get("TENANT_ID") or "").strip()
        archive_dir = (env.get("TENANT_ARCHIVE_DIR") or "").strip()
        if tenant_id and not archive_dir:
            raise SystemExit(
                f"summary_backfill: TENANT_ID={tenant_id} is set without "
                "TENANT_ARCHIVE_DIR or ARCHIVE_DB; refusing owner fallback"
            )
        db = os.path.join(archive_dir or HERE, "archive.db")
    home = os.path.dirname(os.path.abspath(db)) or HERE
    lock = ((env.get("SUMMARY_LOCK") or "").strip()
            or os.path.join(home, ".connector.lock"))
    token_file = ((env.get("ASR_TOKEN_FILE") or "").strip()
                  or os.path.join(home, ".asr_token"))
    return db, lock, token_file


def scoped_token_var(tenant_id):
    return f'ASR_MCP_TOKEN_{re.sub(r"[^A-Z0-9]", "_", (tenant_id or "").upper())}'


def runtime_token(env=None, token_file=None):
    env = os.environ if env is None else env
    if token_file is None:
        token_file = runtime_paths(env)[2]
    tenant_id = (env.get("TENANT_ID") or "").strip()
    name = scoped_token_var(tenant_id) if tenant_id else "ASR_MCP_TOKEN"
    token = (env.get(name) or "").strip()
    if token:
        return token
    if token_file and os.path.exists(token_file):
        try:
            with open(token_file, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""
    return ""


DB, LOCK, TOKEN_FILE = runtime_paths()
URL = os.environ.get("ASR_MCP_URL", "http://127.0.0.1:62362/mcp")
TOKEN = runtime_token(token_file=TOKEN_FILE)
MAX_ATTEMPTS = int(os.environ.get("SUMMARY_MAX_ATTEMPTS", "3"))
HTTP_TIMEOUT = int(os.environ.get("SUMMARY_HTTP_TIMEOUT", "900"))
# Each normal connector drain admits only a bounded share of legacy summaries.
# The durable queue preserves the remainder for later passes rather than turning
# a restart into an unbounded model-call burst.
CATEGORY_BACKFILL_BATCH = max(1, int(os.environ.get("SUMMARY_CATEGORY_BACKFILL_BATCH", "10")))


def log(message):
    print(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} summary_backfill: {message}', flush=True)


def _post(body, sid=None, timeout=120):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    request = urllib.request.Request(
        URL, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
        sid = response.headers.get("Mcp-Session-Id", sid)
        content_type = response.headers.get("Content-Type", "")
    result = None
    if "text/event-stream" in content_type:
        result = last_sse_json(raw)
    else:
        result = json.loads(raw) if raw.strip() else {}
    return result, sid


def mcp_connect():
    _, sid = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "summary-backfill", "version": "1"},
            },
        }
    )
    # Stateless MCP endpoints legally omit Mcp-Session-Id. Never put None into
    # urllib headers: it raises TypeError before the initialized notification is
    # sent and silently prevents every scheduled summary run from starting.
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    body = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    urllib.request.urlopen(
        urllib.request.Request(
            URL, data=json.dumps(body).encode(), headers=headers, method="POST"
        ),
        timeout=15,
    ).read()
    return sid


class McpToolError(RuntimeError):
    """An MCP summary tool returned an error result, never summary data."""


def _successful_tool_payload(value):
    # FastMCP wraps typed tool return values in a single ``result`` field.
    # Unwrap that transport shape before applying the domain contract.
    if (isinstance(value, dict) and set(value) == {"result"}
            and isinstance(value["result"], dict)):
        value = value["result"]
    # FastMCP tools may return a domain error as structured content inside a
    # successful JSON-RPC envelope. Never let its detail become an empty
    # summary or leak into the durable retry ledger.
    if isinstance(value, dict) and value.get("status") == "error":
        raise McpToolError("summary MCP tool call failed")
    return value


def call(name, args, sid, timeout=HTTP_TIMEOUT):
    response, _ = _post(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        sid,
        timeout,
    )
    result = (response or {}).get("result") or {}
    if result.get("isError"):
        raise McpToolError("summary MCP tool call failed")
    if result.get("structuredContent") is not None:
        return _successful_tool_payload(result["structuredContent"])
    content = result.get("content") or []
    if content:
        text = content[0].get("text", "")
        try:
            return _successful_tool_payload(json.loads(text))
        except (TypeError, ValueError):
            return {"summary": text}
    raise RuntimeError(f"unexpected mcp response: {json.dumps(response)[:300]}")


def semantic_title_from_structured(structured):
    """Return the summary's safe generated title, never a guessed source name."""
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except (TypeError, ValueError):
            return None
    if not isinstance(structured, dict):
        return None
    title = structured.get("title")
    if not isinstance(title, str):
        return None
    title = re.sub(r"\s+", " ", title).strip()
    return title[:300] or None


def _promote_local_semantic_title(conn, rid, title):
    """Make a transcript-derived title canonical for a local source.

    PLAUD's provider name remains authoritative for provider recordings. Local
    basenames are different: they are private provenance and must never remain
    in reader-facing ``name`` or FTS metadata after enrichment.
    """
    if not title:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
    if "plaud_meta_json" not in columns:
        return
    row = conn.execute(
        "SELECT COALESCE(plaud_meta_json,'') FROM recordings WHERE id=?", (rid,)
    ).fetchone()
    try:
        metadata = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        metadata = {}
    if (not isinstance(metadata, dict)
            or metadata.get("source_kind") not in {"nextcloud_external_import", "browser_upload"}):
        return
    conn.execute("UPDATE recordings SET name=? WHERE id=?", (title, rid))
    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recordings_fts'"
    ).fetchone()
    if not has_fts:
        return
    updated = conn.execute(
        "UPDATE recordings_fts SET name=? WHERE id=?", (title, rid)
    )
    if updated.rowcount == 0:
        transcript = conn.execute(
            "SELECT COALESCE(NULLIF(asr_transcript,''),plaud_transcript,'') "
            "FROM recordings WHERE id=?",
            (rid,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)",
            (rid, title, transcript),
        )


def ensure_label_schema(conn):
    """Create connector-owned multi-label state; safe for older archives."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    conn.execute('''CREATE TABLE IF NOT EXISTS label_definitions(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    conn.executemany('''INSERT OR IGNORE INTO label_definitions(id,name,kind,created_at)
        VALUES(?,?,?,?)''', (("personal", "Личное", "system", stamp),
                             ("business", "Работа", "system", stamp),
                             ("idea", "Идея", "system", stamp)))
    conn.execute('''CREATE TABLE IF NOT EXISTS recording_labels(
        recording_id TEXT NOT NULL, label_id TEXT NOT NULL,
        auto_assigned INTEGER NOT NULL DEFAULT 0 CHECK(auto_assigned IN (0,1)),
        manual_override INTEGER CHECK(manual_override IN (0,1)),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(recording_id,label_id))''')
    pipeline.ensure_label_catalog_schema(conn)


def label_catalogue(conn):
    ensure_label_schema(conn)
    return [dict(id=row[0], name=row[1]) for row in conn.execute(
        'SELECT id,name FROM label_definitions ORDER BY id')]


def publish_auto_labels(conn, rid, structured, catalogue_ids=None):
    ensure_label_schema(conn)
    raw = structured.get("categories", []) if isinstance(structured, dict) else []
    known = set(catalogue_ids or (row[0] for row in conn.execute('SELECT id FROM label_definitions')))
    selected = {value for value in raw if value in known}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for label_id in known:
        row = conn.execute(
            "SELECT manual_override FROM recording_labels WHERE recording_id=? AND label_id=?",
            (rid, label_id)).fetchone()
        manual = row[0] if row else None
        conn.execute('''INSERT INTO recording_labels(
            recording_id,label_id,auto_assigned,manual_override,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(recording_id,label_id) DO UPDATE SET
            auto_assigned=excluded.auto_assigned,updated_at=excluded.updated_at''',
            (rid, label_id, int(label_id in selected), manual, stamp))


def ensure_attempts(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
    if "summary_json" not in columns:
        conn.execute("ALTER TABLE recordings ADD COLUMN summary_json TEXT")
    # `name` is PLAUD's official/source name. The generated semantic title is a
    # separate durable presentation field, never a source metadata rewrite.
    if "semantic_title" not in columns:
        conn.execute("ALTER TABLE recordings ADD COLUMN semantic_title TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS summary_attempts(
           id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
           last_at TEXT, last_error TEXT)"""
    )
    ensure_label_schema(conn)
    # Existing structured summaries already have model titles and categories. This
    # idempotent local migration fills only connector-owned projections and makes
    # no model calls; manual label overlays retain precedence.
    for rid, raw in conn.execute(
            "SELECT id,summary_json FROM recordings "
            "WHERE TRIM(COALESCE(summary_json,''))<>''").fetchall():
        title = semantic_title_from_structured(raw)
        if title:
            conn.execute("UPDATE recordings SET semantic_title=? WHERE id=? "
                         "AND TRIM(COALESCE(semantic_title,''))=''", (title, rid))
        try:
            structured = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            structured = None
        if isinstance(structured, dict):
            publish_auto_labels(conn, rid, structured)
            # Migration projection for already-category-capable summaries is
            # local-only: do not restart the active historical model backfill.
            if "categories" in structured:
                conn.execute("UPDATE recordings SET label_catalog_revision=COALESCE(label_catalog_revision,?) WHERE id=?",
                             (pipeline.label_catalog_revision(conn), rid))
    conn.commit()


def enqueue_missing_category_summaries(conn, batch_size=CATEGORY_BACKFILL_BATCH):
    """Durably admit a bounded batch of legacy summaries for model replacement.

    Only valid JSON objects which *lack* the authoritative ``categories`` key
    qualify.  This deliberately does not infer labels from prose, touch ASR, or
    rewrite reader-visible summary data: ``request_regenerate`` queues the
    canonical forced summary replacement and publication remains claim-fenced.
    Archived rows remain ordinary recordings and are included; tombstoned rows
    are absent from ``recordings`` and therefore cannot be admitted.
    """
    batch_size = max(1, int(batch_size))
    pipeline.ensure_schema(conn)
    queued = 0
    rows = conn.execute(
        """SELECT id,summary_json FROM recordings
            WHERE TRIM(COALESCE(summary_json,''))<>''
            ORDER BY COALESCE(start_at,created_at) ASC,id ASC""").fetchall()
    for rid, raw in rows:
        if queued >= batch_size:
            break
        try:
            structured = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(structured, dict) or "categories" in structured:
            continue
        job = conn.execute(
            "SELECT state FROM pipeline_jobs WHERE recording_id=? AND stage=?",
            (rid, pipeline.STAGE_SUMMARY)).fetchone()
        # Open work already carries the replacement intent, and a terminal
        # failure must remain visible instead of being silently revived every
        # connector pass. A completed pre-category summary is the exact legacy
        # state this migration replaces.
        if job and job[0] not in (pipeline.JOB_DONE,):
            continue
        if pipeline.request_regenerate(conn, rid, commit=False):
            queued += 1
    conn.commit()
    return queued


def pending(conn, include_exhausted=False, force=False):
    summary_filter = "" if force else \
        "AND (COALESCE(r.summary,'')='' OR COALESCE(r.summary_json,'')='')"
    rows = conn.execute(
        f"""SELECT r.id,r.name,
                  COALESCE(NULLIF(r.asr_transcript,''),r.plaud_transcript,'') transcript,
                  COALESCE(a.attempts,0) attempts
           FROM recordings r LEFT JOIN summary_attempts a ON a.id=r.id
           WHERE LENGTH(TRIM(COALESCE(NULLIF(r.asr_transcript,''),r.plaud_transcript,''))) >= 200
             {summary_filter}
           ORDER BY COALESCE(r.start_at,r.created_at) ASC,r.id ASC"""
    ).fetchall()
    return [row for row in rows if include_exhausted or row[3] < MAX_ATTEMPTS]


def bump(conn, rid, error):
    conn.execute(
        """INSERT INTO summary_attempts(id,attempts,last_at,last_error)
           VALUES(?,1,?,?) ON CONFLICT(id) DO UPDATE SET
           attempts=attempts+1,last_at=excluded.last_at,last_error=excluded.last_error""",
        (rid, time.strftime("%Y-%m-%dT%H:%M:%S%z"), str(error)[:500]),
    )
    conn.commit()


def _normalize_result(result):
    if not isinstance(result, dict):
        raise RuntimeError("summary result is not an object")
    if isinstance(result.get("result"), dict):
        result = result["result"]
    markdown = result.get("summary") or result.get("summary_markdown") or result.get("markdown")
    structured = (
        result.get("summary_json")
        or result.get("structured_summary")
        or result.get("structured")
    )
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except ValueError as exc:
            raise RuntimeError("summary_json is invalid JSON") from exc
    if structured is None:
        keys = ("overview", "themes", "key_facts", "decisions", "risks", "action_items")
        if any(key in result for key in keys):
            structured = {key: result.get(key) for key in keys if key in result}
    if not isinstance(markdown, str) or not markdown.strip():
        raise RuntimeError("empty markdown summary")
    if not isinstance(structured, dict) or not structured:
        raise RuntimeError("missing structured summary")
    return markdown.strip(), structured


def _claim_active(conn, job):
    if job is None:
        return True
    cur = conn.execute(
        """UPDATE pipeline_jobs SET progress_epoch=progress_epoch
           WHERE seq=? AND state='processing' AND claim_epoch=? AND claim_owner=?""",
        (job["seq"], job["claim_epoch"], job["claim_owner"]),
    )
    return cur.rowcount == 1


SUMMARY_MIN_CHARS = 200
IDEA_MIN_CHARS = 100
# Spoken cues that open a dictated idea. Deliberately literal: a mobile dictated
# idea is short, and a missed cue only costs the fallback behaviour.
IDEA_CUE = re.compile(r"\b(иде[яию]|мысл[ьи]|придумал|надо сделать|idea)\b", re.I)


def _min_summary_chars(transcript):
    """Idea dictations earn a lower bar so they get a title, not a bare date.

    The 200-char floor keeps the summariser off silence-hallucination noise
    ("Thank you."), but it also silenced real content: production N-0162 is a
    138-char idea that stayed titled with its raw timestamp. A recording that
    opens by announcing an idea is the case the floor was never meant to catch.
    """
    head = re.sub(r"\[Speaker \d+\]", "", transcript or "")[:80]
    return IDEA_MIN_CHARS if IDEA_CUE.search(head) else SUMMARY_MIN_CHARS


def backfill_one(conn, sid, rid, force=False, transcript=None, job=None):
    ensure_attempts(conn)
    row = conn.execute(
        """SELECT name,COALESCE(NULLIF(asr_transcript,''),plaud_transcript,'')
           FROM recordings WHERE id=?""",
        (rid,),
    ).fetchone()
    if not row:
        raise RuntimeError("recording not found")
    title = row[0] or ""
    transcript = (row[1] if transcript is None else transcript or "").strip()
    if len(transcript) < _min_summary_chars(transcript):
        raise RuntimeError("transcript is shorter than 200 characters")
    catalogue = label_catalogue(conn)
    catalogue_ids = {item["id"] for item in catalogue}
    catalogue_revision = (int(job.get("catalog_revision")) if job and job.get("catalog_revision") is not None
                          else pipeline.label_catalog_revision(conn))
    result = call(
        "summarize_transcript",
        {"transcript": transcript, "title": title, "label_catalogue": catalogue},
        sid,
    )
    markdown, structured = _normalize_result(result)
    semantic_title = semantic_title_from_structured(structured)
    encoded = json.dumps(structured, ensure_ascii=False)
    # Summary and semantic title must be published by the claim that generated
    # them. Keep the fence inside this UPDATE: a separate claim check permits a
    # stale worker to win between check and write.
    fence_sql, fence_args = "", ()
    if job is not None:
        fence_sql = """ AND EXISTS(
            SELECT 1 FROM pipeline_jobs p WHERE p.seq=? AND p.state='processing'
              AND COALESCE(p.claim_owner,'')=? AND COALESCE(p.claim_epoch,0)=?)"""
        fence_args = (job["seq"], job.get("claim_owner") or "",
                      int(job.get("claim_epoch") or 0))
    if force:
        cur = conn.execute(
            """UPDATE recordings SET summary=?,summary_json=?,
               semantic_title=CASE WHEN ? IS NULL THEN semantic_title ELSE ? END
               WHERE id=?""" + fence_sql,
            (markdown, encoded, semantic_title, semantic_title, rid) + fence_args)
    else:
        cur = conn.execute(
            """UPDATE recordings
               SET summary=CASE WHEN TRIM(COALESCE(summary,''))=''
                                THEN ? ELSE summary END,
                   summary_json=CASE WHEN TRIM(COALESCE(summary_json,''))=''
                                     THEN ? ELSE summary_json END,
                   semantic_title=CASE WHEN TRIM(COALESCE(semantic_title,''))=''
                                          AND ? IS NOT NULL THEN ?
                                       ELSE semantic_title END WHERE id=?""" + fence_sql,
            (markdown, encoded, semantic_title, semantic_title, rid) + fence_args)
    if job is not None and cur.rowcount != 1:
        conn.rollback()
        return False
    _promote_local_semantic_title(conn, rid, semantic_title)
    publish_auto_labels(conn, rid, structured, catalogue_ids)
    # A catalog change during the call leaves this row eligible for its newer
    # generation, rather than clearing newer definitions from an old snapshot.
    if catalogue_revision == pipeline.label_catalog_revision(conn):
        conn.execute("UPDATE recordings SET label_catalog_revision=? WHERE id=?", (catalogue_revision, rid))
    conn.execute("DELETE FROM summary_attempts WHERE id=?", (rid,))
    conn.commit()
    log(f"OK {rid} summary={len(markdown)}chars")
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    do_all = "--all" in argv
    force = "--force" in argv
    ids = [arg for arg in argv if not arg.startswith("--")]
    if not TOKEN:
        log("FATAL no ASR_MCP_TOKEN (env or .asr_token)")
        return 1
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    try:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        log('REFUSED archive connector lock is held')
        return 75
    conn = sqlite3.connect(DB, timeout=60)
    try:
        ensure_attempts(conn)
        if ids:
            targets = [(rid, "", "", 0) for rid in ids]
        else:
            targets = pending(conn, force=force)
            if not do_all:
                targets = targets[:1]
        if not targets:
            return 0
        sid = mcp_connect()
        for rid, _name, _text, _attempts in targets:
            try:
                backfill_one(conn, sid, rid, force=force)
            except Exception as exc:  # cron must continue with --all
                bump(conn, rid, f"{type(exc).__name__}: {exc}")
                log(f"FAIL {rid}: {type(exc).__name__}: {exc}")
        return 0
    finally:
        conn.close()
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
