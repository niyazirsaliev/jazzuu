#!/usr/bin/env python3
"""Tenant-local voiceprint enrollment and matching.

This module intentionally has no bundled embedding model: this owned connector image
currently contains ffmpeg only (no torch/pyannote/speechbrain or enrolled sample).
A local runtime may register an encoder and call :func:`enroll`; it never reaches a
network service.  Embeddings are versioned, scoped by tenant, and compared only to
compatible local enrollments.  A missing model, sample, incompatible model version,
weak score, or ambiguous score always produces ``identity=None``.
"""
from __future__ import annotations

import json
import math
import os

DEFAULT_MINIMUM_CONFIDENCE = float(os.environ.get("VOICEPRINT_MIN_CONFIDENCE", "0.82"))
DEFAULT_MINIMUM_MARGIN = float(os.environ.get("VOICEPRINT_MIN_MARGIN", "0.08"))
MAX_DIMENSIONS = 4096
MAX_SEGMENTS = 256
_ENCODERS = {}


def register_encoder(name, encoder):
    """Register a local-only callable(path, start_ms, end_ms) -> embedding."""
    if not isinstance(name, str) or not name or not callable(encoder):
        raise ValueError("voiceprint encoder needs a name and callable")
    _ENCODERS[name] = encoder


def unregister_encoder(name):
    _ENCODERS.pop(name, None)


def available_encoders():
    return tuple(_ENCODERS)


def _embedding(value):
    if not isinstance(value, (list, tuple)) or not value or len(value) > MAX_DIMENSIONS:
        raise ValueError("invalid embedding")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError("invalid embedding")
        out.append(float(item))
    norm = math.sqrt(sum(item * item for item in out))
    if norm == 0:
        raise ValueError("invalid embedding")
    return out, norm


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS voiceprint_enrollments(
        tenant_id TEXT NOT NULL,
        identity TEXT NOT NULL,
        model TEXT NOT NULL,
        model_version TEXT NOT NULL,
        embedding_json TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        enrolled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(tenant_id, identity, model, model_version))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS voiceprint_segment_scores(
        recording_id TEXT NOT NULL,
        source_label TEXT NOT NULL,
        identity TEXT,
        confidence REAL,
        margin REAL,
        model TEXT NOT NULL,
        model_version TEXT NOT NULL,
        scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(recording_id, source_label, model, model_version))''')
    conn.commit()


def enroll(conn, *, tenant_id, identity, embedding, model, model_version, source=""):
    """Replace one explicit local consented enrollment, never raw audio."""
    if not all(isinstance(v, str) and v.strip() for v in (tenant_id, identity, model, model_version)):
        raise ValueError("tenant, identity, model and model version are required")
    vector, _ = _embedding(embedding)
    if not isinstance(source, str) or len(source) > 120:
        raise ValueError("invalid enrollment source")
    conn.execute('''INSERT INTO voiceprint_enrollments(tenant_id,identity,model,model_version,embedding_json,source)
        VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,identity,model,model_version) DO UPDATE SET
        embedding_json=excluded.embedding_json, source=excluded.source, enrolled_at=CURRENT_TIMESTAMP''',
        (tenant_id.strip(), identity.strip(), model.strip(), model_version.strip(),
         json.dumps(vector, separators=(",", ":")), source.strip()))
    conn.commit()


def _unknown(reason, confidence=None, margin=None):
    return {"identity": None, "confidence": confidence, "margin": margin, "reason": reason}


def match(conn, *, tenant_id, embedding, model, model_version,
          minimum_confidence=DEFAULT_MINIMUM_CONFIDENCE, minimum_margin=DEFAULT_MINIMUM_MARGIN):
    """Score one vector against same-tenant, same-model local enrollments only."""
    vector, norm = _embedding(embedding)
    if not (0 <= float(minimum_confidence) <= 1 and 0 <= float(minimum_margin) <= 1):
        raise ValueError("invalid voiceprint thresholds")
    rows = conn.execute('''SELECT identity,embedding_json FROM voiceprint_enrollments
        WHERE tenant_id=? AND model=? AND model_version=? ORDER BY identity''',
        (tenant_id, model, model_version)).fetchall()
    if not rows:
        other = conn.execute("SELECT 1 FROM voiceprint_enrollments WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone()
        return _unknown("no_compatible_enrollment" if other else "no_enrollment")
    candidates = []
    for identity, raw in rows:
        try:
            candidate, candidate_norm = _embedding(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if len(candidate) != len(vector):
            continue
        candidates.append((sum(a * b for a, b in zip(vector, candidate)) / (norm * candidate_norm), identity))
    if not candidates:
        return _unknown("no_compatible_enrollment")
    candidates.sort(reverse=True)
    confidence, identity = candidates[0]
    second = candidates[1][0] if len(candidates) > 1 else 0.0
    margin = confidence - second
    # Keep public scores bounded/portable and do not disclose an identity below a gate.
    confidence, margin = round(confidence, 6), round(margin, 6)
    if confidence < float(minimum_confidence):
        return _unknown("confidence", confidence, margin)
    if margin < float(minimum_margin):
        return _unknown("margin", confidence, margin)
    return {"identity": identity, "confidence": confidence, "margin": margin, "reason": None}


def score_segments(conn, *, tenant_id, segments, embeddings, model, model_version,
                   minimum_confidence=DEFAULT_MINIMUM_CONFIDENCE,
                   minimum_margin=DEFAULT_MINIMUM_MARGIN, recording_id=None):
    """Score diarization labels without changing the diarization evidence."""
    out = []
    for segment in (segments or [])[:MAX_SEGMENTS]:
        if not isinstance(segment, dict) or not isinstance(segment.get("speaker"), str):
            continue
        label = segment["speaker"].strip()
        vector = (embeddings or {}).get(label)
        if not label or vector is None:
            continue
        result = match(conn, tenant_id=tenant_id, embedding=vector, model=model,
                       model_version=model_version, minimum_confidence=minimum_confidence,
                       minimum_margin=minimum_margin)
        public = {key: result[key] for key in ("identity", "confidence", "margin")}
        out.append({"speaker": label, **public})
        if recording_id:
            conn.execute('''INSERT INTO voiceprint_segment_scores(recording_id,source_label,identity,confidence,margin,model,model_version)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(recording_id,source_label,model,model_version) DO UPDATE SET
                identity=excluded.identity,confidence=excluded.confidence,margin=excluded.margin,scored_at=CURRENT_TIMESTAMP''',
                (recording_id, label, public["identity"], public["confidence"], public["margin"], model, model_version))
    if recording_id:
        conn.commit()
    return out


def encode_segments(audio_path, segments, *, encoder):
    """Get real vectors from a registered local encoder; no encoder means no data."""
    fn = _ENCODERS.get(encoder) if isinstance(encoder, str) else encoder
    if not callable(fn):
        return {}
    out = {}
    for segment in (segments or [])[:MAX_SEGMENTS]:
        if not isinstance(segment, dict) or not isinstance(segment.get("speaker"), str):
            continue
        label = segment["speaker"].strip()
        if label and label not in out:
            out[label] = fn(audio_path, segment.get("start_ms"), segment.get("end_ms"))
    return out
