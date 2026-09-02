#!/usr/bin/env python3
"""Canonical owner-scoped import for local/drop-folder recordings.

Usage: ``python local_import.py manifest.json`` inside the owner connector.
The source basename is private provenance only. Before a transcript-derived
summary exists, reader-facing metadata uses a neutral Russian seed; summary
publication later promotes the generated semantic title for local imports.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time


LOCAL_TITLE_SEED = "Локальная аудиозапись"


def semantic_seed(_original_name: str | None = None) -> str:
    """Return a content-neutral title; never derive semantics from a basename."""
    return LOCAL_TITLE_SEED


def provenance_metadata(item: dict) -> dict:
    """Build private dedup/audit metadata for one local source."""
    return {
        "source_kind": "nextcloud_external_import",
        "original_name": item["original_name"],
        "source_sha256": item["source_sha256"],
        "source_size": item["source_size"],
        "source_format": item.get("source_format", "mov,mp4,m4a,3gp,3g2,mj2"),
        "source_duration_seconds": item["source_duration_seconds"],
        "voice_memo_uuid": item.get("voice_memo_uuid", ""),
    }


def recording_values(item, *, duration_seconds, final_audio_path, archived_at):
    """Values for ``recordings`` with semantic and provenance fields separated."""
    created = item["creation_time"]
    return (
        item["id"],
        semantic_seed(item.get("original_name")),
        created,
        created,
        round(float(duration_seconds) * 1000),
        None,
        None,
        None,
        "",
        "",
        final_audio_path,
        archived_at,
        json.dumps(provenance_metadata(item), ensure_ascii=False),
    )


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def probe_duration(path):
    probe = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name", "-of", "json", path]
        )
    )["format"]
    if "mp3" not in probe.get("format_name", ""):
        raise RuntimeError("normalized audio is not MP3")
    return float(probe["duration"])


def normalize_audio(item, final):
    source = item["staged_source"]
    if not os.path.isfile(source):
        raise FileNotFoundError("staged source missing")
    if os.path.getsize(source) != item["source_size"] or digest(source) != item["source_sha256"]:
        raise RuntimeError("source verification failed")
    if not os.path.isfile(final):
        temp = f"{final}.tmp"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", source, "-vn", "-codec:a", "libmp3lame", "-q:a", "2", "-f", "mp3", temp,
            ],
            check=True,
        )
        duration = probe_duration(temp)
        if abs(duration - float(item["source_duration_seconds"])) > 1.0:
            raise RuntimeError("normalized MP3 duration mismatch")
        os.replace(temp, final)
    return probe_duration(final)


def persist(item, duration, final, archive_recording, pipeline, recording_codes):
    fid = item["id"]
    if not archive_recording.is_safe_recording_id(fid):
        raise ValueError("unsafe recording id")
    connection = sqlite3.connect(archive_recording.DB, timeout=120)
    connection.execute("PRAGMA busy_timeout=120000")
    archive_recording.init_db(connection)
    title = semantic_seed(item.get("original_name"))
    try:
        connection.execute(
            """INSERT INTO recordings
            (id,name,start_at,created_at,duration_ms,lang,asr_engine,asr_transcript,
             plaud_transcript,summary,audio_path,archived_at,plaud_meta_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,start_at=excluded.start_at,
              created_at=excluded.created_at,duration_ms=excluded.duration_ms,
              audio_path=excluded.audio_path,archived_at=excluded.archived_at,
              plaud_meta_json=excluded.plaud_meta_json""",
            recording_values(
                item,
                duration_seconds=duration,
                final_audio_path=final,
                archived_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            ),
        )
        archive_recording._sync_fts(connection, fid, title, "")
        if not pipeline.resolve_local_audio(archive_recording.AUDIO, fid, final):
            raise RuntimeError("canonical local audio rejected")
        pipeline.enqueue_asr(connection, fid, commit=False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    number = recording_codes.allocate_number(
        connection, archive_recording.code_prefix(), fid
    )
    connection.close()
    return number


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: local_import.py manifest.json", file=sys.stderr)
        return 2
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import archive_recording
    import pipeline
    import recording_codes

    with open(argv[0], encoding="utf-8") as handle:
        item = json.load(handle)
    fid = item["id"]
    if not archive_recording.is_safe_recording_id(fid):
        raise ValueError("unsafe recording id")
    final = os.path.join(archive_recording.AUDIO, f"{fid}.mp3")
    duration = normalize_audio(item, final)
    number = persist(item, duration, final, archive_recording, pipeline, recording_codes)
    os.unlink(item["staged_source"])
    print(json.dumps({"recording_number": number, "semantic_seed": LOCAL_TITLE_SEED}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
