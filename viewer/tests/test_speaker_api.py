"""Authenticated HTTP -> viewer UDS client -> connector archive integration."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "archive"))
sys.path.insert(0, str(ROOT / "viewer"))
import control  # noqa: E402
import pipeline  # noqa: E402
from app import main  # noqa: E402

SEGMENTS = [
    {"speaker": "Speaker 1", "start_ms": 0, "end_ms": 6000, "text": "Привет"},
    {"speaker": "Speaker 2", "start_ms": 6000, "end_ms": 11000, "text": "Как дела"},
    {"speaker": "Speaker 1", "start_ms": 11000, "end_ms": 14000, "text": "Нормально"},
]


def make_archive(path, rec_id="rec1", **overrides):
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,start_at TEXT,created_at TEXT,
       duration_ms INTEGER,lang TEXT,asr_engine TEXT,asr_transcript TEXT,
       plaud_transcript TEXT,plaud_segments_json TEXT,summary TEXT,summary_json TEXT,
       asr_meta_json TEXT,asr_alternative_transcript TEXT);
      CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED,name,transcript);
    """)
    row = dict(id=rec_id, name="Планёрка", start_at="2026-08-08 10:00:00",
        created_at="2026-08-08 10:00:00", duration_ms=14000, lang="ru",
        asr_engine="host", asr_transcript="[Speaker 1] Привет\n[Speaker 2] Как дела",
        plaud_transcript="", plaud_segments_json=json.dumps(SEGMENTS, ensure_ascii=False),
        summary="Старое резюме", summary_json=json.dumps({"overview":"Старый итог"}, ensure_ascii=False),
        asr_meta_json=json.dumps({"route":{"selected_engine":"host"}}),
        asr_alternative_transcript="СЕКРЕТНАЯ АЛЬТЕРНАТИВА")
    row.update(overrides)
    conn.execute(f"INSERT INTO recordings({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
    conn.execute("INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)", (rec_id, row["name"], row["asr_transcript"]))
    pipeline.ensure_schema(conn); control.ensure_schema(conn)
    conn.commit(); conn.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    archive_path = tmp_path / "archive.db"; make_archive(archive_path)
    token_path = tmp_path / "control.token"; token_path.write_text("a" * 48); os.chmod(token_path, 0o600)
    socket_path = tmp_path / "control.sock"
    server = control.ControlServer(str(archive_path), str(socket_path), str(token_path)); server.start()
    def open_db():
        conn = sqlite3.connect(archive_path); conn.row_factory = sqlite3.Row; return conn
    monkeypatch.setattr(main, "db", open_db)
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setenv("RECORDINGS_CONTROL_SOCKET", str(socket_path))
    monkeypatch.setenv("RECORDINGS_CONTROL_TOKEN_FILE", str(token_path))
    client = TestClient(main.app); client.cookies.set(main.COOKIE_NAME, main.expected_token())
    class Harness:
        csrf = {"X-CSRF-Token": main.csrf_token()}
        def __init__(self): self.client, self.archive_path = client, archive_path
        def sql(self, query, args=()):
            with sqlite3.connect(self.archive_path) as conn: return conn.execute(query, args).fetchall()
        def row(self):
            with open_db() as conn: return dict(conn.execute("SELECT * FROM recordings WHERE id='rec1'").fetchone())
    harness = Harness()
    try: yield harness
    finally:
        client.close(); server.stop()


def aliases(app, rid="rec1"):
    return app.sql("SELECT speaker_id,display_name FROM speaker_aliases WHERE recording_id=? ORDER BY speaker_id", (rid,))


def test_source_poll_requires_auth_and_csrf_and_forwards_only_global_intent(app, monkeypatch):
    anonymous = TestClient(main.app)
    assert anonymous.post("/api/source/poll").status_code == 401
    assert app.client.post("/api/source/poll").status_code == 403
    calls = []
    class Client:
        def request_source_poll(self):
            calls.append(True)
            return {"state": "queued"}
    monkeypatch.setattr(main.control_client, "ControlClient", Client)
    reply = app.client.post("/api/source/poll", json={}, headers=app.csrf)
    assert reply.status_code == 200
    assert reply.json() == {"state": "queued", "message_ru": "Проверка PLAUD запущена"}
    assert calls == [True]


def test_source_poll_control_outage_is_safe_503(app, monkeypatch):
    class Client:
        def request_source_poll(self):
            raise main.control_client.ControlUnavailable()
    monkeypatch.setattr(main.control_client, "ControlClient", Client)
    assert app.client.post("/api/source/poll", json={}, headers=app.csrf).status_code == 503

def jobs(app, rid="rec1"):
    return app.sql("SELECT stage,state,force_replace FROM pipeline_jobs WHERE recording_id=? ORDER BY seq", (rid,))


def test_speakers_detail_is_truthful_and_uses_canonical_aliases(app):
    response = app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":"Аня"}}, headers=app.csrf)
    assert response.status_code == 200
    body = app.client.get("/api/recordings/rec1/speakers").json()
    detail = app.client.get("/api/recordings/rec1").json()
    assert body["state"] == "ready" and body["count"] == 2
    assert body["speakers"][0]["source_label"] == "Speaker 1"
    assert detail["speaker_names"] == {"speaker-1":"Аня"}
    assert detail["segments"][0]["speaker"] == "Speaker 1"
    assert detail["segments"][0]["display_name"] == "Аня"
    assert aliases(app) == [("speaker-1", "Аня")]
    assert jobs(app) == [("summary", "queued", 1)]


def test_no_diarization_labels_are_unavailable_not_guessed(app, tmp_path, monkeypatch):
    path = tmp_path / "empty.db"; make_archive(path, plaud_segments_json=None, asr_transcript="Аня сказала да")
    def empty_db():
        conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row; return conn
    monkeypatch.setattr(main, "db", empty_db)
    client = TestClient(main.app); client.cookies.set(main.COOKIE_NAME, main.expected_token())
    try:
        body = client.get("/api/recordings/rec1/speakers").json()
        assert body["state"] == "unavailable" and body["speakers"] == [] and "недоступна" in body["note_ru"]
    finally: client.close()


def test_authenticated_diarize_action_queues_canonical_job_and_reports_pending(app):
    with sqlite3.connect(app.archive_path) as conn:
        conn.execute("UPDATE recordings SET plaud_segments_json=NULL,asr_meta_json='{}' WHERE id='rec1'")
    reply = app.client.post("/api/recordings/rec1/reprocess", json={"action":"diarize"}, headers=app.csrf)
    assert reply.status_code == 200 and reply.json()["state"] == "queued"
    body = app.client.get("/api/recordings/rec1/speakers").json()
    assert body["state"] == "pending"
    assert body["jobs"]["diarization"] == {"state": "queued"}


def test_rename_is_idempotent_scoped_and_never_enqueues_asr(app):
    for name in ("Аня", "Анна"):
        assert app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":name}}, headers=app.csrf).status_code == 200
    assert aliases(app) == [("speaker-1", "Анна")]
    assert jobs(app) == [("summary", "queued", 1)]
    assert aliases(app, "rec2") == []


def test_blank_unknown_and_invalid_names_are_safe_russian_rejections(app):
    assert app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":"Аня"}}, headers=app.csrf).status_code == 200
    assert app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":""}}, headers=app.csrf).status_code == 200
    assert aliases(app) == []
    for payload in ({"names":{"speaker-9":"Никого"}}, {"names":{"speaker-1":"a"*200}}, {"names":{"speaker-1":"<script>"}}, {"names":"Аня"}, {}):
        r = app.client.post("/api/recordings/rec1/speakers", json=payload, headers=app.csrf)
        assert r.status_code == 400 and set(r.json()) <= {"error_ru","field"} and "Traceback" not in json.dumps(r.json())
    assert aliases(app) == []


def test_retranscribe_maps_to_one_force_local_asr_job_without_mutating_material(app):
    before = app.row()
    first = app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}, headers=app.csrf)
    second = app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}, headers=app.csrf)
    assert first.json()["state"] == "queued" and second.json()["state"] == "already_queued"
    assert jobs(app) == [("asr", "queued", 1)]
    assert app.row() == before


