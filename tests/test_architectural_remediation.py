"""Deterministic regression gates for pipeline ownership and SSE framing."""
import fcntl
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'archive'))
import pipeline
import asr_backfill
import summary_backfill
import archive_recording
import stages
from mcp_sse import last_sse_json


def archive():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
      CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,asr_transcript TEXT,
        asr_engine TEXT,lang TEXT,asr_meta_json TEXT,asr_alternative_transcript TEXT);
      CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED,name,transcript);
    ''')
    pipeline.ensure_schema(conn)
    conn.execute("INSERT INTO recordings(id,name) VALUES('r','R')")
    return conn


def claim(conn, now=100):
    pipeline.enqueue(conn, 'r', pipeline.STAGE_ASR, now=now)
    return pipeline.claim_next(conn, now=now)


def test_stale_worker_cannot_overwrite_newer_transcript_or_charge_retry_budget():
    conn = archive()
    first = claim(conn)
    # Simulate the second worker taking over the durable row before first returns.
    conn.execute("UPDATE pipeline_jobs SET claim_owner='second', claim_epoch=101 WHERE seq=?", (first['seq'],))
    conn.commit()
    try:
        asr_backfill.store(conn, 'r', 'stale', 'local', '', job=first)
    except pipeline.ClaimLost:
        pass
    else:
        raise AssertionError('stale publisher must be fenced')
    assert conn.execute("SELECT asr_transcript FROM recordings WHERE id='r'").fetchone()[0] is None
    assert pipeline.fail(conn, first, RuntimeError('late failure'), now=102) is False
    assert conn.execute("SELECT attempts FROM pipeline_jobs").fetchone()[0] == 0


def test_plaud_wins_review_publication_is_fenced_before_fts_or_ledger_write():
    conn = archive()
    conn.execute("ALTER TABLE recordings ADD COLUMN plaud_transcript TEXT")
    conn.execute("UPDATE recordings SET plaud_transcript='PLAUD' WHERE id='r'")
    asr_backfill.ensure_reviews_table(conn)
    first = claim(conn)
    conn.execute("UPDATE pipeline_jobs SET claim_owner='second', claim_epoch=101 WHERE seq=?", (first['seq'],))
    conn.commit()
    verdict = asr_backfill.Verdict('plaud', 'adequate', (), 'PLAUD', '', '', None, '')
    try:
        asr_backfill.record_review(conn, 'r', verdict, job=first)
    except pipeline.ClaimLost:
        pass
    else:
        raise AssertionError('stale PLAUD-wins publisher must be fenced')
    assert conn.execute("SELECT COUNT(*) FROM recordings_fts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plaud_reviews").fetchone()[0] == 0


def test_readiness_distinguishes_queued_failed_and_full_chain_ready():
    conn = archive()
    pipeline.enqueue(conn, 'r', pipeline.STAGE_ASR, now=1)
    assert pipeline.readiness(conn, 'r')['state'] == pipeline.JOB_QUEUED
    conn.execute("UPDATE pipeline_jobs SET state='failed' WHERE recording_id='r' AND stage='asr'")
    conn.commit()
    assert pipeline.readiness(conn, 'r')['state'] == pipeline.JOB_FAILED
    for stage in pipeline.READINESS_STAGES:
        conn.execute("INSERT OR REPLACE INTO pipeline_jobs(recording_id,stage,state,attempts,enqueued_epoch,available_epoch) VALUES('r',?,?,0,0,0)", (stage, pipeline.JOB_DONE))
    conn.commit()
    assert pipeline.readiness(conn, 'r')['ready'] is True


def test_canonical_readiness_copy_cannot_drift_between_build_contexts():
    assert (ROOT / 'archive' / 'readiness.py').read_bytes() == (
        ROOT / 'viewer' / 'app' / 'readiness.py').read_bytes()


def test_multiline_sse_parser_preserves_json_for_every_mcp_client_shape():
    raw = 'event: message\ndata: {"jsonrpc":"2.0",\ndata: "id":9,"result":{"ok":true}}\n\n'
    expected = {'jsonrpc': '2.0', 'id': 9, 'result': {'ok': True}}
    assert last_sse_json(raw) == expected
    assert archive_recording._response_from_events(raw, 9) == expected

    class Response:
        headers = {'Content-Type': 'text/event-stream', 'Mcp-Session-Id': 'sid'}
        def read(self): return raw.encode()
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    with mock.patch.object(asr_backfill.urllib.request, 'urlopen', return_value=Response()):
        assert asr_backfill._post({'id': 9})[0] == expected
    with mock.patch.object(summary_backfill.urllib.request, 'urlopen', return_value=Response()):
        assert summary_backfill._post({'id': 9})[0] == expected


def test_every_standalone_writer_refuses_the_archive_connector_lock():
    """The connector lock is the single writer gate; no legacy side lock exists."""
    with tempfile.TemporaryDirectory() as tmp:
        lock = os.path.join(tmp, '.connector.lock')
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.object(asr_backfill, 'LOCK', lock), \
                 mock.patch.object(asr_backfill, 'TOKEN', 'token'), \
                 mock.patch.object(asr_backfill, 'mcp_connect', side_effect=AssertionError('ASR ran under lock')):
                assert asr_backfill.main() == 75
            with mock.patch.object(summary_backfill, 'LOCK', lock), \
                 mock.patch.object(summary_backfill, 'TOKEN', 'token'), \
                 mock.patch.object(summary_backfill, 'mcp_connect', side_effect=AssertionError('summary ran under lock')):
                assert summary_backfill.main([]) == 75
            with mock.patch.object(archive_recording, 'LOCK', lock), \
                 mock.patch.object(archive_recording, 'mcp_connect', side_effect=AssertionError('archive ran under lock')), \
                 mock.patch.object(sys, 'argv', ['archive_recording.py', 'r']):
                assert archive_recording.main() == 75
        finally:
            os.close(fd)


def test_review_claim_decline_defers_the_pipeline_job_without_spending_a_retry():
    conn = archive()
    conn.execute("ALTER TABLE recordings ADD COLUMN plaud_transcript TEXT")
    conn.execute("ALTER TABLE recordings ADD COLUMN summary TEXT")
    conn.execute("ALTER TABLE recordings ADD COLUMN duration_ms INTEGER")
    conn.execute("UPDATE recordings SET plaud_transcript='PLAUD' WHERE id='r'")
    conn.commit()
    job = claim(conn)

    class DecliningReview:
        SegmentPaused = type('SegmentPaused', (Exception,), {})
        SegmentIncomplete = type('SegmentIncomplete', (Exception,), {'transient': False})
        def ensure_attempts(self, _conn): pass
        def duration_sec_from_ms(self, _duration): return 0
        def ensure_review_row(self, _conn, _rid): pass
        def review_one(self, *_args, **_kwargs): return None
        def release_review(self, _conn, _rid): pass

    try:
        stages.asr_stage(conn, job, DecliningReview(), 'sid')
    except pipeline.StageDeferred:
        pass
    else:
        raise AssertionError('a held review claim must defer rather than complete ASR')
    assert pipeline.defer(conn, job, 'review held') is True
    assert conn.execute("SELECT state,attempts FROM pipeline_jobs WHERE seq=?", (job['seq'],)).fetchone() == ('queued', 0)


def test_failed_local_audio_is_revived_at_most_the_configured_number_of_times():
    with tempfile.TemporaryDirectory() as tmp:
        conn = archive()
        conn.execute("ALTER TABLE recordings ADD COLUMN audio_path TEXT")
        conn.execute("UPDATE recordings SET audio_path=? WHERE id='r'", (os.path.join(tmp, 'r.mp3'),))
        pipeline.enqueue(conn, 'r', pipeline.STAGE_ASR, now=1)
        conn.execute("UPDATE pipeline_jobs SET state='failed' WHERE recording_id='r'")
        conn.commit()
        audio = Path(tmp, 'r.mp3'); audio.write_bytes(b'ID3')
        for revival in range(pipeline.MAX_REVIVALS):
            assert pipeline.reconcile(conn, audio_dir=tmp, now=10 + revival) == ['r']
            conn.execute("UPDATE pipeline_jobs SET state='failed' WHERE recording_id='r'")
            conn.commit()
        assert pipeline.reconcile(conn, audio_dir=tmp, now=99) == []
        assert conn.execute("SELECT state,revival_attempts FROM pipeline_jobs").fetchone() == ('failed', pipeline.MAX_REVIVALS)


def test_dead_own_host_claim_releases_its_segments_but_live_foreign_claim_survives():
    conn = archive()
    conn.execute("ALTER TABLE recordings ADD COLUMN duration_ms INTEGER")
    conn.execute("UPDATE recordings SET duration_ms=1200000 WHERE id='r'")
    epoch = int(time.time())
    pipeline.enqueue(conn, 'r', pipeline.STAGE_ASR, now=epoch)
    job = pipeline.claim_next(conn, now=epoch)
    dead = f'{pipeline.socket.gethostname()}:99999999'
    conn.execute("UPDATE pipeline_jobs SET claim_owner=?,claim_epoch=? WHERE seq=?", (dead, epoch, job['seq']))
    asr_backfill.ensure_segments(conn)
    asr_backfill.materialize_plan(conn, 'r', asr_backfill.plan_segments(1200))
    asr_backfill.mark_processing(conn, 'r', 0, 0, 600)
    foreign = 'other-host:1'
    asr_backfill.mark_processing(conn, 'r', 1, 600, 600)
    conn.execute("UPDATE asr_segments SET claim_owner=?,claim_epoch=? WHERE id='r' AND seg_index=0", (dead, epoch))
    conn.execute("UPDATE asr_segments SET claim_owner=?,claim_epoch=? WHERE id='r' AND seg_index=1", (foreign, epoch))
    conn.commit()
    recovered = pipeline.recover_stale_jobs(conn, now=epoch + 1)
    assert [entry['recording_id'] for entry in recovered] == ['r']
    assert asr_backfill.release_processing_segments(conn, recovered) == 1
    assert conn.execute("SELECT status FROM asr_segments WHERE id='r' AND seg_index=0").fetchone()[0] == 'pending'
    assert conn.execute("SELECT status FROM asr_segments WHERE id='r' AND seg_index=1").fetchone()[0] == 'processing'
