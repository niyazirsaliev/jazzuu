import html
import math
import sqlite3
import struct
from typing import cast

import pytest

from semantic_search.chunking import DeterministicChunker
from semantic_search.embedder import PrefixingNormalizedEmbedder
from semantic_search.hybrid import HybridRanker
from semantic_search.index import IndexIdentity, TenantIndex
from semantic_search.lifecycle import Lifecycle, SourceDocument, source_sha256
from semantic_search.provider import SemanticProvider
from semantic_search import runtime


class WordCounter:
    def count(self, text):
        return len(text.split())


class Utf8ByteCounter:
    def count(self, text):
        return len(text.encode("utf-8"))


class RecordingBackend:
    dimension = 3

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[3.0, 4.0, 0.0] for _ in texts]


class MeaningBackend:
    dimension = 3

    def embed(self, texts):
        rows = []
        for text in texts:
            folded = text.casefold()
            if "тормоз" in folded or "ремонт" in folded:
                rows.append([1.0, 0.0, 0.0])
            elif "бюджет" in folded:
                rows.append([0.0, 1.0, 0.0])
            else:
                rows.append([0.0, 0.0, 1.0])
        return rows


IDENTITY = IndexIdentity(
    model_id="test/local-adapter",
    model_revision="fixture-v1",
    model_checksum="0" * 64,
    dimension=3,
    chunker_version="sentences-v1",
)


def test_embedder_applies_e5_prefixes_and_normalizes_vectors():
    backend = RecordingBackend()
    embedder = PrefixingNormalizedEmbedder(backend)

    passage = embedder.embed_passages(["текст"])[0]
    query = embedder.embed_queries(["запрос"])[0]

    assert backend.calls == [["passage: текст"], ["query: запрос"]]
    assert passage == pytest.approx([0.6, 0.8, 0.0])
    assert query == pytest.approx([0.6, 0.8, 0.0])
    assert math.sqrt(sum(value * value for value in passage)) == pytest.approx(1.0)


def test_chunker_is_deterministic_overlapping_and_token_bounded():
    chunker = DeterministicChunker(WordCounter(), max_tokens=7, overlap_tokens=2)
    text = "Первое короткое предложение. Второе предложение заметно длиннее обычного.\n\nТретий абзац завершает мысль."

    first = chunker.chunk(text)
    second = chunker.chunk(text)

    assert first == second
    assert len(first) >= 2
    assert all(0 < WordCounter().count(value) <= 7 for value in first)
    assert set(first[0].split()[-2:]) & set(first[1].split()[:2])


def test_chunker_splits_one_oversized_unbroken_token_without_data_loss():
    text = "к" * 100
    chunker = DeterministicChunker(Utf8ByteCounter(), max_tokens=32, overlap_tokens=0)

    chunks = chunker.chunk(text)

    assert "".join(chunks) == text
    assert len(chunks) > 1
    assert all(Utf8ByteCounter().count(chunk) <= 32 for chunk in chunks)


def test_chunker_trims_overlap_before_starting_a_full_next_piece():
    text = ("слово " * 20) + ("к" * 100)
    chunker = DeterministicChunker(Utf8ByteCounter(), max_tokens=64, overlap_tokens=4)

    chunks = chunker.chunk(text)

    assert chunks
    assert all(Utf8ByteCounter().count(chunk) <= 64 for chunk in chunks)


def test_source_hash_is_canonical_and_content_sensitive():
    base = SourceDocument("r1", " Заголовок ", "Резюме", "Текст")
    equivalent = SourceDocument("r1", "Заголовок", "Резюме", "Текст")
    changed = SourceDocument("r1", "Заголовок", "Резюме", "Другой текст")

    assert source_sha256(base) == source_sha256(equivalent)
    assert source_sha256(base) != source_sha256(changed)
    assert len(source_sha256(base)) == 64


