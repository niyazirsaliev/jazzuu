"""Local voiceprint lifecycle: durable, versioned, tenant-local and fail-closed."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import voiceprint  # noqa: E402
import diarization  # noqa: E402
import pipeline  # noqa: E402
import stages  # noqa: E402


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    voiceprint.ensure_schema(conn)
    return conn


def test_owner_enrollment_is_durable_versioned_and_never_stores_raw_audio():
    conn = db()
    voiceprint.enroll(conn, tenant_id="owner", identity="owner", embedding=[1, 0, 0],
                      model="local-test", model_version="1", source="owner-consented-sample")
    row = conn.execute("SELECT tenant_id,identity,model,model_version,embedding_json,source FROM voiceprint_enrollments").fetchone()
    assert dict(row) == {"tenant_id": "owner", "identity": "owner", "model": "local-test",
                         "model_version": "1", "embedding_json": "[1.0,0.0,0.0]",
                         "source": "owner-consented-sample"}
    assert "audio" not in " ".join(row.keys())


def test_matching_requires_minimum_confidence_and_margin_and_falls_back_to_unknown():
    conn = db()
    voiceprint.enroll(conn, tenant_id="owner", identity="owner", embedding=[1, 0], model="local", model_version="1")
    voiceprint.enroll(conn, tenant_id="owner", identity="other", embedding=[0.8, 0.2], model="local", model_version="1")

    assert voiceprint.match(conn, tenant_id="owner", embedding=[1, 0], model="local", model_version="1", minimum_confidence=.95, minimum_margin=.01)["identity"] == "owner"
    near = voiceprint.match(conn, tenant_id="owner", embedding=[.9, .1], model="local", model_version="1", minimum_confidence=.7, minimum_margin=.3)
    assert near["identity"] is None and near["reason"] == "margin"
    weak = voiceprint.match(conn, tenant_id="owner", embedding=[0, 1], model="local", model_version="1", minimum_confidence=.7, minimum_margin=.1)
    assert weak["identity"] is None and weak["reason"] == "confidence"


def test_family_tenant_cannot_use_owner_enrollment_even_when_sharing_the_same_database():
    conn = db()
    voiceprint.enroll(conn, tenant_id="owner", identity="owner", embedding=[1, 0], model="local", model_version="1")
    outcome = voiceprint.match(conn, tenant_id="tenant-b", embedding=[1, 0], model="local", model_version="1", minimum_confidence=.5, minimum_margin=.1)
    assert outcome == {"identity": None, "confidence": None, "margin": None, "reason": "no_enrollment"}


def test_model_version_mismatch_and_invalid_embeddings_fail_closed_without_records():
    conn = db()
    voiceprint.enroll(conn, tenant_id="owner", identity="owner", embedding=[1, 0], model="local", model_version="1")
    outcome = voiceprint.match(conn, tenant_id="owner", embedding=[1, 0], model="local", model_version="2", minimum_confidence=.5, minimum_margin=.1)
    assert outcome["identity"] is None and outcome["reason"] == "no_compatible_enrollment"
    with pytest.raises(ValueError):
        voiceprint.enroll(conn, tenant_id="owner", identity="owner", embedding=[0, 0], model="local", model_version="1")


def test_segment_scoring_persists_only_safe_presentation_labels_and_keeps_diarization_unchanged():
    conn = db()
    voiceprint.enroll(conn, tenant_id="owner", identity="owner", embedding=[1, 0], model="local", model_version="1")
    segments = [{"speaker": "Speaker 1", "start_ms": 0, "end_ms": 2000}, {"speaker": "Speaker 2", "start_ms": 2000, "end_ms": 4000}]
    scored = voiceprint.score_segments(conn, tenant_id="owner", segments=segments,
        embeddings={"Speaker 1": [1, 0], "Speaker 2": [0, 1]}, model="local", model_version="1", minimum_confidence=.8, minimum_margin=.1)
    assert scored == [{"speaker": "Speaker 1", "identity": "owner", "confidence": 1.0, "margin": 1.0}, {"speaker": "Speaker 2", "identity": None, "confidence": 0.0, "margin": 0.0}]
    assert segments[0]["speaker"] == "Speaker 1"


def test_diarization_pipeline_scores_only_registered_local_encoder_and_retries_encoder_failure(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY,audio_path TEXT,duration_ms INTEGER,asr_meta_json TEXT)")
    audio = tmp_path / "audio"; audio.mkdir(); (audio / "r.mp3").write_bytes(b"real-local-file")
    conn.execute("INSERT INTO recordings VALUES('r',?,4000,'{}')", (str(audio / "r.mp3"),))
    pipeline.ensure_schema(conn); voiceprint.ensure_schema(conn)
    voiceprint.enroll(conn, tenant_id="owner", identity="owner", embedding=[1, 0], model="local", model_version="1")
    pipeline.enqueue(conn, "r", pipeline.STAGE_DIARIZATION, now=10)
    monkeypatch.setenv("VOICEPRINT_ENCODER", "test-local")
    monkeypatch.setenv("VOICEPRINT_MODEL", "local")
    monkeypatch.setenv("VOICEPRINT_MODEL_VERSION", "1")
    calls = []
    def encoder(_path, _start, _end):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("local encoder temporary failure")
        return [1, 0]
    diarization.register("test-diarizer", lambda *_a, **_k: [{"speaker":"Speaker 1","start_ms":0,"end_ms":3000}])
    voiceprint.register_encoder("test-local", encoder)
    try:
        handlers = {pipeline.STAGE_DIARIZATION: lambda c, j: stages.diarization_stage(c, j, audio_dir=str(audio))}
        pipeline.drain(conn, handlers, now=10, max_jobs=1)
        state, attempts, available = conn.execute("SELECT state,attempts,available_epoch FROM pipeline_jobs").fetchone()
        assert (state, attempts) == ("retry_wait", 1)
        pipeline.drain(conn, handlers, now=available, max_jobs=1)
        assert conn.execute("SELECT state FROM pipeline_jobs").fetchone()[0] == "done"
        assert conn.execute("SELECT identity FROM voiceprint_segment_scores").fetchone()[0] == "owner"
    finally:
        diarization.unregister("test-diarizer")
        voiceprint.unregister_encoder("test-local")


def test_diarization_voiceprint_uses_tenant_asr_mcp_local_embedding_tool(tmp_path, monkeypatch):
    conn = db()
    audio = tmp_path / "owner.wav"
    audio.write_bytes(b"local owner audio")
    segments = [{"speaker": "Speaker 1", "start_ms": 1250, "end_ms": 6250}]
    version = "sherpa-onnx-speaker-recongition-models/3dspeaker-campplus-zh-en-16k-common-advanced"
    voiceprint.enroll(conn, tenant_id="owner", identity="Айбек", embedding=[1, 0],
                      model="sherpa-onnx", model_version=version,
                      source="explicit-consented-test")
    monkeypatch.setenv("VOICEPRINT_ENCODER", "asr-mcp")
    monkeypatch.setenv("VOICEPRINT_MODEL", "sherpa-onnx")
    monkeypatch.setenv("VOICEPRINT_MODEL_VERSION", version)
    monkeypatch.setenv("TENANT_ID", "owner")
    calls = []

    class Asr:
        @staticmethod
        def audio_url(path):
            assert path == str(audio)
            return audio.as_uri()

        @staticmethod
        def call(name, args, sid):
            calls.append((name, args, sid))
            return {"embedding": [1, 0], "dimension": 2, "model": version,
                    "segment": {"start_sec": 1.25, "duration_sec": 5.0,
                                "sample_rate": 16000}}

    stages._voiceprint_after_diarization(
        conn, "r", str(audio), segments, asr_module=Asr, sid="tenant-session")

    assert calls == [("speaker_embedding_url", {
        "url": audio.as_uri(), "start_sec": 1.25, "duration_sec": 5.0,
    }, "tenant-session")]
    score = conn.execute(
        "SELECT source_label,identity,model,model_version FROM voiceprint_segment_scores"
    ).fetchone()
    assert tuple(score) == ("Speaker 1", "Айбек", "sherpa-onnx", version)
    assert segments == [{"speaker": "Speaker 1", "start_ms": 1250, "end_ms": 6250}]
