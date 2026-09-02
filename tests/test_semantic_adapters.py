import sqlite3
from types import SimpleNamespace

import pytest

from recordings_mcp import grants as mcp_grants
from recordings_mcp import semantic_adapter as mcp_semantic
from recordings_mcp import server as mcp_server
from viewer.app.semantic_adapter import ViewerSearchAdapter


class Lexical:
    def search(self, query, limit=50):
        return [{"id": "exact", "name": "Точный", "snippet": "literal"}]

    def search_ranked(self, query, limit=50):
        return [{"id": "exact", "name": "Точный", "snippet": "literal", "match": "exact"}]


class Semantic:
    def search(self, query, limit=20, **kwargs):
        return [
            {"id": "exact", "name": "duplicate", "snippet": "duplicate", "source": "semantic", "match": "semantic"},
            {"id": "meaning", "name": "Смысл", "snippet": "semantic", "source": "semantic", "match": "semantic"},
        ]


def test_viewer_adapter_preserves_exact_lexical_payload_when_semantic_not_requested():
    adapter = ViewerSearchAdapter(lambda: Lexical(), lambda: Semantic())

    assert adapter.search("ремонт", semantic=False) == Lexical().search("ремонт")


def test_viewer_adapter_fails_closed_to_exact_lexical_payload():
    def unavailable():
        raise RuntimeError("down")

    adapter = ViewerSearchAdapter(lambda: Lexical(), unavailable)

    assert adapter.search("ремонт", semantic=True) == Lexical().search("ремонт")


def test_viewer_adapter_hybrid_labels_semantic_only_and_deduplicates():
    adapter = ViewerSearchAdapter(lambda: Lexical(), lambda: Semantic())

    rows = adapter.search("ремонт", semantic=True)

    assert [row["id"] for row in rows] == ["exact", "meaning"]
    assert rows[0]["source"] == "lexical"
    assert rows[1]["label"] == "По смыслу"


def _archive(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE recordings(
            id TEXT PRIMARY KEY, recording_number TEXT UNIQUE, name TEXT,
            archived_local_at TEXT
        );
        CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED,name,transcript);
    """)
    conn.executemany(
        "INSERT INTO recordings(id,recording_number,name,archived_local_at) VALUES(?,?,?,?)",
        [
            ("exact", "N-0001", "Точный", None),
            ("meaning", "N-0002", "Смысл", None),
            ("foreign", "D-0001", "Чужой", None),
            ("hidden", "N-0003", "Скрытый", "2026-01-01"),
        ],
    )
    conn.execute("INSERT INTO recordings_fts(id,name,transcript) VALUES('exact','Точный','ремонт')")
    conn.commit()
    conn.close()


def test_mcp_hybrid_adapter_uses_same_provider_and_returns_stable_codes_only(tmp_path):
    db = tmp_path / "archive.db"
    _archive(db)
    config = SimpleNamespace(db_path=str(db), code_prefix="N")
    calls = []

    class CapturingSemantic(Semantic):
        def search(self, query, limit=20, **kwargs):
            calls.append(kwargs["allowed_ids"])
            return super().search(query, limit, **kwargs)

    result = mcp_semantic.search_recordings_hybrid(
        config, query="ремонт автомобиля", limit=10,
        allowed_numbers={"N-0001", "N-0002"}, provider=CapturingSemantic(),
    )

    assert calls == [{"exact", "meaning"}]
    assert [row["number"] for row in result["items"]] == ["N-0001", "N-0002"]
    assert result["items"][1]["label"] == "По смыслу"
    assert all("id" not in row and "score" not in row and "vector" not in row for row in result["items"])


def test_mcp_hybrid_adapter_filters_hidden_foreign_and_disallowed_before_provider(tmp_path):
    db = tmp_path / "archive.db"
    _archive(db)
    config = SimpleNamespace(db_path=str(db), code_prefix="N")
    allowed = []

    class Capture:
        def search(self, query, limit=20, **kwargs):
            allowed.append(kwargs["allowed_ids"])
            return []

    mcp_semantic.search_recordings_hybrid(
        config, query="смысл", limit=10,
        allowed_numbers={"N-0002", "N-0003", "D-0001"}, provider=Capture(),
    )

    assert allowed == [{"meaning"}]


def test_mcp_registers_owner_scoped_hybrid_tool_as_transcript_capability():
    tool = mcp_server.TOOLS["recordings_hybrid_search"]

    assert tool["handler"] is mcp_semantic.search_recordings_hybrid
    assert tool["schema"]["additionalProperties"] is False
    assert set(tool["schema"]["properties"]) == {"query", "limit"}
    assert mcp_grants.TOOL_SCOPE["recordings_hybrid_search"] == "transcript"