def test_materials_maps_to_canonical_summary_and_current_material_stays_visible(app):
    r = app.client.post("/api/recordings/rec1/reprocess", json={"action":"materials"}, headers=app.csrf)
    assert r.status_code == 200 and jobs(app) == [("summary", "queued", 1)]
    detail = app.client.get("/api/recordings/rec1").json()
    assert detail["asr_transcript"].startswith("[Speaker 1]") and detail["summary"] == "Старое резюме"
    assert detail["summary_data"] == {"overview":"Старый итог"} and detail["jobs"]["summary"]["state"] == "queued"


def test_control_outage_is_503_and_never_creates_a_second_queue(app, monkeypatch):
    monkeypatch.setenv("RECORDINGS_CONTROL_SOCKET", str(Path(app.archive_path).with_suffix(".missing.sock")))
    r = app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}, headers=app.csrf)
    assert r.status_code == 503 and r.json()["error_ru"]
    assert jobs(app) == []


def test_auth_csrf_not_found_and_hostile_ids_do_not_leak_or_write(app):
    anonymous = TestClient(main.app)
    try:
        assert anonymous.get("/api/recordings/rec1/speakers").status_code == 401
        assert anonymous.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}).status_code == 401
    finally: anonymous.close()
    assert app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}).status_code == 403
    for rid in ("ghost", "../../etc/passwd", "rec 1", "x" * 200):
        r = app.client.post(f"/api/recordings/{rid}/reprocess", json={"action":"transcript"}, headers=app.csrf)
        assert r.status_code in (400,404,405) and "root:" not in r.text
    assert jobs(app) == [] and aliases(app) == []


