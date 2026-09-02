import base64
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import (
    JSONResponse,
    FileResponse,
    RedirectResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import (asr_progress, control_client, mindmap, mindtree, public_share,
               recording_number, identity_api, search as recordings_search,
               semantic_adapter, speakers, summary_card)

ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/archive")
DB_PATH = os.path.join(ARCHIVE_DIR, "archive.db")
AUDIO_DIR = os.path.join(ARCHIVE_DIR, "audio")
STATIC_DIR = str(Path(__file__).parent / "static")
SECRET = os.environ.get("RECORDINGS_MAGIC_SECRET", "")
LEGACY_MAGIC_TOKENS = tuple(
    token.strip()
    for token in os.environ.get("RECORDINGS_LEGACY_MAGIC_TOKENS", "").split(",")
    if token.strip()
)
COOKIE_NAME = "rec_auth"

# Mind maps are derived data: rendered on first view from text already in the
# DB and cached as files (the archive volume is read-only and the schema is
# not ours to extend). MINDMAP_DIR is a small writable volume.
MINDMAP_DIR = os.environ.get("MINDMAP_DIR", "/cache/mindmaps")
MINDMAP_VERSION = "1"  # bump to invalidate every cached PNG
_mindmap_lock = threading.Lock()
SUMMARY_CARD_DIR = os.environ.get("SUMMARY_CARD_DIR", "/cache/summary-cards")
SUMMARY_CARD_VERSION = "4"
_summary_card_lock = threading.Lock()

# Human-facing public links are an owner-only export boundary. Family viewers
# run the same immutable image but never receive the enable flag, secret or
# writable export mount.
PUBLIC_SHARE_ENABLED = os.environ.get("PUBLIC_SHARE_ENABLED", "0").lower() in {
    "1", "true", "yes",
}
TENANT_ID = os.environ.get("TENANT_ID", "").strip().lower()
PUBLIC_SHARE_STORE = os.environ.get("PUBLIC_SHARE_STORE", "/public-shares")
PUBLIC_SHARE_BASE_URL = os.environ.get("PUBLIC_SHARE_BASE_URL", "").strip()


def _configured_public_share_secret() -> str:
    secret_file = os.environ.get("PUBLIC_SHARE_SECRET_FILE", "")
    if secret_file:
        try:
            return Path(secret_file).read_text().strip()
        except OSError:
            return ""
    return ""


PUBLIC_SHARE_SECRET = _configured_public_share_secret()
_public_reconciler_stop = threading.Event()
_public_reconciler_started = False


def _public_share_configured() -> bool:
    if (
        not PUBLIC_SHARE_ENABLED
        or not TENANT_ID
        or not PUBLIC_SHARE_SECRET
    ):
        return False
    try:
        public_share.validate_base_url(PUBLIC_SHARE_BASE_URL)
    except RuntimeError:
        return False
    return True

# Mutations go only through the connector-owned private control client.
# Compatibility name only; it is never opened or written by this viewer.
SPEAKER_STORE_PATH = None

# A reprocess request or a rename may only carry as many speakers as a recording
# can actually have (speakers.MAX_SPEAKERS); anything larger is not a reader.
MAX_NAMES_PER_REQUEST = speakers.MAX_SPEAKERS
MAX_COMMENT_LEN = 2000

# Every message a mutation may show. Russian, about the reader's own request,
# and deliberately free of engine names, paths, ids and exception text.
MSG_RU = {
    "queued_transcript": "Запрос принят: перетранскрибирование поставлено в очередь.",
    "queued_materials": "Запрос принят: материалы будут пересобраны.",
    "queued_diarize": "Запрос принят: распознавание спикеров поставлено в очередь.",
    "already": "Уже в очереди — повторное нажатие ничего не меняет.",
    "names_saved": "Имена сохранены. Материалы будут пересобраны.",
    "bad_request": "Не удалось обработать запрос: проверьте имена спикеров.",
    "bad_action": "Неизвестное действие.",
    "bad_id": "Некорректный идентификатор записи.",
    "not_found": "Запись не найдена.",
    "no_csrf": "Сессия устарела. Обновите страницу и повторите.",
    "unavailable": "Сервис временно не может принять запрос. Попробуйте позже.",
    "no_speaker": "Такого спикера в этой записи нет.",
}

# Set at image build time (Dockerfile ARG); shows which code is actually live.
BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

# Recording timestamps are stored as an instant and never rewritten. Each
# tenant's display zone is explicit configuration, never a browser guess or a
# fixed offset. An unset or unknown zone keeps the previous behaviour.
def _configured_display_timezone():
    name = os.environ.get("DISPLAY_TIMEZONE", "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


DISPLAY_TIMEZONE = _configured_display_timezone()


def _utc_instant(value):
    """The stored timestamp as an explicit UTC instant, or ``None``.

    A naive value carries no offset but is written in UTC by the connector, so
    it is read as UTC rather than as server-local time.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()



@asynccontextmanager
async def _lifespan(application):
    del application
    if _public_share_configured():
        start_public_share_reconciler()
    try:
        yield
    finally:
        _public_reconciler_stop.set()


app = FastAPI(title="Jazzuu", lifespan=_lifespan)


def expected_token() -> str:
    import base64
    digest = hmac.new(SECRET.encode(), b"jazzuu-viewer", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def valid_magic_token(token: str) -> bool:
    if not SECRET or not token:
        return False
    candidates = (expected_token(), *LEGACY_MAGIC_TOKENS)
    return any(hmac.compare_digest(token, candidate) for candidate in candidates)


# The archive DB is mounted read-only and is in WAL mode. Copying db then WAL is
# racy: a checkpoint between copies can make a mismatched pair. SQLite's backup
# API reads a consistent snapshot through a read-only source connection instead.
_snap = {"key": None, "path": None}
_snap_lock = threading.Lock()


def _wal_key():
    wal = DB_PATH + "-wal"
    try:
        st = os.stat(wal) if os.path.exists(wal) else os.stat(DB_PATH)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _snapshot_path():
    key = _wal_key()
    with _snap_lock:
        if _snap["path"] and os.path.exists(_snap["path"]) and _snap["key"] == key:
            return _snap["path"]
        tmpd = tempfile.mkdtemp(prefix="recdb_")
        dst = os.path.join(tmpd, "archive.db")
        # A WAL-mode database whose WAL/SHM were checkpointed away still asks
        # SQLite to create shared-memory files. The archive mount is deliberately
        # read-only, so open the stable main file as immutable only in that case.
        # When a WAL exists, keep the normal read-only connection so committed
        # WAL pages are included in the backup rather than silently ignored.
        wal = DB_PATH + "-wal"
        immutable = "&immutable=1" if not os.path.exists(wal) else ""
        source = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro{immutable}", uri=True, timeout=5)
        try:
            target = sqlite3.connect(dst)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        old = _snap["path"]
        _snap["key"], _snap["path"] = key, dst
        if old and os.path.dirname(old) != tmpd:
            shutil.rmtree(os.path.dirname(old), ignore_errors=True)
        return dst


def db():
    conn = sqlite3.connect(_snapshot_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


# Main owns API wiring only. Lexical and semantic responsibilities stay in
# replaceable providers, and the lambda resolves the active tenant DB per call.
SEARCH_ADAPTER = semantic_adapter.ViewerSearchAdapter(
    lambda: recordings_search.TenantFtsSearchProvider(db)
)


def csrf_token() -> str:
    """A per-secret token a cross-site page cannot read or guess.

    The session cookie is HttpOnly (so JS cannot echo it back) and SameSite=Lax
    (so a cross-site POST does not carry it at all). This is the second lock:
    every mutation must present this value in a custom header, which a form post
    or an image tag cannot set, and which only a same-origin fetch can obtain
    from /api/csrf. Derived from the same secret, so there is no session state to
    keep, and deliberately a DIFFERENT digest from the auth cookie — echoing the
    cookie's own value back through a header would make an XSS a full session
    leak instead of one request.
    """
    import base64
    digest = hmac.new(SECRET.encode(), b"csrf:jazzuu-viewer", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def csrf_ok(request: Request) -> bool:
    if not SECRET:
        return False
    presented = request.headers.get("x-csrf-token") or ""
    if not presented:
        return False
    return hmac.compare_digest(presented, csrf_token())


def authed(request: Request) -> bool:
    tok = request.cookies.get(COOKIE_NAME)
    return valid_magic_token(tok or "")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path == "/api/health":
        return await call_next(request)
    if path.startswith("/api") or path.startswith("/audio"):
        if not authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    if PUBLIC_SHARE_ENABLED and not _public_share_configured():
        raise HTTPException(status_code=503, detail="public share is not configured")
    conn = db()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/login")
def login(t: str = ""):
    if not valid_magic_token(t):
        return HTMLResponse(
            "<html><head><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width,initial-scale=1'></head>"
            "<body style='font-family:system-ui;background:#0b0b0f;color:#e5e5ea;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><div style='font-size:48px'>🔒</div>"
            "<h2>Недействительная ссылка</h2>"
            "<p style='color:#8e8e93'>Запросите новую magic-ссылку.</p></div></body></html>",
            status_code=401,
        )
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        expected_token(),
        httponly=True,
        samesite="lax",
        secure=os.environ.get("VIEWER_DEVELOPMENT_INSECURE_COOKIE") != "1",
        max_age=60 * 60 * 24 * 365,
        path="/",
    )
    return resp


def _summary_md(s):
    """Full summary with only PLAUD image artifacts stripped (markdown kept,
    rendered client-side in the detail view)."""
    if not s:
        return None
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s).strip()
    return s or None


def _summary_plain(s):
    """Clean one-line plaintext for the feed card (markdown stripped)."""
    if not s:
        return None
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)          # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)       # links -> text
    s = re.sub(r"`{1,3}", "", s)                          # code ticks
    s = re.sub(r"[*_]{1,3}", "", s)                       # emphasis
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)   # headings
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)        # blockquotes
    s = re.sub(r"^\s{0,3}[-*+]\s+", "", s, flags=re.M)    # bullets
    s = re.sub(r"^\s{0,3}\d+\.\s+", "", s, flags=re.M)    # ordered
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _safe_json_object(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _asr_route(raw):
    meta = _safe_json_object(raw)
    if not meta:
        return {}
    route = meta.get("route") if isinstance(meta.get("route"), dict) else {}
    sources = (meta, route)
    aliases = {
        "selected_engine": ("selected_engine", "engine_used", "engine_selected"),
        "requested_engine": ("requested_engine", "engine_requested"),
        "route_reason": ("route_reason", "reason", "routing_reason"),
        "language": ("language", "lang", "detected_language", "detected_lang"),
    }
    result = {}
    for output_key, candidates in aliases.items():
        for source in sources:
            value = next((source.get(key) for key in candidates if source.get(key) is not None), None)
            if isinstance(value, (str, int, float, bool)):
                result[output_key] = value
                break
    return result


def _table_columns(conn, table="recordings"):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _parse_segments(raw):
    """Parse plaud_segments_json into a clean list of speaker turns.
    Returns [] on missing/invalid data."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for s in data:
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        out.append(
            {
                "speaker": s.get("speaker") or "",
                "start_ms": s.get("start_ms"),
                "end_ms": s.get("end_ms"),
                "text": text,
            }
        )
    return out


# ---------------- canonical read-only aliases and pipeline status ----------------

def _speaker_names(store_conn, rec_id):
    del store_conn
    conn = db()
    try:
        try:
            rows = conn.execute("SELECT speaker_id,display_name FROM speaker_aliases WHERE recording_id=?", (rec_id,)).fetchall()
        except sqlite3.Error:
            return {}
        return {row["speaker_id"]: row["display_name"] for row in rows}
    finally:
        conn.close()


def _speaker_label_names(store_conn, rec_id):
    del store_conn
    conn = db()
    try:
        try:
            rows = conn.execute("SELECT speaker_id,display_name,source_label FROM speaker_aliases WHERE recording_id=?", (rec_id,)).fetchall()
        except sqlite3.Error:
            return {}
        return {(row["source_label"] or row["speaker_id"]): row["display_name"] for row in rows}
    finally:
        conn.close()


def _pipeline_status(rec_id):
    conn = db()
    try:
        try:
            rows = conn.execute("SELECT stage,state FROM pipeline_jobs WHERE recording_id=?", (rec_id,)).fetchall()
        except sqlite3.Error:
            return {}
        return {row["stage"]: {"state": row["state"]} for row in rows}
    finally:
        conn.close()


def _voiceprint_scores(rec_id):
    """Accepted presentation labels only; embeddings never cross into the viewer."""
    model = os.environ.get("VOICEPRINT_MODEL", "").strip()
    version = os.environ.get("VOICEPRINT_MODEL_VERSION", "").strip()
    if not (model and version):
        return {}
    conn = db()
    try:
        try:
            rows = conn.execute('''SELECT source_label,identity,confidence,margin
                FROM voiceprint_segment_scores WHERE recording_id=? AND model=? AND model_version=?
                ORDER BY scored_at DESC''', (rec_id, model, version)).fetchall()
        except sqlite3.Error:
            return {}
        scores = {}
        for row in rows:
            if row["source_label"] not in scores and row["identity"]:
                scores[row["source_label"]] = {"identity": row["identity"],
                    "confidence": row["confidence"], "margin": row["margin"]}
        return scores
    finally:
        conn.close()


def _speaker_model(row, names, pending=False):
    """The truthful speaker payload for one archive row."""
    keys = row.keys()
    return speakers.speaker_model(
        segments=_parse_segments(row["plaud_segments_json"]
                                 if "plaud_segments_json" in keys else None),
        asr_meta=_safe_json_object(row["asr_meta_json"]
                                   if "asr_meta_json" in keys else None),
        duration_ms=row["duration_ms"] if "duration_ms" in keys else None,
        names=names,
        pending=pending,
    )


def _present_voiceprint_labels(model, scores):
    """Overlay accepted identity facts without changing diarization labels."""
    if not scores or not isinstance(model, dict):
        return model
    model = dict(model)
    model["speakers"] = [{**speaker, "voiceprint": scores.get(speaker.get("source_label"))}
                         for speaker in model.get("speakers", [])]
    return model


def _mindmap_source(rec_id):
    """Indented mind-map text for a recording, or None if there is nothing
    worth drawing."""
    conn = db()
    try:
        r = conn.execute(
            "SELECT name,summary,summary_json,plaud_transcript,asr_transcript "
            "FROM recordings WHERE id=?",
            (rec_id,),
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    transcript = (r["asr_transcript"] or "") or (r["plaud_transcript"] or "")
    # Mind-map labels get the reader's speaker names: the map is derived data,
    # rendered fresh here, while the transcript row it is derived FROM keeps its
    # raw PLAUD/ASR labels untouched (the archive is mounted read-only anyway).
    transcript = speakers.apply_names_to_text(transcript, _speaker_label_names(None, rec_id))
    return _mindmap_tree(r["name"], r["summary_json"], r["summary"], transcript)


def _mindmap_tree(name, summary_json, original_summary, transcript):
    data = _safe_json_object(summary_json)
    if data:
        tree = mindtree.build_structured_tree(name, data, transcript)
        if tree:
            return tree
    return mindtree.build_tree(name, original_summary, transcript)


def _derived_revision(version, name, source):
    return hashlib.sha1(
        (version + "\n" + (name or "") + "\n" + (source or "")).encode("utf-8")
    ).hexdigest()[:12]


def _mindmap_png(rec_id):
    """Path to the cached PNG, rendering it once if needed. None = no map."""
    tree = _mindmap_source(rec_id)
    if not tree:
        return None
    digest = hashlib.sha1(
        (MINDMAP_VERSION + "\n" + tree).encode("utf-8")
    ).hexdigest()[:12]
    path = os.path.join(MINDMAP_DIR, f"{rec_id}.{digest}.png")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    with _mindmap_lock:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        try:
            os.makedirs(MINDMAP_DIR, exist_ok=True)
            tmp = path + ".tmp"
            # title stays empty: the page header and the root node already
            # carry the recording name
            mindmap.render_png(tree, None, tmp)
            os.replace(tmp, path)
        except Exception:
            return None
        # drop stale renders of the same recording
        for old in glob.glob(os.path.join(MINDMAP_DIR, f"{rec_id}.*.png")):
            if old != path:
                try:
                    os.remove(old)
                except OSError:
                    pass
    return path


def _summary_card_source(rec_id):
    conn = db()
    try:
        columns = _table_columns(conn)
        if "summary_json" not in columns:
            return None
        title_sql = "COALESCE(NULLIF(TRIM(semantic_title),''),name)" \
            if "semantic_title" in columns else "name"
        row = conn.execute(
            f"SELECT {title_sql} AS title,summary_json FROM recordings WHERE id=?",
            (rec_id,),
        ).fetchone()
        data = _safe_json_object(row["summary_json"]) if row else None
        tasks = _generated_tasks(conn, rec_id, data) if data else []
    finally:
        conn.close()
    if not row:
        return None
    model = summary_card.content_model(data, tasks)
    return (row["title"] or data.get("title") or "Без названия", model) if data else None


def _summary_card_png(rec_id):
    source = _summary_card_source(rec_id)
    if not source:
        return None
    title, data = source
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(
        (SUMMARY_CARD_VERSION + "\n" + title + "\n" + serialized).encode("utf-8")
    ).hexdigest()[:12]
    path = os.path.join(SUMMARY_CARD_DIR, f"{rec_id}.{digest}.png")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    with _summary_card_lock:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        try:
            os.makedirs(SUMMARY_CARD_DIR, exist_ok=True)
            tmp = path + ".tmp"
            summary_card.render_summary_card(data, title, tmp)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(path + ".tmp")
            except OSError:
                pass
            return None
        for old in glob.glob(os.path.join(SUMMARY_CARD_DIR, f"{rec_id}.*.png")):
            if old != path:
                try:
                    os.remove(old)
                except OSError:
                    pass
    return path


@app.get("/api/recordings/{rec_id}/summary-card.png")
def summary_card_png(rec_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rec_id):
        raise HTTPException(status_code=400, detail="bad id")
    path = _summary_card_png(rec_id)
    if not path:
        raise HTTPException(status_code=404, detail="no structured summary")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, no-cache, max-age=0, must-revalidate"},
    )


@app.get("/api/recordings/{rec_id}/mindmap.png")
def mindmap_png(rec_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rec_id):
        raise HTTPException(status_code=400, detail="bad id")
    path = _mindmap_png(rec_id)
    if not path:
        raise HTTPException(status_code=404, detail="no mindmap")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, no-cache, max-age=0, must-revalidate"},
    )


@app.get("/api/recordings")
def list_recordings(limit: int = 50, offset: int = 0, archived: bool = False,
                    page: str = "", cursor: str = "", lang: str = "ru"):
    requested_language = "en" if lang == "en" else "ru"
    cursor_mode = page == "cursor"
    limit = 20 if cursor_mode else max(1, min(limit, 200))
    offset = 0 if cursor_mode else max(0, offset)
    after = None
    if cursor_mode and cursor:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            value = json.loads(raw.decode("utf-8"))
            if (not isinstance(value, list) or len(value) not in {2, 3}
                    or not all(isinstance(item, str) and len(item) <= 128 for item in value[:2])
                    or (len(value) == 3 and (not isinstance(value[2], int) or value[2] < 0))):
                raise ValueError("bad cursor")
            after = tuple(value)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="bad cursor")
    conn = db()
    try:
        columns = _table_columns(conn)
        code_expr = recording_number.select_expr(columns)
        title_expr = ("semantic_title" if "semantic_title" in columns
                      else "NULL AS semantic_title")
        summary_json_expr = ("summary_json" if "summary_json" in columns else "NULL AS summary_json")
        archive_clause = ("archived_local_at IS NOT NULL" if archived and "archived_local_at" in columns
                          else "(archived_local_at IS NULL OR archived_local_at='')" if "archived_local_at" in columns
                          else "1")
        sort_expr = "COALESCE(start_at, created_at, '')"
        params = []
        snapshot_rowid = after[2] if after and len(after) == 3 else (
            conn.execute("SELECT COALESCE(MAX(rowid),0) FROM recordings WHERE " + archive_clause).fetchone()[0]
            if cursor_mode else None)
        cursor_clause = ""
        if snapshot_rowid is not None:
            cursor_clause += " AND rowid <= ?"
            params.append(snapshot_rowid)
        if after:
            cursor_clause += f" AND ({sort_expr} < ? OR ({sort_expr} = ? AND id < ?))"
            params.extend((after[0], after[0], after[1]))
        params.extend((limit + 1 if cursor_mode else limit, offset))
        rows = conn.execute(
            f"SELECT id,name,{title_expr},start_at,duration_ms,lang,summary,{summary_json_expr},asr_engine,{code_expr},"
            "(TRIM(COALESCE(asr_transcript,''))<>'' OR "
            " TRIM(COALESCE(plaud_transcript,''))<>'') AS has_final_transcript,"
            "TRIM(COALESCE(asr_transcript,''))<>'' AS has_local_transcript,"
            "TRIM(COALESCE(plaud_transcript,''))<>'' AS has_plaud_transcript,"
            f"{sort_expr} AS sort_at FROM recordings WHERE " + archive_clause + cursor_clause +
            f" ORDER BY {sort_expr} DESC,id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        has_more = cursor_mode and len(rows) > limit
        visible_rows = rows[:limit]
        unfinished = [r["id"] for r in visible_rows if not r["has_final_transcript"]]
        progress = asr_progress.progress_map(conn, unfinished)
        statuses = asr_progress.status_map(conn, [
            {"id": r["id"], "has_local_transcript": r["has_local_transcript"],
             "has_plaud_transcript": r["has_plaud_transcript"], "engine": r["asr_engine"]}
            for r in visible_rows
        ])
        label_definitions, assigned_labels = _recording_labels(conn)
        variants = {}
        if requested_language == "en" and visible_rows:
            try:
                placeholders = ",".join("?" for _ in visible_rows)
                variants = {row["recording_id"]: row for row in conn.execute(
                    f"SELECT recording_id,summary,summary_json FROM recording_summary_variants WHERE language='en' AND recording_id IN ({placeholders})",
                    tuple(row["id"] for row in visible_rows))}
            except sqlite3.OperationalError:
                variants = {}
    finally:
        conn.close()
    out = []
    for r in visible_rows:
        variant = variants.get(r["id"])
        selected_json = _safe_json_object(variant["summary_json"]) if variant else (_safe_json_object(r["summary_json"]) if requested_language == "ru" else None)
        selected_markdown = variant["summary"] if variant else (r["summary"] if requested_language == "ru" else None)
        summ = _summary_plain(selected_markdown)
        summary_state = {"language": requested_language, "state": "ready", "retryable": False}
        if requested_language == "en" and not variant:
            if not (r["summary"] or "").strip():
                summary_state = {"language": "en", "state": "waiting_source", "retryable": False}
            else:
                job_state = _pipeline_status(r["id"]).get("summary_en", {}).get("state")
                if job_state == "failed":
                    summary_state = {"language": "en", "state": "failed", "retryable": True}
                elif job_state in {"queued", "processing", "retry_wait"}:
                    summary_state = {"language": "en", "state": job_state, "retryable": job_state == "retry_wait"}
                else:
                    try:
                        result = control_client.ControlClient().request("summary_en", r["id"])
                        summary_state = {"language": "en", "state": result.get("state", "queued"), "retryable": False}
                    except (control_client.ControlRejected, control_client.ControlUnavailable):
                        summary_state = {"language": "en", "state": "unavailable", "retryable": True}
        localized_title = (selected_json or {}).get("title") if isinstance(selected_json, dict) else None
        out.append({
            "id": r["id"], "name": localized_title or r["semantic_title"] or r["name"] or "Без названия",
            "title": localized_title or r["semantic_title"] or r["name"] or "Без названия",
            "source_name": r["name"],
            "recording_number": recording_number.public(r["recording_number"]),
            "start_at": r["start_at"],
            "start_at_utc": _utc_instant(r["start_at"]),
            "duration_ms": r["duration_ms"],
            "lang": r["lang"], "summary": (summ[:160] if summ else None),
            "summary_language": requested_language, "summary_state": summary_state,
            "asr_processing": progress.get(r["id"]),
            "transcript_status": statuses.get(r["id"]),
            "labels": assigned_labels.get(r["id"], []),
            "label_definitions": label_definitions,
        })
    if not cursor_mode:
        return out
    next_cursor = None
    if has_more and visible_rows:
        last = visible_rows[-1]
        payload = json.dumps([last["sort_at"], last["id"], snapshot_rowid], separators=(",", ":"))
        next_cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return {"items": out, "next_cursor": next_cursor,
            "display_timezone": DISPLAY_TIMEZONE}


def _generated_tasks(conn, rec_id, summary_data):
    """Return stable presentation ids plus connector-owned completion state."""
    raw_items = summary_data.get("action_items", []) if isinstance(summary_data, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    try:
        completed = {
            row[0]: bool(row[1]) for row in conn.execute(
                "SELECT task_id,completed FROM recording_task_states WHERE recording_id=?",
                (rec_id,),
            )
        }
    except sqlite3.OperationalError:
        completed = {}
    occurrences = {}
    tasks = []
    for raw in raw_items[:50]:
        obj = raw if isinstance(raw, dict) else {"task": raw}
        text = obj.get("task") or obj.get("text") or obj.get("title")
        if not isinstance(text, str):
            continue
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        identity = text.casefold()
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        task_id = hashlib.sha256(
            f"{identity}\0{occurrence}".encode("utf-8")
        ).hexdigest()[:24]
        tasks.append({
            "id": task_id,
            "text": text,
            "owner": str(obj.get("owner") or "").strip() or None,
            "due": str(obj.get("due") or obj.get("deadline") or "").strip() or None,
            "completed": completed.get(task_id, False),
        })
    return tasks


def _localized_tasks(summary_data, canonical_tasks):
    """Keep canonical task ids/status while presenting translated task text."""
    raw_items = summary_data.get("action_items", []) if isinstance(summary_data, dict) else []
    if not isinstance(raw_items, list):
        return []
    localized = []
    for index, raw in enumerate(raw_items[:50]):
        obj = raw if isinstance(raw, dict) else {"task": raw}
        text = obj.get("task") or obj.get("text") or obj.get("title")
        if not isinstance(text, str) or not (text := re.sub(r"\s+", " ", text).strip()):
            continue
        if index < len(canonical_tasks):
            base = canonical_tasks[index]
            localized.append({
                "id": base["id"], "text": text,
                "owner": str(obj.get("owner") or "").strip() or base.get("owner"),
                "due": str(obj.get("due") or obj.get("deadline") or "").strip() or base.get("due"),
                "completed": bool(base.get("completed")),
            })
    return localized


def _recording_comments(conn, rec_id):
    try:
        rows = conn.execute(
            "SELECT id,body,created_at FROM recording_comments "
            "WHERE recording_id=? ORDER BY id", (rec_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    return [{"id": row[0], "text": row[1], "created_at": row[2],
             "created_at_utc": _utc_instant(row[2])}
            for row in rows]


def _recording_labels(conn, rec_id=None):
    try:
        definitions = [dict(row) for row in conn.execute(
            "SELECT id,name,kind FROM label_definitions ORDER BY kind DESC,name COLLATE NOCASE")]
        rows = conn.execute(
            "SELECT recording_id,label_id,auto_assigned,manual_override FROM recording_labels" +
            (" WHERE recording_id=?" if rec_id else "") + " ORDER BY recording_id,label_id",
            ((rec_id,) if rec_id else ())).fetchall()
    except sqlite3.OperationalError:
        return [], {}
    assigned = {}
    for row in rows:
        active = bool(row[3]) if row[3] is not None else bool(row[2])
        if active:
            assigned.setdefault(row[0], []).append(row[1])
    return definitions, assigned


@app.get("/api/recordings/{rec_id}")
def get_recording(rec_id: str, lang: str = "ru"):
    requested_language = "en" if lang == "en" else "ru"
    conn = db()
    try:
        columns = _table_columns(conn)
        optional = {
            "plaud_segments_json": "NULL AS plaud_segments_json",
            "summary_json": "NULL AS summary_json",
            "asr_meta_json": "NULL AS asr_meta_json",
            "semantic_title": "NULL AS semantic_title",
            "archived_local_at": "NULL AS archived_local_at",
        }
        selected_optional = [
            name if name in columns else fallback for name, fallback in optional.items()
        ]
        r = conn.execute(
            "SELECT id,name,start_at,duration_ms,lang,asr_engine,asr_transcript,"
            "plaud_transcript,summary,"
            + recording_number.select_expr(columns) + ","
            + ",".join(selected_optional) +
            " FROM recordings WHERE id=?",
            (rec_id,),
        ).fetchone()
        # Keep the old block badge compatible (a visible transcript ends it),
        # while separately exposing source-aware review status below.
        progress = asr_progress.progress_for(
            conn,
            rec_id,
            bool((r["asr_transcript"] or "").strip()
                 or (r["plaud_transcript"] or "").strip()),
        ) if r else None
        transcript_status = asr_progress.status_for(
            conn,
            {"id": r["id"],
             "has_local_transcript": bool((r["asr_transcript"] or "").strip()),
             "has_plaud_transcript": bool((r["plaud_transcript"] or "").strip()),
             "engine": r["asr_engine"]},
        ) if r else None
        canonical_summary_data = _safe_json_object(r["summary_json"]) if r else None
        variant = None
        if r and requested_language == "en":
            try:
                variant = conn.execute("SELECT summary,summary_json FROM recording_summary_variants WHERE recording_id=? AND language='en'", (rec_id,)).fetchone()
            except sqlite3.OperationalError:
                variant = None
        selected_summary = variant["summary"] if variant else (r["summary"] if requested_language == "ru" and r else None)
        summary_data = _safe_json_object(variant["summary_json"]) if variant else (canonical_summary_data if requested_language == "ru" else None)
        comments = _recording_comments(conn, rec_id) if r else []
        tasks = _generated_tasks(conn, rec_id, canonical_summary_data) if r else []
        presentation_tasks = (_localized_tasks(summary_data, tasks)
                              if requested_language == "en" else tasks)
        summary_card_data = summary_card.content_model(summary_data, presentation_tasks) if r and summary_data else None
        label_definitions, assigned_labels = _recording_labels(conn, rec_id) if r else ([], {})
    finally:
        conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    names = _speaker_names(None, rec_id)
    jobs = _pipeline_status(rec_id)
    summary_state = {"language": requested_language, "state": "ready", "retryable": False}
    if requested_language == "en" and not variant:
        state = jobs.get("summary_en", {}).get("state")
        if state == "failed":
            summary_state = {"language": "en", "state": "failed", "retryable": True}
        elif state in {"queued", "processing", "retry_wait"}:
            summary_state = {"language": "en", "state": state, "retryable": state == "retry_wait"}
        else:
            try:
                result = control_client.ControlClient().request("summary_en", rec_id)
                summary_state = {"language": "en", "state": result.get("state", "queued"), "retryable": False}
            except control_client.ControlRejected:
                summary_state = {"language": "en", "state": "error", "retryable": True}
            except control_client.ControlUnavailable:
                summary_state = {"language": "en", "state": "unavailable", "retryable": True}
    diarization_pending = jobs.get("diarization", {}).get("state") in {"queued", "processing", "retry_wait"}
    label_names = _speaker_label_names(None, rec_id)
    speaker_payload = _present_voiceprint_labels(
        _speaker_model(r, names, pending=diarization_pending), _voiceprint_scores(rec_id))
    # Raw candidates are preserved on every segment; the id and the reader's name
    # are added beside them so the transcript can be PRESENTED with real names
    # without anything rewriting plaud_segments_json.
    segments = speakers.label_segments(_parse_segments(r["plaud_segments_json"]), names)
    asr_route = _asr_route(r["asr_meta_json"])
    asr_transcript = (r["asr_transcript"] or "").strip() or None
    plaud_transcript = (r["plaud_transcript"] or "").strip() or None
    # legacy flat field kept for backward compat
    transcript = asr_transcript or plaud_transcript or ""
    has_audio = os.path.exists(os.path.join(AUDIO_DIR, f"{rec_id}.mp3"))
    # A rename changes the derived renders (the mind map carries speaker labels),
    # so it has to change their revisions too or the phone keeps the old PNG.
    derived_source = "\n".join([
        r["summary_json"] or r["summary"] or transcript,
        json.dumps(label_names, ensure_ascii=False, sort_keys=True),
    ])
    canonical_display_name = r["semantic_title"] or r["name"] or "Без названия"
    display_name = ((summary_data or {}).get("title") if requested_language == "en" else None) or canonical_display_name
    mindmap_tree = _mindmap_tree(
        canonical_display_name, r["summary_json"], r["summary"],
        speakers.apply_names_to_text(asr_transcript or plaud_transcript, label_names),
    )
    try:
        public_shares = (
            public_share.list_recording(
                PUBLIC_SHARE_STORE, PUBLIC_SHARE_SECRET, rec_id
            )
            if _public_share_configured()
            else []
        )
    except (OSError, sqlite3.Error, RuntimeError):
        public_shares = []
    return {
        "id": r["id"],
        "archived": bool(r["archived_local_at"]),
        "name": display_name,
        "title": display_name,
        "source_name": r["name"],
        "recording_number": recording_number.public(r["recording_number"]),
        "start_at": r["start_at"],
        "start_at_utc": _utc_instant(r["start_at"]),
        "display_timezone": DISPLAY_TIMEZONE,
        "duration_ms": r["duration_ms"],
        "lang": r["lang"],
        "engine": r["asr_engine"],
        "segments": segments,
        "speakers": speaker_payload,
        "speaker_names": names,
        "jobs": jobs,
        "asr_transcript": asr_transcript,
        "plaud_transcript": plaud_transcript,
        "transcript": transcript,
        "summary": _summary_md(selected_summary),
        "summary_data": summary_data,
        "summary_language": requested_language,
        "summary_state": summary_state,
        "summary_card_data": summary_card_data,
        "comments": comments,
        "tasks": tasks,
        "label_definitions": label_definitions,
        "labels": assigned_labels.get(rec_id, []),
        "asr_route": asr_route,
        "asr_processing": progress,
        "transcript_status": transcript_status,
        "has_summary_card": bool(summary_data) and requested_language == "ru",
        "summary_card_revision": _derived_revision(
            SUMMARY_CARD_VERSION, display_name,
            json.dumps(summary_card_data, ensure_ascii=False, sort_keys=True)
        ),
        "has_audio": has_audio,
        "public_share_enabled": _public_share_configured(),
        "public_shares": public_shares,
        "has_mindmap": bool(mindmap_tree) and requested_language == "ru",
        "mindmap_revision": _derived_revision(
            MINDMAP_VERSION, display_name, derived_source
        ),
    }


@app.post("/api/recordings/{rec_id}/summary/en/retry")
def retry_english_summary(rec_id: str, request: Request):
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id) or not _recording_row(rec_id):
        return _bad(MSG_RU["not_found"], status=404)
    try:
        result = control_client.ControlClient().request("summary_en", rec_id)
    except control_client.ControlRejected:
        return _bad(MSG_RU["bad_request"])
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    return {"language": "en", "state": result.get("state", "queued")}


@app.get("/api/csrf")
def api_csrf():
    """The token every mutation must echo in X-CSRF-Token.

    Behind the same auth gate as everything else under /api, so only a
    logged-in, same-origin fetch can read it.
    """
    return {"csrf": csrf_token()}


# ---------------- owner-only public exports ----------------

_PUBLIC_TTLS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}


def _iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _require_public_share(request: Request) -> None:
    if not _public_share_configured():
        raise HTTPException(status_code=404, detail="not found")
    if not csrf_ok(request):
        raise HTTPException(status_code=403, detail=MSG_RU["no_csrf"])


@app.post("/api/recordings/{rec_id}/public-shares", status_code=201)
async def create_public_share(rec_id: str, request: Request):
    _require_public_share(request)
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > 16384):
        return JSONResponse(
            {"error_ru": "Некорректный запрос."},
            status_code=400,
            headers={"Cache-Control": "no-store", "Connection": "close"},
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid request")
    if not isinstance(body, dict) or set(body) != {"ttl", "content"}:
        raise HTTPException(status_code=400, detail="invalid request")
    ttl_name = body.get("ttl")
    ttl = _PUBLIC_TTLS.get(ttl_name) if isinstance(ttl_name, str) else None
    toggles = body.get("content")
    if ttl is None or not isinstance(toggles, dict):
        raise HTTPException(status_code=400, detail="invalid request")

    base = public_share.validate_base_url(PUBLIC_SHARE_BASE_URL)
    recording = get_recording(rec_id)
    payload = {
        "name": recording["name"],
        "start_at": recording["start_at"],
        "duration_ms": recording["duration_ms"],
        "summary": recording["summary"],
        "transcript": recording["transcript"],
        "mindmap": _mindmap_tree(
            recording["name"],
            json.dumps(recording.get("summary_data"), ensure_ascii=False)
            if recording.get("summary_data")
            else None,
            recording["summary"],
            recording["transcript"],
        ),
    }
    audio_path = os.path.join(AUDIO_DIR, f"{rec_id}.mp3")
    mindmap_path = _mindmap_png(rec_id) if toggles.get("mindmap") else None
    summary_card_path = _summary_card_png(rec_id) if toggles.get("images") else None
    try:
        created = public_share.create(
            PUBLIC_SHARE_STORE,
            PUBLIC_SHARE_SECRET,
            rec_id,
            payload,
            ttl_seconds=ttl,
            toggles=toggles,
            audio_path=audio_path,
            mindmap_path=mindmap_path,
            summary_card_path=summary_card_path,
        )
    except (ValueError, public_share.ShareLimitError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        {
            "share_id": created["share_id"],
            "url": base + created["url_path"],
            "expires_at": _iso_utc(created["expires_at"]),
            "expires_in_seconds": created["ttl_seconds"],
            "content": toggles,
        },
        status_code=201,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.delete("/api/public-shares/{share_id}")
def revoke_public_share(share_id: str, request: Request):
    _require_public_share(request)
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", share_id or ""):
        raise HTTPException(status_code=404, detail="not found")
    revoked = public_share.revoke(
        PUBLIC_SHARE_STORE, PUBLIC_SHARE_SECRET, share_id
    )
    return JSONResponse(
        {"revoked": bool(revoked)}, headers={"Cache-Control": "no-store"}
    )


def _reconcile_public_shares() -> int:
    if not _public_share_configured() or not os.path.exists(
        os.path.join(PUBLIC_SHARE_STORE, "shares.db")
    ):
        return 0
    conn = db()
    try:
        columns = _table_columns(conn)
        predicates = []
        # archived_at is the upstream ingest timestamp, not a visibility flag.
        # Only an explicit local archive/delete must revoke a public share.
        for column in ("archived_local_at", "deleted_at"):
            if column in columns:
                predicates.append(f"{column} IS NULL")
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        recording_ids = {
            row[0]
            for row in conn.execute("SELECT id FROM recordings" + where).fetchall()
        }
    finally:
        conn.close()
    return public_share.reconcile_sources(
        PUBLIC_SHARE_STORE, PUBLIC_SHARE_SECRET, recording_ids
    )


def _public_reconciler_loop():
    while not _public_reconciler_stop.wait(60):
        try:
            _reconcile_public_shares()
        except (OSError, sqlite3.Error, RuntimeError):
            pass


def start_public_share_reconciler():
    global _public_reconciler_started
    if not _public_reconciler_started:
        _public_reconciler_stop.clear()
        _public_reconciler_started = True
        threading.Thread(
            target=_public_reconciler_loop,
            name="public-share-reconciler",
            daemon=True,
        ).start()


# ---------------- speakers ----------------

def _bad(message_ru, status=400, field=None):
    """A refusal a reader can act on: Russian, and free of internals."""
    body = {"error_ru": message_ru}
    if field:
        body["field"] = field
    return JSONResponse(body, status_code=status)


def _recording_row(rec_id):
    """The row this feature needs, or None when the id is not in THIS archive.

    Tenant isolation is structural: `db()` only ever opens the archive this
    container has mounted, so a recording that is not in it cannot be named,
    reprocessed or read, whatever id the caller sends.
    """
    conn = db()
    try:
        columns = _table_columns(conn)
        optional = {
            "plaud_segments_json": "NULL AS plaud_segments_json",
            "asr_meta_json": "NULL AS asr_meta_json",
        }
        selected = [name if name in columns else fallback
                    for name, fallback in optional.items()]
        return conn.execute(
            "SELECT id,name,duration_ms," + ",".join(selected) +
            " FROM recordings WHERE id=?", (rec_id,)).fetchone()
    finally:
        conn.close()


def _speakers_response(rec_id, row, _unused=None, saved=None):
    names = _speaker_names(None, rec_id)
    jobs = _pipeline_status(rec_id)
    pending = jobs.get("diarization", {}).get("state") in {"queued", "processing", "retry_wait"}
    model = _present_voiceprint_labels(
        _speaker_model(row, names, pending=pending), _voiceprint_scores(rec_id))
    payload = dict(model)
    payload["speaker_names"] = names
    payload["jobs"] = jobs
    payload["can_edit"] = True
    try:
        identities = control_client.ControlClient().people(
            "get_identities", recording_id=rec_id).get("assignments", {})
    except control_client.ControlUnavailable:
        identities = {}
    for speaker in payload.get("speakers", []):
        speaker["identity"] = identities.get(speaker.get("speaker_id"))
    if saved is not None:
        payload["saved"] = saved
    return payload


@app.get("/api/recordings/{rec_id}/speakers")
def get_speakers(rec_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id):
        return _bad(MSG_RU["bad_id"])
    row = _recording_row(rec_id)
    if not row:
        return _bad(MSG_RU["not_found"], status=404)
    return _speakers_response(rec_id, row)


async def _json_body(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a 400, never a 500
        return None
    return body if isinstance(body, dict) else None


identity_api.configure(
    csrf_ok=csrf_ok,
    json_body=_json_body,
    client_factory=control_client.ControlClient,
    recording_exists=lambda rec_id: _recording_row(rec_id) is not None,
)
app.include_router(identity_api.router)


@app.post("/api/source/poll")
async def source_poll(request: Request):
    """Acknowledge a connector-owned discovery request; never poll PLAUD here."""
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    try:
        result = control_client.ControlClient().request_source_poll()
    except control_client.ControlRejected:
        return _bad(MSG_RU["bad_request"])
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    return {"state": result.get("state", "queued"), "message_ru": "Проверка PLAUD запущена"}


@app.post("/api/recordings/{rec_id}/comments")
async def add_comment(rec_id: str, request: Request):
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id):
        return _bad(MSG_RU["bad_id"])
    body = await _json_body(request)
    text = (body or {}).get("text")
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_COMMENT_LEN:
        return _bad("Введите комментарий длиной до 2000 символов.", field="text")
    if not _recording_row(rec_id):
        return _bad(MSG_RU["not_found"], status=404)
    try:
        result = control_client.ControlClient().request(
            "add_comment", rec_id, comment=text)
    except control_client.ControlRejected:
        return _bad("Не удалось сохранить комментарий.", field="text")
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    # The newly created comment is rendered immediately, so it must carry the
    # same explicit instant the detail read publishes.
    comment = dict(result["comment"])
    comment["created_at_utc"] = _utc_instant(comment.get("created_at"))
    return {"comment": comment}


@app.post("/api/recordings/{rec_id}/labels")
async def set_recording_label(rec_id: str, request: Request):
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id):
        return _bad(MSG_RU["bad_id"])
    body = await _json_body(request)
    if body is None or type(body.get("active")) is not bool or not isinstance(body.get("label_id"), str):
        return _bad("Некорректная метка.", field="label_id")
    if not _recording_row(rec_id):
        return _bad(MSG_RU["not_found"], status=404)
    try:
        result = control_client.ControlClient().request(
            "set_label", rec_id, label_id=body["label_id"], active=body["active"])
    except control_client.ControlRejected:
        return _bad("Не удалось изменить метку.", field="label_id")
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    return {"label": result["label"]}


@app.post("/api/labels")
async def create_label(request: Request):
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    body = await _json_body(request)
    if not isinstance(body, dict) or set(body) != {"name"}:
        return _bad("Введите название метки длиной до 40 символов.", field="name")
    name = (body or {}).get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        return _bad("Введите название метки длиной до 40 символов.", field="name")
    try:
        result = control_client.ControlClient().create_label(name)
    except control_client.ControlRejected:
        return _bad("Не удалось создать метку.", field="name")
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    return {"label": result["label"], "classification_queued": bool(result.get("classification_queued"))}


@app.delete("/api/labels/{label_id}")
async def delete_label(label_id: str, request: Request):
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"custom-[a-f0-9]{16}", label_id):
        return _bad("Некорректная метка.", field="label_id")
    try:
        result = control_client.ControlClient().delete_label(label_id)
    except control_client.ControlRejected:
        return _bad("Не удалось удалить метку.", field="label_id")
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    return {"label_id": result["label_id"], "classification_queued": bool(result.get("classification_queued"))}


@app.post("/api/recordings/{rec_id}/tasks/{task_id}")
async def set_task_completed(rec_id: str, task_id: str, request: Request):
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id):
        return _bad(MSG_RU["bad_id"])
    body = await _json_body(request)
    completed = (body or {}).get("completed")
    if type(completed) is not bool or not re.fullmatch(r"[a-f0-9]{24}", task_id):
        return _bad("Некорректная задача.", field="completed")
    conn = db()
    try:
        row = conn.execute(
            "SELECT summary_json FROM recordings WHERE id=?", (rec_id,)
        ).fetchone()
        valid = ({task["id"] for task in _generated_tasks(
            conn, rec_id, _safe_json_object(row[0]))} if row else set())
    finally:
        conn.close()
    if not row:
        return _bad(MSG_RU["not_found"], status=404)
    if task_id not in valid:
        return _bad("Задача больше не существует.", status=404)
    try:
        result = control_client.ControlClient().request(
            "set_task_completed", rec_id, task_id=task_id,
            completed=completed)
    except control_client.ControlRejected:
        return _bad("Не удалось изменить задачу.", field="completed")
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    return {"task": result["task"]}


