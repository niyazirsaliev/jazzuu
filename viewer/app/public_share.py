"""Isolated exports and capability state for human-facing recording shares."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_TTLS = {3600, 86400, 7 * 86400}
TOGGLE_NAMES = {"metadata", "summary", "transcript", "mindmap", "images", "audio"}
LEGACY_TOGGLE_NAMES = TOGGLE_NAMES - {"images"}
DEFAULT_MAX_ACTIVE = 100
DEFAULT_MAX_STORAGE_BYTES = 2 * 1024 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shares(
    id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    content_json TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    claimed_at INTEGER,
    device_token_hash TEXT
);
CREATE INDEX IF NOT EXISTS shares_source_active
ON shares(source_hash, expires_at, revoked_at);
"""


class ShareLimitError(RuntimeError):
    pass


def validate_base_url(value: str) -> str:
    candidate = str(value or "").rstrip("/")
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("invalid PUBLIC_SHARE_BASE_URL") from exc
    hostname = (parsed.hostname or "").lower()
    labels = hostname.split(".")
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or not hostname.isascii()
        or len(hostname) > 253
        or any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9-]+", label)
            or label.startswith("-")
            or label.endswith("-")
            for label in labels
        )
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port == 0
        or "\\" in candidate
        or any(character.isspace() for character in candidate)
    ):
        raise RuntimeError("invalid PUBLIC_SHARE_BASE_URL")
    authority = hostname if port in {None, 443} else f"{hostname}:{port}"
    return f"https://{authority}"


def _root(path) -> Path:
    return Path(path).resolve()


def _db_path(path) -> Path:
    return _root(path) / "shares.db"


def _connect(path) -> sqlite3.Connection:
    root = _root(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "exports").mkdir(mode=0o700, exist_ok=True)
    db_path = root / "shares.db"
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(shares)")}
    if "claimed_at" not in columns:
        conn.execute("ALTER TABLE shares ADD COLUMN claimed_at INTEGER")
    if "device_token_hash" not in columns:
        conn.execute("ALTER TABLE shares ADD COLUMN device_token_hash TEXT")
    conn.commit()
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn


def _connect_readonly(path):
    db_path = _db_path(path)
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _digest(secret: str, purpose: str, *parts: str) -> str:
    if not secret:
        raise RuntimeError("public share secret is not configured")
    message = "\0".join(("public-share-v1", purpose, *parts)).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _source_hash(secret: str, recording_id: str) -> str:
    return _digest(secret, "source", recording_id)


def _token_hash(secret: str, share_id: str, token: str) -> str:
    return _digest(secret, "token", share_id, token)


def device_token_hash(secret: str, share_id: str, token: str) -> str:
    return _digest(secret, "device", share_id, token)


def _clean_text(value) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", text)
    text = re.sub(r"(?s)<[^>]*>", "", text)
    return html.unescape(text).strip()


def _clean_markdown(value) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", text)
    text = re.sub(r"(?s)<[^>]*>", "", text)
    text = re.sub(
        r"(?i)\[([^\]]*)\]\(\s*(?:javascript|data|vbscript):[^)]*\)",
        r"\1",
        text,
    )
    return html.unescape(text).strip()


