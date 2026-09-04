import json
import sqlite3
from fastapi.testclient import TestClient
from app import main


def database(path, variant=False, job=None):
    conn=sqlite3.connect(path)
    conn.executescript("""CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,start_at TEXT,created_at TEXT,duration_ms INTEGER,lang TEXT,asr_engine TEXT,asr_transcript TEXT,plaud_transcript TEXT,summary TEXT,summary_json TEXT,audio_path TEXT,archived_local_at TEXT,recording_number TEXT); CREATE TABLE recording_summary_variants(recording_id TEXT,language TEXT,summary TEXT,summary_json TEXT,updated_at TEXT,PRIMARY KEY(recording_id,language)); CREATE TABLE pipeline_jobs(seq INTEGER PRIMARY KEY,recording_id TEXT,stage TEXT,state TEXT,attempts INTEGER,last_error TEXT,enqueued_epoch INTEGER,available_epoch INTEGER,claim_epoch INTEGER,claim_owner TEXT,progress_epoch INTEGER,force_local INTEGER DEFAULT 0,force_replace INTEGER DEFAULT 0,revival_attempts INTEGER DEFAULT 0,tool_attempts INTEGER DEFAULT 0,catalog_revision INTEGER,updated_at TEXT);""")
    conn.execute("INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("r1","Russian title","2026-01-01","2026-01-01",1,"ru","local","НЕ МЕНЯТЬ","","# Русский",json.dumps({"title":"RU","overview":"Русский итог","action_items":["Отправить смету"]}),None,None,"N-0001"))
    if variant: conn.execute("INSERT INTO recording_summary_variants VALUES(?,?,?,?,?)", ("r1","en","# English",json.dumps({"title":"EN","overview":"English result","action_items":["Send the proposal"]}),"now"))
    if job: conn.execute("INSERT INTO pipeline_jobs(seq,recording_id,stage,state,attempts,enqueued_epoch,available_epoch) VALUES(1,'r1','summary_en',?,1,1,1)",(job,))
    conn.commit(); conn.close()

def client(path, monkeypatch):
    def open_db():
        conn=sqlite3.connect(path); conn.row_factory=sqlite3.Row; return conn
    monkeypatch.setattr(main,"SECRET","secret")
    monkeypatch.setattr(main,"db",open_db)
    c=TestClient(main.app); c.cookies.set(main.COOKIE_NAME,main.expected_token()); return c

def test_ready_english_variant_is_selected_without_changing_transcript_or_russian(tmp_path, monkeypatch):
    path=tmp_path/'a.db'; database(path,variant=True); c=client(path,monkeypatch)
    en=c.get('/api/recordings/r1',params={'lang':'en'}).json(); ru=c.get('/api/recordings/r1').json()
    assert en['summary']=='# English' and en['summary_data']['overview']=='English result'
    assert en['title']=='EN' and ru['title']=='Russian title'
    assert en['summary_language']=='en' and en['summary_state']['state']=='ready'
    assert en['transcript']=='НЕ МЕНЯТЬ' == ru['transcript']
    assert ru['summary']=='# Русский' and ru['summary_data']['overview']=='Русский итог'
    assert ru['summary_card_data']['tasks'][0]['text']=='Отправить смету'
    assert en['summary_card_data']['tasks'][0]['text']=='Send the proposal'
    assert en['summary_card_data']['tasks'][0]['id']==ru['summary_card_data']['tasks'][0]['id']

def test_first_english_access_queues_bounded_connector_job(tmp_path, monkeypatch):
    path=tmp_path/'a.db'; database(path); c=client(path,monkeypatch); calls=[]
    class FakeClient:
        def request(self, action, rec_id, **kw): calls.append((action,rec_id)); return {'state':'queued'}
    monkeypatch.setattr(main.control_client,'ControlClient',FakeClient)
    data=c.get('/api/recordings/r1',params={'lang':'en'}).json()
    assert calls==[('summary_en','r1')]
    assert data['summary'] is None and data['summary_state']['state']=='queued'

def test_failed_english_job_is_visible_and_retry_requires_csrf(tmp_path, monkeypatch):
    path=tmp_path/'a.db'; database(path,job='failed'); c=client(path,monkeypatch); calls=[]
    class FakeClient:
        def request(self, action, rec_id, **kw): calls.append((action,rec_id)); return {'state':'queued'}
    monkeypatch.setattr(main.control_client,'ControlClient',FakeClient)
    data=c.get('/api/recordings/r1',params={'lang':'en'}).json()
    assert data['summary_state']=={'language':'en','state':'failed','retryable':True}
    assert calls==[]
    assert c.post('/api/recordings/r1/summary/en/retry').status_code==403
    retried=c.post('/api/recordings/r1/summary/en/retry',headers={'X-CSRF-Token':main.csrf_token()}).json()
    assert retried['state']=='queued' and calls==[('summary_en','r1')]

def test_english_summary_api_requires_authentication(tmp_path, monkeypatch):
    path=tmp_path/'a.db'; database(path,variant=True); client(path,monkeypatch)
    assert TestClient(main.app).get('/api/recordings/r1',params={'lang':'en'}).status_code==401


def test_first_japanese_access_queues_selected_language(tmp_path, monkeypatch):
    path=tmp_path/'a.db'; database(path); c=client(path,monkeypatch); calls=[]
    class FakeClient:
        def request(self, action, rec_id, **kw): calls.append((action,rec_id,kw)); return {'state':'queued'}
    monkeypatch.setattr(main.control_client,'ControlClient',FakeClient)
    data=c.get('/api/recordings/r1',params={'lang':'ja'}).json()
    assert calls==[('summary_language','r1',{'language':'ja'})]
    assert data['summary_language']=='ja' and data['summary_state']['state']=='queued'


def test_invalid_summary_language_is_rejected(tmp_path, monkeypatch):
    path=tmp_path/'a.db'; database(path); c=client(path,monkeypatch)
    assert c.get('/api/recordings/r1',params={'lang':'../../etc/passwd'}).status_code==400