@app.post("/api/recordings/{rec_id}/speakers")
async def save_speakers(rec_id: str, request: Request):
    """Persist the reader's speaker names, then queue a derived rebuild.

    Renaming never re-runs ASR: the audio did not change and a transcription
    costs GPU minutes. Only the derived material — summary, mind map, card —
    depends on who is speaking, so only that is re-enqueued.
    """
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id):
        return _bad(MSG_RU["bad_id"])
    body = await _json_body(request)
    if body is None or not isinstance(body.get("names"), dict) or len(body["names"]) > MAX_NAMES_PER_REQUEST:
        return _bad(MSG_RU["bad_request"], field="names")
    if not _recording_row(rec_id):
        return _bad(MSG_RU["not_found"], status=404)
    try:
        result = control_client.ControlClient().request("rename", rec_id, body["names"])
    except control_client.ControlRejected:
        return _bad(MSG_RU["bad_request"], field="names")
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    return {"saved": len(body["names"]), "state": result["state"],
            "status_ru": MSG_RU["names_saved"]}


@app.post("/api/recordings/{rec_id}/archive")
async def archive_mutation(rec_id: str, request: Request):
    if not csrf_ok(request): return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id): return _bad(MSG_RU["bad_id"])
    try: result = control_client.ControlClient().request("archive", rec_id)
    except control_client.ControlRejected: return _bad(MSG_RU["not_found"], status=404)
    except control_client.ControlUnavailable: return _bad(MSG_RU["unavailable"], status=503)
    return {"state": result["state"]}


