"""Fail-closed single-tenant configuration for recordings-mcp."""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 62390
DB_FILENAME = "archive.db"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    tenant_id: str
    code_prefix: str
    archive_dir: str
    db_path: str
    grant_state_path: str
    audio_dir: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    def describe(self):
        return f"service=recordings-mcp tenant={self.tenant_id} series={self.code_prefix}- bind={self.host}:{self.port}"


def _get(env, name):
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def _port(env):
    raw = _get(env, "RECORDINGS_MCP_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        result = int(raw)
    except ValueError:
        raise ConfigError("recordings-mcp: RECORDINGS_MCP_PORT must be a number") from None
    if not 1 <= result <= 65535:
        raise ConfigError("recordings-mcp: RECORDINGS_MCP_PORT is out of range")
    return result


def _validate_archive(path):
    if not os.path.isfile(path):
        raise ConfigError("recordings-mcp: archive.db is missing")

    def columns_for(uri):
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA query_only=1")
            return {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        finally:
            conn.close()

    try:
        try:
            columns = columns_for(f"file:{path}?mode=ro")
        except sqlite3.Error:
            if os.path.exists(path + "-wal"):
                raise
            columns = columns_for(f"file:{path}?mode=ro&immutable=1")
    except sqlite3.Error as exc:
        raise ConfigError("recordings-mcp: archive.db is corrupt or unreadable") from exc
    if not columns:
        raise ConfigError("recordings-mcp: archive.db has no recordings table")


def load(env=None):
    env = os.environ if env is None else env
    tenant_id = _get(env, "TENANT_ID")
    if not tenant_id:
        raise ConfigError("recordings-mcp: TENANT_ID is required")
    code_prefix = _get(env, "RECORDINGS_MCP_CODE_PREFIX")
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,7}", code_prefix):
        raise ConfigError("recordings-mcp: RECORDINGS_MCP_CODE_PREFIX must be 1-8 uppercase letters or digits")
    archive_dir = _get(env, "RECORDINGS_MCP_ARCHIVE_DIR")
    if not os.path.isdir(archive_dir):
        raise ConfigError("recordings-mcp: RECORDINGS_MCP_ARCHIVE_DIR must be an existing archive directory")
    db_path = os.path.join(archive_dir, DB_FILENAME)
    _validate_archive(db_path)
    state = _get(env, "RECORDINGS_MCP_GRANT_STATE")
    if not state:
        raise ConfigError("recordings-mcp: RECORDINGS_MCP_GRANT_STATE is required and must be outside the read-only archive")
    if os.path.abspath(state).startswith(os.path.abspath(archive_dir) + os.sep):
        raise ConfigError("recordings-mcp: grant state must not be inside the read-only archive")
    state_dir = os.path.dirname(os.path.abspath(state))
    if not os.path.isdir(state_dir) or not os.access(state_dir, os.W_OK):
        raise ConfigError("recordings-mcp: grant-state directory is unavailable or not writable")
    return Config(tenant_id=tenant_id, code_prefix=code_prefix,
                  archive_dir=archive_dir, db_path=db_path,
                  grant_state_path=state, audio_dir=os.path.join(archive_dir, "audio"),
                  host=_get(env, "RECORDINGS_MCP_HOST") or DEFAULT_HOST,
                  port=_port(env))
