"""Uploading a recording from the phone.

The viewer is the untrusted side of this: it may write only into the upload
inbox, and the connector re-checks everything before importing. These tests
cover the boundary, not the happy path alone.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))

import control  # noqa: E402
import local_import  # noqa: E402

# The control service rejects weak credentials: >=32 chars, no whitespace.
TOKEN = "t" * 40


class UploadNameTests(unittest.TestCase):
    """The staged name is viewer-supplied, so it is the first thing to distrust."""

    def setUp(self):
        self.server = control.ControlServer.__new__(control.ControlServer)
        self.server.upload_dir = "/inbox"
        self.server._upload_lock = threading.Lock()

    def test_a_traversing_name_is_rejected_before_any_filesystem_access(self):
        for hostile in ("../../etc/passwd", "a/b.upload", "..upload",
                        "/abs/path.upload", "x" * 31 + ".upload",
                        "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ.upload"):
            with self.assertRaises(control.ControlRejected, msg=hostile):
                self.server._import_upload(hostile)

    def test_a_non_string_name_is_rejected(self):
        for hostile in (None, 17, {"name": "x"}, ["x"]):
            with self.assertRaises(control.ControlRejected):
                self.server._import_upload(hostile)

    def test_uploads_are_refused_when_no_inbox_is_configured(self):
        self.server.upload_dir = None
        result = self.server._import_upload("a" * 32 + ".upload")
        self.assertEqual(result, {"ok": False, "error": "uploads_disabled"})

    def test_a_second_concurrent_import_is_rejected_as_busy(self):
        self.server._upload_lock = threading.Lock()
        self.server._upload_lock.acquire()
        try:
            result = self.server._import_upload("a" * 32 + ".upload")
        finally:
            self.server._upload_lock.release()
        self.assertEqual(result, {"ok": False, "error": "busy"})


class UploadContentTests(unittest.TestCase):
    """Content decides, not the extension — and a reject must not leave litter."""

    def setUp(self):
        import tempfile
        self.inbox = tempfile.mkdtemp()
        self.server = control.ControlServer.__new__(control.ControlServer)
        self.server.upload_dir = self.inbox
        self.server._upload_lock = threading.Lock()
        self.name = "b" * 32 + ".upload"
        self.staged = os.path.join(self.inbox, self.name)

    def test_a_file_that_is_not_audio_is_rejected_and_deleted(self):
        Path(self.staged).write_bytes(b"MZ\x90\x00 this is an executable")
        result = self.server._import_upload(self.name)
        self.assertEqual(result, {"ok": False, "error": "not_audio"})
        self.assertFalse(os.path.exists(self.staged), "rejected upload was left on disk")

    def test_an_oversized_file_is_rejected_and_deleted(self):
        Path(self.staged).write_bytes(b"\0")
        with mock.patch.object(control, "MAX_UPLOAD_BYTES", 0):
            result = self.server._import_upload(self.name)
        self.assertEqual(result, {"ok": False, "error": "too_large"})
        self.assertFalse(os.path.exists(self.staged))

    def test_a_missing_file_reports_not_found(self):
        result = self.server._import_upload(self.name)
        self.assertEqual(result, {"ok": False, "error": "not_found"})

    def test_a_symlink_is_consumed_without_touching_its_target(self):
        import tempfile
        target = Path(tempfile.mkdtemp()) / "outside.wav"
        target.write_bytes(b"private")
        os.symlink(target, self.staged)

        result = self.server._import_upload(self.name)

        self.assertEqual(result, {"ok": False, "error": "not_audio"})
        self.assertFalse(os.path.lexists(self.staged))
        self.assertEqual(target.read_bytes(), b"private")

    def test_a_real_audio_file_reaches_the_importer_and_is_consumed(self):
        Path(self.staged).write_bytes(b"fake audio bytes")
        calls = {}

        def fake_import(staged, upload_name, duration):
            calls.update(staged=staged, name=upload_name, duration=duration)
            return {"ok": True, "recording_number": "0281"}

        with mock.patch.object(control, "_upload_duration", return_value=12.5), \
             mock.patch.object(self.server, "_run_local_import", fake_import):
            result = self.server._import_upload(self.name)

        self.assertEqual(result, {"ok": True, "recording_number": "0281"})
        self.assertEqual(calls["name"], self.name)
        self.assertEqual(calls["duration"], 12.5)
        self.assertEqual(Path(calls["staged"]).parent.name, ".upload-processing")
        self.assertFalse(os.path.exists(self.staged), "imported upload was left on disk")

    def test_the_manifest_carries_every_field_local_import_verifies(self):
        """local_import re-checks size and digest, so a partial manifest fails.

        The first version of this shipped without them and the connector
        rejected every real upload; the unit tests missed it because they
        replaced _run_local_import wholesale.
        """
        import hashlib as _h
        payload = b"pretend audio payload"
        Path(self.staged).write_bytes(payload)
        seen = {}

        def fake_run(argv, **kwargs):
            with open(argv[-1], encoding="utf-8") as handle:
                seen.update(json.load(handle))
            return mock.Mock(returncode=0, stdout='{"recording_number": "0281"}', stderr="")

        with mock.patch.object(control, "_run_process_group", fake_run):
            self.server.waker = mock.Mock()
            self.server._run_local_import(self.staged, self.name, 12.5)

        self.assertEqual(seen["source_size"], len(payload))
        self.assertEqual(seen["source_sha256"], _h.sha256(payload).hexdigest())
        self.assertEqual(seen["source_duration_seconds"], 12.5)
        self.assertEqual(seen["staged_source"], self.staged)

    def test_the_manifest_satisfies_local_imports_own_field_reads(self):
        """Every key local_import reads must be present, not just the ones we recall.

        Fields were found missing one deploy at a time (source_size, then
        creation_time); this reads the importer's source instead of a list
        maintained by hand, so a new required key fails here first.
        """
        import re
        importer = (Path(__file__).resolve().parents[1]
                    / "archive" / "local_import.py").read_text()
        required = set(re.findall(r'item\["([a-z_]+)"\]', importer))

        Path(self.staged).write_bytes(b"payload")
        seen = {}

        def fake_run(argv, **kwargs):
            with open(argv[-1], encoding="utf-8") as handle:
                seen.update(json.load(handle))
            return mock.Mock(returncode=0, stdout='{"recording_number": "0281"}', stderr="")

        with mock.patch.object(control, "_run_process_group", fake_run):
            self.server.waker = mock.Mock()
            self.server._run_local_import(self.staged, self.name, 12.5)

        self.assertEqual(required - set(seen), set(),
                         f"manifest is missing keys local_import reads: {required - set(seen)}")

    def test_import_timeout_kills_the_complete_process_group(self):
        import tempfile
        import time

        root = Path(tempfile.mkdtemp())
        child_pid = root / "child.pid"
        script = root / "parent.py"
        script.write_text(
            "import pathlib,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
            "time.sleep(60)\n"
        )

        with self.assertRaises(subprocess.TimeoutExpired):
            control._run_process_group(
                [sys.executable, str(script), str(child_pid)], timeout=0.2)

        pid = int(child_pid.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("descendant process survived the import timeout")


class LocalImportTimeoutTests(unittest.TestCase):
    def test_transcode_timeout_removes_partial_output(self):
        import tempfile

        root = Path(tempfile.mkdtemp())
        source = root / "source.upload"
        final = root / "recording.mp3"
        source.write_bytes(b"audio")
        item = {
            "staged_source": str(source),
            "source_size": source.stat().st_size,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }

        def timeout(*args, **kwargs):
            Path(f"{final}.tmp").write_bytes(b"partial")
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

        with mock.patch.object(local_import.subprocess, "run", timeout), \
             self.assertRaises(subprocess.TimeoutExpired):
            local_import.normalize_audio(item, str(final))

        self.assertFalse(Path(f"{final}.tmp").exists())


class UploadRequestParsingTests(unittest.TestCase):
    """The wire request must survive _handle, not just _import_upload.

    Every earlier test called _import_upload directly and passed while the
    deployed service rejected each upload with bad_request: 'upload_name' was
    missing from the request-field allowlist, so the request died before the
    handler branch ever ran.
    """

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.token_path = os.path.join(self.dir, "token")
        Path(self.token_path).write_text(TOKEN)
        os.chmod(self.token_path, 0o600)
        self.server = control.ControlServer.__new__(control.ControlServer)
        self.server.token_path = self.token_path
        self.server.upload_dir = self.dir
        self.server.source_poll = None

    def test_an_upload_request_reaches_the_upload_handler(self):
        seen = {}

        def fake(upload_name):
            seen["name"] = upload_name
            return {"ok": True, "recording_number": "0281"}

        self.server._import_upload = fake
        name = "f" * 32 + ".upload"
        raw = json.dumps({"token": TOKEN, "action": "import_upload",
                          "upload_name": name}).encode()

        result = self.server._handle(raw)

        self.assertEqual(result, {"ok": True, "recording_number": "0281"})
        self.assertEqual(seen.get("name"), name)

    def test_an_upload_request_with_extra_fields_is_rejected(self):
        self.server._import_upload = lambda name: {"ok": True}
        raw = json.dumps({"token": TOKEN, "action": "import_upload",
                          "upload_name": "g" * 32 + ".upload",
                          "recording_id": "N-0001"}).encode()
        self.assertEqual(self.server._handle(raw), {"ok": False, "error": "bad_request"})

    def test_an_upload_request_with_a_bad_token_is_unauthorized(self):
        self.server._import_upload = lambda name: {"ok": True}
        raw = json.dumps({"token": "wrong", "action": "import_upload",
                          "upload_name": "h" * 32 + ".upload"}).encode()
        self.assertEqual(self.server._handle(raw), {"ok": False, "error": "unauthorized"})

    def test_a_long_import_does_not_block_other_control_requests(self):
        import importlib.util
        import threading
        import tempfile

        root = Path(tempfile.mkdtemp())
        token_path, socket_path = root / "token", root / "control.sock"
        token_path.write_text(TOKEN)
        os.chmod(token_path, 0o600)
        server = control.ControlServer(
            str(root / "archive.db"), str(socket_path), str(token_path),
            source_poll=lambda: {"accepted": True}, upload_dir=str(root / "uploads"))
        entered, release = threading.Event(), threading.Event()

        def slow_import(_name):
            entered.set()
            release.wait(3)
            return {"ok": True, "recording_number": "0281"}

        server._import_upload = slow_import
        spec = importlib.util.spec_from_file_location(
            "viewer_control_client", ROOT / "viewer" / "app" / "control_client.py")
        assert spec and spec.loader
        client_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client_module)
        client = client_module.ControlClient(str(socket_path), str(token_path))
        server.start()
        worker = threading.Thread(
            target=lambda: client.request_import_upload("a" * 32 + ".upload"))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(client.request_source_poll()["state"], {"accepted": True})
        finally:
            release.set()
            worker.join(2)
            server.stop()


class UploadProbeTests(unittest.TestCase):
    """_upload_duration must accept phone formats and refuse everything else."""

    def test_a_silent_wav_is_recognised_as_audio(self):
        import subprocess, tempfile
        wav = Path(tempfile.mkdtemp()) / "probe.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)],
                       check=True, capture_output=True)
        self.assertAlmostEqual(control._upload_duration(str(wav)), 1.0, places=1)

    def test_a_text_file_is_not_audio(self):
        import tempfile
        txt = Path(tempfile.mkdtemp()) / "notes.mp3"   # audio extension, text bytes
        txt.write_text("this is not audio at all")
        self.assertIsNone(control._upload_duration(str(txt)))

    def test_a_missing_path_is_not_audio(self):
        self.assertIsNone(control._upload_duration("/nonexistent/file.mp3"))


class UploadProvenanceTests(unittest.TestCase):
    def test_browser_upload_is_not_mislabeled_as_nextcloud(self):
        metadata = local_import.provenance_metadata({
            "source": "upload", "original_name": "staged.upload",
            "source_sha256": "0" * 64, "source_size": 1,
            "source_duration_seconds": 1.0,
        })
        self.assertEqual(metadata["source_kind"], "browser_upload")


if __name__ == "__main__":
    unittest.main()
