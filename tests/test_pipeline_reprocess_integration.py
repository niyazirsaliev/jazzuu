"""Canonical pipeline reprocess integration boundaries.

These tests intentionally use two SQLite connections: the request side must
commit the durable intent before a separate connector-side drainer can claim it.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import pipeline


def _db(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, asr_transcript TEXT, plaud_transcript TEXT)")
    conn.execute("INSERT INTO recordings VALUES ('rec1', 'old local', 'raw PLAUD')")
    conn.commit()
    pipeline.ensure_schema(conn)
    return conn


def test_duplicate_retranscribe_requests_commit_one_fresh_canonical_asr_job(tmp_path):
    path = tmp_path / "archive.db"
    request_a = _db(path)
    request_b = sqlite3.connect(path, timeout=5)
    try:
        assert pipeline.request_retranscribe(request_a, "rec1") is True
        assert pipeline.request_retranscribe(request_b, "rec1") is False
        # A separate pipeline connection sees exactly one committed job, even
        # though the current local transcript remains readable.
        worker = sqlite3.connect(path, timeout=5)
        try:
            rows = worker.execute(
                "SELECT stage,state FROM pipeline_jobs WHERE recording_id='rec1'"
            ).fetchall()
            assert rows == [(pipeline.STAGE_ASR, pipeline.JOB_QUEUED)]
            assert worker.execute("SELECT asr_transcript,plaud_transcript FROM recordings WHERE id='rec1'").fetchone() == ("old local", "raw PLAUD")
        finally:
            worker.close()
    finally:
        request_a.close()
        request_b.close()
