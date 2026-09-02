import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main, search


def _create_archive(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE recordings(
            id TEXT PRIMARY KEY,
            name TEXT,
            archived_at TEXT,
            archived_local_at TEXT
        );
        CREATE VIRTUAL TABLE recordings_fts USING fts5(
            id UNINDEXED, name, transcript
        );
        """
    )
    for row in rows:
        conn.execute(
            "INSERT INTO recordings(id,name,archived_at,archived_local_at) "
            "VALUES(?,?,?,?)",
            (
                row["id"],
                row["name"],
                row.get("archived_at", "2026-08-15T00:00:00+0000"),
                row.get("archived_local_at"),
            ),
        )
        conn.execute(
            "INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)",
            (row["id"], row["name"], row.get("transcript", "")),
        )
    conn.commit()
    conn.close()


def _open_archive(path):
    def open_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        return conn

    return open_db


def _client_for_archive(path, monkeypatch):
    monkeypatch.setattr(main, "SECRET", "tenant-secret")
    monkeypatch.setattr(main, "db", _open_archive(path))
    client = TestClient(main.app)
    client.cookies.set(main.COOKIE_NAME, main.expected_token())
    return client


@pytest.fixture
def search_client(tmp_path, monkeypatch):
    archive = tmp_path / "tenant.db"
    _create_archive(
        archive,
        [
            {
                "id": "fixture",
                "name": "Рабочая встреча",
                "transcript": "СИГНАЛ подтвердил план запуска.",
            }
        ],
    )
    return _client_for_archive(archive, monkeypatch)


@pytest.mark.parametrize("query", ["С", "СИ", "СИГ", "СИГНАЛ"])
def test_search_matches_incremental_cyrillic_prefixes(search_client, query):
    response = search_client.get("/api/search", params={"q": query})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["fixture"]


def test_search_ranks_full_token_match_before_prefix_only(tmp_path, monkeypatch):
    archive = tmp_path / "tenant.db"
    _create_archive(
        archive,
        [
            {"id": "prefix", "name": "СИГНАЛЬНЫЙ", "transcript": ""},
            {"id": "exact", "name": "СИГНАЛ", "transcript": ""},
        ],
    )
    client = _client_for_archive(archive, monkeypatch)

    response = client.get("/api/search", params={"q": "СИГНАЛ"})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["exact", "prefix"]


@pytest.mark.parametrize("query", ["отчет ел", "ОТЧЁТ ЁЛ"])
def test_search_treats_yo_and_e_as_equivalent_without_changing_display(
    tmp_path, monkeypatch, query
):
    archive = tmp_path / "tenant.db"
    _create_archive(
        archive,
        [
            {"id": "yo", "name": "Отчёт о ёлке", "transcript": ""},
            {"id": "e", "name": "Отчет о елке", "transcript": ""},
        ],
    )
    client = _client_for_archive(archive, monkeypatch)

    response = client.get("/api/search", params={"q": query})

    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {"yo", "e"}
    assert {row["name"] for row in response.json()} == {"Отчёт о ёлке", "Отчет о елке"}


def test_search_uses_feed_visibility_with_production_archived_timestamps(
    tmp_path, monkeypatch
):
    archive = tmp_path / "tenant.db"
    _create_archive(
        archive,
        [
            {"id": "active", "name": "План запуска", "transcript": ""},
            {
                "id": "archived",
                "name": "План архива",
                "transcript": "",
                "archived_local_at": "2026-08-15T00:00:00Z",
            },
        ],
    )
    conn = sqlite3.connect(archive)
    conn.execute(
        "INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)",
        ("deleted", "План удаления", ""),
    )
    conn.commit()
    conn.close()
    client = _client_for_archive(archive, monkeypatch)

    response = client.get("/api/search", params={"q": "план"})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["active"]


def test_search_applies_safe_prefix_matching_to_each_term(tmp_path, monkeypatch):
    archive = tmp_path / "tenant.db"
    _create_archive(
        archive,
        [
            {
                "id": "match",
                "name": "Недельный обзор",
                "transcript": "Согласовали запуск продукта.",
            },
            {
                "id": "missing-term",
                "name": "Недельный обзор",
                "transcript": "Обсудили бюджет.",
            },
        ],
    )
    client = _client_for_archive(archive, monkeypatch)

    response = client.get("/api/search", params={"q": "нед зап"})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["match"]


def test_search_treats_fts_operators_and_quotes_as_plain_data(tmp_path, monkeypatch):
    archive = tmp_path / "tenant.db"
    _create_archive(
        archive,
        [
            {"id": "alpha", "name": "alpha", "transcript": ""},
            {"id": "beta", "name": "beta", "transcript": ""},
            {"id": "literal", "name": "alpha OR beta", "transcript": ""},
            {"id": "fixture", "name": "СИГНАЛ", "transcript": ""},
        ],
    )
    client = _client_for_archive(archive, monkeypatch)

    operator = client.get("/api/search", params={"q": "alpha OR beta"})
    quoted = client.get("/api/search", params={"q": '\"сигнал\"'})

    assert [row["id"] for row in operator.json()] == ["literal"]
    assert [row["id"] for row in quoted.json()] == ["fixture"]


def test_search_is_case_insensitive_and_caps_results(tmp_path, monkeypatch):
    archive = tmp_path / "tenant.db"
    _create_archive(
        archive,
        [
            {"id": f"row-{index:02d}", "name": "", "transcript": "СиГнАл план"}
            for index in range(60)
        ],
    )
    client = _client_for_archive(archive, monkeypatch)

    response = client.get("/api/search", params={"q": "сИгНаЛ"})

    assert response.status_code == 200
    assert len(response.json()) == 50


@pytest.mark.parametrize("tenant", ["owner", "tenant-b", "tenant-c"])
def test_search_uses_only_each_configured_tenant_database(
    tmp_path, monkeypatch, tenant
):
    archives = {}
    for name in ("owner", "tenant-b", "tenant-c"):
        path = tmp_path / f"{name}.db"
        _create_archive(
            path,
            [{"id": name, "name": f"Уникальный {name}", "transcript": f"секрет {name}"}],
        )
        archives[name] = path
    client = _client_for_archive(archives[tenant], monkeypatch)

    own = client.get("/api/search", params={"q": tenant})
    foreign = [
        client.get("/api/search", params={"q": other}).json()
        for other in archives
        if other != tenant
    ]

    assert [row["id"] for row in own.json()] == [tenant]
    assert foreign == [[], []]


def test_search_requires_authentication_and_has_no_public_search_route(search_client):
    unauthenticated = TestClient(main.app)

    response = unauthenticated.get("/api/search", params={"q": "СИГНАЛ"})

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    search_paths = {
        path
        for route in main.app.routes
        if (path := getattr(route, "path", None)) and "search" in path.lower()
    }
    assert search_paths == {"/api/search"}


def test_yo_normalization_expansion_is_bounded():
    assert {"береза", "берёза"} <= set(search._term_variants("береза"))
    assert len(search._term_variants("е" * 6)) <= 32


def test_search_http_wires_optional_semantic_mode_without_changing_default(search_client, monkeypatch):
    calls = []

    class Adapter:
        def search(self, query, *, semantic=False, limit=50):
            calls.append((query, semantic, limit))
            return [{"id": "fixture", "name": "Рабочая встреча", "snippet": "x"}]

    monkeypatch.setattr(main, "SEARCH_ADAPTER", Adapter())

    default = search_client.get("/api/search", params={"q": "СИГНАЛ"})
    hybrid = search_client.get("/api/search", params={"q": "ремонт", "semantic": "true"})

    assert default.status_code == hybrid.status_code == 200
    assert calls == [("СИГНАЛ", False, 50), ("ремонт", True, 50)]