def test_public_mutation_and_speaker_payloads_are_redacted(app):
    replies = [
        app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":"Аня"}}, headers=app.csrf),
        app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}, headers=app.csrf),
        app.client.get("/api/recordings/rec1/speakers")]
    for reply in replies:
        blob = json.dumps(reply.json(), ensure_ascii=False)
        for secret in ("СЕКРЕТНАЯ АЛЬТЕРНАТИВА", "Привет", "test-secret", "Traceback", "/archive"):
            assert secret not in blob
    speaker = replies[-1].json()["speakers"][0]
    assert set(speaker) == {"speaker_id","source_label","display_name","name_ru","segment_count","total_ms","snippet","identity"}


def test_csrf_endpoint_issues_distinct_token(app):
    issued = app.client.get("/api/csrf")
    assert issued.status_code == 200 and issued.json()["csrf"] == main.csrf_token()
    assert issued.json()["csrf"] != main.expected_token()


def test_unknown_reprocess_action_is_safe_and_does_not_queue(app):
    r = app.client.post("/api/recordings/rec1/reprocess", json={"action":"rm -rf /"}, headers=app.csrf)
    assert r.status_code == 400 and r.json()["error_ru"] and jobs(app) == []


def test_missing_recording_returns_404_without_alias_or_job(app):
    r = app.client.post("/api/recordings/ghost/speakers", json={"names":{"speaker-1":"Аня"}}, headers=app.csrf)
    assert r.status_code == 404 and aliases(app) == [] and jobs(app) == []


def test_speakers_endpoint_requires_authenticated_session(app):
    client = TestClient(main.app)
    try: assert client.get("/api/recordings/rec1/speakers").json() == {"error":"unauthorized"}
    finally: client.close()


