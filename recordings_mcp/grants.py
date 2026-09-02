"""Revocable bot grants and audit state for the owner-only Recordings MCP.

This module is the sole writable part of the service.  It never opens the
archive database.  Tokens are caller-supplied high-entropy secrets and are
persisted only as SHA-256 digests; the create API deliberately never generates
or prints a credential.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import os
import re
import sqlite3
from dataclasses import dataclass

READ_SCOPES = frozenset({"metadata", "transcript", "summary", "mindmap"})
TOOL_SCOPE = {
    "__authenticate__": None,
    "recordings_list": "metadata",
    # Search proves that content matched, even if its result has no snippet.
    # It is therefore a transcript capability, never a metadata capability.
    "recordings_search": "transcript",
    "recordings_hybrid_search": "transcript",
    "recording_get": "metadata",
    "recording_mindmap_get": "mindmap",
}


class GrantError(RuntimeError):
    pass


class AuthorizationError(GrantError):
    """Intentionally indistinguishable denials for callers."""


@dataclass(frozen=True)
class Grant:
    caller_id: str
    scopes: frozenset[str]
    recordings: frozenset[str]


def hash_token(token: str) -> str:
    if not isinstance(token, str):
        raise GrantError("caller token must be text")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_expiry(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise GrantError("expiry must be an RFC3339 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GrantError("expiry must be an RFC3339 UTC timestamp") from None
    if parsed.tzinfo is None:
        raise GrantError("expiry must include a timezone")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_token(token):
    # The admin CLI generates 256-bit random credentials. Keep a lower bound for
    # direct registry callers and an upper bound so hashing work stays bounded.
    if not isinstance(token, str) or not 32 <= len(token.encode("utf-8")) <= 512:
        raise GrantError("caller token must be between 32 and 512 bytes")


def _validate_caller(caller_id):
    if not isinstance(caller_id, str):
        raise GrantError("caller_id must be a lowercase-safe identifier up to 64 characters")
    normalized = caller_id.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,63})", normalized):
        raise GrantError("caller_id must be a lowercase-safe identifier up to 64 characters")
    return normalized


def _validate_scopes(scopes):
    if scopes is None:
        return frozenset()
    if not isinstance(scopes, (list, tuple, set)) or any(not isinstance(x, str) for x in scopes):
        raise GrantError("scopes must be a list of read scopes")
    supplied = frozenset(scopes)
    unknown = supplied - READ_SCOPES
    if unknown:
        raise GrantError("unsupported scope: " + sorted(unknown)[0])
    return supplied


def _validate_recordings(recordings):
    if recordings is None:
        return frozenset({"*"})
    if not isinstance(recordings, (list, tuple, set)) or not recordings:
        raise GrantError("recordings must be a non-empty allowlist or ['*']")
    values = frozenset(recordings)
    if any(not isinstance(x, str) or (x != "*" and not x.startswith("N-")) for x in values):
        raise GrantError("recordings must contain owner N-#### numbers or '*'")
    if "*" in values and len(values) != 1:
        raise GrantError("'*' cannot be combined with recording allowlist entries")
    if len(values) > 100:
        raise GrantError("recordings allowlist may contain at most 100 entries")
    return values


class GrantRegistry:
    def __init__(self, state_path: str):
        self.state_path = state_path
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.state_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextlib.contextmanager
    def _session(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self):
        directory = os.path.dirname(os.path.abspath(self.state_path))
        if not os.path.isdir(directory):
            raise GrantError("grant state directory is unavailable")
        try:
            with self._session() as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS grants(
                  caller_id TEXT PRIMARY KEY,
                  token_hash TEXT NOT NULL UNIQUE,
                  scopes_json TEXT NOT NULL,
                  recordings_json TEXT NOT NULL,
                  expires_at TEXT,
                  disabled_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  occurred_at TEXT NOT NULL,
                  caller_id TEXT,
                  tool TEXT NOT NULL,
                  recording_number TEXT,
                  outcome TEXT NOT NULL
                );
                """)
        except sqlite3.Error as exc:
            raise GrantError("grant state is unavailable") from exc

    def health(self):
        """Verify writable authorization/audit state without exposing it."""
        try:
            with self._session() as conn:
                conn.execute("SELECT 1 FROM grants LIMIT 1").fetchone()
                conn.execute("SELECT 1 FROM audit_log LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            raise GrantError("grant state is unavailable") from exc

    def create(self, *, caller_id, token, scopes=None, recordings=None, expires_at=None):
        caller_id = _validate_caller(caller_id)
        _validate_token(token)
        scopes = _validate_scopes(scopes)
        recordings = _validate_recordings(recordings)
        expiry = _parse_expiry(expires_at)
        now = _now()
        try:
            with self._session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if self._lookup_caller(conn, caller_id) is not None:
                    raise GrantError("caller_id or caller token already exists")
                conn.execute(
                    "INSERT INTO grants(caller_id,token_hash,scopes_json,recordings_json,expires_at,disabled_at,created_at,updated_at) VALUES(?,?,?,?,?,NULL,?,?)",
                    (caller_id, hash_token(token), ",".join(sorted(scopes)), ",".join(sorted(recordings)), expiry, now, now),
                )
                self._audit_mutation(conn, caller_id, "admin.create", now)
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise GrantError("caller_id or caller token already exists") from exc
        except sqlite3.Error as exc:
            raise GrantError("grant state is unavailable") from exc
        # Do not return a token, even the one caller supplied: callers must keep
        # their credential in their own secret store.
        return {"caller_id": caller_id, "scopes": sorted(scopes),
                "recordings": sorted(recordings), "expires_at": expiry,
                "disabled": False}

    def list(self):
        try:
            with self._session() as conn:
                rows = conn.execute("SELECT caller_id,scopes_json,recordings_json,expires_at,disabled_at,created_at,updated_at FROM grants ORDER BY caller_id").fetchall()
        except sqlite3.Error as exc:
            raise GrantError("grant state is unavailable") from exc
        return [{"caller_id": r["caller_id"], "scopes": r["scopes_json"].split(",") if r["scopes_json"] else [],
                 "recordings": r["recordings_json"].split(","), "expires_at": r["expires_at"],
                 "disabled": bool(r["disabled_at"]), "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]

    def revoke(self, *, caller_id):
        caller_id = _validate_caller(caller_id)
        now = _now()
        try:
            with self._session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                actual_caller = self._lookup_caller(conn, caller_id)
                row = (conn.execute(
                    "SELECT disabled_at FROM grants WHERE caller_id=?", (actual_caller,)
                ).fetchone() if actual_caller is not None else None)
                if row is None:
                    raise GrantError("grant does not exist")
                changed = row["disabled_at"] is None
                if changed:
                    conn.execute(
                        "UPDATE grants SET disabled_at=?,updated_at=? WHERE caller_id=?",
                        (now, now, actual_caller),
                    )
                    self._audit_mutation(conn, actual_caller, "admin.revoke", now)
                conn.commit()
        except GrantError:
            raise
        except sqlite3.Error as exc:
            raise GrantError("grant state is unavailable") from exc
        return {"caller_id": caller_id, "revoked": True, "changed": changed}

    def rotate(self, *, caller_id, token):
        caller_id = _validate_caller(caller_id)
        _validate_token(token)
        now = _now()
        try:
            with self._session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                actual_caller = self._lookup_caller(conn, caller_id)
                row = (conn.execute(
                    "SELECT scopes_json,recordings_json,expires_at FROM grants WHERE caller_id=?",
                    (actual_caller,),
                ).fetchone() if actual_caller is not None else None)
                if row is None:
                    raise GrantError("grant does not exist")
                conn.execute(
                    "UPDATE grants SET token_hash=?,disabled_at=NULL,updated_at=? WHERE caller_id=?",
                    (hash_token(token), now, actual_caller),
                )
                self._audit_mutation(conn, actual_caller, "admin.rotate", now)
                conn.commit()
        except GrantError:
            raise
        except sqlite3.IntegrityError as exc:
            raise GrantError("caller token already exists") from exc
        except sqlite3.Error as exc:
            raise GrantError("grant state is unavailable") from exc
        return {"caller_id": caller_id,
                "scopes": row["scopes_json"].split(",") if row["scopes_json"] else [],
                "recordings": row["recordings_json"].split(","),
                "expires_at": row["expires_at"], "disabled": False}

    @staticmethod
    def _lookup_caller(conn, normalized_caller):
        """Resolve pre-canonical IDs safely and prevent case-only duplicates."""
        rows = conn.execute(
            "SELECT caller_id FROM grants WHERE lower(caller_id)=? LIMIT 2",
            (normalized_caller,),
        ).fetchall()
        if len(rows) > 1:
            raise GrantError("caller identity is ambiguous")
        return rows[0]["caller_id"] if rows else None

    @staticmethod
    def _audit_mutation(conn, caller_id, tool, occurred_at):
        conn.execute(
            "INSERT INTO audit_log(occurred_at,caller_id,tool,recording_number,outcome) VALUES(?,?,?,NULL,?)",
            (occurred_at, caller_id, tool, "mutated"),
        )

    def authorize(self, token, tool, number=None):
        """Reload persistent state for every request; any state error denies."""
        if tool not in TOOL_SCOPE or not isinstance(token, str) or not token:
            raise AuthorizationError("unauthorized")
        try:
            with self._session() as conn:
                row = conn.execute("SELECT * FROM grants WHERE token_hash=?", (hash_token(token),)).fetchone()
        except sqlite3.Error as exc:
            raise AuthorizationError("unauthorized") from exc
        if row is None or row["disabled_at"]:
            raise AuthorizationError("unauthorized")
        expiry = row["expires_at"]
        if expiry and expiry <= _now():
            raise AuthorizationError("unauthorized")
        # digest equality is a second fixed-size check after indexed lookup.
        if not hmac.compare_digest(row["token_hash"], hash_token(token)):
            raise AuthorizationError("unauthorized")
        scopes = frozenset(filter(None, row["scopes_json"].split(",")))
        recordings = frozenset(filter(None, row["recordings_json"].split(",")))
        required = TOOL_SCOPE[tool]
        if required is not None and required not in scopes:
            raise AuthorizationError("unauthorized")
        if number is not None and "*" not in recordings and number not in recordings:
            raise AuthorizationError("unauthorized")
        return Grant(row["caller_id"], scopes, recordings)

    def audit(self, grant, tool, number, outcome, detail=None):
        """Persist only safe event metadata; detail is intentionally discarded."""
        try:
            with self._session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO audit_log(occurred_at,caller_id,tool,recording_number,outcome) VALUES(?,?,?,?,?)",
                             (_now(), getattr(grant, "caller_id", None), str(tool)[:80],
                              str(number)[:32] if number else None, str(outcome)[:32]))
                conn.commit()
        except sqlite3.Error:
            # Audit write failure is a security failure: the request path must
            # fail closed rather than provide unaccountable archive access.
            raise AuthorizationError("unauthorized")

    def audit_denied(self, token, tool, number):
        """Durably record a refused tool attempt without exposing grant state."""
        try:
            with self._session() as conn:
                caller_id = None
                if isinstance(token, str) and token:
                    row = conn.execute(
                        "SELECT caller_id FROM grants WHERE token_hash=?",
                        (hash_token(token),)).fetchone()
                    caller_id = row["caller_id"] if row else None
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO audit_log(occurred_at,caller_id,tool,recording_number,outcome) VALUES(?,?,?,?,?)",
                    (_now(), caller_id, str(tool)[:80],
                     str(number)[:32] if number else None, "denied"))
                conn.commit()
        except sqlite3.Error:
            # A failed audit is also fail-closed; no denied attempt becomes an
            # unaccountable successful response through a fallback path.
            raise AuthorizationError("unauthorized")