@app.post("/api/recordings/{rec_id}/restore")
async def restore_mutation(rec_id: str, request: Request):
    if not csrf_ok(request): return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id): return _bad(MSG_RU["bad_id"])
    try: result = control_client.ControlClient().request("restore", rec_id)
    except control_client.ControlRejected: return _bad(MSG_RU["not_found"], status=404)
    except control_client.ControlUnavailable: return _bad(MSG_RU["unavailable"], status=503)
    return {"state": result["state"]}


@app.post("/api/recordings/{rec_id}/delete")
async def delete_mutation(rec_id: str, request: Request):
    if not csrf_ok(request): return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id): return _bad(MSG_RU["bad_id"])
    try: result = control_client.ControlClient().request("delete", rec_id)
    except control_client.ControlRejected: return _bad(MSG_RU["not_found"], status=404)
    except control_client.ControlUnavailable: return _bad(MSG_RU["unavailable"], status=503)
    return {"state": result["state"]}


REPROCESS_ACTIONS = {"transcript": "retranscribe", "materials": "regenerate", "diarize": "diarize"}


@app.post("/api/recordings/{rec_id}/reprocess")
async def reprocess(rec_id: str, request: Request):
    """Queue a re-transcription or a derived rebuild. Does neither itself.

    Nothing slow happens in this request: ASR and the summariser run in the
    archive-side worker, which is where the credentials and the retry ledgers
    already live. The reader's current transcript, summary, mind map and card
    stay exactly as they are until a replacement has been published
    successfully, so a failed job costs nothing that was already on screen.
    """
    if not csrf_ok(request):
        return _bad(MSG_RU["no_csrf"], status=403)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rec_id):
        return _bad(MSG_RU["bad_id"])
    body = await _json_body(request)
    action = (body or {}).get("action")
    kind = REPROCESS_ACTIONS.get(action) if isinstance(action, str) else None
    if not kind:
        return _bad(MSG_RU["bad_action"], field="action")
    if not _recording_row(rec_id):
        return _bad(MSG_RU["not_found"], status=404)

    try:
        result = control_client.ControlClient().request(kind, rec_id)
    except control_client.ControlRejected:
        return _bad(MSG_RU["bad_request"], field="action")
    except control_client.ControlUnavailable:
        return _bad(MSG_RU["unavailable"], status=503)
    queued = result["state"] == "queued"
    message = MSG_RU[f"queued_{action}"] if queued else MSG_RU["already"]
    return {"action": action, "state": result["state"], "message_ru": message}


