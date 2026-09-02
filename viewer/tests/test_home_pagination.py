import sqlite3
from fastapi.testclient import TestClient
from app import main


def _archive(path, count=45):
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,start_at TEXT,created_at TEXT,duration_ms INTEGER,lang TEXT,summary TEXT,summary_json TEXT,asr_engine TEXT,asr_transcript TEXT,plaud_transcript TEXT,archived_local_at TEXT,semantic_title TEXT,recording_number TEXT);""")
    for n in range(count):
        conn.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"r{n:03d}", f"R{n}", "2026-08-19 12:00:00" if n >= 40 else f"2026-08-{1+n//5:02d} 10:00:00", f"2026-08-{1+n//5:02d} 10:00:00", 1000, "ru", f"S{n}", "{}", "local", "T", "", None, None, f"N-{n:04d}"))
    conn.commit(); conn.close()


def _client(path, monkeypatch):
    def open_db():
        conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row; return conn
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "db", open_db)
    client = TestClient(main.app); client.cookies.set(main.COOKIE_NAME, main.expected_token()); return client


def test_cursor_pages_are_20_stable_and_ignore_concurrent_newer_insert(tmp_path, monkeypatch):
    path = tmp_path / "archive.db"; _archive(path); client = _client(path, monkeypatch)
    first = client.get("/api/recordings", params={"page": "cursor"}).json()
    assert len(first["items"]) == 20 and first["next_cursor"]
    first_ids = [row["id"] for row in first["items"]]
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("newer", "New", "2026-08-20 00:00:00", "2026-08-20 00:00:00", 1, "ru", "S", "{}", "local", "T", "", None, None, "N-9999"))
    conn.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("older-new", "Old backfill", "2026-01-01 00:00:00", "2026-01-01 00:00:00", 1, "ru", "S", "{}", "local", "T", "", None, None, "N-9998"))
    conn.commit(); conn.close()
    second = client.get("/api/recordings", params={"page": "cursor", "cursor": first["next_cursor"]}).json()
    second_ids = [row["id"] for row in second["items"]]
    assert len(second_ids) == 20 and not set(first_ids) & set(second_ids) and "newer" not in second_ids and "older-new" not in second_ids
    third = client.get("/api/recordings", params={"page": "cursor", "cursor": second["next_cursor"]}).json()
    all_ids = first_ids + second_ids + [row["id"] for row in third["items"]]
    assert len(all_ids) == 45 and len(set(all_ids)) == 45


def test_legacy_limit_offset_shape_remains_a_list(tmp_path, monkeypatch):
    path = tmp_path / "archive.db"; _archive(path, 3)
    data = _client(path, monkeypatch).get("/api/recordings", params={"limit": 2, "offset": 1}).json()
    assert isinstance(data, list) and len(data) == 2


def test_cursor_endpoint_requires_authentication():
    assert TestClient(main.app).get("/api/recordings", params={"page": "cursor"}).status_code == 401


def test_english_page_uses_variants_and_queues_only_visible_missing_rows(tmp_path, monkeypatch):
    path=tmp_path/'archive.db'; _archive(path,25)
    conn=sqlite3.connect(path); conn.execute("CREATE TABLE recording_summary_variants(recording_id TEXT,language TEXT,summary TEXT,summary_json TEXT,updated_at TEXT,PRIMARY KEY(recording_id,language))"); conn.execute("INSERT INTO recording_summary_variants VALUES('r024','en','# English',?, 'now')", ('{"title":"English 24"}',)); conn.commit(); conn.close()
    calls=[]
    class FakeClient:
        def request(self,action,rec_id,**kw): calls.append((action,rec_id)); return {'state':'queued'}
    monkeypatch.setattr(main.control_client,'ControlClient',FakeClient)
    c=_client(path,monkeypatch); data=c.get('/api/recordings',params={'page':'cursor','lang':'en'}).json()
    assert data['items'][0]['title']=='English 24' and data['items'][0]['summary']=='English'
    assert ('summary_en','r024') not in calls and len(calls)==19
    assert all(item['summary_language']=='en' for item in data['items'])