def _clean_tree(value, depth=0):
    if depth > 10:
        return None
    if isinstance(value, dict):
        cleaned = {}
        for key, item in list(value.items())[:100]:
            safe_key = _clean_text(key)[:100]
            if safe_key:
                cleaned[safe_key] = _clean_tree(item, depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_clean_tree(item, depth + 1) for item in value[:200]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clean_text(value)[:10000]


def _selected_payload(payload: dict, toggles: dict) -> dict:
    selected = {}
    if toggles["metadata"]:
        selected["metadata"] = {
            "name": _clean_text(payload.get("name"))[:500] or "Без названия",
            "start_at": _clean_text(payload.get("start_at"))[:100] or None,
            "duration_ms": max(0, int(payload.get("duration_ms") or 0)),
        }
    if toggles["summary"]:
        selected["summary"] = _clean_markdown(payload.get("summary"))[:500000]
    if toggles["transcript"]:
        selected["transcript"] = str(payload.get("transcript") or "")[:2_000_000]
    if toggles["mindmap"]:
        selected["mindmap"] = _clean_tree(payload.get("mindmap"))
    return selected


def _remove_exports(path, share_ids) -> None:
    exports = _root(path) / "exports"
    for share_id in share_ids:
        if re.fullmatch(r"[A-Za-z0-9_-]{20,40}", share_id or ""):
            shutil.rmtree(exports / share_id, ignore_errors=True)


def _remove_orphaned_staging(exports: Path) -> None:
    if not exports.is_dir():
        return
    for directory in exports.iterdir():
        if (
            directory.name.startswith(".share-")
            and directory.is_dir()
            and not directory.is_symlink()
        ):
            shutil.rmtree(directory, ignore_errors=True)


def _copy_bounded(source, destination: Path, byte_limit: int) -> int:
    written = 0
    with destination.open("xb") as target:
        while written < byte_limit:
            chunk = source.read(min(1024 * 1024, byte_limit - written))
            if not chunk:
                break
            target.write(chunk)
            written += len(chunk)
    return written


def _storage_bytes(exports: Path) -> int:
    total = 0
    if not exports.is_dir():
        return 0
    for directory in exports.iterdir():
        if directory.name.startswith(".") or not directory.is_dir() or directory.is_symlink():
            continue
        for name in ("content.json", "audio.mp3", "mindmap.png", "summary-card.png"):
            candidate = directory / name
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
    return total


def create(
    path,
    secret: str,
    recording_id: str,
    payload: dict,
    *,
    ttl_seconds: int = 86400,
    toggles: dict,
    audio_path=None,
    mindmap_path=None,
    summary_card_path=None,
    now=None,
    max_active: int = DEFAULT_MAX_ACTIVE,
    max_storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES,
) -> dict:
    ttl = int(ttl_seconds)
    if ttl not in ALLOWED_TTLS:
        raise ValueError("ttl must be one of 3600, 86400, 604800 seconds")
    if set(toggles) == LEGACY_TOGGLE_NAMES:
        toggles = {**toggles, "images": False}
    if set(toggles) != TOGGLE_NAMES or any(type(toggles[name]) is not bool for name in TOGGLE_NAMES):
        raise ValueError("toggles must explicitly contain six booleans")
    if not any(toggles.values()):
        raise ValueError("at least one content toggle is required")
    if not recording_id:
        raise ValueError("recording id is required")

    current = int(time.time() if now is None else now)
    share_id = secrets.token_urlsafe(18)
    token = "ps1_" + secrets.token_urlsafe(32)
    selected = _selected_payload(payload, toggles)
    root = _root(path)
    exports = root / "exports"
    exports.mkdir(parents=True, mode=0o700, exist_ok=True)
    final = exports / share_id
    content_bytes = json.dumps(selected, ensure_ascii=False, separators=(",", ":")).encode()
    total_bytes = len(content_bytes)
    audio_source = None
    audio_size = 0
    if toggles["audio"] and audio_path:
        candidate = Path(audio_path)
        if candidate.is_file():
            audio_source = candidate.open("rb")
            audio_size = os.fstat(audio_source.fileno()).st_size
            total_bytes += audio_size
    image_sources = []
    for enabled, source_path, filename in (
        (toggles["mindmap"], mindmap_path, "mindmap.png"),
        (toggles["images"], summary_card_path, "summary-card.png"),
    ):
        if not enabled or not source_path:
            continue
        candidate = Path(source_path)
        if not candidate.is_file():
            continue
        source = candidate.open("rb")
        header = source.read(8)
        source.seek(0)
        if header != b"\x89PNG\r\n\x1a\n":
            source.close()
            continue
        size = os.fstat(source.fileno()).st_size
        image_sources.append((source, filename, size))
        total_bytes += size
    if total_bytes > max_storage_bytes:
        if audio_source is not None:
            audio_source.close()
        for source, _, _ in image_sources:
            source.close()
        raise ShareLimitError("public share storage limit exceeded")

    temporary = None
    try:
        conn = _connect(root)
    except Exception:
        if audio_source is not None:
            audio_source.close()
        for source, _, _ in image_sources:
            source.close()
        raise
    try:
        conn.execute("BEGIN IMMEDIATE")
        stale = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM shares WHERE revoked_at IS NOT NULL OR expires_at<=?",
                (current,),
            ).fetchall()
        ]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(f"DELETE FROM shares WHERE id IN ({placeholders})", stale)
            _remove_exports(root, stale)
        _remove_orphaned_staging(exports)
        existing_storage = _storage_bytes(exports)
        active, used = conn.execute(
            """SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM shares
               WHERE revoked_at IS NULL AND expires_at>?""",
            (current,),
        ).fetchone()
        if active >= max_active:
            raise ShareLimitError("active public share limit exceeded")
        if (
            existing_storage + total_bytes > max_storage_bytes
            or used + total_bytes > max_storage_bytes
        ):
            raise ShareLimitError("public share storage limit exceeded")

        temporary = Path(tempfile.mkdtemp(prefix=".share-", dir=exports))
        (temporary / "content.json").write_bytes(content_bytes)
        if audio_source is not None:
            copied = _copy_bounded(
                audio_source,
                temporary / "audio.mp3",
                audio_size,
            )
        else:
            copied = 0
        copied_images = 0
        for source, filename, size in image_sources:
            copied_images += _copy_bounded(source, temporary / filename, size)
        total_bytes = len(content_bytes) + copied + copied_images
        os.chmod(temporary, 0o700)
        temporary.rename(final)
        conn.execute(
            """INSERT INTO shares(
                   id,source_hash,token_hash,created_at,expires_at,revoked_at,
                   content_json,bytes
               ) VALUES(?,?,?,?,?,NULL,?,?)""",
            (
                share_id,
                _source_hash(secret, recording_id),
                _token_hash(secret, share_id, token),
                current,
                current + ttl,
                json.dumps(toggles, sort_keys=True, separators=(",", ":")),
                total_bytes,
            ),
        )
        conn.commit()
    except ShareLimitError:
        conn.commit()
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        raise
    except Exception:
        conn.rollback()
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        conn.close()
        if audio_source is not None:
            audio_source.close()
        for source, _, _ in image_sources:
            source.close()

    return {
        "share_id": share_id,
        "secret": token,
        "created_at": current,
        "expires_at": current + ttl,
        "ttl_seconds": ttl,
        "url_path": f"/{share_id}#{token}",
    }


