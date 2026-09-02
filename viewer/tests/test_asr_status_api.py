"""HTTP-boundary regressions for truthful, redacted transcript status."""
import json
import os
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import main  # noqa: E402


DDL = """
CREATE TABLE recordings(
  id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
  duration_ms INTEGER, lang TEXT, asr_engine TEXT, asr_transcript TEXT,
  plaud_transcript TEXT, summary TEXT, audio_path TEXT, archived_at TEXT,
  plaud_segments_json TEXT, summary_json TEXT, asr_meta_json TEXT);
CREATE TABLE plaud_reviews(
  id TEXT PRIMARY KEY, state TEXT NOT NULL, eligible_epoch INTEGER,
  selected_source TEXT, reason TEXT, candidates_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  claim_epoch INTEGER, claim_owner TEXT, updated_at TEXT);
CREATE TABLE asr_attempts(
  id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
  last_at TEXT, last_error TEXT);
CREATE TABLE pipeline_jobs(
  seq INTEGER PRIMARY KEY, recording_id TEXT, stage TEXT, state TEXT,
  attempts INTEGER, last_error TEXT, enqueued_epoch INTEGER,
  available_epoch INTEGER, claim_epoch INTEGER, claim_owner TEXT,
  progress_epoch INTEGER, updated_at TEXT,
  UNIQUE(recording_id, stage));
CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED, name, transcript);
"""


def client_for(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "_snap", {"key": None, "path": None})
    client = TestClient(main.app)
    client.cookies.set(main.COOKIE_NAME, main.expected_token())
    return client


def test_list_and_detail_expose_provisional_plaud_validation_status_not_raw_errors(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "archive.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DDL)
        conn.execute(
            """INSERT INTO recordings(id,name,start_at,created_at,duration_ms,
               plaud_transcript,asr_transcript,asr_engine,summary)
               VALUES('provisional','Private recording','2026-08-10','2026-08-10',
                      14000,'PLAUD text','','large-v3','')"""
        )
        conn.execute(
            """INSERT INTO plaud_reviews(id,state,candidates_json,last_error)
               VALUES('provisional','processing',?,?)""",
            (json.dumps([{"text": "secret candidate"}]),
             "decode failed at /private/archive/audio/provisional.mp3"),
        )

    client = client_for(db_path, monkeypatch)
    listed = client.get("/api/recordings")
    detailed = client.get("/api/recordings/provisional")

    assert listed.status_code == detailed.status_code == 200
    list_status = listed.json()[0]["transcript_status"]
    detail_status = detailed.json()["transcript_status"]
    assert list_status == detail_status == {
        "state": "processing", "source": "plaud", "engine": "large-v3",
        "label_ru": "Текст PLAUD · идёт проверка",
    }
    payload = json.dumps([listed.json(), detailed.json()], ensure_ascii=False)
    for secret in ("secret candidate", "decode failed", "/private/archive", ".mp3",
                   "last_error", "candidates_json"):
        assert secret not in payload


def test_pipeline_job_states_are_truthful_and_redacted_at_list_and_detail_boundary(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "archive.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DDL)
        for seq, (rec_id, state, next_retry, progress) in enumerate((
            ("queued", "queued", 0, None),
            ("working", "processing", 0, 123),
            ("retry", "retry_wait", 456, None),
            ("terminal", "failed", 789, None),
            ("finished", "done", 0, 999),
        ), 1):
            conn.execute(
                "INSERT INTO recordings(id,name,start_at,created_at,duration_ms,"
                "asr_transcript,plaud_transcript,asr_engine) VALUES(?,?,?,?"
                ",14000,?,?,?)",
                (rec_id, rec_id, "2026-08-10", "2026-08-10",
                 "local text" if state == "done" else "", "", "large-v3"),
            )
            conn.execute(
                "INSERT INTO pipeline_jobs(seq,recording_id,stage,state,attempts,"
                "last_error,enqueued_epoch,available_epoch,progress_epoch) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (seq, rec_id, "asr", state, 3 if state == "failed" else 0,
                 "decode failed at /private/archive/audio/terminal.mp3", 0,
                 next_retry, progress),
            )

    client = client_for(db_path, monkeypatch)
    listed = {r["id"]: r["transcript_status"]
              for r in client.get("/api/recordings?limit=10").json()}
    assert {rec_id: status["state"] for rec_id, status in listed.items()} == {
        "queued": "queued", "working": "processing", "retry": "retry_wait",
        "terminal": "failed", "finished": "completed",
    }
    assert client.get("/api/recordings/terminal").json()["transcript_status"] == listed["terminal"]
    payload = json.dumps([listed, client.get("/api/recordings/terminal").json()])
    for secret in ("decode failed", "/private/archive", ".mp3", "last_error"):
        assert secret not in payload


def test_three_real_asr_execution_failures_become_failed_at_http_boundary(
    tmp_path, monkeypatch
):
    sys.path.insert(0, str(ROOT / "archive"))
    import pipeline

    db_path = tmp_path / "archive.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DDL)
        conn.execute("INSERT INTO recordings(id,name,start_at,created_at,duration_ms) "
                     "VALUES('bad-audio','Bad','2026-08-10','2026-08-10',14000)")
        pipeline.ensure_schema(conn)
        pipeline.enqueue(conn, "bad-audio", pipeline.STAGE_ASR, now=100)
        clock = 100
        def fail_asr(_conn, _job):
            raise ValueError("decode failed at /private/archive/audio/bad-audio.mp3")
        for _ in range(pipeline.MAX_ATTEMPTS):
            pipeline.drain(conn, {pipeline.STAGE_ASR: fail_asr}, now=clock,
                           max_jobs=1, stages=(pipeline.STAGE_ASR,))
            clock = conn.execute(
                "SELECT available_epoch FROM pipeline_jobs WHERE recording_id='bad-audio'"
            ).fetchone()[0]

    client = client_for(db_path, monkeypatch)
    response = client.get("/api/recordings/bad-audio")
    assert response.status_code == 200
    status = response.json()["transcript_status"]
    assert status["state"] == "failed"
    rendered = json.dumps(response.json())
    assert "decode failed" not in rendered
    assert "/private/archive" not in rendered
