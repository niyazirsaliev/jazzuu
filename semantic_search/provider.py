from __future__ import annotations

import html
import heapq
import math

from .contracts import Embedder
from .index import TenantIndex


class _ScoredChunk:
    def __init__(self, score, chunk):
        self.score = score
        self.chunk = chunk

    def rank_key(self):
        return (-self.score, self.chunk.recording_id, self.chunk.ordinal)

    def __lt__(self, other):
        # heapq keeps the worst retained result at the root.
        return self.rank_key() > other.rank_key()


class SemanticProvider:
    def __init__(self, index: TenantIndex, embedder: Embedder, *, max_results: int = 20, snippet_chars: int = 240, min_score: float = 0.5):
        self.index = index
        self.embedder = embedder
        self.max_results = max(1, int(max_results))
        self.snippet_chars = max(40, int(snippet_chars))
        self.min_score = max(-1.0, min(1.0, float(min_score)))

    def search(self, query: str, limit: int = 20, *, allowed_ids: set[str] | None = None) -> list[dict]:
        normalized = " ".join(str(query or "").split())[:512]
        if len(normalized) < 3:
            return []
        requested = max(1, min(int(limit), self.max_results))
        vector = self.embedder.embed_queries([normalized])[0]
        top_hits = []
        current_id = None
        best = None

        def retain(hit):
            if hit is None:
                return
            heapq.heappush(top_hits, hit)
            if len(top_hits) > requested:
                heapq.heappop(top_hits)

        for chunk in self.index.iter_candidates(allowed_ids=allowed_ids):
            if current_id is not None and chunk.recording_id != current_id:
                retain(best)
                best = None
            current_id = chunk.recording_id
            score = sum(left * right for left, right in zip(vector, chunk.vector))
            if math.isfinite(score) and score >= self.min_score:
                candidate = _ScoredChunk(score, chunk)
                if best is None or candidate.rank_key() < best.rank_key():
                    best = candidate
        retain(best)

        rows = []
        for hit in sorted(top_hits, key=lambda item: item.rank_key()):
            chunk = hit.chunk
            rows.append({
                "id": chunk.recording_id,
                "name": chunk.title or "Без названия",
                "snippet": html.escape(chunk.text[: self.snippet_chars]),
                "source": "semantic",
                "match": "semantic",
            })
        return rows
