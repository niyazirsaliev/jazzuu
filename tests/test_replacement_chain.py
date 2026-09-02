"""Replacement intents traverse the canonical pipeline exactly once."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import pipeline


def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, asr_transcript TEXT)")
    conn.execute("INSERT INTO recordings VALUES ('rec', 'existing local transcript')")
    pipeline.ensure_schema(conn)
    for stage in (pipeline.STAGE_ASR, pipeline.STAGE_SUMMARY,
                  pipeline.STAGE_MINDMAP, pipeline.STAGE_CARD):
        pipeline.enqueue(conn, "rec", stage, now=1)
        conn.execute("UPDATE pipeline_jobs SET state='done' WHERE recording_id='rec' AND stage=?", (stage,))
    conn.commit()
    return conn


def run(conn):
    order = []
    def handler(stage):
        def one(_conn, job):
            order.append(job["stage"])
            return None
        return one
    handlers = {stage: handler(stage) for stage in (
        pipeline.STAGE_ASR, pipeline.STAGE_SUMMARY,
        pipeline.STAGE_MINDMAP, pipeline.STAGE_CARD)}
    pipeline.drain(conn, handlers, now=100, max_jobs=10)
    return order


def flags(conn):
    return conn.execute("SELECT stage,state,force_local,force_replace FROM pipeline_jobs ORDER BY seq").fetchall()


def test_retranscribe_reruns_completed_replacement_chain_once_in_stage_order():
    conn = db()
    assert pipeline.request_retranscribe(conn, "rec", now=100)
    assert not pipeline.request_retranscribe(conn, "rec", now=101)
    assert flags(conn)[0] == ("asr", "queued", 1, 1)
    assert run(conn) == ["asr", "summary", "mindmap", "card"]
    assert flags(conn) == [
        ("asr", "done", 0, 0), ("summary", "done", 0, 0),
        ("mindmap", "done", 0, 0), ("card", "done", 0, 0)]


def test_regenerate_reruns_completed_derived_chain_once_without_asr():
    conn = db()
    assert pipeline.request_regenerate(conn, "rec", now=100)
    assert not pipeline.request_regenerate(conn, "rec", now=101)
    assert flags(conn)[1] == ("summary", "queued", 0, 1)
    assert run(conn) == ["summary", "mindmap", "card"]
    assert flags(conn) == [
        ("asr", "done", 0, 0), ("summary", "done", 0, 0),
        ("mindmap", "done", 0, 0), ("card", "done", 0, 0)]
