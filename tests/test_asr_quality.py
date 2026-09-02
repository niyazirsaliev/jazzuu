"""Tests for ASR quality routing: tenant-local audio, and the review of
non-empty PLAUD transcripts.

Two production facts drive this file.

Each connector's audio is already on its own disk under the tenant archive.
The ASR path must use `transcribe_url` for that local file and fail closed when
the file is missing.

And `pending()` skips every row with a non-empty `plaud_transcript`, so PLAUD
output is accepted purely for being non-empty — a truncated or looping
transcript is final and invisible. Review makes that acceptance explicit and
revisable, without ever touching the PLAUD text itself.
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(filename, module_name, env=None):
    """Import an archive/ module under a controlled environment.

    These modules resolve paths, credentials and deployment mode at import
    time, so `env` is the only way to see what a tenant container would really
    do. A value of None removes the variable — the machine running the suite
    may well export it.
    """
    path = ROOT / "archive" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    env = env or {}
    with mock.patch.dict(sys.modules, {module_name: module}):
        with mock.patch.dict(
                os.environ, {k: v for k, v in env.items() if v is not None},
                clear=False):
            for key, value in env.items():
                if value is None:
                    os.environ.pop(key, None)
            spec.loader.exec_module(module)
    return module


ARCHIVE_DDL = """
CREATE TABLE recordings(
  id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
  duration_ms INTEGER, lang TEXT, asr_engine TEXT, asr_transcript TEXT,
  plaud_transcript TEXT, summary TEXT, audio_path TEXT, archived_at TEXT,
  plaud_meta_json TEXT, summary_json TEXT, asr_meta_json TEXT,
  asr_alternative_transcript TEXT);
