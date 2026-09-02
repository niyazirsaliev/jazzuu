from pathlib import Path

from fastapi.testclient import TestClient

from app import main


def authed_client(audio_dir: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "AUDIO_DIR", str(audio_dir))
    monkeypatch.setattr(main, "_recording_row", lambda rec_id: {"id": rec_id})
    client = TestClient(main.app)
    client.cookies.set(main.COOKIE_NAME, main.expected_token())
    return client


def test_download_mode_returns_audio_as_attachment(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "rec_123.mp3").write_bytes(b"audio bytes")
    client = authed_client(audio_dir, monkeypatch)

    response = client.get("/audio/rec_123?download=1")

    assert response.status_code == 200
    assert response.content == b"audio bytes"
    assert response.headers["content-disposition"] == (
        'attachment; filename="rec_123.mp3"; filename*=UTF-8\'\'rec_123.mp3'
    )
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_download_requires_tenant_cookie(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "rec_123.mp3").write_bytes(b"private")
    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "AUDIO_DIR", str(audio_dir))

    response = TestClient(main.app).get("/audio/rec_123?download=1")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert "content-disposition" not in response.headers

    invalid = TestClient(main.app)
    invalid.cookies.set(main.COOKIE_NAME, "wrong-tenant-cookie")
    assert invalid.get("/audio/rec_123?download=1").status_code == 401


def test_player_mode_remains_inline_and_range_download_keeps_attachment(
    tmp_path, monkeypatch
):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "rec_123.mp3").write_bytes(b"0123456789")
    client = authed_client(audio_dir, monkeypatch)

    inline = client.get("/audio/rec_123")
    partial = client.get(
        "/audio/rec_123?download=1", headers={"Range": "bytes=2-5"}
    )

    assert "content-disposition" not in inline.headers
    assert inline.headers["cache-control"] == "private, no-store"
    assert inline.headers["x-content-type-options"] == "nosniff"
    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-disposition"] == (
        'attachment; filename="rec_123.mp3"; filename*=UTF-8\'\'rec_123.mp3'
    )
    assert partial.headers["cache-control"] == "private, no-store"


def test_download_supports_suffix_range(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "rec_123.mp3").write_bytes(b"0123456789")
    client = authed_client(audio_dir, monkeypatch)

    response = client.get(
        "/audio/rec_123?download=1", headers={"Range": "bytes=-4"}
    )

    assert response.status_code == 206
    assert response.content == b"6789"
    assert response.headers["content-range"] == "bytes 6-9/10"


def test_malformed_download_ranges_are_bounded_and_private(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "rec_123.mp3").write_bytes(b"0123456789")
    client = authed_client(audio_dir, monkeypatch)

    for value in (
        "bytes=", "bytes=0-1,3-4", "bytes=3-2", "bytes=999-", "garbage",
        "bytes=999999999999999999999-",
    ):
        response = client.get(
            "/audio/rec_123?download=1", headers={"Range": value}
        )
        assert response.status_code == 416, value
        assert response.headers["content-range"] == "bytes */10"
        assert response.headers["cache-control"] == "private, no-store"


def test_range_on_empty_audio_is_private_416(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "rec_123.mp3").write_bytes(b"")
    client = authed_client(audio_dir, monkeypatch)

    response = client.get(
        "/audio/rec_123?download=1", headers={"Range": "bytes=0-"}
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */0"
    assert response.headers["cache-control"] == "private, no-store"


def test_filename_is_human_readable_utf8_and_path_safe():
    filename = main._safe_audio_download_name(
        '  План: квартал/отчёт? "финал".mp3  ',
        "2026-08-10 09:30:00",
        "rec_123",
    )

    assert filename == "2026-08-10 - План_ квартал_отчёт_ _финал.mp3"
    assert "/" not in filename
    assert "\\" not in filename
    header = main._attachment_header(filename, "rec_123")
    assert header.startswith('attachment; filename="rec_123.mp3"; filename*=UTF-8\'\'')
    assert "%D0%9F%D0%BB%D0%B0%D0%BD" in header
