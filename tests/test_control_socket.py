"""Real UDS boundary tests: canonical connector control, never a viewer queue."""
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import control  # noqa: E402
import pipeline  # noqa: E402


def make_db(path, segments='[{"speaker":"Speaker 1","start_ms":0,"end_ms":1000}]'):
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, asr_transcript TEXT, plaud_transcript TEXT, plaud_segments_json TEXT)")
    conn.execute("INSERT INTO recordings VALUES(?,?,?,?)", ("rec1", "old local", "raw PLAUD", segments))
    pipeline.ensure_schema(conn)
    conn.commit()
    return conn


@pytest.fixture
def service(tmp_path):
    db_path, token_path, socket_path = tmp_path / "archive.db", tmp_path / "token", tmp_path / "control.sock"
    token_path.write_text("a" * 48 + "\n")
    os.chmod(token_path, 0o600)
    conn = make_db(db_path)
    conn.close()
    waker = pipeline.Waker()
    server = control.ControlServer(str(db_path), str(socket_path), str(token_path), waker=waker)
    server.start()
    yield server, db_path, token_path, socket_path, waker
    server.stop()


def test_raw_uds_auth_and_retranscribe_are_atomic_and_wake(service):
    _server, db_path, token_path, socket_path, waker = service
    token = token_path.read_text().strip()
    client = control.ControlClient(str(socket_path), str(token_path))
    assert client.request("retranscribe", "rec1")["state"] == "queued"
    assert waker.wait(0.1) is True
    other = sqlite3.connect(db_path)
    assert other.execute("SELECT stage,state,force_local FROM pipeline_jobs").fetchall() == [("asr", "queued", 1)]
    assert other.execute("SELECT asr_transcript,plaud_transcript FROM recordings").fetchone() == ("old local", "raw PLAUD")
    other.close()
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(str(socket_path))
    raw.sendall(json.dumps({"token": "wrong", "action": "retranscribe", "recording_id": "rec1"}).encode() + b"\n")
    assert json.loads(raw.recv(4096))["error"] == "unauthorized"
    raw.close()


def test_split_request_and_response_frames_are_read_to_newline(service):
    _server, _db_path, token_path, socket_path, _waker = service
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(str(socket_path))
    request = json.dumps({"token": token_path.read_text().strip(), "action": "retranscribe", "recording_id": "rec1"}).encode() + b"\n"
    raw.sendall(request[:11]); time.sleep(.05); raw.sendall(request[11:])
    chunks = []
    while not b"\n" in b"".join(chunks):
        chunks.append(raw.recv(1))
    assert json.loads(b"".join(chunks))["state"] == "queued"
    raw.close()


def test_bad_or_oversize_json_is_rejected_without_mutation(service):
    _server, db_path, _token_path, socket_path, _waker = service
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(str(socket_path)); raw.sendall(b"{" + b"x" * (control.MAX_REQUEST_BYTES + 1)); raw.shutdown(socket.SHUT_WR)
    assert json.loads(raw.recv(4096))["error"] == "bad_request"
    raw.close()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0
    conn.close()


def test_rotation_and_duplicate_taps_collapse(service):
    _server, db_path, token_path, socket_path, _waker = service
    client = control.ControlClient(str(socket_path), str(token_path))
    assert client.request("regenerate", "rec1")["state"] == "queued"
    assert client.request("regenerate", "rec1")["state"] == "already_queued"
    token_path.write_text("b" * 48 + "\n")
    assert client.request("regenerate", "rec1")["state"] == "already_queued"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT stage,state FROM pipeline_jobs").fetchall() == [("summary", "queued")]
    conn.close()


def test_rename_requires_real_diarization_and_is_canonical(service):
    _server, db_path, token_path, socket_path, _waker = service
    client = control.ControlClient(str(socket_path), str(token_path))
    assert client.request("rename", "rec1", {"speaker-1": "Аня"})["state"] == "queued"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT display_name FROM speaker_aliases").fetchone()[0] == "Аня"
    assert conn.execute("SELECT stage FROM pipeline_jobs ORDER BY seq").fetchall() == [("summary",)]
    conn.close()


def test_no_diarization_rename_is_rejected(service, tmp_path):
    server, db_path, token_path, socket_path, _waker = service
    server.stop()
    empty_db = tmp_path / "empty.db"; conn = make_db(empty_db, "[]"); conn.close()
    server = control.ControlServer(str(empty_db), str(socket_path), str(token_path)); server.start()
    try:
        with pytest.raises(control.ControlRejected):
            control.ControlClient(str(socket_path), str(token_path)).request("rename", "rec1", {"speaker-1": "Аня"})
    finally:
        server.stop()