CREATE VIRTUAL TABLE recordings_fts USING fts5(id UNINDEXED, name, transcript);
"""


class TenantArchive:
    """A tenant archive on disk: DB, audio directory, and the module for it."""

    def __init__(self, case, tenant_id="tenant-c", **env):
        self.dir = tempfile.mkdtemp()
        self.audio_dir = os.path.join(self.dir, "audio")
        os.makedirs(self.audio_dir)
        Path(self.dir, ".asr_token").write_text("tenant-caller-token")
        settings = {"TENANT_ID": tenant_id, "TENANT_ARCHIVE_DIR": self.dir,
                    "ARCHIVE_DB": None, "ASR_LOCK_PATH": None,
                    "ASR_TOKEN_FILE": None, "ASR_AUDIO_URL_BASE": None,
                    "ASR_AUDIO_PATH_PREFIX": None}
        settings.update(env)
        self.asr = load_module(
            "asr_backfill.py", f"asr_backfill_{tenant_id}_{id(self)}", settings)
        self.db_path = os.path.join(self.dir, "archive.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(ARCHIVE_DDL)
        self.asr.ensure_attempts(self.conn)
        case.addCleanup(self.conn.close)

    def audio(self, rec_id, payload=b"ID3fake-mp3-bytes"):
        path = os.path.join(self.audio_dir, f"{rec_id}.mp3")
        Path(path).write_bytes(payload)
        return path

    def recording(self, rec_id, duration_ms=14_000, plaud="", audio_path=None,
                  archived_at="2026-08-05T22:00:00", start_at=None):
        self.conn.execute(
            """INSERT INTO recordings(id,name,start_at,duration_ms,
                 plaud_transcript,asr_transcript,audio_path,archived_at)
               VALUES(?,?,?,?,?,'',?,?)""",
            (rec_id, f"rec {rec_id}", start_at or archived_at, duration_ms,
             plaud, audio_path, archived_at))
        self.conn.commit()


def recording_calls(module):
    """Patch `call` so every tool invocation is recorded, with a stub result."""
    seen = []

    def fake_call(name, args, sid, **kwargs):
        seen.append((name, dict(args)))
        return {"text": f"local words at {args.get('start_sec') or 0}",
                "engine_used": "large-v3",
                "meta": {"detected_lang": "ru", "route_reason": "cyrillic"}}

    return seen, mock.patch.object(module, "call", side_effect=fake_call)


class TenantLocalAudioTests(unittest.TestCase):
    """A family connector transcribes the audio it already has on disk.

    The account-bound tool is not merely suboptimal here: tenant-c's connector
    holds no owner PLAUD credentials, asr-mcp resolves file_ids against the
    owner account, and the live 14-second recording failed with an API 500
    from PLAUD's get_file. The whole recording is sitting in /archive/audio.
    """

    def setUp(self):
        self.tenant = TenantArchive(self)

    def test_tenant_mode_transcribes_the_local_file_through_transcribe_url(self):
        self.tenant.recording("b14", duration_ms=14_000)
        path = self.tenant.audio("b14")
        seen, patched = recording_calls(self.tenant.asr)

        with patched:
            self.tenant.asr.transcribe(self.tenant.conn, "sid", "b14", "auto", 14)

        self.assertEqual([name for name, _ in seen], ["transcribe_url"])
        self.assertEqual(seen[0][1]["url"], Path(path).as_uri())
        self.assertEqual(seen[0][1]["engine"], "auto")
        self.assertNotIn("file_id", seen[0][1])
        self.assertEqual(
            self.tenant.conn.execute(
                "SELECT asr_transcript FROM recordings WHERE id='b14'"
            ).fetchone()[0],
            "local words at 0",
        )

    def test_tenant_segmented_windows_are_local_too(self):
        """A long tenant recording still gets start_sec/duration_sec windows —
        on the local file, never on a PLAUD file_id."""
        duration = 40 * 60
        self.tenant.recording("blong", duration_ms=duration * 1000)
        path = self.tenant.audio("blong")
        seen, patched = recording_calls(self.tenant.asr)

        with patched, mock.patch.object(self.tenant.asr, "SEGMENTS_PER_RUN", 0):
            self.tenant.asr.transcribe(
                self.tenant.conn, "sid", "blong", "auto", duration)

        self.assertEqual([name for name, _ in seen], ["transcribe_url"] * 4)
        self.assertEqual([(a["start_sec"], a["duration_sec"]) for _, a in seen],
                         [(0, 600), (600, 600), (1200, 600), (1800, 600)])
        for _name, args in seen:
            self.assertEqual(args["url"], Path(path).as_uri())
            self.assertNotIn("file_id", args)

    def test_owner_without_local_audio_fails_before_any_call(self):
        """The public Tilmech boundary accepts only local, tenant-owned audio."""
        owner = load_module("asr_backfill.py", "asr_backfill_owner_route",
                            {"TENANT_ID": None, "ARCHIVE_DB": None})
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(ARCHIVE_DDL)
        owner.ensure_attempts(conn)
        conn.execute("INSERT INTO recordings(id,name) VALUES('o1','Owner')")
        conn.commit()
        seen, patched = recording_calls(owner)

        with patched:
            with self.assertRaises(owner.LocalAudioMissing):
                owner.transcribe(conn, "sid", "o1", "auto", 14)

        self.assertEqual(seen, [])

    def test_owner_runs_use_local_audio_when_the_file_is_there(self):
        """The owner path uses the same local-only ASR boundary as every tenant."""
        archive_dir = tempfile.mkdtemp()
        audio_dir = os.path.join(archive_dir, "audio")
        os.makedirs(audio_dir)
        path = os.path.join(audio_dir, "o2.mp3")
        Path(path).write_bytes(b"ID3owner")
        owner = load_module(
            "asr_backfill.py", "asr_backfill_owner_local",
            {"TENANT_ID": None,
             "ARCHIVE_DB": os.path.join(archive_dir, "archive.db"),
             "ASR_AUDIO_URL_BASE": None})
        conn = sqlite3.connect(os.path.join(archive_dir, "archive.db"))
        self.addCleanup(conn.close)
        conn.executescript(ARCHIVE_DDL)
        owner.ensure_attempts(conn)
        conn.execute("INSERT INTO recordings(id,name) VALUES('o2','Owner')")
        conn.commit()
        seen, patched = recording_calls(owner)

        with patched:
            owner.transcribe(conn, "sid", "o2", "auto", 14)

        self.assertEqual([name for name, _ in seen], ["transcribe_url"])
        self.assertEqual(seen[0][1]["url"], Path(path).as_uri())
        self.assertNotIn("file_id", seen[0][1])

    def test_missing_local_audio_fails_before_any_call(self):
        """Fail visibly rather than silently falling back to the owner's
        account — and say so without printing a host path."""
        self.tenant.recording("gone")
        seen, patched = recording_calls(self.tenant.asr)

        with patched:
            with self.assertRaises(self.tenant.asr.LocalAudioMissing) as caught:
                self.tenant.asr.transcribe(
                    self.tenant.conn, "sid", "gone", "auto", 14)

        self.assertEqual(seen, [])
        message = str(caught.exception)
        self.assertIn("gone", message)
        self.assertNotIn(self.tenant.audio_dir, message)
        self.assertNotIn(".mp3", message)

    def test_an_empty_audio_file_is_not_audio(self):
        self.tenant.recording("empty")
        self.tenant.audio("empty", payload=b"")
        with self.assertRaises(self.tenant.asr.LocalAudioMissing):
            self.tenant.asr.local_audio_path(self.tenant.conn, "empty")

    def test_audio_path_outside_the_tenant_audio_dir_is_refused(self):
        """audio_path is archived metadata, not an instruction. A row pointing
        anywhere but this tenant's own audio directory is fail-closed."""
        outside = tempfile.mkdtemp()
        stray = os.path.join(outside, "elsewhere.mp3")
        Path(stray).write_bytes(b"ID3other")
        self.tenant.recording("stray", audio_path=stray)

        with self.assertRaises(self.tenant.asr.LocalAudioMissing):
            self.tenant.asr.local_audio_path(self.tenant.conn, "stray")

    def test_an_archived_audio_path_inside_the_archive_is_honoured(self):
        """The row's own path wins when it really is this tenant's file: an
        archive whose files are not named <id>.mp3 still resolves."""
        path = os.path.join(self.tenant.audio_dir, "2026-08-05-tenant-c.mp3")
        Path(path).write_bytes(b"ID3real")
        self.tenant.recording("named", audio_path=path)

        self.assertEqual(
            self.tenant.asr.local_audio_path(self.tenant.conn, "named"), path)

    def test_a_url_base_is_used_when_asr_cannot_share_the_volume(self):
        """Deployments where asr-mcp cannot mount the tenant volume publish the
        audio over HTTP instead; the local file still decides what exists."""
        tenant = TenantArchive(
            self, tenant_id="tenant-b",
            ASR_AUDIO_URL_BASE="http://127.0.0.1:62399/tenant-audio/")
        tenant.recording("d1")
        tenant.audio("d1")
        seen, patched = recording_calls(tenant.asr)

        with patched:
            tenant.asr.transcribe(tenant.conn, "sid", "d1", "auto", 14)

        self.assertEqual(
            seen[0][1]["url"],
            "http://127.0.0.1:62399/tenant-audio/d1.mp3")

    def test_an_mcp_tool_error_is_not_persisted_as_a_transcript(self):
        self.tenant.recording("bad-tool")
        path = self.tenant.audio("bad-tool")
        private_url = Path(path).as_uri() + "?token=private-token"
        response = {"result": {"isError": True, "content": [{
            "type": "text",
            "text": ("2 validation errors for call[transcribe_url]: "
                     f"url={private_url} token=private-token"),
        }]}}

        with mock.patch.object(self.tenant.asr, "_post", return_value=(response, "sid")):
            with self.assertRaises(self.tenant.asr.McpToolError) as caught:
                self.tenant.asr.transcribe(self.tenant.conn, "sid", "bad-tool", "auto", 14)

        self.assertEqual(
            self.tenant.conn.execute(
                "SELECT asr_transcript FROM recordings WHERE id='bad-tool'").fetchone()[0],
            "",
        )
        self.assertNotIn(private_url, str(caught.exception))
        self.assertNotIn("private-token", str(caught.exception))

    def test_a_shared_asr_path_prefix_maps_a_tenant_b_file_by_basename(self):
        tenant = TenantArchive(
            self, tenant_id="tenant-b", ASR_AUDIO_PATH_PREFIX="/shared-asr/tenant-b-audio")
        tenant.recording("dmap")
        tenant.audio("dmap")
        seen, patched = recording_calls(tenant.asr)

        with patched:
            tenant.asr.transcribe(tenant.conn, "sid", "dmap", "auto", 14)

        self.assertEqual(
            seen[0][1]["url"], "file:///shared-asr/tenant-b-audio/dmap.mp3")
        # The shared container mount receives a filename, not an archived path.
        self.assertEqual(
            tenant.asr.audio_url("/untrusted/../../other-tenant/dmap.mp3"),
            "file:///shared-asr/tenant-b-audio/dmap.mp3")

    def test_a_shared_asr_path_prefix_maps_an_owner_file_by_basename(self):
        archive_dir = tempfile.mkdtemp()
        audio_dir = os.path.join(archive_dir, "audio")
        os.makedirs(audio_dir)
        path = os.path.join(audio_dir, "omap.mp3")
        Path(path).write_bytes(b"ID3owner")
        owner = load_module(
            "asr_backfill.py", "asr_backfill_owner_shared_path",
            {"TENANT_ID": None,
             "ARCHIVE_DB": os.path.join(archive_dir, "archive.db"),
             "ASR_AUDIO_URL_BASE": None,
             "ASR_AUDIO_PATH_PREFIX": "/shared-asr/owner-audio"})
        conn = sqlite3.connect(os.path.join(archive_dir, "archive.db"))
        self.addCleanup(conn.close)
        conn.executescript(ARCHIVE_DDL)
        owner.ensure_attempts(conn)
        conn.execute("INSERT INTO recordings(id,name) VALUES('omap','Owner')")
        conn.commit()
        seen, patched = recording_calls(owner)

        with patched:
            owner.transcribe(conn, "sid", "omap", "auto", 14)

        self.assertEqual(
            seen[0][1]["url"], "file:///shared-asr/owner-audio/omap.mp3")

    def test_without_url_base_or_shared_prefix_audio_keeps_its_local_file_uri(self):
        self.tenant.recording("default-uri")
        path = self.tenant.audio("default-uri")
        seen, patched = recording_calls(self.tenant.asr)

        with patched:
            self.tenant.asr.transcribe(self.tenant.conn, "sid", "default-uri", "auto", 14)

        self.assertEqual(seen[0][1]["url"], Path(path).as_uri())

    def test_a_relative_shared_asr_path_prefix_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            TenantArchive(self, tenant_id="tenant-b", ASR_AUDIO_PATH_PREFIX="shared-asr/tenant-b")
        self.assertIn("ASR_AUDIO_PATH_PREFIX", str(caught.exception))

    def test_failures_never_record_a_host_path_or_a_file_uri(self):
        """Diagnostics reach cron mail, container logs and the retry ledger."""
        self.tenant.recording("noisy")
        path = self.tenant.audio("noisy")
        detail = (f"RuntimeError: download failed for {Path(path).as_uri()} "
                  f"(cached at {path})")

        self.tenant.asr.bump(self.tenant.conn, "noisy", detail)

        stored = self.tenant.conn.execute(
            "SELECT last_error FROM asr_attempts WHERE id='noisy'").fetchone()[0]
        self.assertNotIn("file://", stored)
        self.assertNotIn(self.tenant.audio_dir, stored)
        self.assertIn("download failed", stored)

    def test_missing_audio_is_a_verdict_not_a_transient(self):
        """Nothing about a re-run makes the file appear, so it spends the retry
        budget instead of re-entering the queue on every pass forever."""
        self.assertTrue(self.tenant.asr.counts_against_budget(
            self.tenant.asr.LocalAudioMissing("no local audio for gone")))


