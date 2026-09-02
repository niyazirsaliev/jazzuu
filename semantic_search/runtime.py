from __future__ import annotations

import json
import os
from pathlib import Path

from .embedder import PrefixingNormalizedEmbedder
from .index import IndexIdentity, TenantIndex
from .ollama import OllamaBackend
from .provider import SemanticProvider


_MANIFEST = json.loads((Path(__file__).with_name("model_manifest.json")).read_text(encoding="utf-8"))
CHUNKER_VERSION = "sentences-utf8bytes-v3"


def index_identity(chunker_version: str = CHUNKER_VERSION) -> IndexIdentity:
    return IndexIdentity(
        model_id=_MANIFEST["model"],
        model_revision=_MANIFEST["model_digest"],
        model_checksum=_MANIFEST["model_digest"],
        dimension=int(_MANIFEST["dimension"]),
        chunker_version=chunker_version,
    )


def embedder_from_env() -> PrefixingNormalizedEmbedder:
    backend = OllamaBackend(
        endpoint=os.environ.get("OLLAMA_EMBED_ENDPOINT", "http://host.docker.internal:11434"),
        model=_MANIFEST["model"], digest=_MANIFEST["model_digest"],
        dimension=int(_MANIFEST["dimension"]),
        timeout_seconds=float(os.environ.get("SEMANTIC_QUERY_TIMEOUT_S", "0.75")),
    )
    return PrefixingNormalizedEmbedder(backend, query_prefix="", passage_prefix="")


def readonly_provider_from_env() -> SemanticProvider:
    if os.environ.get("SEMANTIC_SEARCH_ENABLED", "0") != "1":
        raise RuntimeError("semantic search disabled")
    tenant_id = os.environ.get("TENANT_ID", "").strip()
    index_path = os.environ.get("SEMANTIC_INDEX_PATH", "").strip()
    if not tenant_id or not index_path:
        raise RuntimeError("semantic tenant index is not configured")
    embedder = embedder_from_env()
    index = TenantIndex(index_path, tenant_id=tenant_id, identity=index_identity(), readonly=True)
    return SemanticProvider(index, embedder, max_results=20)
