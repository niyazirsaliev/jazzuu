from __future__ import annotations

from collections.abc import Callable

from semantic_search.hybrid import HybridRanker
from semantic_search.runtime import readonly_provider_from_env


class ViewerSearchAdapter:
    """Viewer-specific wiring over the shared retrieval core."""

    def __init__(self, lexical_factory: Callable, semantic_factory: Callable = readonly_provider_from_env):
        self._lexical_factory = lexical_factory
        self._semantic_factory = semantic_factory
        self._ranker = HybridRanker()

    def search(self, query: str, *, semantic: bool = False, limit: int = 50) -> list[dict]:
        lexical = self._lexical_factory()
        baseline = lexical.search(query, limit)
        if not semantic or len(" ".join(str(query or "").split())) < 3:
            return baseline
        try:
            semantic_rows = self._semantic_factory().search(query, limit=min(limit, 20))
            lexical_rows = lexical.search_ranked(query, limit)
            return self._ranker.rank(lexical_rows, semantic_rows, limit=limit)
        except Exception:
            # Optional compute/index failures must preserve the exact old payload.
            return baseline
