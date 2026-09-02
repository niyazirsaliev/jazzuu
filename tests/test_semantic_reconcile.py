import sqlite3

import pytest

from archive import semantic_reconcile
from semantic_search.index import IndexIdentity, TenantIndex


IDENTITY = IndexIdentity("fixture", "v1", "0" * 64, 3, "sentences-v1")


class Embedder:
    dimension = 3

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def embed_passages(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("offline")
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_queries(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


def _archive(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE recordings(
            id TEXT PRIMARY KEY, name TEXT, semantic_title TEXT, summary TEXT,
            asr_transcript TEXT, plaud_transcript TEXT, archived_local_at TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO recordings VALUES(?,?,?,?,?,?,?)",
        [
            ("a", "A", "", "резюме A", "текст A", "", None),
            ("b", "B", "", "резюме B", "текст B", "", None),
            ("hidden", "H", "", "hidden", "hidden", "", "2026-01-01"),
        ],
    )
    conn.commit()
    return conn


def test_connector_reconciliation_is_bounded_resumable_and_visibility_scoped(tmp_path):
    archive = _archive(tmp_path / "archive.db")
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    embedder = Embedder()

    first = semantic_reconcile.reconcile_visible(
        archive, tenant_id="owner", index=index, embedder=embedder, batch_size=1,
    )
    second = semantic_reconcile.reconcile_visible(
        archive, tenant_id="owner", index=index, embedder=embedder, batch_size=1,
    )
    third = semantic_reconcile.reconcile_visible(
        archive, tenant_id="owner", index=index, embedder=embedder, batch_size=1,
    )

    assert first == {"replaced": 1, "purged": 0, "remaining": 1}
    assert second == {"replaced": 1, "purged": 0, "remaining": 0}
    assert third == {"replaced": 0, "purged": 0, "remaining": 0}
    assert {row.recording_id for row in index.candidates(20)} == {"a", "b"}
    assert len(embedder.calls) == 2


def test_failed_reconcile_preserves_published_generation_and_retry_intent(tmp_path):
    archive = _archive(tmp_path / "archive.db")
    index_path = tmp_path / "semantic.db"
    index = TenantIndex(index_path, tenant_id="owner", identity=IDENTITY)
    semantic_reconcile.reconcile_visible(archive, tenant_id="owner", index=index, embedder=Embedder(), batch_size=1)
    before = [row.text for row in index.chunks_for("a")]
    archive.execute("UPDATE recordings SET asr_transcript='changed' WHERE id='a'")
    archive.commit()

    with pytest.raises(RuntimeError, match="offline"):
        semantic_reconcile.reconcile_visible(
            archive, tenant_id="owner", index=index, embedder=Embedder(fail=True), batch_size=1,
        )

    assert [row.text for row in index.chunks_for("a")] == before
    with sqlite3.connect(index_path) as other:
        assert other.execute(
            "SELECT recording_id,state,attempts FROM reconcile_jobs"
        ).fetchall() == [("a", "retry", 1)]


def test_hidden_or_deleted_rows_are_purged_on_next_connector_pass(tmp_path):
    archive = _archive(tmp_path / "archive.db")
    index = TenantIndex(tmp_path / "semantic.db", tenant_id="owner", identity=IDENTITY)
    semantic_reconcile.reconcile_visible(archive, tenant_id="owner", index=index, embedder=Embedder(), batch_size=5)
    archive.execute("UPDATE recordings SET archived_local_at='2026-01-01' WHERE id='a'")
    archive.execute("DELETE FROM recordings WHERE id='b'")
    archive.commit()

    result = semantic_reconcile.reconcile_visible(
        archive, tenant_id="owner", index=index, embedder=Embedder(), batch_size=5,
    )

    assert result["purged"] == 2
    assert index.candidates(20) == []