def test_tenant_index_replaces_generation_atomically_and_purges_stale_chunks(tmp_path):
    path = tmp_path / "semantic.db"
    index = TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    index.replace("r1", "a" * 64, [("old one", [1.0, 0.0, 0.0]), ("old two", [1.0, 0.0, 0.0])])

    index.replace("r1", "b" * 64, [("new", [0.0, 1.0, 0.0])])

    assert [row.text for row in index.chunks_for("r1")] == ["new"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE recording_id='r1'").fetchone()[0] == 1


def test_failed_replacement_preserves_previous_searchable_generation(tmp_path):
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    index.replace("r1", "a" * 64, [("old", [1.0, 0.0, 0.0])])

    with pytest.raises(ValueError, match="dimension"):
        index.replace("r1", "b" * 64, [("bad", [1.0, 0.0])])

    assert [row.text for row in index.chunks_for("r1")] == ["old"]
    assert index.source_hash("r1") == "a" * 64


def test_tenant_and_model_identity_mismatch_fail_closed_or_reindex(tmp_path):
    path = tmp_path / "semantic.db"
    original = TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    original.replace("r1", "a" * 64, [("old", [1.0, 0.0, 0.0])])

    with pytest.raises(ValueError, match="tenant"):
        TenantIndex(path, tenant_id="tenant-b", identity=IDENTITY)
    changed = IndexIdentity(**{**IDENTITY.__dict__, "dimension": 4})
    with pytest.raises(ValueError, match="identity"):
        TenantIndex(path, tenant_id="owner", identity=changed, readonly=True)

    rebuilt = TenantIndex(path, tenant_id="owner", identity=changed)
    assert rebuilt.candidates(10) == []
    assert rebuilt.identity == changed


def test_lifecycle_skips_unchanged_replaces_changed_and_purges_hidden(tmp_path):
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    embedder = PrefixingNormalizedEmbedder(MeaningBackend())
    lifecycle = Lifecycle(index, DeterministicChunker(WordCounter(), 20, 2), embedder)
    first = SourceDocument("visible", "Машина", "", "Поменяли тормозные колодки")

    assert lifecycle.reconcile(first) == "replaced"
    assert lifecycle.reconcile(first) == "unchanged"
    assert lifecycle.reconcile(SourceDocument("visible", "Машина", "", "Проверили тормоза")) == "replaced"
    assert lifecycle.purge_except(set()) == 1
    assert index.chunks_for("visible") == []


def test_purge_removes_hidden_pending_intent_without_visible_generation(tmp_path):
    path = tmp_path / "semantic.db"
    index = TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    index.admit("hidden-pending", "a" * 64)

    assert index.purge_except(set()) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM reconcile_jobs").fetchone()[0] == 0


def test_lifecycle_commits_durable_intent_before_embedding_work(tmp_path):
    path = tmp_path / "semantic.db"
    index = TenantIndex(path, tenant_id="owner", identity=IDENTITY)

    class InspectingBackend(MeaningBackend):
        def embed(self, texts):
            with sqlite3.connect(path) as other:
                assert other.execute(
                    "SELECT recording_id,state FROM reconcile_jobs"
                ).fetchall() == [("r1", "queued")]
            return super().embed(texts)

    lifecycle = Lifecycle(
        index, DeterministicChunker(WordCounter(), 20, 2),
        PrefixingNormalizedEmbedder(InspectingBackend()),
    )

    assert lifecycle.reconcile(SourceDocument("r1", "Машина", "", "Тормоза")) == "replaced"
    with sqlite3.connect(path) as other:
        assert other.execute("SELECT COUNT(*) FROM reconcile_jobs").fetchone()[0] == 0


def test_semantic_provider_returns_escaped_bounded_snippet_and_no_vectors(tmp_path):
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    index.replace("r1", "a" * 64, [("<script>alert(1)</script> поменяли тормозные колодки", [1.0, 0.0, 0.0])], title="Ремонт")
    provider = SemanticProvider(index, PrefixingNormalizedEmbedder(MeaningBackend()), max_results=5)

    rows = provider.search("ремонт автомобиля", limit=50)

    assert rows == [{
        "id": "r1",
        "name": "Ремонт",
        "snippet": html.escape("<script>alert(1)</script> поменяли тормозные колодки"),
        "source": "semantic",
        "match": "semantic",
    }]
    assert "vector" not in rows[0] and "score" not in rows[0]


def test_semantic_provider_drops_hard_negatives_below_similarity_floor(tmp_path):
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    index.replace("negative", "a" * 64, [("unrelated", [0.0, 1.0, 0.0])])
    provider = SemanticProvider(
        index, PrefixingNormalizedEmbedder(MeaningBackend()), min_score=0.5
    )

    assert provider.search("ремонт автомобиля") == []


def test_semantic_provider_streams_default_corpus_without_candidate_cap():
    class RecordingIndex:
        observed_allowed_ids = "unset"

        def iter_candidates(self, *, allowed_ids=None):
            self.observed_allowed_ids = allowed_ids
            return iter(())

    index = RecordingIndex()
    provider = SemanticProvider(cast(TenantIndex, index), PrefixingNormalizedEmbedder(MeaningBackend()))

    assert provider.search("ремонт автомобиля") == []
    assert index.observed_allowed_ids is None


def test_hybrid_ranker_keeps_lexical_order_and_deduplicates_semantic_hits():
    lexical = [
        {"id": "exact", "name": "ремонт", "snippet": "x", "match": "exact"},
        {"id": "prefix", "name": "ремонтный", "snippet": "y", "match": "prefix"},
    ]
    semantic = [
        {"id": "prefix", "name": "duplicate", "snippet": "z", "source": "semantic", "match": "semantic"},
        {"id": "meaning", "name": "Колодки", "snippet": "z", "source": "semantic", "match": "semantic"},
    ]

    rows = HybridRanker().rank(lexical, semantic, limit=10)

    assert [row["id"] for row in rows] == ["exact", "prefix", "meaning"]
    assert rows[0]["source"] == "lexical"
    assert rows[1]["name"] == "ремонтный"
    assert rows[2]["label"] == "По смыслу"


def test_allowlist_is_applied_before_candidate_iteration_ranking_and_limit(tmp_path):
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    for recording_id in ("a-foreign", "b-foreign", "z-allowed"):
        index.replace(recording_id, recording_id.ljust(64, "0")[:64], [(recording_id, [1.0, 0.0, 0.0])])
    provider = SemanticProvider(index, PrefixingNormalizedEmbedder(MeaningBackend()))

    rows = provider.search("ремонт", allowed_ids={"z-allowed"})

    assert [row["id"] for row in rows] == ["z-allowed"]


def test_authorization_filter_supports_full_archive_without_post_filtering(tmp_path):
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    index.replace("allowed-149", "a" * 64, [("repair", [1.0, 0.0, 0.0])])
    allowed = {f"allowed-{number}" for number in range(150)}

    rows = index.candidates(10, allowed_ids=allowed)

    assert [row.recording_id for row in rows] == ["allowed-149"]


def test_readonly_index_never_creates_or_replaces_content(tmp_path):
    path = tmp_path / "semantic.db"
    TenantIndex(path, tenant_id="owner", identity=IDENTITY).replace(
        "r1", "a" * 64, [("text", [1.0, 0.0, 0.0])]
    )
    readonly = TenantIndex(path, tenant_id="owner", identity=IDENTITY, readonly=True)

    assert [row.recording_id for row in readonly.candidates(10)] == ["r1"]
    with pytest.raises(PermissionError, match="read-only"):
        readonly.replace("r2", "b" * 64, [])


def test_corrupt_index_fails_closed_for_reader_and_writer_recovers_derived_state(tmp_path):
    path = tmp_path / "semantic.db"
    path.write_bytes(b"not sqlite")

    with pytest.raises(ValueError, match="corrupt"):
        TenantIndex(path, tenant_id="owner", identity=IDENTITY, readonly=True)

    recovered = TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    assert recovered.candidates(10) == []


def test_writer_never_mistakes_lock_contention_for_corruption(tmp_path):
    path = tmp_path / "semantic.db"
    index = TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    index.replace("kept", "a" * 64, [("safe", [1.0, 0.0, 0.0])])
    published_inode = path.stat().st_ino
    published_bytes = path.read_bytes()
    lock = sqlite3.connect(path)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(ValueError, match="unavailable"):
            TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    finally:
        lock.rollback()
        lock.close()

    assert path.stat().st_ino == published_inode
    assert path.read_bytes() == published_bytes
    assert list(tmp_path.glob("semantic.db.corrupt*")) == []
    reopened = TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    assert [row.recording_id for row in reopened.candidates()] == ["kept"]


def test_sqlite_busy_code_cannot_be_overridden_by_corruption_words(tmp_path, monkeypatch):
    calls = 0

    def busy(_self):
        nonlocal calls
        calls += 1
        error = sqlite3.OperationalError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        error.sqlite_errorname = "SQLITE_BUSY"
        raise error

    monkeypatch.setattr(TenantIndex, "_initialize", busy)

    with pytest.raises(ValueError, match="unavailable"):
        TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)

    assert calls == 1
    assert list(tmp_path.iterdir()) == []


