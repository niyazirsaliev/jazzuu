# Tenant-local semantic search

## Contract

Lexical FTS remains the default API and the first browser result. `GET /api/search?q=...&semantic=true` is an optional hybrid upgrade. The recordings MCP keeps `recordings_search` unchanged and adds `recordings_hybrid_search` with the same transcript grant scope and stable-code visibility rules.

Ranking is deterministic: lexical exact matches, lexical token/prefix matches, then semantic-only matches. Recording IDs are deduplicated. Semantic snippets are escaped and vectors, scores, transcripts, and model diagnostics never enter responses.

Any missing, stale, incompatible, corrupt, locked, or slow semantic index and any Ollama error falls back to lexical search. A reader never creates or mutates an index.

## Privacy and authorization

- One physical index lives inside each tenant archive at `/archive/semantic/semantic.db`.
- The connector mounts that tenant archive read/write. Viewer and recordings MCP mount the same tenant archive read-only.
- The index manifest binds the file to one tenant and one immutable model/adapter/chunker identity. A reader fails closed on mismatch. A writer clears only the derived index and performs bounded deterministic rebuilding.
- MCP allowed recording IDs are included in the index candidate SQL. Unauthorized chunks are not materialized and filtered after ranking.
- Public-share routes do not expose search or the recordings MCP.
- Queries and passages go only to an operator-managed Ollama endpoint over the Docker host gateway. No credentials, hosted embedding APIs, telemetry, or model downloads are added.
- The service worker caches an exact static allowlist only. API queries, index files, embeddings, and model artifacts cannot enter the PWA cache.
- Logs contain operation state and exception classes, never query, transcript, chunk, vector, or token content.

## Model and chunking identity

The reviewed manifest is `semantic_search/model_manifest.json`.

- Model: `BAAI/bge-m3`, served by Ollama as `bge-m3:latest`
- Immutable fleet digest: `7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`
- Dimension: 1024, L2 normalized before indexing and query scoring
- Input policy: raw BGE-M3, no query or passage prefix
- License: MIT
- Upstream model card: https://huggingface.co/BAAI/bge-m3
- Upstream license: https://huggingface.co/BAAI/bge-m3/blob/main/LICENSE
- Adapter: native Ollama `/api/embed`, with `truncate=false`

The runtime verifies the installed Ollama digest before every batch and fails closed on mutable-tag drift. Images package the manifest and adapter but no model weights.

Chunking is deterministic sentence-oriented `sentences-utf8bytes-v3`. Its conservative UTF-8 byte counter is an upper bound for model tokens and enforces a 2048-byte input ceiling, safely below BGE-M3's 8192-token context. Oversized unbroken tokens are split losslessly at character boundaries, and overlap is trimmed before a full next piece so every published chunk remains within the bound. Chunk overlap and chunker version participate in index identity.

## Durable lifecycle

The canonical archive pipeline has no optional derived stage whose failure can be excluded from archive readiness. Semantic indexing therefore uses the allowed connector-owned bounded reconciliation ledger inside the derived tenant index.

For every changed recording:

1. Calculate a stable source hash over title, summary, and preferred transcript.
2. Commit `reconcile_jobs` intent before embedding work.
3. Chunk and embed outside the index write transaction.
4. Atomically publish a new per-recording generation and clear intent.
5. Preserve the previous visible generation on failure and retry during a later connector pass.

Each pass indexes at most `SEMANTIC_REINDEX_BATCH` changed recordings and purges IDs no longer visible. Unchanged recordings are skipped. Model/chunker identity changes atomically invalidate only derived records and trigger the same bounded rebuild. Readers fail closed for every SQLite error. A writer quarantines and recreates derived state only for verified `SQLITE_CORRUPT`/`SQLITE_NOTADB`; lock, busy, I/O, and other transient errors preserve the published index for lexical fallback and a later retry. Quarantine uses one bounded atomic replacement generation and refuses an `archive.db` path. `archive.db` remains the sole archive authority.

## Acceptance evidence

Sanitized retrieval fixture: `tests/fixtures/semantic_retrieval.json`.

Actual pinned-model result:

- Russian/Kyrgyz/paraphrase/hard-negative cases: 4
- Recall@3: 1.0, required 1.0
- MRR: 0.875, required 0.8


Production-shaped benchmark used 140 sanitized records, using fully synthetic tenant data:

- Initial build: 24.619 s
- One-record incremental update: 181.20 ms
- Warm query p50: 134.19 ms
- Warm query p95: 143.01 ms, release threshold 750 ms
- Cold-load query: 2230.08 ms; this path degrades to lexical under the 750 ms interactive timeout
- Index size: 1,798,144 bytes
- The operator-managed model artifact is shared; added per-service model storage is zero.
- Exact streaming scan support is validated at 2,500 tenant-local 1024-dimensional chunks with no prefix cap: p50 205.14 ms, p95 215.31 ms, 11,792,384-byte index. This is the documented supported corpus ceiling for the initial release; larger corpora remain exact but require a fresh latency qualification before release.


## Operations and rollback

- Disable immediately with `SEMANTIC_SEARCH_ENABLED=0`; lexical APIs and UI remain unchanged.
- Re-enable and let bounded reconciliation resume. No full re-download or re-embedding is required for unchanged compatible records.
- If the manifest identity changes, connector rebuilding is automatic and bounded.
- Removing `/archive/semantic/semantic.db` is safe only as an explicit derived-index recovery action; never remove `archive.db`.
- Roll back application images normally. Old readers reject an incompatible derived index and continue lexical search.
