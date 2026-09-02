from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .contracts import Chunker, Embedder
from .index import TenantIndex


@dataclass(frozen=True)
class SourceDocument:
    recording_id: str
    title: str
    summary: str
    transcript: str


def _clean(value: str) -> str:
    return "\n".join(" ".join(line.split()) for line in str(value or "").strip().splitlines())


def source_sha256(document: SourceDocument) -> str:
    body = {
        "recording_id": str(document.recording_id),
        "title": _clean(document.title),
        "summary": _clean(document.summary),
        "transcript": _clean(document.transcript),
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Lifecycle:
    """Bounded connector-owned reconciliation primitive."""

    def __init__(self, index: TenantIndex, chunker: Chunker, embedder: Embedder):
        self.index = index
        self.chunker = chunker
        self.embedder = embedder

    def reconcile(self, document: SourceDocument) -> str:
        digest = source_sha256(document)
        if self.index.source_hash(document.recording_id) == digest:
            return "unchanged"
        sections = [
            f"Заголовок: {_clean(document.title)}" if _clean(document.title) else "",
            f"Резюме: {_clean(document.summary)}" if _clean(document.summary) else "",
            _clean(document.transcript),
        ]
        text = "\n\n".join(filter(None, sections))
        chunks = self.chunker.chunk(text)
        self.index.admit(document.recording_id, digest)
        try:
            vectors = self.embedder.embed_passages(chunks)
            self.index.replace(document.recording_id, digest, list(zip(chunks, vectors)), title=_clean(document.title))
        except Exception:
            self.index.mark_retry(document.recording_id)
            raise
        return "replaced"

    def purge_except(self, visible_ids: set[str]) -> int:
        return self.index.purge_except(visible_ids)