def verify(path, secret: str, share_id: str, token: str, *, now=None):
    """Compatibility probe for an unclaimed link; never authorizes content."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", share_id or "") or not token:
        return None
    current = int(time.time() if now is None else now)
    conn = _connect_readonly(path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            """SELECT token_hash,expires_at,content_json FROM shares
               WHERE id=? AND revoked_at IS NULL AND expires_at>?
                 AND claimed_at IS NULL AND device_token_hash IS NULL""",
            (share_id, current),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not hmac.compare_digest(_token_hash(secret, share_id, token), row["token_hash"]):
        return None
    return {"share_id": share_id, "expires_at": row["expires_at"]}


def claim(path, secret: str, share_id: str, claim_token: str, *, now=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", share_id or "") or not claim_token:
        return None
    current = int(time.time() if now is None else now)
    directory = _root(path) / "exports" / share_id
    if not (directory / "content.json").is_file():
        return None
    device_token = "pd1_" + secrets.token_urlsafe(32)
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT token_hash,expires_at,content_json FROM shares
               WHERE id=? AND revoked_at IS NULL AND expires_at>?
                 AND claimed_at IS NULL AND device_token_hash IS NULL""",
            (share_id, current),
        ).fetchone()
        if not row or not hmac.compare_digest(
            _token_hash(secret, share_id, claim_token), row["token_hash"]
        ):
            conn.rollback()
            return None
        updated = conn.execute(
            """UPDATE shares SET claimed_at=?,device_token_hash=?
               WHERE id=? AND revoked_at IS NULL AND expires_at>?
                 AND claimed_at IS NULL AND device_token_hash IS NULL""",
            (current, device_token_hash(secret, share_id, device_token), share_id, current),
        ).rowcount
        if updated != 1:
            conn.rollback()
            return None
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return None
    finally:
        conn.close()
    return {
        "device_token": device_token,
        "share_id": share_id,
        "expires_at": row["expires_at"],
        "toggles": json.loads(row["content_json"]),
        "directory": directory,
    }