@app.get("/api/search")
def search(q: str = "", semantic: bool = False):
    return SEARCH_ADAPTER.search(q, semantic=semantic, limit=50)


def _safe_audio_download_name(name, start_at, rec_id):
    title = str(name or "Запись").strip()
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" ._")
    if title.lower().endswith(".mp3"):
        title = title[:-4].rstrip(" ._")
    date = str(start_at or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = ""
    stem = f"{date} - {title}" if date and title else (title or rec_id)
    return f"{stem[:120].rstrip(' ._') or rec_id}.mp3"


def _audio_download_name(rec_id):
    try:
        conn = db()
        try:
            row = conn.execute(
                "SELECT name,start_at FROM recordings WHERE id=?", (rec_id,)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        row = None
    if not row:
        return f"{rec_id}.mp3"
    return _safe_audio_download_name(row["name"], row["start_at"], rec_id)


def _attachment_header(filename, rec_id):
    fallback = f"{rec_id}.mp3"
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _audio_file_response(path, request, *, extra_headers=None):
    file_size = os.path.getsize(path)
    common_headers = {"Accept-Ranges": "bytes", **(extra_headers or {})}
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type="audio/mpeg", headers=common_headers)

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)

    def unsatisfiable():
        return Response(
            status_code=416,
            headers={
                **common_headers,
                "Content-Range": f"bytes */{file_size}",
            },
        )

    if not match or not (match.group(1) or match.group(2)) or file_size <= 0:
        return unsatisfiable()
    first, last = match.groups()
    if len(first) > 20 or len(last) > 20:
        return unsatisfiable()
    try:
        if first:
            start = int(first)
            end = int(last) if last else file_size - 1
        else:
            suffix_length = int(last)
            if suffix_length <= 0:
                return unsatisfiable()
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
    except ValueError:
        return unsatisfiable()
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        return unsatisfiable()
    length = end - start + 1

    def iterfile():
        with open(path, "rb") as file_handle:
            file_handle.seek(start)
            remaining = length
            chunk = 64 * 1024
            while remaining > 0:
                data = file_handle.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        **common_headers,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
        "Content-Type": "audio/mpeg",
    }
    return StreamingResponse(iterfile(), status_code=206, headers=headers)