def test_wrong_csrf_never_reaches_control_server(app):
    r = app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}, headers={"X-CSRF-Token":"wrong"})
    assert r.status_code == 403 and jobs(app) == []


def test_alias_clear_keeps_only_one_canonical_summary_job(app):
    app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":"Аня"}}, headers=app.csrf)
    app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":""}}, headers=app.csrf)
    assert aliases(app) == [] and jobs(app) == [("summary","queued",1)]


def test_speaker_response_exposes_intervals_but_no_audio_url(app):
    body = app.client.get("/api/recordings/rec1/speakers").json()
    assert "/audio/" not in json.dumps(body)
    assert body["speakers"][0]["snippet"] == {"start_ms":0,"end_ms":6000}


def test_people_directory_and_manual_identity_round_trip(app):
    created = app.client.post("/api/people", json={"display_name": "Анна"}, headers=app.csrf)
    assert created.status_code == 200
    person = created.json()["person"]
    assert set(person) == {"person_id", "display_name", "is_self", "consent_status", "active", "revision"}
    assert app.client.get("/api/people").json() == {"people": [person]}
    assigned = app.client.post("/api/recordings/rec1/speakers/speaker-1/identity",
        json={"person_id": person["person_id"]}, headers=app.csrf)
    assert assigned.status_code == 200 and assigned.json()["assignment"]["display_name"] == "Анна"
    body = app.client.get("/api/recordings/rec1/speakers").json()
    assert body["speakers"][0]["identity"]["person_id"] == person["person_id"]
    assert app.client.post("/api/recordings/rec1/speakers/speaker-1/identity",
        json={"unknown": True}, headers=app.csrf).json()["assignment"]["person_id"] is None
    assert app.client.post("/api/recordings/rec1/speakers/speaker-1/identity/undo",
        json={}, headers=app.csrf).json()["assignment"]["person_id"] == person["person_id"]


def test_this_is_me_requires_explicit_action_and_identity_mutations_require_csrf(app):
    assert app.client.post("/api/people", json={"display_name": "Айбек", "is_self": True}, headers=app.csrf).status_code == 400
    me = app.client.post("/api/people", json={"display_name": "Айбек", "this_is_me": True}, headers=app.csrf)
    assert me.status_code == 200 and me.json()["person"]["consent_status"] == "self_confirmed"
    assert app.client.post("/api/recordings/rec1/speakers/speaker-1/identity",
        json={"person_id": me.json()["person"]["person_id"]}).status_code == 403


def test_comment_round_trips_through_authenticated_http_uds_and_detail(app):
    created = app.client.post(
        "/api/recordings/rec1/comments",
        json={"text": "  Позвонить клиенту\nпосле обеда  "},
        headers=app.csrf,
    )
    assert created.status_code == 200
    comment = created.json()["comment"]
    assert comment["id"] > 0
    assert comment["text"] == "Позвонить клиенту\nпосле обеда"
    assert comment["created_at"]
    assert app.sql(
        "SELECT recording_id,body,created_at FROM recording_comments"
    ) == [("rec1", comment["text"], comment["created_at"])]
    assert app.client.get("/api/recordings/rec1").json()["comments"] == [comment]


