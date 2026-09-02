import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "viewer"))
from app import control_client  # noqa: E402


def test_catalogue_mutation_waits_for_connector_database_timeout(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("a" * 48)
    os.chmod(token_path, 0o600)
    socket_path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)

    def serve():
        peer, _ = listener.accept()
        with peer:
            assert json.loads(peer.recv(4096).decode())["action"] == "create_label"
            # Connector jobs can hold SQLite's writer lock longer than the
            # control server's former five-second database timeout.
            time.sleep(5.2)
            peer.sendall(json.dumps({
                "ok": True,
                "label": {"id": "custom-abc", "name": "Клиенты", "kind": "custom"},
            }).encode() + b"\n")
        listener.close()

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        result = control_client.ControlClient(str(socket_path), str(token_path)).create_label("Клиенты")
    finally:
        worker.join(3)
        listener.close()
    assert result["label"]["name"] == "Клиенты"
