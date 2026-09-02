import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import diarization  # noqa: E402


class Turn:
    def __init__(self, start, end, speaker):
        self.start, self.end, self.speaker = start, end, speaker


class Result(list):
    def sort_by_start_time(self):
        return self


class Runtime:
    sample_rate = 16000
    def process(self, _samples):
        return Result([Turn(1.0, 2.5, 0), Turn(3.0, 4.0, 2)])


def test_sherpa_adapter_reports_pinned_metadata_and_real_turns(tmp_path, monkeypatch):
    segmentation = tmp_path / "seg.onnx"; segmentation.write_bytes(b"seg")
    embedding = tmp_path / "emb.onnx"; embedding.write_bytes(b"emb")
    audio = tmp_path / "a.mp3"; audio.write_bytes(b"audio")
    monkeypatch.setenv("DIARIZATION_SEGMENTATION_MODEL", str(segmentation))
    monkeypatch.setenv("DIARIZATION_EMBEDDING_MODEL", str(embedding))
    engine = diarization.SherpaOnnxEngine(
        runtime_loader=lambda **_kw: Runtime(),
        audio_loader=lambda *_args: [0.0],
    )
    result = engine(str(audio), duration_ms=3500, recording_id="r")
    assert result == [
        {"speaker": "Speaker 1", "start_ms": 1000, "end_ms": 2500},
        {"speaker": "Speaker 3", "start_ms": 3000, "end_ms": 3500},
    ]
    assert engine.metadata() == {"provider": "sherpa-onnx", "version": "1.13.5",
                                 "segmentation": "pyannote-segmentation-3.0",
                                 "embedding": "3dspeaker-campplus-16k"}


def test_environment_registration_fails_closed_without_models(monkeypatch):
    monkeypatch.setenv("DIARIZATION_ENGINE", "sherpa-onnx")
    monkeypatch.setenv("DIARIZATION_SEGMENTATION_MODEL", "/missing/seg.onnx")
    monkeypatch.setenv("DIARIZATION_EMBEDDING_MODEL", "/missing/emb.onnx")
    diarization.configure_from_environment()
    assert not diarization.available()
