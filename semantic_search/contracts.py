from __future__ import annotations

from typing import Protocol, Sequence


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class EmbeddingBackend(Protocol):
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Embedder(Protocol):
    dimension: int

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


class Chunker(Protocol):
    version: str

    def chunk(self, text: str) -> list[str]: ...


class SemanticSearch(Protocol):
    def search(self, query: str, limit: int = 20) -> list[dict]: ...
