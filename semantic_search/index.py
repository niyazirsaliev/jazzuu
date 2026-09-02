from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class IndexIdentity:
    model_id: str
    model_revision: str
    model_checksum: str
    dimension: int
    chunker_version: str

    def canonical(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StoredChunk:
    recording_id: str
    ordinal: int
    text: str
    vector: tuple[float, ...]
    title: str


def _pack(vector):
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob, dimension):
    if len(blob) != dimension * 4:
        raise ValueError("corrupt vector dimension")
    return struct.unpack(f"<{dimension}f", blob)


def _is_corruption(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    primary_code = code & 0xFF
    return primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


class TenantIndex:
    """One physical SQLite index for exactly one tenant."""

    def __init__(self, path, *, tenant_id: str, identity: IndexIdentity, readonly: bool = False):
        self.path = Path(path)
        self.tenant_id = str(tenant_id)
        self.identity = identity
        self.readonly = bool(readonly)
        if not self.readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            if self.readonly or not _is_corruption(exc):
                message = "corrupt semantic index" if _is_corruption(exc) else "semantic index unavailable"
                raise ValueError(message) from exc
            # This file is derived state, never the archive authority. A writer
            # may discard corruption and deterministically rebuild it; readers
            # fail closed and keep lexical search active.
            try:
                self._quarantine_corrupt_index()
                self._initialize()
            except (OSError, sqlite3.DatabaseError) as recovery:
                raise ValueError("corrupt semantic index") from recovery

    def _quarantine_corrupt_index(self) -> None:
        if self.path.name == "archive.db":
            raise OSError("refusing to quarantine archive authority")
        if not self.path.exists():
            raise OSError("corrupt semantic index disappeared")
        # One deterministic quarantine generation per SQLite file keeps
        # recovery bounded. os.replace publishes each rename atomically.
        for suffix in ("", "-wal", "-shm", "-journal"):
            source = Path(str(self.path) + suffix)
            if source.exists():
                os.replace(source, Path(str(self.path) + ".corrupt" + suffix))

    def _connect(self):
        if self.readonly:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=0.25)
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = sqlite3.connect(self.path, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self):
        with self._connect() as conn:
            if not self.readonly:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS manifest(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    recording_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reconcile_jobs(
                    recording_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chunks(
                    recording_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY(recording_id,generation,ordinal)
                );
            """)
            stored = dict(conn.execute("SELECT key,value FROM manifest"))
            if not stored:
                conn.executemany("INSERT INTO manifest(key,value) VALUES(?,?)", (
                    ("tenant_id", self.tenant_id),
                    ("identity", self.identity.canonical()),
                ))
            elif stored.get("tenant_id") != self.tenant_id:
                raise ValueError("tenant index mismatch")
            elif stored.get("identity") != self.identity.canonical():
                if self.readonly:
                    raise ValueError("index identity mismatch; reindex required")
                # Derived data only: clear the old generation atomically and let
                # bounded connector reconciliation repopulate the new identity.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM records")
                conn.execute("DELETE FROM reconcile_jobs")
                conn.execute("UPDATE manifest SET value=? WHERE key='identity'",
                             (self.identity.canonical(),))
                conn.commit()

    def source_hash(self, recording_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT source_hash FROM records WHERE recording_id=?", (recording_id,)).fetchone()
            return row[0] if row else None

    def admit(self, recording_id: str, source_hash: str) -> None:
        if self.readonly:
            raise PermissionError("semantic index is read-only")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reconcile_jobs(recording_id,source_hash,state,attempts) VALUES(?,?,'queued',0) "
                "ON CONFLICT(recording_id) DO UPDATE SET source_hash=excluded.source_hash,state='queued'",
                (recording_id, source_hash),
            )
            conn.commit()

    def mark_retry(self, recording_id: str) -> None:
        if self.readonly:
            raise PermissionError("semantic index is read-only")
        with self._connect() as conn:
            conn.execute(
                "UPDATE reconcile_jobs SET state='retry',attempts=attempts+1 WHERE recording_id=?",
                (recording_id,),
            )
            conn.commit()

    def replace(self, recording_id: str, source_hash: str, chunks, *, title: str = "") -> None:
        if self.readonly:
            raise PermissionError("semantic index is read-only")
        prepared = []
        for ordinal, (text, vector) in enumerate(chunks):
            numeric = tuple(float(value) for value in vector)
            if len(numeric) != self.identity.dimension:
                raise ValueError("embedding dimension mismatch")
            if not all(math.isfinite(value) for value in numeric):
                raise ValueError("invalid embedding vector")
            prepared.append((recording_id, ordinal, str(text), _pack(numeric)))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT generation FROM records WHERE recording_id=?", (recording_id,)).fetchone()
            generation = (int(row[0]) + 1) if row else 1
            conn.executemany(
                "INSERT INTO chunks(recording_id,generation,ordinal,text,vector) VALUES(?,?,?,?,?)",
                [(rid, generation, ordinal, text, vector) for rid, ordinal, text, vector in prepared],
            )
            conn.execute(
                "INSERT INTO records(recording_id,source_hash,generation,title) VALUES(?,?,?,?) "
                "ON CONFLICT(recording_id) DO UPDATE SET source_hash=excluded.source_hash,generation=excluded.generation,title=excluded.title",
                (recording_id, source_hash, generation, str(title or "")),
            )
            conn.execute("DELETE FROM chunks WHERE recording_id=? AND generation<>?", (recording_id, generation))
            conn.execute("DELETE FROM reconcile_jobs WHERE recording_id=?", (recording_id,))
            conn.commit()

    def chunks_for(self, recording_id: str) -> list[StoredChunk]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.recording_id,c.ordinal,c.text,c.vector,r.title FROM chunks c "
                "JOIN records r ON r.recording_id=c.recording_id AND r.generation=c.generation "
                "WHERE c.recording_id=? ORDER BY c.ordinal",
                (recording_id,),
            ).fetchall()
        return [StoredChunk(row[0], row[1], row[2], _unpack(row[3], self.identity.dimension), row[4]) for row in rows]

    def _candidate_query(self, limit: int | None, allowed_ids: set[str] | None):
        params: list[object] = []
        where = ""
        if allowed_ids is not None:
            values = sorted(set(allowed_ids))
            if not values:
                return None, []
            # json_each keeps this to one bound SQL variable even for a full
            # owner archive, so authorization remains inside candidate retrieval.
            where = " WHERE c.recording_id IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(values, ensure_ascii=False))
        suffix = " ORDER BY c.recording_id,c.ordinal"
        if limit is not None:
            params.append(max(1, int(limit)))
            suffix += " LIMIT ?"
        sql = (
            "SELECT c.recording_id,c.ordinal,c.text,c.vector,r.title FROM chunks c "
            "JOIN records r ON r.recording_id=c.recording_id AND r.generation=c.generation" +
            where + suffix
        )
        return sql, params

    def iter_candidates(self, *, allowed_ids: set[str] | None = None, batch_size: int = 128):
        sql, params = self._candidate_query(None, allowed_ids)
        if sql is None:
            return
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            while rows := cursor.fetchmany(max(1, int(batch_size))):
                for row in rows:
                    yield StoredChunk(
                        row[0], row[1], row[2],
                        _unpack(row[3], self.identity.dimension), row[4],
                    )

    def candidates(self, limit: int | None = None, allowed_ids: set[str] | None = None) -> list[StoredChunk]:
        sql, params = self._candidate_query(limit, allowed_ids)
        if sql is None:
            return []
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            StoredChunk(row[0], row[1], row[2], _unpack(row[3], self.identity.dimension), row[4])
            for row in rows
        ]

    def purge_except(self, visible_ids: set[str]) -> int:
        if self.readonly:
            raise PermissionError("semantic index is read-only")
        with self._connect() as conn:
            existing = {row[0] for row in conn.execute(
                "SELECT recording_id FROM records UNION SELECT recording_id FROM reconcile_jobs"
            )}
            stale = sorted(existing - set(visible_ids))
            for recording_id in stale:
                conn.execute("DELETE FROM chunks WHERE recording_id=?", (recording_id,))
                conn.execute("DELETE FROM records WHERE recording_id=?", (recording_id,))
                conn.execute("DELETE FROM reconcile_jobs WHERE recording_id=?", (recording_id,))
            conn.commit()
        return len(stale)
