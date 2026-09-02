#!/usr/bin/env python3
"""diarization.py — the pipeline stage that would label who is speaking.

Status in this repository: **there is no diarization engine here.** The ingest
side is stdlib-only by design (see archive/Dockerfile), transcription is a remote
asr-mcp service, and nothing in this tree does speaker segmentation. So this
module ships the *stage*, not an engine:

* `available()` is False and `engine_names()` is empty until something registers
  an engine, so every caller can tell the difference between "no speakers" and
  "we cannot tell yet";
* `run()` returns a `unavailable / no_engine` outcome instead of guessing, and
  the worker records that on the job. A reader is told the truth ("разметка
  недоступна"), never shown a fabricated speaker count;
* an engine, when one exists, is a single callable that receives an audio path
  and returns real segments. Whatever it returns is normalised and validated
  here — an engine that answers with junk cannot put junk in front of a reader.

That keeps the future work to one registration call (`register('pyannote', fn)`
in whatever process owns the model), with the durable queue, the DB write and
the honest reporting already in place and already tested.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess

# name -> callable(audio_path, *, duration_ms=None, recording_id=None) -> segments
_ENGINES: dict[str, object] = {}

# An engine that returns more than this is malfunctioning; a diarization of a
# four-hour meeting is thousands of turns, not hundreds of thousands.
MAX_SEGMENTS = 20_000

STATE_OK = "ok"
STATE_UNAVAILABLE = "unavailable"
REASON_NO_ENGINE = "no_engine"


class SherpaOnnxEngine:
    """Local, replaceable sherpa-onnx adapter. Model files never enter SQLite."""
    PROVIDER = "sherpa-onnx"
    VERSION = "1.13.5"

    def __init__(self, runtime_loader=None, audio_loader=None):
        self.runtime_loader = runtime_loader or self._runtime
        self.audio_loader = audio_loader or self._audio

    def metadata(self):
        return {"provider": self.PROVIDER, "version": self.VERSION,
                "segmentation": "pyannote-segmentation-3.0",
                "embedding": "3dspeaker-campplus-16k"}

    @staticmethod
    def _runtime(*, segmentation_model, embedding_model, threads=2):
        import sherpa_onnx
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=segmentation_model)),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=embedding_model, num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=-1, threshold=float(os.environ.get("DIARIZATION_CLUSTER_THRESHOLD", "0.5"))),
            min_duration_on=0.3, min_duration_off=0.5)
        if not config.validate():
            raise RuntimeError("invalid diarization model configuration")
        return sherpa_onnx.OfflineSpeakerDiarization(config)

    @staticmethod
    def _audio(path, sample_rate):
        import numpy
        command = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le",
                   "-ac", "1", "-ar", str(sample_rate), "pipe:1"]
        result = subprocess.run(command, check=True, capture_output=True,
                                timeout=max(60, int(os.environ.get("DIARIZATION_DECODE_TIMEOUT", "600"))))
        return numpy.frombuffer(result.stdout, dtype=numpy.float32)

    def __call__(self, audio_path, *, duration_ms=None, recording_id=None):
        del recording_id
        segmentation = os.environ.get("DIARIZATION_SEGMENTATION_MODEL", "")
        embedding = os.environ.get("DIARIZATION_EMBEDDING_MODEL", "")
        if not (os.path.isfile(segmentation) and os.path.isfile(embedding)):
            raise RuntimeError("diarization models unavailable")
        try:
            threads = max(1, min(4, int(os.environ.get("DIARIZATION_THREADS", "2"))))
        except ValueError:
            threads = 2
        runtime = self.runtime_loader(segmentation_model=segmentation,
                                      embedding_model=embedding, threads=threads)
        turns = runtime.process(self.audio_loader(audio_path, runtime.sample_rate)).sort_by_start_time()
        segments = []
        for turn in turns:
            end_ms = round(float(turn.end) * 1000)
            if duration_ms:
                end_ms = min(end_ms, int(duration_ms))
            segments.append({"speaker": f"Speaker {int(turn.speaker) + 1}",
                             "start_ms": round(float(turn.start) * 1000),
                             "end_ms": end_ms})
        return normalize_segments(segments, duration_ms)


def configure_from_environment():
    unregister("sherpa-onnx")
    if os.environ.get("DIARIZATION_ENGINE", "").strip() != "sherpa-onnx":
        return
    if not (os.path.isfile(os.environ.get("DIARIZATION_SEGMENTATION_MODEL", ""))
            and os.path.isfile(os.environ.get("DIARIZATION_EMBEDDING_MODEL", ""))):
        return
    register("sherpa-onnx", SherpaOnnxEngine())


def register(name: str, engine) -> None:
    """Make one diarization engine available to the pipeline."""
    if not name or not callable(engine):
        raise ValueError("a diarization engine needs a name and a callable")
    _ENGINES[name] = engine


def unregister(name: str) -> None:
    _ENGINES.pop(name, None)


@contextlib.contextmanager
def registered(name: str, engine):
    """Scope an engine to a block. Used by tests, and by a one-off backfill."""
    register(name, engine)
    try:
        yield
    finally:
        unregister(name)


def available() -> bool:
    """Whether this deployment can diarise at all. False in this repository."""
    return bool(_ENGINES)


def engine_names() -> tuple:
    return tuple(_ENGINES)


def normalize_segments(raw, duration_ms=None) -> list:
    """Validated speaker segments, in order. Junk is dropped, never repaired.

    The output shape is exactly what the viewer's speaker model already reads
    (`speaker`/`start_ms`/`end_ms`), so a future engine needs no new reader.
    """
    out = []
    for entry in (raw or [])[:MAX_SEGMENTS]:
        if not isinstance(entry, dict):
            continue
        speaker = entry.get("speaker") or entry.get("label")
        if not isinstance(speaker, str) or not speaker.strip():
            continue  # a segment with no label is not evidence of a speaker
        start, end = entry.get("start_ms"), entry.get("end_ms")
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        start, end = int(start), int(end)
        if start < 0 or end <= start:
            continue
        if duration_ms and start >= int(duration_ms):
            continue  # outside the audio we actually hold
        if duration_ms:
            end = min(end, int(duration_ms))
        out.append({"speaker": speaker.strip(), "start_ms": start, "end_ms": end})
    out.sort(key=lambda s: (s["start_ms"], s["end_ms"]))
    return out


def run(audio_path, duration_ms=None, recording_id=None, engine=None) -> dict:
    """One diarization attempt, classified.

    Returns either
      {'state': 'unavailable', 'reason': 'no_engine'}            — nothing installed
      {'state': 'ok', 'engine': name, 'segments': [...]}          — real evidence
    and raises whatever the engine raises, so the worker can record the failure
    against the job and leave the recording untouched.
    """
    if engine:
        chosen = engine if callable(engine) else _ENGINES.get(engine)
        name = engine if isinstance(engine, str) else getattr(engine, "__name__", "engine")
    else:
        name = next(iter(_ENGINES), None)
        chosen = _ENGINES.get(name) if name else None
    if not chosen:
        return {"state": STATE_UNAVAILABLE, "reason": REASON_NO_ENGINE}
    segments = chosen(audio_path, duration_ms=duration_ms, recording_id=recording_id)
    return {"state": STATE_OK, "engine": name,
            "segments": normalize_segments(segments, duration_ms)}


def meta_block(outcome) -> dict:
    """The `asr_meta_json['diarization']` block for a successful outcome."""
    engine = _ENGINES.get(outcome.get("engine"))
    return {"engine": outcome.get("engine"),
            "model": engine.metadata() if hasattr(engine, "metadata") else None,
            "segment_count": len(outcome.get("segments") or []),
            "segments": outcome.get("segments") or []}


def describe() -> str:
    """One non-secret line for a log or a status page."""
    return json.dumps({"available": available(), "engines": list(engine_names())})
