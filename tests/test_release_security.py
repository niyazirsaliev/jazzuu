import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_archive(tmp_path):
    token = tmp_path / "plaud-token"
    token.write_text("synthetic-token")
    env = {
        "TENANT_ID": "tenant-alpha",
        "TENANT_ARCHIVE_DIR": str(tmp_path),
        "PLAUD_MCP_TOKEN_FILE": str(token),
        "PLAUD_MCP_TENANT_URLS_JSON": json.dumps({"tenant-alpha": "https://mcp.example/mcp"}),
        "RECORDING_CODE_PREFIXES_JSON": json.dumps({"tenant-alpha": "A"}),
        "PLAUD_AUDIO_DOWNLOAD_HOSTS": "audio.example",
    }
    path = ROOT / "archive" / "archive_recording.py"
    spec = importlib.util.spec_from_file_location("archive_recording_security", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(sys.modules, {spec.name: module}):
        spec.loader.exec_module(module)
    return module


def test_audio_download_rejects_non_https_and_unlisted_hosts(tmp_path):
    archive = load_archive(tmp_path)
    source = tmp_path / "secret.txt"
    source.write_text("private")
    for url in (source.as_uri(), "https://127.0.0.1/audio.mp3", "https://other.example/audio.mp3"):
        with pytest.raises(ValueError):
            archive.download_audio(url, tmp_path / "audio" / "out.mp3")


def test_audio_download_streams_allowed_public_https_with_size_cap(tmp_path, monkeypatch):
    archive = load_archive(tmp_path)
    monkeypatch.setenv("PLAUD_AUDIO_DOWNLOAD_HOSTS", "audio.example")

    class Response(io.BytesIO):
        headers = {"Content-Length": "5"}
        def __enter__(self): return self
        def __exit__(self, *_args): self.close()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://audio.example/recording.mp3"
            assert timeout == archive.REQUEST_TIMEOUT_S
            return Response(b"audio")

    monkeypatch.setattr(archive.socket, "getaddrinfo", lambda *_a, **_k: [
        (archive.socket.AF_INET, archive.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(archive.urllib.request, "build_opener", lambda *_a: Opener())
    output = tmp_path / "audio" / "out.mp3"
    assert archive.download_audio("https://audio.example/recording.mp3", output) == output
    assert output.read_bytes() == b"audio"

    monkeypatch.setenv("PLAUD_AUDIO_MAX_BYTES", "4")
    with pytest.raises(ValueError, match="too large"):
        archive.download_audio("https://audio.example/recording.mp3", output)


def test_private_viewer_has_no_runtime_third_party_code():
    html = (ROOT / "viewer/app/static/index.html").read_text()
    assert "cdn.tailwindcss.com" not in html
    assert '<link rel="stylesheet" href="/app.css"' in html
    assert (ROOT / "viewer/app/static/app.css").stat().st_size > 1000


def test_connector_uses_immutable_debian_packages():
    dockerfile = (ROOT / "archive/Dockerfile").read_text()
    assert "snapshot.debian.org" in dockerfile
    assert "ffmpeg=7:5.1.8-0+deb12u1" in dockerfile