def verify_device(path, secret: str, share_id: str, token: str, *, now=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", share_id or "") or not token:
        return None
    current = int(time.time() if now is None else now)
    conn = _connect_readonly(path)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute(
                """SELECT device_token_hash,expires_at,content_json FROM shares
                   WHERE id=? AND revoked_at IS NULL AND expires_at>?
                     AND claimed_at IS NOT NULL AND device_token_hash IS NOT NULL""",
                (share_id, current),
            ).fetchone()
        except sqlite3.Error:
            return None
    finally:
        conn.close()
    if not row or not hmac.compare_digest(
        device_token_hash(secret, share_id, token), row["device_token_hash"]
    ):
        return None
    directory = _root(path) / "exports" / share_id
    if not (directory / "content.json").is_file():
        return None
    return {
        "share_id": share_id,
        "expires_at": row["expires_at"],
        "toggles": json.loads(row["content_json"]),
        "directory": directory,
    }


def verify_active(path, share_id: str, *, now=None):
    """Return one active export without treating its source store as auth state."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", share_id or ""):
        return None
    current = int(time.time() if now is None else now)
    conn = _connect_readonly(path)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute(
                """SELECT expires_at,content_json FROM shares
                   WHERE id=? AND revoked_at IS NULL AND expires_at>?""",
                (share_id, current),
            ).fetchone()
        except sqlite3.Error:
            return None
    finally:
        conn.close()
    directory = _root(path) / "exports" / share_id
    if not row or not (directory / "content.json").is_file():
        return None
    return {
        "share_id": share_id,
        "expires_at": row["expires_at"],
        "toggles": json.loads(row["content_json"]),
        "directory": directory,
    }


def list_recording(path, secret: str, recording_id: str, *, now=None) -> list[dict]:
    current = int(time.time() if now is None else now)
    conn = _connect_readonly(path)
    if conn is None:
        return []
    try:
        try:
            rows = conn.execute(
                """SELECT id,created_at,expires_at,content_json FROM shares
                   WHERE source_hash=? AND revoked_at IS NULL AND expires_at>?
                   ORDER BY created_at DESC""",
                (_source_hash(secret, recording_id), current),
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    return [
        {
            "share_id": row["id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "content": json.loads(row["content_json"]),
        }
        for row in rows
    ]


def revoke(path, secret: str, share_id: str, *, now=None) -> int:
    del secret  # API symmetry; no caller-controlled source lookup is performed.
    current = int(time.time() if now is None else now)
    conn = _connect(path)
    try:
        result = conn.execute(
            "UPDATE shares SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (current, share_id),
        )
        conn.commit()
        if result.rowcount:
            _remove_exports(path, [share_id])
        return result.rowcount
    finally:
        conn.close()


def revoke_recording(path, secret: str, recording_id: str, *, now=None) -> int:
    current = int(time.time() if now is None else now)
    conn = _connect(path)
    try:
        source_hash = _source_hash(secret, recording_id)
        share_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM shares WHERE source_hash=? AND revoked_at IS NULL",
                (source_hash,),
            ).fetchall()
        ]
        result = conn.execute(
            """UPDATE shares SET revoked_at=?
               WHERE source_hash=? AND revoked_at IS NULL""",
            (current, source_hash),
        )
        conn.commit()
        _remove_exports(path, share_ids)
        return result.rowcount
    finally:
        conn.close()


def reconcile_sources(path, secret: str, active_recording_ids, *, now=None) -> int:
    current = int(time.time() if now is None else now)
    active_hashes = {_source_hash(secret, value) for value in active_recording_ids}
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT id,source_hash FROM shares WHERE revoked_at IS NULL AND expires_at>?",
            (current,),
        ).fetchall()
        missing = [row["id"] for row in rows if row["source_hash"] not in active_hashes]
        if missing:
            placeholders = ",".join("?" for _ in missing)
            conn.execute(
                f"UPDATE shares SET revoked_at=? WHERE id IN ({placeholders})",
                (current, *missing),
            )
        conn.commit()
        _remove_exports(path, missing)
        return len(missing)
    finally:
        conn.close()