def test_runtime_semantic_search_scores_winner_after_first_two_thousand(tmp_path, monkeypatch):
    path = tmp_path / "semantic.db"
    TenantIndex(path, tenant_id="owner", identity=IDENTITY)
    with sqlite3.connect(path) as conn:
        records = []
        chunks = []
        for number in range(2001):
            recording_id = f"r-{number:04d}"
            records.append((recording_id, str(number), 1, recording_id))
            vector = [0.0, 1.0, 0.0]
            if number == 2000:
                vector = [1.0, 0.0, 0.0]
            chunks.append((recording_id, 1, 0, recording_id, struct.pack("<3f", *vector)))
        conn.executemany(
            "INSERT INTO records(recording_id,source_hash,generation,title) VALUES(?,?,?,?)",
            records,
        )
        conn.executemany(
            "INSERT INTO chunks(recording_id,generation,ordinal,text,vector) VALUES(?,?,?,?,?)",
            chunks,
        )
        conn.commit()

    monkeypatch.setenv("SEMANTIC_SEARCH_ENABLED", "1")
    monkeypatch.setenv("TENANT_ID", "owner")
    monkeypatch.setenv("SEMANTIC_INDEX_PATH", str(path))
    monkeypatch.setattr(runtime, "index_identity", lambda: IDENTITY)
    monkeypatch.setattr(
        runtime,
        "embedder_from_env",
        lambda: PrefixingNormalizedEmbedder(MeaningBackend()),
    )

    rows = runtime.readonly_provider_from_env().search("ремонт автомобиля", limit=1)

    assert [row["id"] for row in rows] == ["r-2000"]
