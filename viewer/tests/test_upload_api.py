"""The upload endpoint, exercised through the real streaming HTTP boundary."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "viewer"))
from app import main  # noqa: E402


def client(tmp_path, monkeypatch, *, control=None):
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path))
    if control is not None:
        monkeypatch.setattr(main.control_client, "ControlClient", control)
    c = TestClient(main.app)
    c.cookies.set(main.COOKIE_NAME, main.expected_token())
    return c


def csrf(c):
    return {"X-CSRF-Token": main.csrf_token(), "Content-Type": "application/octet-stream"}


class _Control:
    """Stands in for the connector; records what the viewer handed it."""
    seen = []

    def __init__(self, *a, **kw):
        pass

    def request_import_upload(self, upload_name, timeout=None):
        _Control.seen.append(upload_name)
        return {"ok": True, "recording_number": "0281"}


def test_a_streamed_upload_reaches_the_connector(tmp_path, monkeypatch):
    _Control.seen = []
    c = client(tmp_path, monkeypatch, control=_Control)

    r = c.post("/api/uploads", headers=csrf(c), content=b"pretend audio")

    assert r.status_code == 200, r.text
    assert r.json()["recording_number"] == "0281"
    assert len(_Control.seen) == 1
    assert _Control.seen[0].endswith(".upload")


def test_upload_requires_csrf(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch, control=_Control)
    assert c.post("/api/uploads", headers={"Content-Type": "application/octet-stream"}, content=b"x").status_code == 403


def test_upload_requires_a_session(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path))
    r = TestClient(main.app).post(
        "/api/uploads", headers={"X-CSRF-Token": main.csrf_token(),
                                 "Content-Type": "application/octet-stream"}, content=b"x")
    assert r.status_code == 401


def test_an_empty_body_is_refused(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch, control=_Control)
    r = c.post("/api/uploads", headers=csrf(c), content=b"")
    assert r.status_code == 400
    assert not list(tmp_path.iterdir()), "nothing should be staged"


def test_an_oversized_body_is_refused_and_leaves_nothing_staged(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 8)
    c = client(tmp_path, monkeypatch, control=_Control)

    r = c.post("/api/uploads", headers=csrf(c), content=b"x" * 64)

    assert r.status_code == 400
    assert not list(tmp_path.iterdir()), "rejected upload was left on disk"


def test_a_rejected_import_removes_the_staged_file(tmp_path, monkeypatch):
    class Rejects(_Control):
        def request_import_upload(self, upload_name, timeout=None):
            raise main.control_client.ControlRejected("not audio")

    c = client(tmp_path, monkeypatch, control=Rejects)
    r = c.post("/api/uploads", headers=csrf(c), content=b"not audio")

    assert r.status_code == 400
    assert not list(tmp_path.iterdir()), "staged file outlived a rejected import"


def test_uploads_are_unavailable_when_no_inbox_is_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "UPLOAD_DIR", "")
    c = TestClient(main.app)
    c.cookies.set(main.COOKIE_NAME, main.expected_token())

    r = c.post("/api/uploads", headers=csrf(c), content=b"x")

    assert r.status_code == 503
