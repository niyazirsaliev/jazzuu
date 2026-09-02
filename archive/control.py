"""Connector-owned, authenticated Unix-socket control boundary.

The viewer is only a human-authenticated client.  This module is run by the
connector beside the canonical archive and is the sole writer for user-requested
pipeline intent and speaker aliases.  It deliberately exposes no HTTP port.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import threading
from pathlib import Path

import pipeline
import people

MAX_REQUEST_BYTES = 12288
MAX_RECORDING_ID = 64
MAX_ALIASES = 24
MAX_ALIAS_LEN = 60
MAX_COMMENT_LEN = 2000
MAX_COMMENTS_PER_RECORDING = 500
MAX_LABEL_NAME = 40
_RECORDING_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SPEAKER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_TASK_RE = re.compile(r"[a-f0-9]{24}\Z")
_FORBIDDEN_NAME = re.compile(r"[\x00-\x1f\x7f<>&\"\\`]")
ACTIONS = frozenset(("rename", "retranscribe", "regenerate", "diarize",
                      "add_comment", "set_task_completed", "set_label",
                      "archive", "restore", "delete", "source_poll", "summary_en"))


class ControlUnavailable(RuntimeError):
    """The private control service is absent or cannot be safely used."""


class ControlRejected(RuntimeError):
    """A syntactically valid request violates controlled archive state."""


def _token(path):
    """Read a mounted high-entropy token file, fail closed on unsafe shape."""
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise ControlUnavailable("credential unavailable")
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except (OSError, UnicodeError) as exc:
        raise ControlUnavailable("credential unavailable") from exc
    if len(value) < 32 or len(value) > 512 or any(ch.isspace() for ch in value):
        raise ControlUnavailable("credential unavailable")
    return value


def _clean_aliases(value):
    if not isinstance(value, dict) or len(value) > MAX_ALIASES:
        raise ControlRejected("bad aliases")
    cleaned = {}
    for sid, name in value.items():
        if not isinstance(sid, str) or not _SPEAKER_RE.fullmatch(sid):
            raise ControlRejected("bad aliases")
        if name is None:
            cleaned[sid] = None
            continue
        if not isinstance(name, str):
            raise ControlRejected("bad aliases")
        name = re.sub(r"[ \u00a0]+", " ", name).strip(" \u00a0")
        if not name:
            cleaned[sid] = None
        elif len(name) > MAX_ALIAS_LEN or _FORBIDDEN_NAME.search(name):
            raise ControlRejected("bad aliases")
        else:
            cleaned[sid] = name
    return cleaned


def _speaker_id(label):
    normalized = re.sub(r"\s+", " ", str(label or "")).strip().casefold()
    match = re.fullmatch(r"(?:speaker|спикер|говорящий|голос|spk|s)[\s_:#.\-]*(\d{1,3})", normalized)
    if match:
        return "speaker-" + str(int(match.group(1)))
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:24].strip("-")
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}" if slug else f"spk-{digest}"


def _known_labels(conn, recording_id):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
    plaud = "plaud_segments_json" if "plaud_segments_json" in columns else "NULL"
    asr = "asr_meta_json" if "asr_meta_json" in columns else "NULL"
    row = conn.execute(f"SELECT {plaud}, {asr} FROM recordings WHERE id=?", (recording_id,)).fetchone()
    if row is None:
        raise ControlRejected("not found")
    values = []
    for raw, nested in ((row[0], False), (row[1], True)):
        try:
            decoded = json.loads(raw or "")
            if nested:
                decoded = (decoded.get("diarization") or {}).get("segments", []) if isinstance(decoded, dict) else []
        except (TypeError, ValueError):
            decoded = []
        if isinstance(decoded, list):
            values.extend(decoded)
    known = {}
    for segment in values:
        if isinstance(segment, dict) and isinstance(segment.get("speaker"), str):
            label = segment["speaker"].strip()
            sid = _speaker_id(label)
            if sid:
                known[sid] = label
    return known


def ensure_schema(conn):
    pipeline.ensure_schema(conn)
    people.ensure_schema(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS speaker_aliases(
        recording_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
        display_name TEXT NOT NULL, source_label TEXT NOT NULL, updated_at TEXT,
        PRIMARY KEY(recording_id, speaker_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS recording_comments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recording_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS recording_comments_recording
        ON recording_comments(recording_id, id)''')
    conn.execute("CREATE TABLE IF NOT EXISTS recording_summary_variants(recording_id TEXT NOT NULL, language TEXT NOT NULL CHECK(language IN ('en')), summary TEXT NOT NULL, summary_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(recording_id, language))")
    conn.execute('''CREATE TABLE IF NOT EXISTS recording_task_states(
        recording_id TEXT NOT NULL, task_id TEXT NOT NULL,
        completed INTEGER NOT NULL CHECK(completed IN (0,1)), updated_at TEXT NOT NULL,
        PRIMARY KEY(recording_id, task_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS label_definitions(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    conn.executemany('''INSERT OR IGNORE INTO label_definitions(id,name,kind,created_at)
        VALUES(?,?,?,?)''', (
        ("personal", "Личное", "system", pipeline.stamp()),
        ("business", "Работа", "system", pipeline.stamp()),
    ))
    pipeline.ensure_label_catalog_schema(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS recording_labels(
        recording_id TEXT NOT NULL, label_id TEXT NOT NULL,
        auto_assigned INTEGER NOT NULL DEFAULT 0 CHECK(auto_assigned IN (0,1)),
        manual_override INTEGER CHECK(manual_override IN (0,1)),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(recording_id,label_id))''')
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
    if "archived_local_at" not in columns:
        conn.execute("ALTER TABLE recordings ADD COLUMN archived_local_at TEXT")
    conn.execute('''CREATE TABLE IF NOT EXISTS recording_tombstones(
        recording_id TEXT PRIMARY KEY, recording_number TEXT, deleted_at TEXT NOT NULL)''')
    conn.commit()


def _rename(conn, recording_id, aliases):
    known = _known_labels(conn, recording_id)
    if not known or any(sid not in known for sid in aliases):
        raise ControlRejected("unknown speaker")
    for sid, name in aliases.items():
        if name is None:
            conn.execute("DELETE FROM speaker_aliases WHERE recording_id=? AND speaker_id=?", (recording_id, sid))
        else:
            conn.execute('''INSERT INTO speaker_aliases(recording_id,speaker_id,display_name,source_label,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(recording_id,speaker_id) DO UPDATE SET
                display_name=excluded.display_name,source_label=excluded.source_label,updated_at=excluded.updated_at''',
                (recording_id, sid, name, known[sid], pipeline.stamp()))
    created = pipeline.request_regenerate(conn, recording_id, commit=False)
    return "queued" if created else "already_queued"


def _clean_comment(value):
    if not isinstance(value, str):
        raise ControlRejected("bad comment")
    value = value.strip()
    if not value or len(value) > MAX_COMMENT_LEN:
        raise ControlRejected("bad comment")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value) or "\x7f" in value:
        raise ControlRejected("bad comment")
    return value


def _clean_label_name(value):
    if not isinstance(value, str):
        raise ControlRejected("bad label")
    value = re.sub(r"[ \u00a0]+", " ", value).strip(" \u00a0")
    if not value or len(value) > MAX_LABEL_NAME or _FORBIDDEN_NAME.search(value):
        raise ControlRejected("bad label")
    return value


def create_label(conn, label_name):
    """Create one tenant-local catalogue item without assigning it."""
    ensure_schema(conn)
    name = _clean_label_name(label_name)
    existing = conn.execute("SELECT id,name FROM label_definitions WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    label_id = existing[0] if existing else "custom-" + hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:16]
    created = existing is None
    if created:
        conn.execute("INSERT INTO label_definitions(id,name,kind,created_at) VALUES(?,?,?,?)", (label_id, name, "custom", pipeline.stamp()))
        revision = pipeline.bump_label_catalog_revision(conn)
        admitted = pipeline.admit_label_reclassification(conn, limit=10)
    else: revision, admitted = pipeline.label_catalog_revision(conn), 0
    conn.commit()
    return {"ok": True, "label": {"id": label_id, "name": existing[1] if existing else name, "kind": "custom"}, "catalog_revision": revision, "classification_queued": created, "admitted": admitted}


def delete_label(conn, label_id):
    """Atomically remove a custom definition and every assignment/override."""
    ensure_schema(conn)
    if not isinstance(label_id, str) or not re.fullmatch(r"custom-[a-f0-9]{16}", label_id): raise ControlRejected("bad label")
    row = conn.execute("SELECT kind FROM label_definitions WHERE id=?", (label_id,)).fetchone()
    if row is None or row[0] != "custom": raise ControlRejected("bad label")
    conn.execute("DELETE FROM recording_labels WHERE label_id=?", (label_id,)); conn.execute("DELETE FROM label_definitions WHERE id=?", (label_id,))
    revision = pipeline.bump_label_catalog_revision(conn); admitted = pipeline.admit_label_reclassification(conn, limit=10); conn.commit()
    return {"ok": True, "label_id": label_id, "catalog_revision": revision, "classification_queued": True, "admitted": admitted}


def _archive_action(conn, recording_id, action):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
    number = "recording_number" if "recording_number" in columns else "NULL"
    row = conn.execute(f"SELECT archived_local_at,{number} FROM recordings WHERE id=?", (recording_id,)).fetchone()
    if row is None:
        raise ControlRejected("not found")
    archived = bool(row[0])
    if action == "archive":
        if archived:
            raise ControlRejected("wrong state")
        conn.execute("UPDATE recordings SET archived_local_at=? WHERE id=?", (pipeline.stamp(), recording_id))
        return {"ok": True, "state": "archived"}
    if action == "restore":
        if not archived:
            raise ControlRejected("wrong state")
        conn.execute("UPDATE recordings SET archived_local_at=NULL WHERE id=?", (recording_id,))
        return {"ok": True, "state": "active"}
    if not archived:
        raise ControlRejected("wrong state")
    conn.execute("INSERT OR IGNORE INTO recording_tombstones(recording_id,recording_number,deleted_at) VALUES(?,?,?)",
                 (recording_id, row[1], pipeline.stamp()))
    # Tombstone and all relational removal are one transaction. This boundary
    # has no source/MCP client and never calls PLAUD delete functionality.
    for table, key in (("speaker_aliases", "recording_id"), ("recording_comments", "recording_id"),
                       ("speaker_identity_assignments", "recording_id"),
                       ("speaker_identity_events", "recording_id"),
                       ("recording_task_states", "recording_id"), ("recording_labels", "recording_id"),
                       ("pipeline_jobs", "recording_id"), ("recording_summary_variants", "recording_id"), ("asr_attempts", "id"),
                       ("asr_segments", "id"), ("summary_attempts", "id"), ("plaud_reviews", "id"),
                       ("voiceprint_segment_scores", "recording_id")):
        try:
            conn.execute(f"DELETE FROM {table} WHERE {key}=?", (recording_id,))
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("DELETE FROM recordings_fts WHERE id=?", (recording_id,))
    except sqlite3.OperationalError:
        pass
    conn.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
    return {"ok": True, "state": "deleted"}


def apply(conn, action, recording_id, aliases=None, *, comment=None,
          task_id=None, completed=None, label_id=None, label_name=None,
          active=None):
    if action not in ACTIONS or not isinstance(recording_id, str) or not _RECORDING_RE.fullmatch(recording_id):
        raise ControlRejected("bad request")
    ensure_schema(conn)
    try:
        if conn.execute("SELECT 1 FROM recordings WHERE id=?", (recording_id,)).fetchone() is None:
            raise ControlRejected("not found")
        if action in {"archive", "restore", "delete"}:
            result = _archive_action(conn, recording_id, action)
            conn.commit()
            return result
        if action == "retranscribe":
            state = "queued" if pipeline.request_retranscribe(conn, recording_id, commit=False) else "already_queued"
        elif action == "regenerate":
            state = "queued" if pipeline.request_regenerate(conn, recording_id, commit=False) else "already_queued"
        elif action == "diarize":
            state = "queued" if pipeline.request_diarize(conn, recording_id, commit=False) else "already_queued"
        elif action == "summary_en":
            state = "queued" if pipeline.request_summary_language(conn, recording_id, "en", commit=False) else "already_queued"
        elif action == "add_comment":
            body = _clean_comment(comment)
            count = conn.execute(
                "SELECT COUNT(*) FROM recording_comments WHERE recording_id=?",
                (recording_id,),
            ).fetchone()[0]
            if count >= MAX_COMMENTS_PER_RECORDING:
                raise ControlRejected("too many comments")
            created_at = pipeline.stamp()
            cursor = conn.execute(
                "INSERT INTO recording_comments(recording_id,body,created_at) VALUES(?,?,?)",
                (recording_id, body, created_at),
            )
            conn.commit()
            return {"ok": True, "comment": {
                "id": cursor.lastrowid, "text": body, "created_at": created_at}}
        elif action == "set_task_completed":
            if not isinstance(task_id, str) or not _TASK_RE.fullmatch(task_id) or type(completed) is not bool:
                raise ControlRejected("bad task")
            updated_at = pipeline.stamp()
            conn.execute('''INSERT INTO recording_task_states(recording_id,task_id,completed,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(recording_id,task_id) DO UPDATE SET
                completed=excluded.completed,updated_at=excluded.updated_at''',
                (recording_id, task_id, int(completed), updated_at))
            conn.commit()
            return {"ok": True, "task": {"id": task_id, "completed": completed}}
        elif action == "set_label":
            if (not isinstance(label_id, str) or
                    conn.execute("SELECT 1 FROM label_definitions WHERE id=?", (label_id,)).fetchone() is None or
                    type(active) is not bool):
                raise ControlRejected("bad label")
            conn.execute('''INSERT INTO recording_labels(
                recording_id,label_id,auto_assigned,manual_override,updated_at)
                VALUES(?,?,0,?,?) ON CONFLICT(recording_id,label_id) DO UPDATE SET
                manual_override=excluded.manual_override,updated_at=excluded.updated_at''',
                (recording_id, label_id, int(active), pipeline.stamp()))
            conn.commit()
            return {"ok": True, "label": {"id": label_id, "active": active}}
        else:
            state = _rename(conn, recording_id, _clean_aliases(aliases))
        conn.commit()
        return {"ok": True, "state": state}
    except Exception:
        conn.rollback()
        raise


def _recv_frame(peer, limit=MAX_REQUEST_BYTES):
    """Read exactly one newline-delimited frame without accepting an overrun."""
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


class ControlServer:
    def __init__(self, db_path, socket_path, token_path, *, waker=None, source_poll=None, audio_dir=None, cache_dirs=()):
        self.db_path, self.socket_path, self.token_path = db_path, socket_path, token_path
        self.waker = waker or pipeline.Waker()
        self.source_poll = source_poll
        self.audio_dir = audio_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)), "audio")
        self.cache_dirs = tuple(cache_dirs)
        self._stop = threading.Event(); self._thread = None; self._listener = None

    def start(self):
        _token(self.token_path)
        directory = os.path.dirname(os.path.abspath(self.socket_path))
        if not os.path.isdir(directory) or os.stat(directory).st_mode & 0o002:
            raise ControlUnavailable("socket unavailable")
        try:
            try:
                st = os.lstat(self.socket_path)
                if not stat.S_ISSOCK(st.st_mode):
                    raise ControlUnavailable("socket unavailable")
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old_umask = os.umask(0o117)  # bind directly as 0660; no loose-mode window
            try:
                listener.bind(self.socket_path)
            finally:
                os.umask(old_umask)
            listener.listen(16); listener.settimeout(.1)
        except OSError as exc:
            try: listener.close()
            except (UnboundLocalError, OSError): pass
            raise ControlUnavailable("socket unavailable") from exc
        self._listener = listener; self._thread = threading.Thread(target=self._serve, daemon=True); self._thread.start()

    def stop(self):
        self._stop.set()
        if self._listener: self._listener.close()
        if self._thread: self._thread.join(1)
        try: os.unlink(self.socket_path)
        except FileNotFoundError: pass

    def _serve(self):
        while not self._stop.is_set():
            try: peer, _ = self._listener.accept()
            except (TimeoutError, OSError): continue
            with peer:
                peer.settimeout(1)
                try: payload = _recv_frame(peer)
                except OSError: continue
                response = self._handle(payload)
                try: peer.sendall(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                except OSError: pass

    def _handle(self, raw):
        try:
            if not raw or len(raw) > MAX_REQUEST_BYTES: raise ControlRejected("bad")
            request = json.loads(raw.decode("utf-8"))
            allowed = {"token", "action", "recording_id", "aliases", "comment", "task_id", "completed",
                       "label_id", "label_name", "active", "display_name", "is_self",
                       "consent_status", "person_id", "speaker_id"}
            if not isinstance(request, dict) or set(request) - allowed: raise ControlRejected("bad")
            presented, expected = request.get("token"), _token(self.token_path)
            if not isinstance(presented, str) or not hmac.compare_digest(presented, expected): return {"ok": False, "error": "unauthorized"}
            if request.get("action") == "source_poll":
                if set(request) != {"token", "action"} or self.source_poll is None:
                    raise ControlRejected("bad")
                return {"ok": True, "state": self.source_poll()}
            if request.get("action") in {"create_label", "delete_label"}:
                needed = {"token", "action", "label_name"} if request["action"] == "create_label" else {"token", "action", "label_id"}
                if set(request) != needed: raise ControlRejected("bad")
                conn = sqlite3.connect(self.db_path, timeout=60)
                try: return create_label(conn, request["label_name"]) if request["action"] == "create_label" else delete_label(conn, request["label_id"])
                finally: conn.close()
            if request.get("action") in {"list_people", "create_person", "update_person",
                                         "get_identities", "assign_identity", "set_unknown", "undo_identity"}:
                return self._people(request)
            conn = sqlite3.connect(self.db_path, timeout=5)
            try: outcome = apply(
                conn, request.get("action"), request.get("recording_id"),
                request.get("aliases"), comment=request.get("comment"),
                task_id=request.get("task_id"), completed=request.get("completed"),
                label_id=request.get("label_id"), label_name=request.get("label_name"),
                active=request.get("active"))
            finally: conn.close()
            if outcome.get("state") == "deleted":
                self._cleanup_local(request.get("recording_id"))
            if request.get("action") in {"retranscribe", "regenerate", "rename", "diarize"}:
                self.waker.wake()
            return outcome
        except ControlRejected: return {"ok": False, "error": "bad_request"}
        except (ControlUnavailable, OSError, sqlite3.Error, UnicodeError, ValueError): return {"ok": False, "error": "unavailable"}

    def _people(self, request):
        action = request["action"]
        fields = set(request) - {"token", "action"}
        exact = {
            "list_people": set(),
            "create_person": {"display_name"},
            "update_person": {"person_id", "display_name", "active"},
            "get_identities": {"recording_id"},
            "assign_identity": {"recording_id", "speaker_id", "person_id"},
            "set_unknown": {"recording_id", "speaker_id"},
            "undo_identity": {"recording_id", "speaker_id"},
        }
        if action == "create_person" and fields in ({"display_name"}, {"display_name", "is_self", "consent_status"}):
            pass
        elif action == "update_person" and fields in ({"person_id", "display_name"}, {"person_id", "active"}, {"person_id", "display_name", "active"}):
            pass
        elif fields != exact[action]:
            raise ControlRejected("bad")
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            if action == "list_people":
                return {"ok": True, "people": people.list_people(conn)}
            if action == "create_person":
                person = people.create(conn, request["display_name"],
                    is_self=request.get("is_self", False),
                    consent_status=request.get("consent_status", "not_enrolled"))
                return {"ok": True, "person": person}
            if action == "update_person":
                person = people.update(conn, request["person_id"],
                    display_name=request.get("display_name"), active=request.get("active"))
                return {"ok": True, "person": person}
            if action == "get_identities":
                return {"ok": True, "assignments": people.assignments(conn, request["recording_id"])}
            if action == "assign_identity":
                value = people.assign(conn, request["recording_id"], request["speaker_id"], request["person_id"])
            elif action == "set_unknown":
                value = people.set_unknown(conn, request["recording_id"], request["speaker_id"])
            else:
                value = people.undo(conn, request["recording_id"], request["speaker_id"])
            return {"ok": True, "assignment": value}
        except people.IdentityRejected as exc:
            raise ControlRejected("bad identity") from exc
        finally:
            conn.close()

    def _cleanup_local(self, recording_id):
        """Post-commit only; constructed safe IDs prevent arbitrary deletion."""
        if not _RECORDING_RE.fullmatch(recording_id or ""):
            return
        for directory, suffixes in ((self.audio_dir, (".mp3",)), *[(d, (".png", ".json", ".txt")) for d in self.cache_dirs]):
            try:
                root = Path(directory).resolve()
                paths = []
                for suffix in suffixes:
                    paths.extend((root / f"{recording_id}{suffix}", *root.glob(f"{recording_id}.*{suffix}")))
                for path in paths:
                    if path.is_file() and path.resolve().parent == root:
                        path.unlink()
            except OSError:
                pass


class ControlClient:
    def __init__(self, socket_path, token_path, timeout=1): self.socket_path, self.token_path, self.timeout = socket_path, token_path, timeout
    def create_label(self, label_name): return self._catalogue("create_label", label_name=label_name)
    def delete_label(self, label_id): return self._catalogue("delete_label", label_id=label_id)
    def people(self, action, **fields): return self._request({"token": _token(self.token_path), "action": action, **fields})
    def _catalogue(self, action, **fields):
        request = {"token": _token(self.token_path), "action": action, **fields}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(max(self.timeout, 65)); peer.connect(self.socket_path); peer.sendall(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()+b"\n"); response=json.loads(_recv_frame(peer).decode())
        except (OSError, ValueError, UnicodeError) as exc: raise ControlUnavailable("control unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"): raise ControlRejected("rejected")
        return response

    def request(self, action, recording_id, aliases=None, *, comment=None,
                task_id=None, completed=None, label_id=None, label_name=None,
                active=None):
        token = _token(self.token_path)
        request = {"token": token, "action": action, "recording_id": recording_id}
        if aliases is not None: request["aliases"] = aliases
        if comment is not None: request["comment"] = comment
        if task_id is not None: request["task_id"] = task_id
        if completed is not None: request["completed"] = completed
        if label_id is not None: request["label_id"] = label_id
        if label_name is not None: request["label_name"] = label_name
        if active is not None: request["active"] = active
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(self.timeout); peer.connect(self.socket_path); peer.sendall(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                raw = _recv_frame(peer)
            response = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ControlUnavailable("control unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            if response.get("error") == "bad_request": raise ControlRejected("request rejected")
            raise ControlUnavailable("control unavailable")
        return {key: value for key, value in response.items() if key != "ok"}

    def request_source_poll(self):
        return self._request({"token": _token(self.token_path), "action": "source_poll"})

    def _request(self, request):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(self.timeout); peer.connect(self.socket_path); peer.sendall(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                raw = _recv_frame(peer)
            response = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ControlUnavailable("control unavailable") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            if response.get("error") == "bad_request": raise ControlRejected("request rejected")
            raise ControlUnavailable("control unavailable")
        return {key: value for key, value in response.items() if key != "ok"}
