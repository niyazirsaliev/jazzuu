"""Bounded connector-owned reconciliation for the optional semantic index."""
from __future__ import annotations

import os

from semantic_search.chunking import DeterministicChunker
from semantic_search.index import TenantIndex
from semantic_search.lifecycle import Lifecycle, SourceDocument, source_sha256
from semantic_search.runtime import CHUNKER_VERSION, embedder_from_env, index_identity


class Utf8ByteCounter:
    """Conservative tokenizer-independent upper bound for model input units."""

    def count(self, text: str) -> int:
        return len(str(text).encode("utf-8"))


def _documents(conn) -> list[SourceDocument]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}

    def value(column, fallback="''"):
        return column if column in columns else fallback

    title = (
        f"COALESCE(NULLIF({value('semantic_title')},''),NULLIF({value('name')},''),'')"
    )
    summary = f"COALESCE({value('summary')},'')"
    transcript = (
        f"COALESCE(NULLIF({value('asr_transcript')},''),"
        f"NULLIF({value('plaud_transcript')},''),'')"
    )
    visibility = ""
    if "archived_local_at" in columns:
        visibility = " WHERE archived_local_at IS NULL OR archived_local_at=''"
    rows = conn.execute(
        f"SELECT id,{title},{summary},{transcript} FROM recordings" +
        visibility + " ORDER BY id"
    ).fetchall()
    return [SourceDocument(str(row[0]), row[1] or "", row[2] or "", row[3] or "") for row in rows]


def reconcile_visible(conn, *, tenant_id: str, index: TenantIndex, embedder,
                      batch_size: int = 8) -> dict[str, int]:
    documents = _documents(conn)
    visible_ids = {document.recording_id for document in documents}
    lifecycle = Lifecycle(
        index,
        DeterministicChunker(Utf8ByteCounter(), max_tokens=2048, overlap_tokens=16),
        embedder,
    )
    purged = lifecycle.purge_except(visible_ids)
    changed = [document for document in documents
               if index.source_hash(document.recording_id) != source_sha256(document)]
    replaced = 0
    for document in changed[: max(0, int(batch_size))]:
        lifecycle.reconcile(document)
        replaced += 1
    return {
        "replaced": replaced,
        "purged": purged,
        "remaining": max(0, len(changed) - replaced),
    }


def run_from_env(conn, tenant_id: str) -> dict[str, int]:
    if os.environ.get("SEMANTIC_SEARCH_ENABLED", "0") != "1":
        return {"replaced": 0, "purged": 0, "remaining": 0}
    path = os.environ.get("SEMANTIC_INDEX_PATH", "/archive/semantic/semantic.db")
    index = TenantIndex(path, tenant_id=tenant_id, identity=index_identity(CHUNKER_VERSION))
    return reconcile_visible(
        conn, tenant_id=tenant_id, index=index, embedder=embedder_from_env(),
        batch_size=int(os.environ.get("SEMANTIC_REINDEX_BATCH", "8")),
    )
