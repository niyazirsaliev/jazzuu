"""Canonical reprocessing integration: UDS intent, pipeline claims, and stages.

No viewer sidecar or retired worker participates here.  The control service commits
intent to archive.db; a separate connector-side connection drains pipeline_jobs.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import control  # noqa: E402
import diarization  # noqa: E402
import asr_backfill  # noqa: E402
import pipeline  # noqa: E402
import stages  # noqa: E402

RAW = "[Speaker 1] Привет\n[Speaker 2] Как дела"
SUMMARY = "Старое резюме"
SUMMARY_JSON = {"overview": "Старый итог"}
SEGMENTS = [{"speaker": "Speaker 1", "start_ms": 0, "end_ms": 6000, "text": "Привет"}]
NOW = 2_000_000_000


def make_db(path, *, audio=True):
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
      CREATE TABLE recordings(
        id TEXT PRIMARY KEY, name TEXT, duration_ms INTEGER, audio_path TEXT,
        asr_engine TEXT, lang TEXT, asr_transcript TEXT, plaud_transcript TEXT,
        plaud_segments_json TEXT, summary TEXT, summary_json TEXT,
        asr_meta_json TEXT, asr_alternative_transcript TEXT);
      CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED,name,transcript);
    """)
    conn.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "rec1", "Планёрка", 11000, "", "old-engine", "", RAW, "",
        json.dumps(SEGMENTS, ensure_ascii=False), SUMMARY,
        json.dumps(SUMMARY_JSON, ensure_ascii=False), None, "secret alternative"))
    conn.execute("INSERT INTO recordings_fts VALUES(?,?,?)", ("rec1", "Планёрка", RAW))
    pipeline.ensure_schema(conn)
    control.ensure_schema(conn)
    conn.commit()
    if audio:
        audio_dir = path.parent / "audio"; audio_dir.mkdir()
        (audio_dir / "rec1.mp3").write_bytes(b"audio")
    return conn


