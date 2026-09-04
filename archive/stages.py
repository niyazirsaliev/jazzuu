#!/usr/bin/env python3
"""stages.py — what each durable pipeline stage actually does.

`pipeline.py` owns the queue: ordering, leases, retries, recovery. This module
owns the work, and nothing else. Keeping them apart is what lets the queue be
tested without asr-mcp and the stages be tested without a clock.

The chain, and why each step hands to the next:

    asr      the recording becomes readable. Either local ASR produces the
             transcript, or — when PLAUD delivered provisional text with the
             recording — local ASR produces the candidates that text is
             validated against. Publication of the SELECTED transcript and its
             search index is atomic either way.
    summary  the Russian summary, from whichever transcript was selected.
    mindmap  the mind-map source, derived from the summary.
    card     the summary-card payload, derived from the summary.

The last two record a derived artifact and its revision. Rendering the PNGs
stays in the viewer, which owns the fonts and the image library; what belongs
here is the durable statement that the derived data is current for exactly this
source, so "ready" means something a reader can trust.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline  # noqa: E402
import diarization  # noqa: E402
import voiceprint  # noqa: E402

# Bumped when a derived artifact's meaning changes, so every recording
# re-derives rather than reporting stale data as current.
MINDMAP_VERSION = '1'
CARD_VERSION = '2'

# Below this a transcript cannot carry a summary — and without a summary there
# is no mind map and no card. Matches summary_backfill.pending, deliberately:
# two different answers to "is this summarisable" is how a recording ends up
# retrying a stage that can never succeed.
MIN_SUMMARY_CHARS = 200

# Every ASR entry point uses the same short-window mixed router by default.
# PIPELINE_ASR_ENGINE remains the explicit emergency rollback control.
ASR_ENGINE = os.environ.get('PIPELINE_ASR_ENGINE', 'mixed').strip() or 'mixed'


def ensure_schema(conn) -> None:
    """Where derived-artifact state lives.

    The revision is a digest of exactly the source the artifact was derived
    from, so a re-summarised recording is visibly stale rather than quietly
    wrong.
    """
    conn.execute('''CREATE TABLE IF NOT EXISTS derived_artifacts(
        recording_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        revision TEXT NOT NULL,
        updated_at TEXT,
        PRIMARY KEY(recording_id, kind))''')
    # Connector-owned local enrollment/scores; the viewer only reads a safe view.
    voiceprint.ensure_schema(conn)
    conn.commit()


def revision(version, name, source) -> str:
    return hashlib.sha1(
        (version + '\n' + (name or '') + '\n' + (source or '')).encode('utf-8')
    ).hexdigest()[:12]


def record_artifact(conn, rid, kind, digest) -> None:
    conn.execute(
        '''INSERT INTO derived_artifacts(recording_id,kind,revision,updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(recording_id,kind) DO UPDATE SET
             revision=excluded.revision, updated_at=excluded.updated_at''',
        (rid, kind, digest, pipeline.stamp()))
    conn.commit()


def _row(conn, rid):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    summary_json = 'summary_json' if 'summary_json' in columns else "''"
    return conn.execute(
        f"""SELECT COALESCE(name,''), COALESCE(plaud_transcript,''),
                   COALESCE(asr_transcript,''), COALESCE(summary,''),
                   COALESCE({summary_json},''), COALESCE(duration_ms,0)
              FROM recordings WHERE id=?""", (rid,)).fetchone()


def selected_transcript(conn, rid) -> str:
    """The one transcript this recording is published with.

    Local ASR wins when it exists, exactly as the viewer decides it: a review
    that kept PLAUD's text deliberately leaves asr_transcript empty rather than
    storing the hypothesis it just turned down.
    """
    row = _row(conn, rid)
    if not row:
        return ''
    _name, plaud, asr, _summary, _json, _duration = row
    return (asr or '').strip() or (plaud or '').strip()


# --------------------------------------------------------- diarization ----

def _voiceprint_after_diarization(conn, rid, audio_path, segments, *,
                                  asr_module=None, sid=None):
    """Score real turns only with an explicitly registered local encoder.

    An absent runtime/enrollment is deliberately a no-op. A configured encoder
    error bubbles to the optional diarization job's canonical retry ledger.
    """
    encoder = os.environ.get('VOICEPRINT_ENCODER', '').strip()
    model = os.environ.get('VOICEPRINT_MODEL', '').strip()
    version = os.environ.get('VOICEPRINT_MODEL_VERSION', '').strip()
    if not (encoder and model and version):
        return
    if encoder in voiceprint.available_encoders():
        embeddings = voiceprint.encode_segments(audio_path, segments, encoder=encoder)
    elif encoder == 'asr-mcp' and asr_module is not None and sid:
        url = asr_module.audio_url(audio_path)
        if urllib.parse.urlsplit(url).scheme.lower() != 'file':
            # Voiceprints are local/offline only. HTTP audio publication is a
            # valid ASR compatibility path, but never an identity path.
            return

        def mcp_encoder(_path, start_ms, end_ms):
            try:
                start = max(0.0, float(start_ms) / 1000.0)
                duration = min(300.0, (float(end_ms) - float(start_ms)) / 1000.0)
            except (TypeError, ValueError) as exc:
                raise ValueError('invalid voiceprint segment bounds') from exc
            if duration <= 0:
                raise ValueError('invalid voiceprint segment bounds')
            result = asr_module.call('speaker_embedding_url', {
                'url': url, 'start_sec': start, 'duration_sec': duration,
            }, sid)
            if not isinstance(result, dict) or result.get('status') == 'error':
                raise RuntimeError('speaker embedding unavailable')
            vector = result.get('embedding')
            if (result.get('model') != version
                    or result.get('dimension') != len(vector or ())):
                raise RuntimeError('speaker embedding contract mismatch')
            voiceprint._embedding(vector)
            return vector

        embeddings = voiceprint.encode_segments(audio_path, segments, encoder=mcp_encoder)
    else:
        return
    if embeddings:
        voiceprint.score_segments(
            conn, tenant_id=os.environ.get('TENANT_ID', 'owner').strip() or 'owner',
            recording_id=rid, segments=segments, embeddings=embeddings,
            model=model, model_version=version,
        )


def diarization_stage(conn, job, *, audio_dir, asr_module=None, session=None):
    """Persist only real diarization evidence beside the canonical recording.

    Diarization is optional and is deliberately not a fabricated prerequisite
    of ASR.  Without an engine there is no write and the caller can finish a
    manually requested stage as skipped.  A present engine must return at least
    one valid segment; otherwise preserving the previous metadata is safer than
    publishing invented speakers.
    """
    rid = job['recording_id']
    columns = {row[1] for row in conn.execute('PRAGMA table_info(recordings)')}
    audio_expr = 'audio_path' if 'audio_path' in columns else "''"
    meta_expr = 'asr_meta_json' if 'asr_meta_json' in columns else "''"
    row = conn.execute(
        f"SELECT {audio_expr}, COALESCE(duration_ms,0), {meta_expr} "
        "FROM recordings WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise FileNotFoundError(f'{rid} is not in this archive')
    audio_path = pipeline.resolve_local_audio(audio_dir, rid, row[0])
    if not audio_path:
        raise FileNotFoundError(f'no local audio for {rid}')
    outcome = diarization.run(audio_path, duration_ms=row[1], recording_id=rid)
    if outcome['state'] == diarization.STATE_UNAVAILABLE:
        return pipeline.STOP
    segments = outcome.get('segments') or []
    if not segments:
        raise ValueError('diarization produced no usable segments')
    try:
        metadata = json.loads(row[2] or '{}')
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata['diarization'] = diarization.meta_block(outcome)
    conn.execute("UPDATE recordings SET asr_meta_json=? WHERE id=?",
                 (json.dumps(metadata, ensure_ascii=False), rid))
    conn.commit()
    encoder = os.environ.get('VOICEPRINT_ENCODER', '').strip()
    sid = session() if encoder == 'asr-mcp' and asr_module is not None and session else None
    _voiceprint_after_diarization(
        conn, rid, audio_path, segments, asr_module=asr_module, sid=sid)
    return None


# ------------------------------------------------------------------- ASR ----

def asr_stage(conn, job, asr, sid):
    """Make the recording readable, from LOCAL audio.

    Two shapes, one stage. A textless recording is transcribed. A recording
    that arrived with PLAUD's own transcript is VALIDATED: the provisional text
    is already on screen and stays there, local auto ASR supplies the Kyrgyz and
    large-v3 candidates, and the deterministic rules in asr_backfill pick the
    winner. Either way exactly one transcript, its search index and its verdict
    are published in a single transaction.

    A long recording is windowed at the deployment's bounded segment size, and
    each window is committed before the next is asked for. When a run's bounded
    share is spent the stage defers: the job goes back on the queue with its
    budget untouched, and the next turn resumes from the last committed window.
    """
    rid = job['recording_id']
    # Reject a deleted recording before touching the ASR integration or its
    # attempt ledger. A durable stale job is harmless; opening an MCP path for
    # it is not.
    row = _row(conn, rid)
    if not row:
        raise RuntimeError(f'{rid} is not in this archive')
    # The canonical layout path, exactly as every other asr_backfill entry
    # point calls it: asr_attempts, asr_segments, the derived columns and the
    # review ledger. It is idempotent, and it is the reason a brand-new tenant
    # archive can take its first job without anyone having run a migration.
    asr.ensure_attempts(conn)
    _name, plaud_text, _asr_text, _summary, _json, duration_ms = row
    duration_sec = asr.duration_sec_from_ms(duration_ms)

    def committed_window(index, total):
        """One window is durably stored; renew the lease or stop.

        Two workers on the same four hours of audio is the outcome no later
        write can undo, and a fenced publish would only catch it at the end —
        after the GPU time is already spent. checkpoint raises ClaimLost here
        instead, at the cheapest possible moment.
        """
        pipeline.checkpoint(conn, job)
        pipeline.log(f'asr {rid}: window {index + 1}/{total} committed')

    try:
        # A reader explicitly asked for a replacement: PLAUD remains preserved
        # as source/fallback text, but must not turn the fresh local run into a
        # review path that can retain the old preference.
        if (plaud_text or '').strip() and not job.get('force_local'):
            asr.ensure_review_row(conn, rid)
            verdict = asr.review_one(conn, sid, rid, engine=ASR_ENGINE,
                                    duration_sec=duration_sec,
                                    on_window=committed_window, job=job)
            if verdict is None:
                raise pipeline.StageDeferred('review claim is held; publication deferred')
        else:
            kwargs = {'on_window': committed_window}
            # Preserve the long-standing tiny test/dummy ASR interface while
            # production's real transcriber receives the fenced publisher.
            if 'publish' in inspect.signature(asr.transcribe).parameters:
                kwargs['publish'] = lambda *args: asr.store(*args, job=job)
            asr.transcribe(conn, sid, rid, ASR_ENGINE, duration_sec, **kwargs)
    except asr.SegmentPaused as exc:
        # Nothing failed: this turn transcribed its bounded share and committed
        # it. Handing the queue back is the point — a four-hour recording must
        # not hold the worker against everything behind it.
        raise pipeline.StageDeferred(str(exc)) from exc
    except asr.SegmentIncomplete as exc:
        if exc.transient:
            raise pipeline.StageDeferred(str(exc)) from exc
        raise
    except Exception:
        # Review is subordinate to this pipeline attempt: do not leave an
        # independent same-process claim that turns the retry into a decline.
        if (plaud_text or '').strip() and not job.get('force_local'):
            asr.release_review(conn, rid)
        raise
    pipeline.heartbeat(conn, job)
    return None


# --------------------------------------------------------------- summary ----

def summary_stage(conn, job, summary_module, sid):
    """The Russian summary, from whichever transcript the ASR stage selected.

    A recording too short to summarise ends the chain here rather than failing:
    there is nothing to summarise, so there is no mind map and no card, and
    retrying a stage that can never succeed would leave it in retry_wait
    forever while reading as unfinished work.
    """
    rid = job['recording_id']
    transcript = selected_transcript(conn, rid)
    if len(transcript) < MIN_SUMMARY_CHARS:
        pipeline.log(f'summary SKIP {rid}: transcript is '
                     f'{len(transcript)} chars, nothing to summarise')
        return pipeline.STOP
    # Aliases are canonical archive data, but presentation-only: do not mutate
    # either raw transcript just because a reader named a speaker.  The explicit
    # regenerate generation alone uses the rendered text to rebuild material.
    names = {}
    try:
        rows = conn.execute('''SELECT source_label, display_name FROM speaker_aliases
                               WHERE recording_id=?''', (rid,)).fetchall()
        names = {source: display for source, display in rows if source and display}
    except Exception:  # pre-alias archives remain valid summary archives
        names = {}
    presentation = transcript
    for source, display in names.items():
        presentation = re.sub(r'(?<=\[)' + re.escape(source) + r'(?=\])',
                              display, presentation)
    summary_module.ensure_attempts(conn)
    # asr-mcp's deployed summarize_transcript contract accepts transcript and
    # title only.  Aliases stay inline in presentation, never as a speculative
    # speaker_names keyword that would make a real deployment reject the call.
    published = summary_module.backfill_one(
        conn, sid, rid,
        force=bool(job.get('force_replace')),
        transcript=presentation,
        job=job,
    )
    if published is False:
        raise pipeline.StageDeferred('summary pipeline claim changed before publication')
    return None



def summary_en_stage(conn, job, summary_module, sid):
    """Translate the canonical summary into an isolated language variant."""
    rid = job['recording_id']
    ensure_schema(conn)
    pipeline.ensure_summary_variants_schema(conn)
    language = pipeline.normalize_summary_language(job.get('summary_language') or 'en')
    row = conn.execute("SELECT COALESCE(name,''),COALESCE(summary,''),COALESCE(summary_json,'') FROM recordings WHERE id=?", (rid,)).fetchone()
    if not row or not (row[1].strip() or row[2].strip()):
        raise pipeline.StageDeferred('canonical Russian summary is not ready')
    source = row[2].strip() or row[1].strip()
    result = summary_module.call('summarize_transcript', {'transcript': source, 'title': row[0], 'target_language': language}, sid)
    markdown = result.get('summary_markdown') if isinstance(result, dict) else None
    structured = result.get('structured') if isinstance(result, dict) else None
    if not isinstance(markdown, str) or not markdown.strip() or not isinstance(structured, dict):
        raise RuntimeError('summary translation tool returned invalid output')
    if not pipeline.holds_claim(conn, job):
        raise pipeline.StageDeferred('summary pipeline claim changed before publication')
    conn.execute("INSERT INTO recording_summary_variants(recording_id,language,summary,summary_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(recording_id,language) DO UPDATE SET summary=excluded.summary,summary_json=excluded.summary_json,updated_at=excluded.updated_at", (rid, language, markdown.strip() + '\n', json.dumps(structured, ensure_ascii=False), pipeline.stamp()))
    conn.commit()
    return None

# --------------------------------------------------------------- derived ----

def mindmap_stage(conn, job, **_unused):
    """Record that the mind-map source is current for this summary."""
    ensure_schema(conn)
    rid = job['recording_id']
    row = _row(conn, rid)
    name, plaud, asr_text, summary, summary_json, _duration = row
    source = summary_json or summary or (asr_text or plaud)
    if not (source or '').strip():
        pipeline.log(f'mindmap SKIP {rid}: nothing to draw a map from')
        return pipeline.STOP
    record_artifact(conn, rid, 'mindmap',
                    revision(MINDMAP_VERSION, name, source))
    return None


def card_stage(conn, job, **_unused):
    """Record that the summary-card payload is current.

    The card is rendered from the structured summary alone, so a recording
    whose summary never became structured JSON has no card. That ends the chain
    cleanly — it is a fact about the summary, not a failure to retry.
    """
    ensure_schema(conn)
    rid = job['recording_id']
    row = _row(conn, rid)
    name, _plaud, _asr_text, _summary, summary_json, _duration = row
    try:
        data = json.loads(summary_json) if summary_json else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        pipeline.log(f'card SKIP {rid}: no structured summary to render')
        return pipeline.STOP
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    record_artifact(conn, rid, 'card', revision(CARD_VERSION, name, payload))
    return None


# -------------------------------------------------------------- assembly ----

def build_handlers(asr_module=None, summary_module=None, session=None, audio_dir=None):
    """{stage: handler} for one worker.

    `session` resolves a module to a live MCP session id, lazily: a drain that
    finds an empty queue must not open a connection to asr-mcp, and a PLAUD
    outage must not stop already-local jobs from draining.
    """
    def resolve(module):
        return session(module) if session else module.mcp_connect()

    def unavailable(stage):
        # Token/config/module readiness can change between passes (a mounted
        # caller token may arrive or rotate).  It is not evidence that audio is
        # bad, so preserve the job and its retry budget.  pipeline.drain gives
        # a deferred row only one turn per pass, avoiding an in-process hot
        # loop; the next connector wake retries it immediately.
        def defer_unavailable(_conn, _job):
            raise pipeline.StageDeferred(
                f'{stage} handler is temporarily unavailable')
        return defer_unavailable

    handlers = {}
    if asr_module is not None:
        handlers[pipeline.STAGE_ASR] = (
            lambda conn, job: asr_stage(conn, job, asr_module,
                                        resolve(asr_module)))
    else:
        handlers[pipeline.STAGE_ASR] = unavailable(pipeline.STAGE_ASR)
    if summary_module is not None:
        handlers[pipeline.STAGE_SUMMARY] = (
            lambda conn, job: summary_stage(conn, job, summary_module,
                                            resolve(summary_module)))
    else:
        handlers[pipeline.STAGE_SUMMARY] = unavailable(pipeline.STAGE_SUMMARY)
    if summary_module is not None:
        handlers[pipeline.STAGE_SUMMARY_EN] = (lambda conn, job: summary_en_stage(conn, job, summary_module, resolve(summary_module)))
    else:
        handlers[pipeline.STAGE_SUMMARY_EN] = unavailable(pipeline.STAGE_SUMMARY_EN)
    handlers[pipeline.STAGE_DIARIZATION] = (
        lambda conn, job: diarization_stage(
            conn, job, audio_dir=audio_dir, asr_module=asr_module,
            session=(lambda: resolve(asr_module)) if asr_module else None)
        if audio_dir else unavailable(pipeline.STAGE_DIARIZATION))
    handlers[pipeline.STAGE_MINDMAP] = mindmap_stage
    handlers[pipeline.STAGE_CARD] = card_stage
    return handlers