def test_generated_tasks_have_stable_distinct_ids_and_persistent_toggle(app):
    summary = {
        "overview": "План",
        "action_items": [
            {"task": "Позвонить", "owner": "Айбек", "due": "завтра"},
            {"task": "Позвонить", "owner": "Айбек", "due": "завтра"},
            "Отправить письмо",
        ],
    }
    with sqlite3.connect(app.archive_path) as conn:
        conn.execute("UPDATE recordings SET summary_json=? WHERE id='rec1'",
                     (json.dumps(summary, ensure_ascii=False),))
    tasks = app.client.get("/api/recordings/rec1").json()["tasks"]
    assert [task["text"] for task in tasks] == ["Позвонить", "Позвонить", "Отправить письмо"]
    assert len({task["id"] for task in tasks}) == 3
    assert all(len(task["id"]) == 24 and not task["completed"] for task in tasks)

    task_id = tasks[1]["id"]
    done = app.client.post(
        f"/api/recordings/rec1/tasks/{task_id}",
        json={"completed": True}, headers=app.csrf)
    assert done.status_code == 200
    assert done.json()["task"] == {"id": task_id, "completed": True}
    assert app.sql(
        "SELECT recording_id,task_id,completed FROM recording_task_states"
    ) == [("rec1", task_id, 1)]
    refreshed = app.client.get("/api/recordings/rec1").json()["tasks"]
    assert refreshed[1]["completed"] is True

    reopened = app.client.post(
        f"/api/recordings/rec1/tasks/{task_id}",
        json={"completed": False}, headers=app.csrf)
    assert reopened.json()["task"]["completed"] is False
    assert app.client.get("/api/recordings/rec1").json()["tasks"][1]["completed"] is False


def test_task_completion_survives_regenerated_metadata_for_same_text(app):
    first_summary = {"action_items": [{"task": "Отправить письмо", "owner": "Аня", "due": "завтра"}]}
    with sqlite3.connect(app.archive_path) as conn:
        conn.execute("UPDATE recordings SET summary_json=? WHERE id='rec1'",
                     (json.dumps(first_summary, ensure_ascii=False),))
    first = app.client.get("/api/recordings/rec1").json()["tasks"][0]
    assert app.client.post(
        f"/api/recordings/rec1/tasks/{first['id']}",
        json={"completed": True}, headers=app.csrf).status_code == 200

    regenerated = {"action_items": [{"text": "  Отправить   письмо  ", "owner": "Айбек", "deadline": "пятница"}]}
    with sqlite3.connect(app.archive_path) as conn:
        conn.execute("UPDATE recordings SET summary_json=? WHERE id='rec1'",
                     (json.dumps(regenerated, ensure_ascii=False),))
    after = app.client.get("/api/recordings/rec1").json()["tasks"][0]
    assert after["id"] == first["id"]
    assert after["completed"] is True
    assert after["owner"] == "Айбек" and after["due"] == "пятница"


def test_comment_and_task_mutations_fail_closed_at_every_boundary(app):
    summary = {"action_items": [{"task": "Разрешённая задача"}]}
    with sqlite3.connect(app.archive_path) as conn:
        conn.execute("UPDATE recordings SET summary_json=? WHERE id='rec1'",
                     (json.dumps(summary, ensure_ascii=False),))
    task_id = app.client.get("/api/recordings/rec1").json()["tasks"][0]["id"]
    anonymous = TestClient(main.app)
    try:
        assert anonymous.post("/api/recordings/rec1/comments", json={"text": "x"}).status_code == 401
        assert anonymous.post(
            f"/api/recordings/rec1/tasks/{task_id}", json={"completed": True}).status_code == 401
    finally:
        anonymous.close()
    assert app.client.post(
        "/api/recordings/rec1/comments", json={"text": "x"}).status_code == 403
    assert app.client.post(
        f"/api/recordings/rec1/tasks/{task_id}", json={"completed": True}).status_code == 403
    assert app.client.post(
        "/api/recordings/ghost/comments", json={"text": "x"}, headers=app.csrf).status_code == 404
    assert app.client.post(
        "/api/recordings/rec1/comments", json={"text": "x" * 2001}, headers=app.csrf).status_code == 400
    assert app.client.post(
        "/api/recordings/rec1/tasks/000000000000000000000000",
        json={"completed": True}, headers=app.csrf).status_code == 404
    assert app.sql("SELECT COUNT(*) FROM recording_comments")[0][0] == 0
    assert app.sql("SELECT COUNT(*) FROM recording_task_states")[0][0] == 0


