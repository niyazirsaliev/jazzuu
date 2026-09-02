"""Tenant-local people directory and event-sourced manual cluster identity."""
from __future__ import annotations

import re
import secrets

import pipeline

MAX_NAME_LEN = 60
CONSENT = frozenset(("not_enrolled", "self_confirmed", "consented"))
_PERSON_RE = re.compile(r"p_[a-f0-9]{24}\Z")
_SPEAKER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f<>&\"\\`]")


class IdentityRejected(ValueError):
    pass


def ensure_schema(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS people(
        person_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
        is_self INTEGER NOT NULL DEFAULT 0 CHECK(is_self IN (0,1)),
        consent_status TEXT NOT NULL DEFAULT 'not_enrolled',
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE UNIQUE INDEX IF NOT EXISTS people_one_active_self
        ON people(is_self) WHERE is_self=1 AND active=1;
      CREATE TABLE IF NOT EXISTS speaker_identity_assignments(
        recording_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
        person_id TEXT, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(recording_id,speaker_id));
      CREATE TABLE IF NOT EXISTS speaker_identity_events(
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        recording_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
        action TEXT NOT NULL, before_person_id TEXT, after_person_id TEXT,
        undo_of_event_id INTEGER,
        created_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS speaker_identity_event_target
        ON speaker_identity_events(recording_id,speaker_id,event_id);
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(speaker_identity_events)")}
    if "undo_of_event_id" not in columns:
        conn.execute("ALTER TABLE speaker_identity_events ADD COLUMN undo_of_event_id INTEGER")


def _name(value):
    if not isinstance(value, str):
        raise IdentityRejected("bad name")
    value = re.sub(r"[ \u00a0]+", " ", value).strip(" \u00a0")
    if not value or len(value) > MAX_NAME_LEN or _FORBIDDEN.search(value):
        raise IdentityRejected("bad name")
    return value


def _person(row):
    return {"person_id": row[0], "display_name": row[1],
            "is_self": bool(row[2]), "consent_status": row[3],
            "active": bool(row[4]), "revision": row[5]}


def list_people(conn, include_inactive=False):
    ensure_schema(conn)
    rows = conn.execute("SELECT person_id,display_name,is_self,consent_status,active,revision FROM people "
                        + ("" if include_inactive else "WHERE active=1 ")
                        + "ORDER BY is_self DESC,display_name COLLATE NOCASE,person_id").fetchall()
    return [_person(row) for row in rows]


def create(conn, display_name, *, is_self=False, consent_status="not_enrolled"):
    ensure_schema(conn)
    if type(is_self) is not bool or consent_status not in CONSENT:
        raise IdentityRejected("bad person")
    if is_self and consent_status != "self_confirmed":
        raise IdentityRejected("self needs explicit confirmation")
    if not is_self and consent_status == "self_confirmed":
        raise IdentityRejected("bad consent")
    person_id, stamp = "p_" + secrets.token_hex(12), pipeline.stamp()
    try:
        conn.execute("INSERT INTO people VALUES(?,?,?,?,1,1,?,?)",
                     (person_id, _name(display_name), int(is_self), consent_status, stamp, stamp))
        conn.commit()
    except Exception:
        conn.rollback()
        raise IdentityRejected("person rejected") from None
    return get(conn, person_id)


def get(conn, person_id):
    ensure_schema(conn)
    row = conn.execute("SELECT person_id,display_name,is_self,consent_status,active,revision FROM people WHERE person_id=?",
                       (person_id,)).fetchone()
    if not row:
        raise IdentityRejected("unknown person")
    return _person(row)


def update(conn, person_id, *, display_name=None, active=None):
    person = get(conn, person_id)
    if display_name is None and active is None:
        raise IdentityRejected("no change")
    name = person["display_name"] if display_name is None else _name(display_name)
    enabled = person["active"] if active is None else active
    if type(enabled) is not bool:
        raise IdentityRejected("bad active")
    if person["is_self"] and not enabled:
        # Revocation is supported, but it removes current assignments below.
        pass
    conn.execute("UPDATE people SET display_name=?,active=?,revision=revision+1,updated_at=? WHERE person_id=?",
                 (name, int(enabled), pipeline.stamp(), person_id))
    if not enabled:
        conn.execute("UPDATE speaker_identity_assignments SET person_id=NULL,revision=revision+1,updated_at=? WHERE person_id=?",
                     (pipeline.stamp(), person_id))
    conn.commit()
    return get(conn, person_id)


def _known_speakers(conn, recording_id):
    import control
    return control._known_labels(conn, recording_id)


def _validate_target(conn, recording_id, speaker_id):
    if not isinstance(recording_id, str) or not isinstance(speaker_id, str) or not _SPEAKER_RE.fullmatch(speaker_id):
        raise IdentityRejected("bad target")
    if speaker_id not in _known_speakers(conn, recording_id):
        raise IdentityRejected("unknown speaker")


def _current(conn, recording_id, speaker_id):
    row = conn.execute("SELECT person_id,revision FROM speaker_identity_assignments WHERE recording_id=? AND speaker_id=?",
                       (recording_id, speaker_id)).fetchone()
    return (row[0], row[1]) if row else (None, 0)


def _set(conn, recording_id, speaker_id, person_id, action, *, before_override=None,
         undo_of_event_id=None):
    ensure_schema(conn); _validate_target(conn, recording_id, speaker_id)
    if person_id is not None:
        person = get(conn, person_id)
        if not person["active"]:
            raise IdentityRejected("inactive person")
    before, revision = _current(conn, recording_id, speaker_id)
    event_before = before if before_override is None else before_override
    conn.execute("""INSERT INTO speaker_identity_assignments(recording_id,speaker_id,person_id,revision,updated_at)
        VALUES(?,?,?,?,?) ON CONFLICT(recording_id,speaker_id) DO UPDATE SET
        person_id=excluded.person_id,revision=excluded.revision,updated_at=excluded.updated_at""",
        (recording_id, speaker_id, person_id, revision + 1, pipeline.stamp()))
    conn.execute("INSERT INTO speaker_identity_events(recording_id,speaker_id,action,before_person_id,after_person_id,undo_of_event_id,created_at) VALUES(?,?,?,?,?,?,?)",
                 (recording_id, speaker_id, action, event_before, person_id,
                  undo_of_event_id, pipeline.stamp()))
    pipeline.request_regenerate(conn, recording_id, commit=False)
    conn.commit()
    return assignment(conn, recording_id, speaker_id)


def assign(conn, recording_id, speaker_id, person_id):
    if not isinstance(person_id, str) or not _PERSON_RE.fullmatch(person_id):
        raise IdentityRejected("bad person")
    return _set(conn, recording_id, speaker_id, person_id, "assign")


def set_unknown(conn, recording_id, speaker_id):
    return _set(conn, recording_id, speaker_id, None, "unknown")


def undo(conn, recording_id, speaker_id):
    ensure_schema(conn); _validate_target(conn, recording_id, speaker_id)
    row = conn.execute("""SELECT e.event_id,e.before_person_id
        FROM speaker_identity_events e
        WHERE e.recording_id=? AND e.speaker_id=? AND e.action<>'undo'
          AND NOT EXISTS(SELECT 1 FROM speaker_identity_events u
                         WHERE u.undo_of_event_id=e.event_id)
        ORDER BY e.event_id DESC LIMIT 1""",
                       (recording_id, speaker_id)).fetchone()
    if not row:
        raise IdentityRejected("nothing to undo")
    current, _ = _current(conn, recording_id, speaker_id)
    return _set(conn, recording_id, speaker_id, row[1], "undo",
                before_override=current, undo_of_event_id=row[0])


def assignment(conn, recording_id, speaker_id):
    ensure_schema(conn)
    row = conn.execute("""SELECT a.speaker_id,a.person_id,a.revision,p.display_name,p.is_self,p.consent_status
        FROM speaker_identity_assignments a LEFT JOIN people p ON p.person_id=a.person_id
        WHERE a.recording_id=? AND a.speaker_id=?""", (recording_id, speaker_id)).fetchone()
    if not row:
        return {"speaker_id": speaker_id, "person_id": None, "display_name": None,
                "is_self": False, "consent_status": None, "revision": 0}
    return {"speaker_id": row[0], "person_id": row[1], "revision": row[2],
            "display_name": row[3], "is_self": bool(row[4]), "consent_status": row[5]}


def assignments(conn, recording_id):
    ensure_schema(conn)
    speakers = _known_speakers(conn, recording_id)
    return {speaker_id: assignment(conn, recording_id, speaker_id) for speaker_id in speakers}


def history(conn, recording_id, speaker_id, limit=20):
    ensure_schema(conn); _validate_target(conn, recording_id, speaker_id)
    rows = conn.execute("""SELECT event_id,action,before_person_id,after_person_id,created_at
        FROM speaker_identity_events WHERE recording_id=? AND speaker_id=?
        ORDER BY event_id DESC LIMIT ?""", (recording_id, speaker_id, max(1, min(int(limit), 50)))).fetchall()
    return [{"event_id": r[0], "action": r[1], "before_person_id": r[2],
             "after_person_id": r[3], "created_at": r[4]} for r in rows]
