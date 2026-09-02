import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import control  # noqa: E402
import people  # noqa: E402
import pipeline  # noqa: E402
import archive_recording  # noqa: E402


def archive(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, plaud_segments_json TEXT, asr_meta_json TEXT)")
    conn.execute("INSERT INTO recordings VALUES('r1', ?, '{}')", (json.dumps([
        {"speaker": "Speaker 1", "start_ms": 0, "end_ms": 2000},
        {"speaker": "Speaker 2", "start_ms": 2000, "end_ms": 4000},
    ]),))
    pipeline.ensure_schema(conn)
    control.ensure_schema(conn)
    conn.commit()
    return conn


def test_people_directory_create_assign_unknown_and_undo_are_event_sourced(tmp_path):
    conn = archive(tmp_path / "a.db")
    person = people.create(conn, "  Анна  ", consent_status="not_enrolled")
    assert person["display_name"] == "Анна"
    assert person["active"] is True and person["revision"] == 1

    assigned = people.assign(conn, "r1", "speaker-1", person["person_id"])
    assert assigned["person_id"] == person["person_id"]
    assert people.assignments(conn, "r1")["speaker-1"]["display_name"] == "Анна"

    unknown = people.set_unknown(conn, "r1", "speaker-1")
    assert unknown["person_id"] is None
    restored = people.undo(conn, "r1", "speaker-1")
    assert restored["person_id"] == person["person_id"]
    assert [e[0] for e in conn.execute(
        "SELECT action FROM speaker_identity_events ORDER BY event_id")] == [
            "assign", "unknown", "undo"]

    # Undo walks backward through state changes; it does not toggle forever.
    assert people.undo(conn, "r1", "speaker-1")["person_id"] is None


def test_this_is_me_is_explicit_self_consent_and_never_creates_embedding(tmp_path):
    conn = archive(tmp_path / "a.db")
    me = people.create(conn, "Айбек", is_self=True, consent_status="self_confirmed")
    people.assign(conn, "r1", "speaker-1", me["person_id"])
    assert me["is_self"] is True and me["consent_status"] == "self_confirmed"
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='speaker_exemplars'").fetchone() is None


def test_people_are_tenant_local_and_invalid_cross_recording_assignments_fail(tmp_path):
    a, b = archive(tmp_path / "a.db"), archive(tmp_path / "b.db")
    person = people.create(a, "Анна")
    with pytest.raises(people.IdentityRejected):
        people.assign(b, "r1", "speaker-1", person["person_id"])
    with pytest.raises(people.IdentityRejected):
        people.assign(a, "r1", "speaker-9", person["person_id"])


def test_control_boundary_exposes_minimal_people_actions(tmp_path):
    path = tmp_path / "a.db"; archive(path).close()
    token = tmp_path / "token"; token.write_text("a" * 48); os.chmod(token, 0o600)
    sock = tmp_path / "control.sock"
    server = control.ControlServer(str(path), str(sock), str(token)); server.start()
    client = control.ControlClient(str(sock), str(token))
    try:
        created = client.people("create_person", display_name="Анна")
        pid = created["person"]["person_id"]
        assert client.people("assign_identity", recording_id="r1", speaker_id="speaker-1", person_id=pid)["assignment"]["person_id"] == pid
        assert client.people("set_unknown", recording_id="r1", speaker_id="speaker-1")["assignment"]["person_id"] is None
        assert client.people("undo_identity", recording_id="r1", speaker_id="speaker-1")["assignment"]["person_id"] == pid
    finally:
        server.stop()


def test_ingest_admits_diarization_independently_from_asr(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "archive.db")
    monkeypatch.setattr(archive_recording, "AUDIO", str(tmp_path / "audio"))
    monkeypatch.setattr(archive_recording, "code_prefix", lambda: None)
    archive_recording.init_db(conn)
    responses = {
        "get_file": json.dumps({"id": "r1", "name": "x", "duration": 4000,
                                "presigned_url": "local"}),
        "get_transcript": json.dumps({"segments": []}),
        "get_note": json.dumps([]),
    }
    monkeypatch.setattr(archive_recording, "call", lambda name, *_a, **_k: responses[name])
    monkeypatch.setattr(archive_recording, "download_audio", lambda _url, path: Path(path).parent.mkdir(parents=True, exist_ok=True) or Path(path).write_bytes(b"audio") or path)
    monkeypatch.setattr(archive_recording.diarization, "available", lambda: True)
    archive_recording.archive_one(conn, "sid", "r1")
    assert conn.execute("SELECT stage FROM pipeline_jobs ORDER BY seq").fetchall() == [
        ("asr",), ("diarization",)]


def test_local_delete_purges_identity_material(tmp_path):
    conn = archive(tmp_path / "a.db")

    person = people.create(conn, "Анна")
    people.assign(conn, "r1", "speaker-1", person["person_id"])
    conn.execute("UPDATE recordings SET archived_local_at='now' WHERE id='r1'")
    conn.commit()
    assert control.apply(conn, "delete", "r1")["state"] == "deleted"
    assert conn.execute("SELECT COUNT(*) FROM speaker_identity_assignments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM speaker_identity_events").fetchone()[0] == 0