def test_labels_round_trip_through_authenticated_http_uds_and_effective_projection(app):
    with sqlite3.connect(app.archive_path) as conn:
        conn.execute("INSERT INTO recording_labels VALUES('rec1','personal',1,NULL,'now')")
        conn.execute("INSERT INTO recording_labels VALUES('rec1','business',1,NULL,'now')")
    detail = app.client.get("/api/recordings/rec1").json()
    assert set(detail["labels"]) == {"personal", "business"}
    removed = app.client.post("/api/recordings/rec1/labels", json={
        "label_id": "personal", "active": False}, headers=app.csrf)
    assert removed.status_code == 200 and removed.json()["label"] == {
        "id": "personal", "active": False}
    created = app.client.post("/api/labels", json={
        "name": "  Поставщики  "}, headers=app.csrf)
    assert created.status_code == 200
    label = created.json()["label"]
    assert label["name"] == "Поставщики" and label["kind"] == "custom"
    assigned = app.client.post("/api/recordings/rec1/labels", json={
        "label_id": label["id"], "active": True}, headers=app.csrf)
    assert assigned.status_code == 200 and assigned.json()["label"] == {
        "id": label["id"], "active": True}
    refreshed = app.client.get("/api/recordings/rec1").json()
    assert refreshed["labels"] == ["business", label["id"]]
    assert app.sql("SELECT stage FROM pipeline_jobs") == []


def test_label_mutations_reject_auth_csrf_bad_input_and_control_outage(app, monkeypatch):
    anonymous = TestClient(main.app)
    try:
        assert anonymous.post("/api/recordings/rec1/labels", json={"label_id": "x", "active": True}).status_code == 401
    finally:
        anonymous.close()
    assert app.client.post("/api/recordings/rec1/labels", json={"label_id": "x", "active": True}).status_code == 403
    assert app.client.post("/api/recordings/rec1/labels", json={"label_id": "<x>", "active": True}, headers=app.csrf).status_code == 400
    assert app.client.post("/api/labels", json={"name": ""}, headers=app.csrf).status_code == 400
    assert app.client.post("/api/labels", json={"name": "x", "recording_id": "rec1"}, headers=app.csrf).status_code == 400
    monkeypatch.setenv("RECORDINGS_CONTROL_SOCKET", str(Path(app.archive_path).with_suffix(".missing.sock")))
    assert app.client.post("/api/labels", json={"name": "x"}, headers=app.csrf).status_code == 503


def test_label_delete_rejects_auth_csrf_system_unknown_and_control_outage(app, monkeypatch):
    created = app.client.post("/api/labels", json={"name": "Удалить"}, headers=app.csrf).json()["label"]
    anonymous = TestClient(main.app)
    try:
        assert anonymous.delete(f"/api/labels/{created['id']}").status_code == 401
    finally:
        anonymous.close()
    assert app.client.delete(f"/api/labels/{created['id']}").status_code == 403
    assert app.client.delete("/api/labels/personal", headers=app.csrf).status_code == 400
    assert app.client.delete("/api/labels/custom-0000000000000000", headers=app.csrf).status_code == 400
    monkeypatch.setenv("RECORDINGS_CONTROL_SOCKET", str(Path(app.archive_path).with_suffix(".missing.sock")))
    assert app.client.delete(f"/api/labels/{created['id']}", headers=app.csrf).status_code == 503


def test_retranscribe_and_regenerate_use_no_sidecar_database(app):
    app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}, headers=app.csrf)
    assert app.sql("SELECT COUNT(*) FROM sqlite_master WHERE name='jobs'")[0][0] == 0
    assert jobs(app) == [("asr","queued",1)]


def test_pending_canonical_job_is_visible_through_public_stage_mapping(app):
    app.client.post("/api/recordings/rec1/reprocess", json={"action":"transcript"}, headers=app.csrf)
    detail = app.client.get("/api/recordings/rec1").json()
    assert detail["jobs"] == {"asr":{"state":"queued"}}


