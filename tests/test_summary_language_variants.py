import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import control
import pipeline
import stages


def database():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,start_at TEXT,created_at TEXT,duration_ms INTEGER,asr_transcript TEXT,plaud_transcript TEXT,summary TEXT,summary_json TEXT)")
    conn.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?)", ("r1","Russian title","2026-01-01","2026-01-01",1000,"transcript","","RU markdown",json.dumps({"title":"RU","overview":"Итог"})))
    control.ensure_schema(conn)
    return conn


def test_english_summary_request_is_idempotent_and_separate():
    conn = database()
    assert pipeline.request_summary_language(conn, "r1", "en") is True
    assert pipeline.request_summary_language(conn, "r1", "en") is False
    row = conn.execute("SELECT stage,state FROM pipeline_jobs WHERE recording_id='r1'").fetchone()
    assert row == (pipeline.STAGE_SUMMARY_EN, pipeline.JOB_QUEUED)
    original = conn.execute("SELECT summary,summary_json FROM recordings WHERE id='r1'").fetchone()
    assert original[0] == "RU markdown" and json.loads(original[1])["overview"] == "Итог"


def test_english_stage_publishes_variant_without_overwriting_russian():
    conn = database()
    pipeline.request_summary_language(conn, "r1", "en")
    job = pipeline.claim_next(conn, stages=(pipeline.STAGE_SUMMARY_EN,))
    calls = []
    class Summary:
        @staticmethod
        def call(name, args, sid):
            calls.append((name,args,sid))
            return {"summary_markdown":"# English\n","structured":{"title":"English","overview":"Result","themes":[],"key_facts":[],"decisions":[],"action_items":[],"risks":[],"open_questions":[],"projects":[],"categories":[]}}
    stages.summary_en_stage(conn, job, Summary, "sid")
    variant = conn.execute("SELECT language,summary,summary_json FROM recording_summary_variants WHERE recording_id='r1'").fetchone()
    assert variant[0] == "en" and variant[1] == "# English\n" and json.loads(variant[2])["overview"] == "Result"
    assert conn.execute("SELECT summary FROM recordings WHERE id='r1'").fetchone()[0] == "RU markdown"
    assert calls[0][1]["target_language"] == "en"
    assert "transcript" not in json.dumps(calls[0][1]["transcript"], ensure_ascii=False).lower()


def test_control_action_retries_failed_english_job():
    conn = database()
    assert control.apply(conn, "summary_en", "r1")["state"] == "queued"
    conn.execute("UPDATE pipeline_jobs SET state='failed' WHERE recording_id='r1' AND stage=?", (pipeline.STAGE_SUMMARY_EN,)); conn.commit()
    assert control.apply(conn, "summary_en", "r1")["state"] == "queued"


def test_arbitrary_summary_language_is_queued_and_published_separately():
    conn = database()
    assert pipeline.request_summary_language(conn, "r1", "ja") is True
    job = pipeline.claim_next(conn, stages=(pipeline.STAGE_SUMMARY_EN,))
    assert job["summary_language"] == "ja"
    calls = []

    class Summary:
        @staticmethod
        def call(name, args, sid):
            calls.append((name, args, sid))
            return {"summary_markdown": "# 日本語\n", "structured": {"title": "日本語"}}

    stages.summary_en_stage(conn, job, Summary, "sid")
    variant = conn.execute(
        "SELECT language,summary FROM recording_summary_variants WHERE recording_id='r1'"
    ).fetchone()
    assert variant == ("ja", "# 日本語\n")
    assert calls[0][1]["target_language"] == "ja"


def test_summary_language_rejects_non_bcp47_input():
    conn = database()
    for value in ("", "../../etc/passwd", "en<script>", "x" * 100):
        try:
            pipeline.request_summary_language(conn, "r1", value)
        except ValueError:
            pass
        else:
            raise AssertionError(value)


def test_english_only_variant_table_migrates_without_data_loss():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO recordings VALUES('r1')")
    conn.execute("CREATE TABLE recording_summary_variants(recording_id TEXT NOT NULL,language TEXT NOT NULL CHECK(language IN ('en')),summary TEXT NOT NULL,summary_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(recording_id,language))")
    conn.execute("INSERT INTO recording_summary_variants VALUES('r1','en','English','{}','now')")
    control.ensure_schema(conn)
    conn.execute("INSERT INTO recording_summary_variants VALUES('r1','ja','日本語','{}','now')")
    assert conn.execute("SELECT language FROM recording_summary_variants ORDER BY language").fetchall() == [("en",), ("ja",)]