def test_client_outage_is_safe_and_does_not_open_database(tmp_path):
    token = tmp_path / "token"; token.write_text("a" * 48); os.chmod(token, 0o600)
    with pytest.raises(control.ControlUnavailable):
        control.ControlClient(str(tmp_path / "missing.sock"), str(token)).request("retranscribe", "rec1")


def test_source_poll_is_global_authenticated_without_recording_id(service):
    server, _db_path, token_path, socket_path, _waker = service
    calls = []
    server.source_poll = lambda: calls.append("poll") or "queued"
    client = control.ControlClient(str(socket_path), str(token_path))
    assert client.request_source_poll()["state"] == "queued"
    assert calls == ["poll"]
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(str(socket_path))
    raw.sendall(json.dumps({"token": token_path.read_text().strip(), "action": "source_poll", "recording_id": "rec1"}).encode() + b"\n")
    assert json.loads(raw.recv(4096))["error"] == "bad_request"
    raw.close()


def test_annotations_are_bounded_persistent_and_do_not_wake_pipeline(service):
    _server, db_path, token_path, socket_path, waker = service
    client = control.ControlClient(str(socket_path), str(token_path))
    text = "я" * control.MAX_COMMENT_LEN
    result = client.request("add_comment", "rec1", comment=text)
    assert result["comment"]["text"] == text
    assert result["comment"]["id"] == 1
    assert waker.wait(0.02) is False

    task_id = "a" * 24
    assert client.request(
        "set_task_completed", "rec1", task_id=task_id,
        completed=True)["task"] == {"id": task_id, "completed": True}
    assert waker.wait(0.02) is False
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT body FROM recording_comments WHERE recording_id='rec1'"
        ).fetchone()[0] == text
        assert conn.execute(
            "SELECT completed FROM recording_task_states WHERE recording_id='rec1' AND task_id=?",
            (task_id,),
        ).fetchone()[0] == 1


def test_recording_labels_are_multivalued_and_manual_removal_overrides_auto(service):
    _server, db_path, token_path, socket_path, waker = service
    client = control.ControlClient(str(socket_path), str(token_path))
    with sqlite3.connect(db_path) as conn:
        control.ensure_schema(conn)
        conn.executemany(
            "INSERT INTO recording_labels(recording_id,label_id,auto_assigned,manual_override,updated_at) "
            "VALUES('rec1',?,1,NULL,'now')", [('personal',), ('business',)])
        conn.commit()

    assert client.request("set_label", "rec1", label_id="personal", active=False)["label"] == {
        "id": "personal", "active": False}
    assert client.request("set_label", "rec1", label_id="business", active=True)["label"] == {
        "id": "business", "active": True}
    assert waker.wait(0.02) is False

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT label_id,auto_assigned,manual_override FROM recording_labels "
            "WHERE recording_id='rec1' ORDER BY label_id").fetchall()
    assert rows == [('business', 1, 1), ('personal', 1, 0)]


def test_custom_label_catalogue_delete_is_global_atomic_and_revisions_once(service):
    _server, db_path, token_path, socket_path, _waker = service
    client = control.ControlClient(str(socket_path), str(token_path))
    created = client.create_label("Поставщики")["label"]
    client.request("set_label", "rec1", label_id=created["id"], active=True)
    with sqlite3.connect(db_path) as conn:
        before = pipeline.label_catalog_revision(conn)
    result = client.delete_label(created["id"])
    assert result["label_id"] == created["id"]
    assert result["catalog_revision"] == before + 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT 1 FROM label_definitions WHERE id=?", (created["id"],)).fetchone() is None
        assert conn.execute("SELECT 1 FROM recording_labels WHERE label_id=?", (created["id"],)).fetchone() is None
        assert pipeline.label_catalog_revision(conn) == before + 1


def test_catalogue_rejects_system_unknown_and_extra_delete_fields(service):
    _server, _db_path, token_path, socket_path, _waker = service
    client = control.ControlClient(str(socket_path), str(token_path))
    for label_id in ("personal", "business", "custom-missing"):
        with pytest.raises(control.ControlRejected):
            client.delete_label(label_id)
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(str(socket_path))
    raw.sendall(json.dumps({"token": token_path.read_text().strip(), "action": "delete_label",
                            "label_id": "custom-missing", "recording_id": "rec1"}).encode() + b"\n")
    assert json.loads(raw.recv(4096))["error"] == "bad_request"
    raw.close()
