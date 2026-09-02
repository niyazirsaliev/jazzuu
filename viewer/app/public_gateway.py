"""Minimal public gateway for isolated one-record exports.

This process deliberately knows nothing about the private archive or viewer API.
It reads only PUBLIC_SHARE_STORE and authenticates one opaque share per cookie.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import public_share

COOKIE_NAME = "__Secure-share"
OPAQUE_BODY = "Недействительная или истёкшая ссылка"

_DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_claims(
    store_key TEXT NOT NULL,
    share_id TEXT NOT NULL,
    device_hash TEXT NOT NULL,
    claimed_at INTEGER NOT NULL,
    PRIMARY KEY(store_key, share_id)
);
"""

_SCRIPT = """(()=>{const p=location.pathname.replace(/\\/$/,'');const s=location.hash.slice(1);history.replaceState(null,'',p);if(!s)return;fetch(p+'/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({secret:s}),credentials:'same-origin'}).then(r=>{if(r.ok)location.replace(p+'/content');});})();"""
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
_LANDING = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><title>Jazzuu Share</title></head><body><main><h1>Jazzuu Share</h1><p>Проверяем ссылку…</p></main><script>{_SCRIPT}</script></body></html>"""


class _Limiter:
    def __init__(self, limit=30, window=60, max_keys=10000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self.events = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key, now=None):
        current = time.monotonic() if now is None else now
        with self.lock:
            if key not in self.events and len(self.events) >= self.max_keys:
                for existing in list(self.events):
                    values = self.events[existing]
                    while values and values[0] <= current - self.window:
                        values.popleft()
                    if not values:
                        del self.events[existing]
                while len(self.events) >= self.max_keys:
                    self.events.pop(next(iter(self.events)))
            events = self.events[key]
            while events and events[0] <= current - self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(current)
            return True


def _headers(content=False):
    values = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    values["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
        "style-src 'unsafe-inline'; media-src 'self'; img-src 'self'"
        if content
        else "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
        f"connect-src 'self'; script-src 'sha256-{_SCRIPT_HASH}'"
    )
    return values


def _opaque(status=404):
    return HTMLResponse(OPAQUE_BODY, status_code=status, headers=_headers())


def _share_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{20,40}", value or ""))


def _store_key(secret, store):
    return public_share.device_token_hash(secret, "store", str(Path(store).resolve()))


def _state_connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DEVICE_SCHEMA)
    conn.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _verify_claim(stores, share_id, token):
    matches = []
    for store, secret in stores:
        capability = public_share.verify(store, secret, share_id, token)
        if capability:
            matches.append((store, secret, capability))
    return matches[0] if len(matches) == 1 else None


def _claim_device(state_path, store, secret, capability):
    device_token = "pd1_" + secrets.token_urlsafe(32)
    device_hash = public_share.device_token_hash(
        secret, capability["share_id"], device_token
    )
    conn = _state_connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM device_claims WHERE store_key=? AND share_id=?",
            (_store_key(secret, store), capability["share_id"]),
        ).fetchone()
        if existing:
            conn.rollback()
            return None
        conn.execute(
            """INSERT INTO device_claims(store_key,share_id,device_hash,claimed_at)
               VALUES(?,?,?,?)""",
            (
                _store_key(secret, store), capability["share_id"],
                device_hash, int(time.time()),
            ),
        )
        conn.commit()
        return device_token
    finally:
        conn.close()


def _authorized(request, stores, state_path, share_id):
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return None
    try:
        conn = _state_connect(state_path)
    except (OSError, sqlite3.Error):
        return None
    try:
        for store, secret in stores:
            row = conn.execute(
                """SELECT device_hash FROM device_claims
                   WHERE store_key=? AND share_id=?""",
                (_store_key(secret, store), share_id),
            ).fetchone()
            if not row or not secrets.compare_digest(
                row[0], public_share.device_token_hash(secret, share_id, token)
            ):
                continue
            return public_share.verify_active(store, share_id)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None


def _range_response(path: Path, request: Request):
    size = path.stat().st_size
    common = {**_headers(content=True), "Accept-Ranges": "bytes"}
    value = request.headers.get("range")
    if not value:
        return FileResponse(path, media_type="audio/mpeg", headers=common)

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)

    def invalid():
        return Response(
            status_code=416,
            headers={**common, "Content-Range": f"bytes */{size}"},
        )

    if not match or not (match.group(1) or match.group(2)) or size <= 0:
        return invalid()
    first, last = match.groups()
    if len(first) > 20 or len(last) > 20:
        return invalid()
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            return invalid()
        start = max(0, size - suffix)
        end = size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return invalid()
    length = end - start + 1

    def body():
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        body(),
        status_code=206,
        headers={
            **common,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
            "Content-Type": "audio/mpeg",
        },
    )


def _content_html(payload, toggles, share_id, directory):
    sections = []
    metadata = payload.get("metadata") if toggles.get("metadata") else None
    if metadata:
        sections.append(f"<h1>{html.escape(metadata.get('name') or 'Без названия')}</h1>")
        sections.append(
            f"<p>{html.escape(str(metadata.get('start_at') or ''))} · "
            f"{int(metadata.get('duration_ms') or 0) // 1000} сек.</p>"
        )
    else:
        sections.append("<h1>Запись</h1>")
    if toggles.get("audio"):
        sections.append(
            f'<section><h2>Аудио</h2><audio controls preload="metadata" src="/{share_id}/audio"></audio></section>'
        )
    if toggles.get("images") and (directory / "summary-card.png").is_file():
        sections.append(
            f'<section><h2>Визуальная карточка</h2><img src="/{share_id}/summary-card.png" alt="Визуальная карточка PNG"></section>'
        )
    if toggles.get("summary") and payload.get("summary"):
        sections.append(
            "<section><h2>Резюме</h2><pre>"
            + html.escape(str(payload["summary"]))
            + "</pre></section>"
        )
    if toggles.get("mindmap") and (directory / "mindmap.png").is_file():
        sections.append(
            f'<section><h2>Карта</h2><img src="/{share_id}/mindmap.png" alt="Mind map"></section>'
        )
    elif toggles.get("mindmap") and payload.get("mindmap"):
        tree = json.dumps(payload["mindmap"], ensure_ascii=False, indent=2)
        sections.append("<section><h2>Карта</h2><pre>" + html.escape(tree) + "</pre></section>")
    if toggles.get("transcript") and payload.get("transcript"):
        sections.append(
            "<section><h2>Транскрипт</h2><pre>"
            + html.escape(str(payload["transcript"]))
            + "</pre></section>"
        )
    body = "".join(sections)
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex,nofollow,noarchive\"><title>Jazzuu Share</title>"
        "<style>body{font-family:system-ui;max-width:760px;margin:auto;padding:24px;line-height:1.5}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}audio,img{width:100%}img{height:auto}</style></head><body>"
        + body
        + "<p>Доступ даёт владение ссылкой до истечения срока или отзыва. Это не DRM.</p>"
        "</body></html>"
    )


def _configured_secret():
    secret_file = os.environ.get("PUBLIC_SHARE_SECRET_FILE", "")
    if secret_file:
        try:
            return Path(secret_file).read_text().strip()
        except OSError:
            return ""
    return ""


def _configured_stores(store=None, secret=None, stores=None):
    if stores is not None:
        configured = []
        for item in stores:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                path, value = item
            else:
                path, value = item, secret
            if path and value:
                configured.append((Path(path), value))
        return configured
    if store is not None or secret is not None:
        value = secret if secret is not None else _configured_secret()
        return [(Path(store or "/shares"), value)] if value else []

    items = [item.strip() for item in os.environ.get("PUBLIC_SHARE_STORES", "").split(",") if item.strip()]
    configured = []
    for item in items:
        try:
            path, secret_file = item.split(":", 1)
            value = Path(secret_file).read_text().strip()
        except (OSError, ValueError):
            return []
        if not path or not value:
            return []
        configured.append((Path(path), value))
    if items:
        return configured
    value = _configured_secret()
    return [(Path(os.environ.get("PUBLIC_SHARE_STORE", "/shares")), value)] if value else []


def create_app(
    store=None, secret=None, *, stores=None, gateway_state=None,
    cookie_secure=True, public_base_url=None,
):
    share_stores = _configured_stores(store, secret, stores)
    state_path = Path(
        gateway_state
        or (
            Path(store).parent / "gateway-device-auth.db"
            if store is not None
            else (
                share_stores[0][0].parent / "gateway-device-auth.db"
                if stores is not None and share_stores
                else os.environ.get("PUBLIC_SHARE_GATEWAY_STATE", "/state/device-auth.db")
            )
        )
    )
    configured_base_url = (
        public_base_url
        if public_base_url is not None
        else os.environ.get("PUBLIC_SHARE_BASE_URL", "")
    )
    try:
        expected_origin = public_share.validate_base_url(configured_base_url)
    except RuntimeError:
        expected_origin = None
    limiter = _Limiter()
    app = FastAPI(
        title="Jazzuu Share",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security(request: Request, call_next):
        key = (request.client.host if request.client else "unknown", request.url.path)
        if not limiter.allow(key):
            return _opaque(429)
        response = await call_next(request)
        for name, value in _headers(content=request.url.path.endswith("/content")).items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/healthz")
    def healthz():
        if not share_stores:
            return Response(status_code=503)
        try:
            conn = _state_connect(state_path)
            conn.close()
        except (OSError, sqlite3.Error):
            return Response(status_code=503)
        return {"ok": True}

    @app.get("/{share_id}")
    def landing(share_id: str):
        if not _share_id(share_id):
            return _opaque()
        return HTMLResponse(_LANDING, headers=_headers())

    @app.post("/{share_id}/session", status_code=204)
    async def exchange(share_id: str, request: Request):
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 4096):
            response = _opaque()
            response.headers["Connection"] = "close"
            return response
        origin = (request.headers.get("origin") or "").rstrip("/")
        if expected_origin is None:
            return _opaque()
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return _opaque()
        if origin and origin != expected_origin:
            return _opaque()
        try:
            data = await request.json()
        except Exception:
            return _opaque()
        token = data.get("secret") if isinstance(data, dict) else None
        if not isinstance(token, str):
            return _opaque()
        claimed = _verify_claim(share_stores, share_id, token)
        if not claimed:
            return _opaque()
        store, store_secret, capability = claimed
        try:
            device_token = _claim_device(state_path, store, store_secret, capability)
        except (OSError, sqlite3.Error):
            return _opaque()
        if not device_token:
            return _opaque()
        response = Response(status_code=204, headers=_headers())
        response.set_cookie(
            COOKIE_NAME,
            device_token,
            httponly=True,
            secure=cookie_secure,
            samesite="strict",
            path=f"/{share_id}",
            max_age=max(0, capability["expires_at"] - int(time.time())),
        )
        return response

    @app.get("/{share_id}/content")
    def content(share_id: str, request: Request):
        capability = _authorized(request, share_stores, state_path, share_id)
        if not capability:
            return _opaque()
        try:
            payload = json.loads((capability["directory"] / "content.json").read_text())
        except (OSError, ValueError):
            return _opaque()
        return HTMLResponse(
            _content_html(payload, capability["toggles"], share_id, capability["directory"]),
            headers=_headers(content=True),
        )

    @app.get("/{share_id}/audio")
    def audio(share_id: str, request: Request):
        capability = _authorized(request, share_stores, state_path, share_id)
        if not capability or not capability["toggles"].get("audio"):
            return _opaque()
        path = capability["directory"] / "audio.mp3"
        if not path.is_file():
            return _opaque()
        return _range_response(path, request)

    @app.get("/{share_id}/{filename}")
    def image(share_id: str, filename: str, request: Request):
        if filename not in {"mindmap.png", "summary-card.png"}:
            return _opaque()
        capability = _authorized(request, share_stores, state_path, share_id)
        toggle = "mindmap" if filename == "mindmap.png" else "images"
        if not capability or not capability["toggles"].get(toggle):
            return _opaque()
        path = capability["directory"] / filename
        if not path.is_file() or path.is_symlink():
            return _opaque()
        return FileResponse(path, media_type="image/png", headers=_headers(content=True))

    return app


def cookie_secure_from_env():
    return os.environ.get("PUBLIC_SHARE_DEVELOPMENT_INSECURE_COOKIE") != "1"


app = create_app(
    cookie_secure=cookie_secure_from_env()
)
