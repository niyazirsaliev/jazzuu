from __future__ import annotations

import math
from collections.abc import Sequence

from .contracts import EmbeddingBackend


class PrefixingNormalizedEmbedder:
    """Model-agnostic E5 request shaping with fail-closed validation."""

    def __init__(self, backend: EmbeddingBackend, *, query_prefix: str = "query", passage_prefix: str = "passage"):
        self._backend = backend
        self.dimension = int(backend.dimension)
        self.query_prefix = str(query_prefix).strip()
        self.passage_prefix = str(passage_prefix).strip()
        if self.dimension <= 0:
            raise ValueError("embedding dimension must be positive")

    def _embed(self, prefix: str, texts: Sequence[str]) -> list[list[float]]:
        shaped = [
            f"{prefix}: {str(text).strip()}" if prefix else str(text).strip()
            for text in texts
        ]
        values = self._backend.embed(shaped)
        if len(values) != len(texts):
            raise ValueError("embedding response count mismatch")
        normalized = []
        for vector in values:
            if len(vector) != self.dimension:
                raise ValueError("embedding dimension mismatch")
            numeric = [float(value) for value in vector]
            norm = math.sqrt(sum(value * value for value in numeric))
            if not math.isfinite(norm) or norm <= 0:
                raise ValueError("invalid embedding vector")
            normalized.append([value / norm for value in numeric])
        return normalized

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(self.passage_prefix, texts)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(self.query_prefix, texts)
