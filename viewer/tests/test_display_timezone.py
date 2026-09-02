"""Tenant-local presentation of recording timestamps.

The archive stores three shapes for ``start_at``/``created_at``:

* naive ``2026-08-20T18:16:10``      -- PLAUD feed metadata, always UTC
* offset-bearing ``...-0500`` / ``+06:00`` -- local import provenance
* ``Z``-suffixed ``...000Z``          -- normalised import

All three denote an unambiguous instant. The viewer must not guess: the raw
column is canonical and untouched, and the API additionally publishes the
instant in UTC plus the tenant's configured IANA zone so the client formats in
one explicit zone instead of the reader's browser zone.
"""

import sqlite3

from fastapi.testclient import TestClient

from app import main


COLUMNS = ("id", "name", "start_at", "created_at", "duration_ms", "lang",
           "summary", "summary_json", "asr_engine", "asr_transcript",
           "plaud_transcript", "archived_local_at", "semantic_title",
           "recording_number")


def _archive(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,start_at TEXT,"
        "created_at TEXT,duration_ms INTEGER,lang TEXT,summary TEXT,"
        "summary_json TEXT,asr_engine TEXT,asr_transcript TEXT,"
        "plaud_transcript TEXT,archived_local_at TEXT,semantic_title TEXT,"
        "recording_number TEXT)"
    )
    for index, start_at in enumerate(rows):
        conn.execute(
            f"INSERT INTO recordings VALUES({','.join('?' * len(COLUMNS))})",
            (f"r{index:03d}", f"R{index}", start_at, start_at, 1000, "ru",
             f"S{index}", "{}", "local", "T", "", None, None, f"N-{index:04d}"),
        )
    conn.commit()
    conn.close()


def _client(path, monkeypatch, timezone_name=None):
    def open_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(main, "SECRET", "test-secret")
    monkeypatch.setattr(main, "db", open_db)
    monkeypatch.setattr(main, "DISPLAY_TIMEZONE", timezone_name)
    client = TestClient(main.app)
    client.cookies.set(main.COOKIE_NAME, main.expected_token())
    return client


def test_feed_publishes_naive_start_at_as_an_explicit_utc_instant(
    tmp_path, monkeypatch
):
    """A naive stored value is UTC, so the wire instant must carry ``+00:00``.

    Without this the browser parses ``2026-08-20T18:16:10`` as *its own* local
    time, so a New York reader sees 18:16 for a recording actually made at 14:16.
    """
    path = tmp_path / "archive.db"
    _archive(path, ["2026-08-20T18:16:10"])
    client = _client(path, monkeypatch, "America/New_York")

    payload = client.get("/api/recordings", params={"page": "cursor"}).json()
    row = payload["items"][0]

    assert row["start_at"] == "2026-08-20T18:16:10"       # canonical, untouched
    assert row["start_at_utc"] == "2026-08-20T18:16:10+00:00"


def test_feed_normalises_offset_and_zulu_start_at_to_the_same_instant(
    tmp_path, monkeypatch
):
    """The three stored shapes are provenance, not three different instants."""
    path = tmp_path / "archive.db"
    _archive(path, ["2026-08-19T17:28:15-0500",
                    "2026-08-19T22:28:15.000Z",
                    "2026-08-20T04:28:15+06:00"])
    client = _client(path, monkeypatch, "America/New_York")

    payload = client.get("/api/recordings", params={"page": "cursor"}).json()
    instants = {row["start_at_utc"] for row in payload["items"]}

    assert instants == {"2026-08-19T22:28:15+00:00"}


def test_feed_publishes_the_configured_tenant_display_timezone(
    tmp_path, monkeypatch
):
    """The client must be told which zone to format in, not guess the browser's."""
    path = tmp_path / "archive.db"
    _archive(path, ["2026-08-20T18:16:10"])
    client = _client(path, monkeypatch, "Asia/Dhaka")

    payload = client.get("/api/recordings", params={"page": "cursor"}).json()

    assert payload["display_timezone"] == "Asia/Dhaka"


def test_detail_publishes_the_instant_and_the_zone(tmp_path, monkeypatch):
    """The detail metadata line is a visible date/time surface too."""
    path = tmp_path / "archive.db"
    _archive(path, ["2026-08-20T18:16:10"])
    client = _client(path, monkeypatch, "America/New_York")

    detail = client.get("/api/recordings/r000").json()

    assert detail["start_at"] == "2026-08-20T18:16:10"
    assert detail["start_at_utc"] == "2026-08-20T18:16:10+00:00"
    assert detail["display_timezone"] == "America/New_York"


def test_an_unset_or_unknown_zone_leaves_the_tenant_unconfigured(
    tmp_path, monkeypatch
):
    """Unspecified tenants keep their existing behaviour: no zone is claimed."""
    path = tmp_path / "archive.db"
    _archive(path, ["2026-08-20T18:16:10"])
    client = _client(path, monkeypatch, None)

    payload = client.get("/api/recordings", params={"page": "cursor"}).json()

    assert payload["display_timezone"] is None
    # the instant is still explicit -- that is a correctness fix, not a policy
    assert payload["items"][0]["start_at_utc"] == "2026-08-20T18:16:10+00:00"


def test_an_unparsable_stored_timestamp_yields_no_instant(tmp_path, monkeypatch):
    """A malformed row must not 500 the feed; it simply has no instant."""
    path = tmp_path / "archive.db"
    _archive(path, ["not a timestamp"])
    client = _client(path, monkeypatch, "America/New_York")

    payload = client.get("/api/recordings", params={"page": "cursor"}).json()

    assert payload["items"][0]["start_at_utc"] is None


def test_comment_timestamps_are_published_as_explicit_instants(
    tmp_path, monkeypatch
):
    """Comment dates render next to recording dates and must share the zone."""
    path = tmp_path / "archive.db"
    _archive(path, ["2026-08-20T18:16:10"])
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE recording_comments(id INTEGER PRIMARY KEY,"
        "recording_id TEXT,body TEXT,created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO recording_comments(recording_id,body,created_at) "
        "VALUES(?,?,?)", ("r000", "note", "2026-08-20T23:40:00")
    )
    conn.commit()
    conn.close()
    client = _client(path, monkeypatch, "America/New_York")

    comment = client.get("/api/recordings/r000").json()["comments"][0]

    assert comment["created_at"] == "2026-08-20T23:40:00"
    assert comment["created_at_utc"] == "2026-08-20T23:40:00+00:00"
