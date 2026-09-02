"""Tenant-local semantic retrieval core.

Model/runtime adapters live outside this package. The core never calls a hosted
service and never owns credentials.
"""

from .chunking import DeterministicChunker
from .embedder import PrefixingNormalizedEmbedder
from .hybrid import HybridRanker
from .index import IndexIdentity, TenantIndex
from .lifecycle import Lifecycle, SourceDocument
from .provider import SemanticProvider

__all__ = [
    "DeterministicChunker",
    "PrefixingNormalizedEmbedder",
    "HybridRanker",
    "IndexIdentity",
    "TenantIndex",
    "Lifecycle",
    "SourceDocument",
    "SemanticProvider",
]