@pytest.fixture
def archive(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = make_db(db_path); conn.close()
    token = tmp_path / "control.token"; token.write_text("a" * 48); os.chmod(token, 0o600)
    sock = tmp_path / "control.sock"
    server = control.ControlServer(str(db_path), str(sock), str(token)); server.start()
    try:
        yield db_path, token, sock, tmp_path / "audio"
    finally:
        server.stop()


def rows(path, query, args=()):
    with sqlite3.connect(path) as conn:
        return conn.execute(query, args).fetchall()


def request(token, sock, action, aliases=None):
    return control.ControlClient(str(sock), str(token)).request(action, "rec1", aliases)


def drain(path, handlers, **kwargs):
    conn = sqlite3.connect(path, timeout=5)
    try:
        return pipeline.drain(conn, handlers, now=NOW, **kwargs)
    finally:
        conn.close()


def test_missing_recording_is_rejected_and_missing_audio_never_calls_diarizer(archive):
    db_path, token, sock, audio_dir = archive
    with pytest.raises(control.ControlRejected):
        control.ControlClient(str(sock), str(token)).request("retranscribe", "gone")
    request(token, sock, "retranscribe")
    # The canonical stage must reject absent audio before a diarization engine can run.
    (audio_dir / "rec1.mp3").unlink()
    conn = sqlite3.connect(db_path)
    job = pipeline.claim_next(conn, now=NOW)
    with pytest.raises(FileNotFoundError):
        stages.diarization_stage(conn, job, audio_dir=str(audio_dir))
    conn.close()


def test_asr_failure_preserves_artifacts_and_redacts_bounded_internal_ledger(archive):
    db_path, token, sock, _audio = archive
    request(token, sock, "retranscribe")
    def broken(_conn, _job):
        raise RuntimeError("asr exploded at /private/secret/token " + "x" * 1000)
    drain(db_path, {pipeline.STAGE_ASR: broken}, max_jobs=1,
          redact=lambda _detail: "ASR error")
    row = rows(db_path, "SELECT asr_transcript,summary,summary_json,asr_engine FROM recordings")[0]
    assert row == (RAW, SUMMARY, json.dumps(SUMMARY_JSON, ensure_ascii=False), "old-engine")
    assert rows(db_path, "SELECT transcript FROM recordings_fts")[0][0] == RAW
    state, detail = rows(db_path, "SELECT state,last_error FROM pipeline_jobs WHERE stage='asr'")[0]
    assert state == pipeline.JOB_RETRY_WAIT and detail == "ASR error" and len(detail) <= 500


def test_empty_asr_result_never_publishes_blank(archive):
    db_path, token, sock, _audio = archive
    request(token, sock, "retranscribe")
    def empty(_conn, _job):
        raise ValueError("empty ASR output")
    drain(db_path, {pipeline.STAGE_ASR: empty}, max_jobs=1)
    assert rows(db_path, "SELECT asr_transcript FROM recordings")[0][0] == RAW


def test_successful_forced_asr_publishes_transcript_metadata_fts_then_orders_successors(archive):
    db_path, token, sock, _audio = archive
    request(token, sock, "retranscribe")
    fresh = "[Speaker 1] Новый текст"
    def publish(conn, job):
        assert job["force_local"] == 1
        conn.execute("UPDATE recordings SET asr_transcript=?,asr_engine=?,asr_meta_json=? WHERE id='rec1'",
                     (fresh, "local", json.dumps({"route": "local"})))
        conn.execute("DELETE FROM recordings_fts WHERE id='rec1'")
        conn.execute("INSERT INTO recordings_fts VALUES(?,?,?)", ("rec1", "Планёрка", fresh))
    drain(db_path, {pipeline.STAGE_ASR: publish}, max_jobs=1)
    assert rows(db_path, "SELECT asr_transcript,asr_engine,asr_meta_json FROM recordings")[0] == (fresh, "local", '{"route": "local"}')
    assert rows(db_path, "SELECT transcript FROM recordings_fts")[0][0] == fresh
    assert rows(db_path, "SELECT stage,state FROM pipeline_jobs ORDER BY seq") == [("asr", "done"), ("summary", "queued")]
    # Each completed canonical stage creates exactly its ordered successor.
    drain(db_path, {pipeline.STAGE_SUMMARY: lambda *_: None}, max_jobs=1)
    drain(db_path, {pipeline.STAGE_MINDMAP: lambda *_: None}, max_jobs=1)
    assert [r[0] for r in rows(db_path, "SELECT stage FROM pipeline_jobs ORDER BY seq")] == ["asr", "summary", "mindmap", "card"]


def test_fts_failure_rolls_back_transcript_publication(archive):
    db_path, token, sock, _audio = archive
    request(token, sock, "retranscribe")
    def broken_publish(conn, _job):
        conn.execute("UPDATE recordings SET asr_transcript='new' WHERE id='rec1'")
        conn.execute("DROP TABLE recordings_fts")
        conn.execute("INSERT INTO recordings_fts VALUES('rec1','x','new')")
    drain(db_path, {pipeline.STAGE_ASR: broken_publish}, max_jobs=1)
    assert rows(db_path, "SELECT asr_transcript FROM recordings")[0][0] == RAW


def test_aliases_change_presentation_only_and_rename_never_queues_asr(archive):
    db_path, token, sock, _audio = archive
    assert request(token, sock, "rename", {"speaker-1": "Аня"})["state"] == "queued"
    assert rows(db_path, "SELECT display_name FROM speaker_aliases")[0][0] == "Аня"
    assert rows(db_path, "SELECT asr_transcript,plaud_segments_json FROM recordings")[0] == (RAW, json.dumps(SEGMENTS, ensure_ascii=False))
    assert rows(db_path, "SELECT stage FROM pipeline_jobs") == [("summary",)]


def test_summary_failure_and_no_transcript_do_not_wipe_prior_summary(archive):
    db_path, token, sock, _audio = archive
    request(token, sock, "regenerate")
    def broken(_conn, _job): raise RuntimeError("summary down")
    drain(db_path, {pipeline.STAGE_SUMMARY: broken}, max_jobs=1)
    assert rows(db_path, "SELECT summary,summary_json FROM recordings")[0] == (SUMMARY, json.dumps(SUMMARY_JSON, ensure_ascii=False))
    # A later forced regeneration with no text stops safely rather than invoking a summarizer.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE recordings SET asr_transcript='',plaud_transcript=''")
        conn.execute("UPDATE pipeline_jobs SET state='failed' WHERE stage='summary'")
        conn.commit()
    request(token, sock, "regenerate")
    class NeverSummarize:
        def ensure_attempts(self, _conn):
            raise AssertionError("a missing transcript must not call the summarizer")
    drain(db_path, {pipeline.STAGE_SUMMARY: lambda conn, job: stages.summary_stage(
        conn, job, NeverSummarize(), "unused")}, max_jobs=1)
    assert rows(db_path, "SELECT summary,summary_json FROM recordings")[0] == (SUMMARY, json.dumps(SUMMARY_JSON, ensure_ascii=False))


def test_registered_diarization_persists_real_segments_and_no_engine_or_empty_never_fabricates(archive):
    db_path, _token, _sock, audio_dir = archive
    conn = sqlite3.connect(db_path)
    # No registered engine records no inferred speakers.
    job = {"recording_id": "rec1"}
    assert stages.diarization_stage(conn, job, audio_dir=str(audio_dir)) is pipeline.STOP
    assert rows(db_path, "SELECT asr_meta_json FROM recordings")[0][0] is None
    with diarization.registered("fake", lambda *_a, **_k: [{"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 4000}]):
        stages.diarization_stage(conn, job, audio_dir=str(audio_dir))
    metadata = json.loads(rows(db_path, "SELECT asr_meta_json FROM recordings")[0][0])
    assert metadata["diarization"]["engine"] == "fake"
    assert metadata["diarization"]["segments"][0]["speaker"] == "SPEAKER_00"
    with diarization.registered("empty", lambda *_a, **_k: []):
        with pytest.raises(ValueError): stages.diarization_stage(conn, job, audio_dir=str(audio_dir))
    assert metadata == json.loads(rows(db_path, "SELECT asr_meta_json FROM recordings")[0][0])
    conn.close()


def test_canonical_claims_allow_one_worker_and_release_lease(archive):
    db_path, token, sock, _audio = archive
    request(token, sock, "retranscribe")
    a, b = sqlite3.connect(db_path), sqlite3.connect(db_path)
    first, second = pipeline.claim_next(a, now=NOW), pipeline.claim_next(b, now=NOW)
    assert first is not None and second is None
    assert pipeline.complete(a, first, now=NOW) is True
    assert rows(db_path, "SELECT COUNT(*) FROM pipeline_jobs WHERE claim_owner IS NOT NULL")[0][0] == 0
    a.close(); b.close()


def test_empty_canonical_queue_is_a_quiet_no_op(archive):
    db_path, _token, _sock, _audio = archive
    assert drain(db_path, {}, max_jobs=1) == []
    assert rows(db_path, "SELECT asr_transcript FROM recordings")[0][0] == RAW


def test_missing_archive_recording_never_reaches_asr_handler(archive):
    db_path, _token, _sock, _audio = archive
    conn = sqlite3.connect(db_path); pipeline.enqueue(conn, "gone", pipeline.STAGE_ASR); conn.close()
    # A real ASR stage checks the archive before it opens its MCP session.
    conn = sqlite3.connect(db_path); job = pipeline.claim_next(conn, now=NOW)
    with pytest.raises(RuntimeError): stages.asr_stage(conn, job, object(), "unused")
    conn.close()


def test_textless_asr_stage_uses_configured_pipeline_engine(archive, monkeypatch):
    db_path, token, sock, _audio = archive
    request(token, sock, "retranscribe")
    seen = []

    def transcribe(_conn, _sid, _rid, engine, _duration_sec, on_window=None):
        seen.append(engine)

    monkeypatch.setattr(stages, "ASR_ENGINE", "large-v3", raising=False)
    monkeypatch.setattr(asr_backfill, "transcribe", transcribe)
    conn = sqlite3.connect(db_path)
    job = pipeline.claim_next(conn, now=NOW)
    stages.asr_stage(conn, job, asr_backfill, "sid")
    conn.close()

    assert seen == ["large-v3"]


def test_plaud_review_uses_configured_pipeline_engine(archive, monkeypatch):
    db_path, _token, _sock, _audio = archive
    seen = []

    class ReviewingASR:
        SegmentPaused = asr_backfill.SegmentPaused
        SegmentIncomplete = asr_backfill.SegmentIncomplete

        @staticmethod
        def ensure_attempts(_conn):
            return None

        @staticmethod
        def duration_sec_from_ms(value):
            return value / 1000

        @staticmethod
        def ensure_review_row(_conn, _rid):
            return None

        @staticmethod
        def review_one(_conn, _sid, _rid, **kwargs):
            seen.append(kwargs.get("engine"))
            return object()

        @staticmethod
        def release_review(_conn, _rid):
            return None

    monkeypatch.setattr(stages, "ASR_ENGINE", "mixed", raising=False)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE recordings SET plaud_transcript='provider text', asr_transcript='' WHERE id='rec1'")
    pipeline.enqueue(conn, "rec1", pipeline.STAGE_ASR)
    job = pipeline.claim_next(conn, now=NOW)
    stages.asr_stage(conn, job, ReviewingASR(), "sid")
    conn.close()

    assert seen == ["mixed"]


def test_unredacted_pipeline_error_is_still_bounded(archive):
    db_path, token, sock, _audio = archive; request(token, sock, "retranscribe")
    drain(db_path, {pipeline.STAGE_ASR: lambda *_: (_ for _ in ()).throw(RuntimeError("x" * 5000))}, max_jobs=1)
    detail = rows(db_path, "SELECT last_error FROM pipeline_jobs WHERE stage='asr'")[0][0]
    assert len(detail) <= 500 and detail.startswith("RuntimeError:")


def test_regeneration_does_not_queue_asr_when_asr_is_already_done(archive):
    db_path, token, sock, _audio = archive; request(token, sock, "regenerate")
    jobs = rows(db_path, "SELECT stage FROM pipeline_jobs ORDER BY seq")
    assert jobs
    assert jobs == [("summary",)]


def test_diarization_normalizes_junk_without_inventing_segments(archive):
    db_path, _token, _sock, audio_dir = archive; conn = sqlite3.connect(db_path)
    with diarization.registered("junk", lambda *_a, **_k: [{"speaker": "", "start_ms": 0, "end_ms": 1}]):
        with pytest.raises(ValueError): stages.diarization_stage(conn, {"recording_id": "rec1"}, audio_dir=str(audio_dir))
    assert rows(db_path, "SELECT asr_meta_json FROM recordings")[0][0] is None
    conn.close()


def test_explicit_diarize_is_reachable_and_no_engine_completes_truthfully(archive):
    db_path, token, sock, audio_dir = archive
    assert request(token, sock, "diarize")["state"] == "queued"
    drain(db_path, {pipeline.STAGE_DIARIZATION: lambda conn, job: stages.diarization_stage(conn, job, audio_dir=str(audio_dir))}, max_jobs=1)
    assert rows(db_path, "SELECT stage,state FROM pipeline_jobs ORDER BY seq") == [("diarization", "done")]
    assert rows(db_path, "SELECT asr_meta_json FROM recordings")[0][0] is None


def test_completed_asr_releases_claim_before_summary_is_claimed(archive):
    db_path, token, sock, _audio = archive; request(token, sock, "retranscribe")
    drain(db_path, {pipeline.STAGE_ASR: lambda *_: None}, max_jobs=1)
    conn = sqlite3.connect(db_path); summary = pipeline.claim_next(conn, now=NOW)
    assert summary["stage"] == pipeline.STAGE_SUMMARY and summary["claim_owner"]
    pipeline.complete(conn, summary, now=NOW); conn.close()


def test_force_local_bit_survives_control_to_claim_boundary(archive):
    db_path, token, sock, _audio = archive; request(token, sock, "retranscribe")
    conn = sqlite3.connect(db_path); job = pipeline.claim_next(conn, now=NOW)
    assert job["stage"] == pipeline.STAGE_ASR and job["force_local"] == 1
    conn.close()


def test_sixty_minute_retranscribe_replaces_six_fresh_checkpoints_once(archive, monkeypatch):
    """An explicit replacement never resumes an old oversized checkpoint plan."""
    db_path, token, sock, _audio = archive
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE recordings SET duration_ms=3600000, plaud_transcript='old PLAUD'")
        asr_backfill.ensure_attempts(conn)
        conn.executemany("""INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,text,status)
                          VALUES('rec1',?,?,?,'stale','complete')""",
                         [(0, 0, 1800), (1, 1800, 1800)])
    calls = []
    monkeypatch.setattr(asr_backfill, "SEGMENTS_PER_RUN", 1)
    monkeypatch.setattr(asr_backfill, "CHAIN_IDLE_SEGMENTS", False)
    monkeypatch.setattr(asr_backfill, "has_competing_pending", lambda *_: True)
    monkeypatch.setattr(asr_backfill, "asr_request", lambda _c, _r, _e, start, duration: ("fresh", {"start": start, "duration": duration}))
    monkeypatch.setattr(asr_backfill, "call", lambda _tool, args, _sid: calls.append(args["start"]) or {"text": f"fresh-{args['start']}", "engine_used": "local", "meta": {}})
    assert request(token, sock, "retranscribe")["state"] == "queued"
    assert request(token, sock, "retranscribe")["state"] == "already_queued"
    drain(db_path, {pipeline.STAGE_ASR: lambda conn, job: stages.asr_stage(conn, job, asr_backfill, "sid")}, max_jobs=1)
    assert calls == [0]
    assert rows(db_path, "SELECT asr_transcript FROM recordings")[0][0] == RAW
    assert rows(db_path, "SELECT COUNT(*) FROM asr_segments WHERE id='rec1'")[0][0] == 6
    for _ in range(5):
        drain(db_path, {pipeline.STAGE_ASR: lambda conn, job: stages.asr_stage(conn, job, asr_backfill, "sid")}, max_jobs=1)
    assert calls == [i * 600 for i in range(6)]
    published = rows(db_path, "SELECT asr_transcript FROM recordings")[0][0]
    assert all(f"fresh-{i * 600}" in published for i in range(6)) and published != RAW
    assert rows(db_path, "SELECT transcript FROM recordings_fts")[0][0] == published
    assert rows(db_path, "SELECT state,force_local FROM pipeline_jobs WHERE stage='asr'")[0] == ("done", 0)


def test_regeneration_uses_alias_presentation_and_replaces_only_after_valid_result(archive):
    db_path, token, sock, _audio = archive
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE recordings SET asr_transcript=?", (RAW * 8,))
    assert request(token, sock, "rename", {"speaker-1": "Аня"})["state"] == "queued"
    captured = {}
    class Summary:
        def ensure_attempts(self, _conn): pass
        def backfill_one(self, conn, _sid, _rid, **kwargs):
            captured.update(kwargs)
            conn.execute("UPDATE recordings SET summary='Новый итог',summary_json='{" + '"overview":"Аня"' + "}' WHERE id='rec1'")
            conn.commit()
    drain(db_path, {pipeline.STAGE_SUMMARY: lambda conn, job: stages.summary_stage(conn, job, Summary(), "sid")}, max_jobs=1)
    assert captured["force"] is True
    assert "[Аня]" in captured["transcript"]
    assert "speaker_names" not in captured
    assert rows(db_path, "SELECT summary FROM recordings")[0][0] == "Новый итог"