def test_name_save_response_has_only_safe_public_fields(app):
    body = app.client.post("/api/recordings/rec1/speakers", json={"names":{"speaker-1":"Аня"}}, headers=app.csrf).json()
    assert set(body) <= {"saved","status_ru","state"} and body["saved"] == 1


def test_reprocess_response_has_only_safe_public_fields(app):
    body = app.client.post("/api/recordings/rec1/reprocess", json={"action":"materials"}, headers=app.csrf).json()
    assert set(body) == {"action","state","message_ru"} and body["state"] == "queued"


def test_duplicate_material_tap_collapses_to_one_canonical_summary_job(app):
    app.client.post("/api/recordings/rec1/reprocess", json={"action":"materials"}, headers=app.csrf)
    r = app.client.post("/api/recordings/rec1/reprocess", json={"action":"materials"}, headers=app.csrf)
    assert r.json()["state"] == "already_queued" and jobs(app) == [("summary","queued",1)]


def test_archive_restore_and_delete_are_durable_and_cascade_local_state(app):
    with sqlite3.connect(app.archive_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS asr_segments(
            id TEXT, seg_index INTEGER, status TEXT)""")
        conn.execute("INSERT INTO asr_segments VALUES('rec1',0,'done')")
    assert app.client.post("/api/recordings/rec1/archive", headers=app.csrf).json() == {"state": "archived"}
    assert [row["id"] for row in app.client.get("/api/recordings").json()] == []
    archived = app.client.get("/api/recordings?archived=true").json()
    assert [row["id"] for row in archived] == ["rec1"]
    assert app.client.get("/api/recordings/rec1").json()["archived"] is True
    assert app.client.post("/api/recordings/rec1/restore", headers=app.csrf).json() == {"state": "active"}
    assert [row["id"] for row in app.client.get("/api/recordings").json()] == ["rec1"]
    assert app.client.post("/api/recordings/rec1/delete", headers=app.csrf).status_code == 404
    assert app.client.post("/api/recordings/rec1/archive", headers=app.csrf).status_code == 200
    assert app.client.post("/api/recordings/rec1/delete", headers=app.csrf).json() == {"state": "deleted"}
    assert app.client.get("/api/recordings/rec1").status_code == 404
    assert app.sql("SELECT recording_id FROM recording_tombstones") == [("rec1",)]
    assert app.sql("SELECT COUNT(*) FROM pipeline_jobs")[0][0] == 0
    assert app.sql("SELECT COUNT(*) FROM asr_segments")[0][0] == 0


def test_deleted_recording_audio_is_denied_even_if_unlink_failed(app, tmp_path, monkeypatch):
    audio = tmp_path / "rec1.mp3"
    audio.write_bytes(b"orphan")
    monkeypatch.setattr(main, "AUDIO_DIR", str(tmp_path))
    assert app.client.get("/audio/rec1").status_code == 200
    assert app.client.post("/api/recordings/rec1/archive", headers=app.csrf).status_code == 200
    assert app.client.post("/api/recordings/rec1/delete", headers=app.csrf).status_code == 200
    assert app.client.get("/audio/rec1").status_code == 404


def test_archive_mutations_require_auth_csrf_and_fail_closed_on_outage(app, monkeypatch):
    anonymous = TestClient(main.app)
    try:
        assert anonymous.post("/api/recordings/rec1/archive").status_code == 401
    finally: anonymous.close()
    assert app.client.post("/api/recordings/rec1/archive").status_code == 403
    monkeypatch.setenv("RECORDINGS_CONTROL_SOCKET", str(Path(app.archive_path).with_suffix(".missing.sock")))
    assert app.client.post("/api/recordings/rec1/archive", headers=app.csrf).status_code == 503


def test_search_excludes_archived_records(app):
    assert app.client.get("/api/search?q=Привет").json()
    assert app.client.post("/api/recordings/rec1/archive", headers=app.csrf).status_code == 200
    assert app.client.get("/api/search?q=Привет").json() == []
