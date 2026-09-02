"""Semantic display titles are summary-derived, durable, and claim-fenced."""
import json
import inspect
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))

import pipeline
import summary_backfill
import connector


NOW = 1_786_100_000


def archive():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,asr_transcript TEXT,"
        "plaud_transcript TEXT,summary TEXT,summary_json TEXT)"
    )
    return conn


def test_summary_schema_backfills_generated_title_without_rewriting_source_name():
    conn = archive()
    conn.execute(
        "INSERT INTO recordings VALUES(?,?,?,?,?,?)",
        ("r1", "Official PLAUD filename", "x" * 200, "", "old",
         json.dumps({"title": "Бюджет и сроки поставки"}, ensure_ascii=False)),
    )

    summary_backfill.ensure_attempts(conn)

    row = conn.execute("SELECT name,semantic_title FROM recordings WHERE id='r1'").fetchone()
    assert row == ("Official PLAUD filename", "Бюджет и сроки поставки")


def test_summary_publication_fences_title_and_summary_against_a_stale_claim(monkeypatch):
    conn = archive()
    conn.execute(
        "INSERT INTO recordings VALUES(?,?,?,?,?,?)",
        ("r1", "Official PLAUD filename", "x" * 200, "", "old summary",
         json.dumps({"title": "Old generated title"}, ensure_ascii=False)),
    )
    summary_backfill.ensure_attempts(conn)
    pipeline.ensure_schema(conn)
    pipeline.enqueue(conn, "r1", pipeline.STAGE_SUMMARY, now=NOW)
    job = pipeline.claim_next(conn, now=NOW, stages=(pipeline.STAGE_SUMMARY,))
    conn.execute("UPDATE pipeline_jobs SET claim_owner='new-owner' WHERE seq=?", (job["seq"],))
    conn.commit()
    monkeypatch.setattr(
        summary_backfill, "call",
        lambda *_args: {"summary": "new summary", "structured": {"title": "New generated title"}},
    )

    published = summary_backfill.backfill_one(conn, "session", "r1", force=True, job=job)

    assert published is False
    assert conn.execute(
        "SELECT summary,semantic_title FROM recordings WHERE id='r1'"
    ).fetchone() == ("old summary", "Old generated title")


def test_summary_publication_atomically_replaces_generated_title_but_not_source_name(monkeypatch):
    conn = archive()
    conn.execute(
        "INSERT INTO recordings VALUES(?,?,?,?,?,?)",
        ("r1", "Official PLAUD filename", "x" * 200, "", "old summary",
         json.dumps({"title": "Old generated title"}, ensure_ascii=False)),
    )
    summary_backfill.ensure_attempts(conn)
    pipeline.ensure_schema(conn)
    pipeline.enqueue(conn, "r1", pipeline.STAGE_SUMMARY, now=NOW)
    job = pipeline.claim_next(conn, now=NOW, stages=(pipeline.STAGE_SUMMARY,))
    monkeypatch.setattr(
        summary_backfill, "call",
        lambda *_args: {"summary": "new summary", "structured": {"title": "New generated title"}},
    )

    assert summary_backfill.backfill_one(conn, "session", "r1", force=True, job=job) is True
    assert conn.execute(
        "SELECT name,summary,semantic_title FROM recordings WHERE id='r1'"
    ).fetchone() == ("Official PLAUD filename", "new summary", "New generated title")


def test_normal_connector_path_runs_idempotent_existing_summary_backfill():
    source = inspect.getsource(connector.drain_pipeline)
    assert "summary_module.ensure_attempts(conn)" in source


def test_summary_publication_promotes_generated_title_for_local_import_and_updates_fts(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recordings(
          id TEXT PRIMARY KEY,name TEXT,asr_transcript TEXT,plaud_transcript TEXT,
          summary TEXT,summary_json TEXT,plaud_meta_json TEXT);
        CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED,name,transcript);
        """
    )
    provenance = json.dumps(
        {"source_kind": "nextcloud_external_import", "original_name": "technical-source.m4a"}
    )
    conn.execute(
        "INSERT INTO recordings VALUES(?,?,?,?,?,?,?)",
        ("local", "Локальная аудиозапись", "x" * 200, "", "old", "{}", provenance),
    )
    conn.execute(
        "INSERT INTO recordings_fts VALUES(?,?,?)",
        ("local", "Локальная аудиозапись", "x" * 200),
    )
    monkeypatch.setattr(
        summary_backfill,
        "call",
        lambda *_args: {
            "summary": "new summary",
            "structured": {"title": "Содержательное название записи"},
        },
    )

    assert summary_backfill.backfill_one(conn, "session", "local", force=True) is True
    assert conn.execute(
        "SELECT name,semantic_title FROM recordings WHERE id='local'"
    ).fetchone() == ("Содержательное название записи", "Содержательное название записи")
    assert conn.execute(
        "SELECT name FROM recordings_fts WHERE id='local'"
    ).fetchone()[0] == "Содержательное название записи"