# 2026-08-10T12:00:00Z, the clock every enrollment test reasons from.
NOW = 1786_100_000


class ReviewQueueTests(unittest.TestCase):
    """Which non-empty PLAUD transcripts become validation candidates, when.

    Enrollment is where a well-meaning migration turns into an outage: 61
    historical recordings all becoming eligible at once would spend days of the
    single-flight queue re-transcribing text the reader is already happy with,
    while genuinely textless recordings — the ones showing nothing at all —
    wait behind them.
    """

    def setUp(self):
        self.tenant = TenantArchive(self)
        self.asr = self.tenant.asr
        self.conn = self.tenant.conn

    def enroll(self, now=NOW):
        self.asr.ensure_reviews(self.conn, now=now)

    def queued(self, now=NOW):
        return [row[0] for row in self.asr.review_pending(self.conn, now=now)]

    def state_of(self, rec_id):
        row = self.conn.execute(
            "SELECT state, eligible_epoch FROM plaud_reviews WHERE id=?",
            (rec_id,)).fetchone()
        return row

    def old(self, rec_id, days=120, plaud="Расшифровка PLAUD целиком.",
            start_at=None):
        """A recording archived long before review existed."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(NOW - days * 86400))
        self.tenant.recording(rec_id, plaud=plaud, archived_at=stamp,
                              start_at=start_at or stamp)

    def fresh(self, rec_id, plaud="Свежая расшифровка PLAUD.", hours=2):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(NOW - hours * 3600))
        self.tenant.recording(rec_id, plaud=plaud, archived_at=stamp,
                              start_at=stamp)

    def test_the_review_table_is_created_through_the_ensure_layout_path(self):
        """ensure_attempts is what every entry point already calls."""
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(ARCHIVE_DDL)

        self.asr.ensure_attempts(conn)

        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("plaud_reviews", tables)

    def test_enrollment_is_idempotent(self):
        self.fresh("a")
        self.enroll()
        self.enroll()
        self.enroll(now=NOW + 86400)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM plaud_reviews WHERE id='a'").fetchone()[0],
            1)

    def test_a_non_empty_plaud_recording_becomes_a_validation_candidate(self):
        self.fresh("b14")
        self.enroll()

        self.assertEqual(self.state_of("b14"), ("queued", None))
        self.assertIn("b14", self.queued())

    def test_a_textless_recording_is_not_a_review_candidate(self):
        """Textless work belongs to pending(); enrolling it too would have one
        recording in two queues and transcribe it twice."""
        self.tenant.recording("silent", plaud="")
        self.enroll()

        self.assertIsNone(self.state_of("silent"))
        self.assertIn("silent", [r[0] for r in self.asr.pending(self.conn)])

    def test_a_recording_that_already_has_local_text_is_not_re_reviewed(self):
        self.fresh("done")
        self.conn.execute(
            "UPDATE recordings SET asr_transcript='локальный текст' WHERE id='done'")
        self.conn.commit()
        self.enroll()

        self.assertIsNone(self.state_of("done"))

    def test_history_is_enrolled_as_a_bounded_backlog_not_a_flood(self):
        """The migration policy, pinned: existing rows are enrolled but only
        ASR_REVIEW_BACKLOG_PER_DAY of them become eligible per day, newest
        first. Nothing about the migration changes what any of them display."""
        for index in range(12):
            self.old(f"old{index:02d}", days=200 - index)
        self.enroll()

        eligible_now = self.queued()
        self.assertEqual(eligible_now,
                         ["old11", "old10", "old09", "old08", "old07"])
        self.assertEqual(len(self.queued(now=NOW + 86400)), 10)
        self.assertEqual(len(self.queued(now=NOW + 2 * 86400)), 12)
        # And every one of them still shows exactly the text it showed before.
        for index in range(12):
            row = self.conn.execute(
                "SELECT plaud_transcript, COALESCE(asr_transcript,'') "
                "FROM recordings WHERE id=?", (f"old{index:02d}",)).fetchone()
            self.assertEqual(row, ("Расшифровка PLAUD целиком.", ""))

    def test_new_work_outranks_the_whole_backlog(self):
        for index in range(12):
            self.old(f"old{index:02d}", days=200 - index)
        self.enroll()
        self.fresh("brand-new")
        self.enroll()

        self.assertEqual(self.queued()[0], "brand-new")

    def test_textless_work_outranks_validation_work(self):
        """A recording showing nothing at all beats re-checking one that
        already shows text, however stale the backlog gets."""
        self.fresh("has-plaud")
        self.tenant.recording("has-nothing", plaud="")
        self.enroll()

        kind, targets = self.asr.next_targets(self.conn, now=NOW)

        self.assertEqual(kind, "transcribe")
        self.assertEqual([row[0] for row in targets], ["has-nothing"])

    def test_validation_runs_once_the_textless_queue_is_empty(self):
        self.fresh("has-plaud")
        self.enroll()

        kind, targets = self.asr.next_targets(self.conn, now=NOW)

        self.assertEqual(kind, "review")
        self.assertEqual([row[0] for row in targets], ["has-plaud"])

    def test_a_reviewed_row_leaves_the_queue_durably(self):
        """Including — especially — one where PLAUD kept the selection. A
        verdict that does not persist is a recording re-transcribed forever."""
        self.fresh("keeper")
        self.enroll()
        self.asr.record_review(
            self.conn, "keeper",
            self.asr.Verdict(source="plaud", reason="plaud-adequate",
                             candidates=[], text="Свежая расшифровка PLAUD.",
                             engine="", lang="", meta={}, alternative=""))

        self.assertEqual(self.queued(), [])
        self.assertEqual(self.queued(now=NOW + 400 * 86400), [])
        self.enroll(now=NOW + 400 * 86400)   # a later migration sweep
        self.assertEqual(self.queued(now=NOW + 400 * 86400), [])
        self.assertEqual(self.state_of("keeper")[0], "reviewed")

    def test_recordings_past_the_segment_threshold_are_not_enrolled(self):
        """Validation is whole-file work by design. Letting a four-hour
        recording into it would hand a nine-window segmented job to a queue
        whose whole purpose is not to delay textless recordings."""
        self.tenant.recording(
            "marathon", duration_ms=(self.asr.SEGMENT_THRESHOLD_SEC + 1) * 1000,
            plaud="Очень длинная расшифровка.",
            archived_at=time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.localtime(NOW - 3600)))
        self.fresh("normal")
        self.enroll()

        self.assertIsNone(self.state_of("marathon"))
        self.assertEqual(self.queued(), ["normal"])

    def test_a_dead_claim_returns_the_row_to_the_queue(self):
        """A run killed mid-review must not park the row in processing."""
        self.fresh("stuck")
        self.enroll()
        self.assertTrue(self.asr.claim_review(self.conn, "stuck"))
        self.assertEqual(self.state_of("stuck")[0], "processing")
        self.assertEqual(self.queued(), [])

        self.conn.execute(
            "UPDATE plaud_reviews SET claim_epoch=?, claim_owner='ghost:1' "
            "WHERE id='stuck'",
            (int(time.time()) - self.asr.SEGMENT_LEASE_SEC - 60,))
        self.conn.commit()

        self.assertEqual(self.queued(), ["stuck"])

    def test_a_row_that_exhausted_its_attempts_stops_being_offered(self):
        self.fresh("bad")
        self.enroll()
        for _ in range(self.asr.MAX_ATTEMPTS):
            self.asr.fail_review(self.conn, "bad", ValueError("decode failed"))
        self.assertEqual(self.state_of("bad")[0], "error")
        self.assertEqual(self.queued(), [])

    def test_transient_review_failures_keep_their_budget(self):
        self.fresh("blip")
        self.enroll()
        for _ in range(5):
            self.asr.fail_review(
                self.conn, "blip", RuntimeError("HTTP Error 500: Internal Server Error"))

        self.assertEqual(self.queued(), ["blip"])

    def test_a_legacy_archive_opens_and_migrates_without_manual_sql(self):
        """An archive predating every derived column must still open."""
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            "CREATE TABLE recordings(id TEXT PRIMARY KEY, name TEXT, "
            "plaud_transcript TEXT);")

        self.asr.ensure_attempts(conn)   # must not raise

        self.assertIn("plaud_reviews", {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})


class OwnerLayoutTests(unittest.TestCase):
    """The owner archive migrates through the path it already runs."""

    def test_owner_init_db_creates_the_review_ledger(self):
        archive = load_module("archive_recording.py", "archive_owner_schema",
                              {"TENANT_ID": None})
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, name TEXT)")

        archive.init_db(conn)
        archive.init_db(conn)   # idempotent

        self.assertIn("plaud_reviews", {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})

    def test_every_layout_path_agrees_on_the_review_columns(self):
        """Three ensure-layout paths create this table — the owner archiver,
        the tenant connector, and the ASR worker. A column added to one and not
        the others is a tenant whose reviews silently cannot be written."""
        shapes = []
        for build in (self._owner_layout, self._tenant_layout, self._asr_layout):
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            build(conn)
            shapes.append([(row[1], row[2].upper())
                           for row in conn.execute(
                               "PRAGMA table_info(plaud_reviews)")])
        self.assertTrue(shapes[0])
        self.assertEqual(shapes[0], shapes[1])
        self.assertEqual(shapes[0], shapes[2])

    def _owner_layout(self, conn):
        conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, name TEXT)")
        load_module("archive_recording.py", "archive_shape",
                    {"TENANT_ID": None}).init_db(conn)

    def _tenant_layout(self, conn):
        connector = ROOT / "archive" / "connector.py"
        schema = re.search(r'SCHEMA = """(.*?)"""', connector.read_text(),
                           re.S).group(1)
        conn.executescript(schema)

    def _asr_layout(self, conn):
        conn.executescript(ARCHIVE_DDL)
        load_module("asr_backfill.py", "asr_shape",
                    {"TENANT_ID": None}).ensure_reviews(conn)


def speech(count, seed="слово"):
    """`count` distinct plausible words — a transcript that is not degenerate."""
    return " ".join(f"{seed}{index}" for index in range(count)) + "."


class QualityScoringTests(unittest.TestCase):
    """Deterministic, conservative comparison. No model, no clock, no network.

    The point is not to grade transcripts. It is to catch the two ways a
    non-empty PLAUD transcript is nonetheless not the recording — a degenerate
    loop, and a fragment standing in for the whole — while leaving every
    ordinary transcript exactly where it is.
    """

    def setUp(self):
        self.asr = load_module("asr_backfill.py", f"asr_scoring_{id(self)}",
                               {"TENANT_ID": None})

    def test_an_empty_transcript_scores_zero(self):
        for text in ("", "   ", None, "!!!"):
            self.assertEqual(self.asr.quality_score(text), 0.0, repr(text))

    def test_a_looping_transcript_scores_below_real_speech(self):
        loop = " ".join(["да"] * 60)
        self.assertLess(self.asr.quality_score(loop),
                        self.asr.quality_score(speech(60)))

    def test_symbol_soup_scores_below_real_speech(self):
        soup = "".join("№#¤§±" for _ in range(40)) + " текст"
        self.assertLess(self.asr.quality_score(soup),
                        self.asr.quality_score(speech(60)))

    def test_a_fragment_scores_below_a_full_transcript(self):
        self.assertLess(self.asr.quality_score(speech(4)),
                        self.asr.quality_score(speech(80)))

    def test_the_score_is_a_bounded_deterministic_number(self):
        for text in ("", speech(3), speech(400), " ".join(["ага"] * 200)):
            score = self.asr.quality_score(text)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)
            self.assertEqual(score, self.asr.quality_score(text))

    def test_plaud_keeps_the_selection_when_local_is_merely_different(self):
        """Conservative by construction: an equal-quality local hypothesis is
        not a reason to replace text the reader is already reading."""
        verdict = self.asr.review_verdict(speech(60), speech(60, "иное"))

        self.assertEqual(verdict.source, "plaud")
        self.assertEqual(verdict.reason, "plaud-adequate")

    def test_a_looping_plaud_transcript_loses_to_the_local_one(self):
        verdict = self.asr.review_verdict(" ".join(["да"] * 60), speech(60))

        self.assertEqual(verdict.source, "local")
        self.assertEqual(verdict.reason, "local-clearly-better")
        self.assertEqual(verdict.text, speech(60))

    def test_a_truncated_plaud_transcript_loses_to_the_full_local_one(self):
        """Same words, a third of the recording. Scores alone call that a tie,
        so length relative to the challenger is judged too."""
        head = speech(45)
        verdict = self.asr.review_verdict(head, head + " " + speech(120, "далее"))

        self.assertEqual(verdict.source, "local")
        self.assertEqual(verdict.reason, "plaud-truncated")

    def test_an_empty_local_hypothesis_never_wins(self):
        verdict = self.asr.review_verdict(speech(60), "")
        self.assertEqual(verdict.source, "plaud")
        self.assertEqual(verdict.reason, "local-empty")

    def test_the_better_local_candidate_becomes_the_challenger(self):
        """auto mode returns two hypotheses; the review compares PLAUD against
        the better of them and keeps the other as the alternative."""
        verdict = self.asr.review_verdict(
            " ".join(["да"] * 60), " ".join(["нет"] * 60),
            alternative=speech(90), engine="kyrgyz",
            alternative_engine="large-v3")

        self.assertEqual(verdict.source, "local")
        self.assertEqual(verdict.text, speech(90))
        self.assertEqual(verdict.engine, "large-v3")
        self.assertEqual(verdict.alternative, " ".join(["нет"] * 60))

    def test_unresolved_counterfactual_can_never_win_review(self):
        hallucinated = speech(100, "галлюцинация")
        verdict = self.asr.review_verdict(
            " ".join(["да"] * 60),
            "[неразборчиво]",
            alternative=hallucinated,
            engine="mixed",
            alternative_engine="mixed-counterfactual",
            meta={"unresolved_count": 1, "alternative": {"text": hallucinated}},
        )

        self.assertNotEqual(verdict.text, hallucinated)
        self.assertNotIn(hallucinated, json.dumps(verdict.meta or {}, ensure_ascii=False))

    def test_every_candidate_is_kept_with_its_score_and_one_is_selected(self):
        verdict = self.asr.review_verdict(
            speech(60), speech(60, "локально"), alternative=speech(20, "второе"),
            engine="large-v3", alternative_engine="kyrgyz")

        sources = [c["source"] for c in verdict.candidates]
        self.assertEqual(sources, ["plaud", "local", "local-alternative"])
        self.assertEqual([c["selected"] for c in verdict.candidates],
                         [True, False, False])
        for candidate in verdict.candidates:
            self.assertIn("score", candidate)
            self.assertIn("engine", candidate)
            self.assertIn("chars", candidate)
            self.assertTrue(candidate["text"])


class ReviewPublicationTests(unittest.TestCase):
    """What one finished review leaves in the archive.

    The reader sees one transcript. Which one is the verdict's job; that the
    transcript, the search index and the verdict agree — always, even when the
    write fails halfway — is this code's.
    """

    PLAUD_LOOP = " ".join(["да"] * 60)
    PLAUD_GOOD = speech(60, "плауд")

    def setUp(self):
        self.tenant = TenantArchive(self)
        self.asr = self.tenant.asr
        self.conn = self.tenant.conn

    def candidate_row(self, rec_id="r1", plaud=PLAUD_GOOD):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 3600))
        self.tenant.recording(rec_id, plaud=plaud, archived_at=stamp,
                              start_at=stamp)
        self.conn.execute(
            "INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)",
            (rec_id, f"rec {rec_id}", plaud))
        self.conn.commit()
        self.tenant.audio(rec_id)
        self.asr.ensure_reviews(self.conn, now=NOW)
        return rec_id

    def local_result(self, text, alternative=""):
        def fake_call(name, args, sid, **kwargs):
            self.calls.append((name, dict(args)))
            return {"text": text, "engine_used": "large-v3",
                    "meta": {"detected_lang": "ru",
                             "route_reason": "cyrillic detected",
                             "alternative": {"engine": "kyrgyz",
                                             "text": alternative}}}

        self.calls = []
        return mock.patch.object(self.asr, "call", side_effect=fake_call)

    def stored(self, rec_id="r1"):
        return self.conn.execute(
            "SELECT COALESCE(asr_transcript,''), COALESCE(plaud_transcript,''),"
            " COALESCE(asr_alternative_transcript,'') FROM recordings WHERE id=?",
            (rec_id,)).fetchone()

    def fts(self, rec_id="r1"):
        return [row[0] for row in self.conn.execute(
            "SELECT transcript FROM recordings_fts WHERE id=?", (rec_id,))]

    def review(self, rec_id="r1"):
        return self.conn.execute(
            "SELECT state, selected_source, reason, candidates_json "
            "FROM plaud_reviews WHERE id=?", (rec_id,)).fetchone()

    def test_a_local_winner_becomes_the_preferred_text_and_the_index(self):
        self.candidate_row(plaud=self.PLAUD_LOOP)
        local = speech(90, "локально")

        with self.local_result(local, alternative=speech(30, "запас")):
            self.asr.review_one(self.conn, "sid", "r1")

        asr_text, plaud_text, alternative = self.stored()
        self.assertEqual(asr_text, local)
        self.assertEqual(plaud_text, self.PLAUD_LOOP)   # never overwritten
        self.assertEqual(alternative, speech(30, "запас"))
        self.assertEqual(self.fts(), [local])
        state, source, reason, _ = self.review()
        self.assertEqual((state, source), ("reviewed", "local"))
        self.assertEqual(reason, "local-clearly-better")

    def test_a_plaud_winner_keeps_the_preferred_text_and_the_index(self):
        self.candidate_row(plaud=self.PLAUD_GOOD)

        with self.local_result(speech(60, "локально")):
            self.asr.review_one(self.conn, "sid", "r1")

        asr_text, plaud_text, _alt = self.stored()
        # asr_transcript is what the viewer prefers; the rejected hypothesis
        # must not be sitting in it.
        self.assertEqual(asr_text, "")
        self.assertEqual(plaud_text, self.PLAUD_GOOD)
        self.assertEqual(self.fts(), [self.PLAUD_GOOD])
        state, source, reason, candidates = self.review()
        self.assertEqual((state, source, reason),
                         ("reviewed", "plaud", "plaud-adequate"))
        # Losing does not mean discarded: the hypothesis is kept for a later
        # look, with the score that rejected it.
        kept = json.loads(candidates)
        self.assertEqual([c["source"] for c in kept],
                         ["plaud", "local", "local-alternative"])
        self.assertEqual(kept[1]["text"], speech(60, "локально"))

    def test_a_reviewed_recording_is_not_reviewed_again(self):
        self.candidate_row(plaud=self.PLAUD_GOOD)
        with self.local_result(speech(60, "локально")):
            self.asr.review_one(self.conn, "sid", "r1")

        self.assertEqual(self.asr.review_pending(self.conn, now=NOW), [])
        with self.local_result(speech(60, "локально")):
            reviewed = self.asr.review_next(
                self.conn, "sid", now=NOW + 10 * 86400)
        self.assertEqual(reviewed, [])
        self.assertEqual(self.calls, [])

    def test_review_reads_the_tenant_local_audio_not_the_plaud_account(self):
        self.candidate_row(plaud=self.PLAUD_LOOP)

        with self.local_result(speech(90, "локально")):
            self.asr.review_one(self.conn, "sid", "r1")

        self.assertEqual([name for name, _ in self.calls], ["transcribe_url"])
        self.assertNotIn("file_id", self.calls[0][1])

    def test_a_failed_publication_rolls_back_every_change(self):
        """The verdict, the transcript and the index land together or not at
        all: a transcript without its index entry is silently unsearchable, and
        an index without its verdict is re-reviewed forever."""
        self.candidate_row(plaud=self.PLAUD_LOOP)
        local = speech(90, "локально")
        verdict = self.asr.review_verdict(self.PLAUD_LOOP, local)
        self.assertEqual(verdict.source, "local")

        broken = FailingConn(self.conn, on="INSERT INTO plaud_reviews")
        with self.assertRaises(sqlite3.OperationalError):
            self.asr.record_review(broken, "r1", verdict)

        self.assertEqual(self.stored()[0], "")               # no ASR text
        self.assertEqual(self.fts(), [self.PLAUD_LOOP])      # index untouched
        self.assertEqual(self.review()[0], "queued")         # still to review

    def test_the_row_is_processing_during_the_call_and_ready_after(self):
        """A reader watching the archive sees the review happen."""
        self.candidate_row(plaud=self.PLAUD_LOOP)
        observer = sqlite3.connect(self.tenant.db_path)
        self.addCleanup(observer.close)
        seen = []

        def watching_call(name, args, sid, **kwargs):
            seen.append(observer.execute(
                "SELECT state FROM plaud_reviews WHERE id='r1'").fetchone()[0])
            return {"text": speech(90, "локально"), "engine_used": "large-v3",
                    "meta": {}}

        before = observer.execute(
            "SELECT state FROM plaud_reviews WHERE id='r1'").fetchone()[0]
        with mock.patch.object(self.asr, "call", side_effect=watching_call):
            self.asr.review_one(self.conn, "sid", "r1")
        after = observer.execute(
            "SELECT state, selected_source FROM plaud_reviews WHERE id='r1'"
        ).fetchone()

        self.assertEqual(before, "queued")
        self.assertEqual(seen, ["processing"])
        self.assertEqual(after, ("reviewed", "local"))

    def test_a_failing_review_leaves_the_plaud_text_on_screen(self):
        self.candidate_row(plaud=self.PLAUD_GOOD)

        with mock.patch.object(self.asr, "call",
                               side_effect=RuntimeError("ValueError: decode failed")):
            self.asr.review_next(self.conn, "sid", now=NOW)

        self.assertEqual(self.stored()[1], self.PLAUD_GOOD)
        self.assertEqual(self.fts(), [self.PLAUD_GOOD])
        state, source, _reason, _candidates = self.review()
        self.assertEqual((state, source), ("error", None))

    def test_review_next_takes_bounded_work_and_reports_it(self):
        for index in range(3):
            self.candidate_row(f"c{index}", plaud=self.PLAUD_LOOP)

        with self.local_result(speech(90, "локально")):
            reviewed = self.asr.review_next(self.conn, "sid", now=NOW)

        self.assertEqual(len(reviewed), self.asr.REVIEWS_PER_RUN)


class FailingConn:
    """A connection whose first matching statement fails, as a disk error would.

    Everything else passes straight through, so the rollback under test is the
    real one on the real connection.
    """

    def __init__(self, conn, on):
        self._conn = conn
        self._on = on

    def execute(self, sql, *args):
        if self._on in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


if __name__ == "__main__":
    unittest.main()
