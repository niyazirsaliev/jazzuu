"""Narrow viewer client for the connector's private authenticated UDS service."""
import json
import os
import socket
import stat


class ControlUnavailable(RuntimeError): pass
class ControlRejected(RuntimeError): pass

# Import transcodes the whole upload, unlike the sub-second control calls.
IMPORT_TIMEOUT = 900


def _token(path):
    try:
        mode = os.stat(path).st_mode
        if not stat.S_ISREG(mode) or mode & 0o077: raise OSError("unsafe")
        value = open(path, encoding="utf-8").read().strip()
    except (OSError, UnicodeError) as exc:
        raise ControlUnavailable("unavailable") from exc
    if len(value) < 32 or len(value) > 512 or any(c.isspace() for c in value):
        raise ControlUnavailable("unavailable")
    return value


def _recv_frame(peer, limit=12288):
    chunks = []
    size = 0
    while True:
        chunk = peer.recv(min(1024, limit + 1 - size))
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        frame = b''.join(chunks)
        newline = frame.find(b'\n')
        if newline >= 0:
            return frame[:newline]
        if size > limit:
            return frame


class ControlClient:
    def __init__(self, socket_path=None, token_path=None):
        self.socket_path = socket_path or os.environ.get("RECORDINGS_CONTROL_SOCKET", "/run/recordings/control.sock")
        self.token_path = token_path or os.environ.get("RECORDINGS_CONTROL_TOKEN_FILE", "/creds/recordings-control-token")

    def request(self, action, recording_id, aliases=None, *, comment=None,
                task_id=None, completed=None, label_id=None, label_name=None,
                active=None, language=None):
        payload = {"token": _token(self.token_path), "action": action, "recording_id": recording_id}
        if aliases is not None: payload["aliases"] = aliases
        if comment is not None: payload["comment"] = comment
        if task_id is not None: payload["task_id"] = task_id
        if completed is not None: payload["completed"] = completed
        if label_id is not None: payload["label_id"] = label_id
        if label_name is not None: payload["label_name"] = label_name
        if active is not None: payload["active"] = active
        if language is not None: payload["language"] = language
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(1); peer.connect(self.socket_path)
                peer.sendall(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                response = json.loads(_recv_frame(peer).decode())
        except (OSError, ValueError, UnicodeError) as exc:
            raise ControlUnavailable("unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            if isinstance(response, dict) and response.get("error") == "bad_request": raise ControlRejected("rejected")
            raise ControlUnavailable("unavailable")
        return {key: value for key, value in response.items() if key != "ok"}

    def create_label(self, name):
        return self._catalogue("create_label", label_name=name)

    def delete_label(self, label_id):
        return self._catalogue("delete_label", label_id=label_id)

    def people(self, action, **fields):
        return self._global({"token": _token(self.token_path), "action": action, **fields})

    def _global(self, payload):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(1); peer.connect(self.socket_path)
                peer.sendall(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                response = json.loads(_recv_frame(peer).decode())
        except (OSError, ValueError, UnicodeError) as exc:
            raise ControlUnavailable("unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            if isinstance(response, dict) and response.get("error") == "bad_request":
                raise ControlRejected("rejected")
            raise ControlUnavailable("unavailable")
        return {key: value for key, value in response.items() if key != "ok"}

    def _catalogue(self, action, **fields):
        payload = {"token": _token(self.token_path), "action": action, **fields}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                # Pipeline handlers can hold SQLite's writer lock while a
                # derived stage runs. Match the connector's normal 60-second
                # database window, plus room to return the response.
                peer.settimeout(65)
                peer.connect(self.socket_path)
                peer.sendall(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                response = json.loads(_recv_frame(peer).decode())
        except (OSError, ValueError, UnicodeError) as exc:
            raise ControlUnavailable("unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            if isinstance(response, dict) and response.get("error") == "bad_request":
                raise ControlRejected("rejected")
            raise ControlUnavailable("unavailable")
        return {key: value for key, value in response.items() if key != "ok"}

    def request_import_upload(self, upload_name, timeout=None):
        """Ask the connector to import a file already staged in the inbox.

        Import runs ffmpeg over the whole file, so this waits far longer than
        the ordinary one-second control calls — a phone recording can be hours.
        """
        payload = {"token": _token(self.token_path), "action": "import_upload",
                   "upload_name": upload_name}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(timeout or IMPORT_TIMEOUT)
                peer.connect(self.socket_path)
                peer.sendall(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                response = json.loads(_recv_frame(peer).decode())
        except (OSError, ValueError, UnicodeError) as exc:
            raise ControlUnavailable("unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            reason = response.get("error") if isinstance(response, dict) else None
            if reason in {"not_audio", "too_large", "not_found", "bad_request"}:
                raise ControlRejected(reason)
            raise ControlUnavailable(reason or "unavailable")
        return {key: value for key, value in response.items() if key != "ok"}

    def request_source_poll(self):
        payload = {"token": _token(self.token_path), "action": "source_poll"}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(1); peer.connect(self.socket_path)
                peer.sendall(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                response = json.loads(_recv_frame(peer).decode())
        except (OSError, ValueError, UnicodeError) as exc:
            raise ControlUnavailable("unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            if isinstance(response, dict) and response.get("error") == "bad_request": raise ControlRejected("rejected")
            raise ControlUnavailable("unavailable")
        return {key: value for key, value in response.items() if key != "ok"}