@app.get("/audio/{rec_id}")
def audio(rec_id: str, request: Request, download: bool = False):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rec_id):
        raise HTTPException(status_code=400, detail="bad id")
    # The canonical row is the authorization source for local media. A delete
    # commits its tombstone before best-effort unlinking, so an orphaned file
    # must become unreachable immediately even if filesystem cleanup fails.
    if not _recording_row(rec_id):
        raise HTTPException(status_code=404, detail="no audio")
    path = os.path.join(AUDIO_DIR, f"{rec_id}.mp3")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="no audio")
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if download:
        filename = _audio_download_name(rec_id)
        headers["Content-Disposition"] = _attachment_header(filename, rec_id)
    return _audio_file_response(path, request, extra_headers=headers)



# Numbers-only health. Public on purpose for deployment health checks.
# and it must never leak recording content — counts and a build stamp, nothing else.
@app.get("/api/health")
def api_health():
    counts = {"recordings": 0, "audio_files": 0, "mindmaps_cached": 0,
              "summary_cards_cached": 0}
    try:
        with db() as conn:
            counts["recordings"] = conn.execute(
                "SELECT COUNT(*) FROM recordings").fetchone()[0]
    except Exception:
        pass
    try:
        counts["audio_files"] = len(glob.glob(os.path.join(AUDIO_DIR, "*")))
    except Exception:
        pass
    try:
        counts["mindmaps_cached"] = len(glob.glob(os.path.join(MINDMAP_DIR, "*.png")))
    except Exception:
        pass
    try:
        counts["summary_cards_cached"] = len(
            glob.glob(os.path.join(SUMMARY_CARD_DIR, "*.png"))
        )
    except Exception:
        pass
    selftest = "no_source"
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id FROM recordings ORDER BY COALESCE(start_at, created_at) DESC LIMIT 40").fetchall()
        for row in rows:
            if _mindmap_source(row["id"]):
                path = _mindmap_png(row["id"])
                selftest = "ok" if path and os.path.getsize(path) > 0 else "empty"
                break
    except Exception as exc:
        selftest = f"error: {type(exc).__name__}"
    card_selftest = "no_source"
    try:
        with db() as conn:
            columns = _table_columns(conn)
            if "summary_json" in columns:
                rows = conn.execute(
                    "SELECT id FROM recordings WHERE COALESCE(summary_json,'')<>'' "
                    "ORDER BY COALESCE(start_at, created_at) DESC LIMIT 1"
                ).fetchall()
            else:
                rows = []
        if rows:
            card_path = _summary_card_png(rows[0]["id"])
            card_selftest = "ok" if card_path and os.path.getsize(card_path) > 0 else "empty"
    except Exception as exc:
        card_selftest = f"error: {type(exc).__name__}"
    return {"ok": True, "version": BUILD_VERSION, "mindmap_selftest": selftest,
            "summary_card_selftest": card_selftest, **counts}


# The app shell must never be served stale: an installed PWA kept showing an old
# app.js after a deploy because these files sat in the browser HTTP cache.
_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


def _shell_file(name: str, media_type: str) -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, name), media_type=media_type,
                        headers=_NO_STORE)


@app.get("/sw.js")
def sw_js():
    return _shell_file("sw.js", "application/javascript")


@app.get("/app.js")
def app_js():
    return _shell_file("app.js", "application/javascript")


@app.get("/identity-selector.js")
def identity_selector_js():
    return _shell_file("identity-selector.js", "application/javascript")


@app.get("/asr-progress.js")
def asr_progress_js():
    return _shell_file("asr-progress.js", "application/javascript")


@app.get("/index.html")
def index_html():
    return _shell_file("index.html", "text/html")


@app.get("/")
def root_html():
    return _shell_file("index.html", "text/html")


# static app shell (no auth on shell so PWA can install / show login prompt)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
