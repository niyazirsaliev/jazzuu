"""Ingest → durable enqueue → immediate worker, with no downstream schedule.

These are the end-to-end properties of the corrected architecture. The PLAUD
poll is the only clock; from the moment audio lands on local disk, every further
step is committed as a row and driven by a worker that is woken in the same
pass. The tests deliberately never advance a clock between stages — if any
assertion here needs one, a scheduled gap has crept back in.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NOW = 1786_100_000


def load_module(filename, module_name, env=None):
    """Import an archive/ module under a controlled environment."""
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


class TenantIngest:
    """One tenant archive with archive_recording loaded against it."""

    def __init__(self, case, tenant_id="tenant-c"):
        self.dir = tempfile.mkdtemp()
        self.audio_dir = os.path.join(self.dir, "audio")
        os.makedirs(self.audio_dir)
        Path(self.dir, "plaud-token").write_text("caller-token")
        Path(self.dir, ".asr_token").write_text("asr-caller-token")
        env = {
            "TENANT_ID": tenant_id,
            "TENANT_ARCHIVE_DIR": self.dir,
            "PLAUD_MCP_TOKEN_FILE": os.path.join(self.dir, "plaud-token"),
            "PLAUD_MCP_TENANT_URLS_JSON": json.dumps({
                tenant_id: "https://mcp.example/mcp"}),
            "RECORDING_CODE_PREFIXES_JSON": json.dumps({tenant_id: "T"}),
            "PLAUD_MCP_EXPECTED_URL": None,
            "ARCHIVE_DB": None, "ASR_LOCK_PATH": None, "ASR_TOKEN_FILE": None,
        }
        self.archive = load_module(
            "archive_recording.py", f"archive_recording_{id(self)}", env)
        # The very same module object the ingest path and the stage handlers
        # use, not a second copy of it. Two instances of pipeline.py would give
        # two distinct StageDeferred classes, so `except StageDeferred` would
        # silently stop matching — which is exactly the bug a second copy
        # caused here, and would cause in a deployment that loaded it twice.
        self.pipeline = self.archive.pipeline
        self.db_path = os.path.join(self.dir, "archive.db")
        self.conn = sqlite3.connect(self.db_path)
        self.archive.init_db(self.conn)
        self.pipeline.ensure_schema(self.conn)
        case.addCleanup(self.conn.close)

    def audio_path(self, rec_id):
        return os.path.join(self.audio_dir, f"{rec_id}.mp3")

    def jobs(self):
        return [(row[0], row[1], row[2]) for row in self.conn.execute(
            "SELECT recording_id, stage, state FROM pipeline_jobs ORDER BY seq")]


def plaud_tools(meta, transcript="", note=""):
    """A fake PLAUD MCP: one metadata blob, one transcript, one note."""
    import json

    def call(name, args, sid, timeout=None):
        if name == "get_file":
            return json.dumps(meta)
        if name == "get_transcript":
            return json.dumps(
                {"segments": [{"content": transcript}] if transcript else []})
        if name == "get_note":
            return json.dumps(
                [{"data_type": "auto_sum_note", "data_content": note}])
        raise AssertionError(f"unexpected tool {name}")

    return call


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


class AtomicAudioTests(unittest.TestCase):
    """Audio appears in the archive whole, or it does not appear."""

    def setUp(self):
        self.tenant = TenantIngest(self)

    def test_an_interrupted_download_leaves_no_archived_audio(self):
        """Half a file at the canonical path is worse than no file. Every
        later stage — reconciliation, local ASR, the viewer's has_audio — reads
        presence and non-zero size as 'this recording is archived', so a
        truncated download would be transcribed as if it were the recording."""
        meta = {"name": "Взлом", "duration": 14_000,
                "presigned_url": "https://plaud.example/x.mp3"}

        def dying_download(url, path):
            Path(path).write_bytes(b"ID3 first half")
            raise OSError("connection reset by peer")

        with mock.patch.object(self.tenant.archive, "call",
                               side_effect=plaud_tools(meta)), \
                mock.patch.object(self.tenant.archive,
                                  "_download_to_file", side_effect=dying_download):
            with self.assertRaises(OSError):
                self.tenant.archive.archive_one(
                    self.tenant.conn, "sid", "rec1", False)

        self.assertFalse(os.path.exists(self.tenant.audio_path("rec1")))
        self.assertEqual(
            os.listdir(self.tenant.audio_dir), [],
            "a partial download must not be left behind under any name")

    def test_a_finished_download_is_renamed_into_place_atomically(self):
        """The file becomes visible at its final name only once it is whole:
        os.replace, not a write in place."""
        meta = {"name": "Готово", "duration": 14_000,
                "presigned_url": "https://plaud.example/x.mp3"}
        seen = []

        def download(url, path):
            seen.append(path)
            Path(path).write_bytes(b"ID3 whole file")

        with mock.patch.object(self.tenant.archive, "call",
                               side_effect=plaud_tools(meta, "Текст PLAUD")), \
                mock.patch.object(self.tenant.archive,
                                  "_download_to_file", side_effect=download):
            self.tenant.archive.archive_one(
                self.tenant.conn, "sid", "rec1", False)

        self.assertNotEqual(seen, [])
        self.assertNotEqual(seen[0], self.tenant.audio_path("rec1"),
                            "the download must not write the canonical path")
        self.assertEqual(os.path.dirname(seen[0]), self.tenant.audio_dir,
                         "the temp file must share the directory, or the "
                         "rename is a cross-device copy and not atomic")
        self.assertEqual(Path(self.tenant.audio_path("rec1")).read_bytes(),
                         b"ID3 whole file")


class UntrustedFileIdTests(unittest.TestCase):
    """A PLAUD file id is remote input, not a path fragment.

    Every id in the system arrives from `list_files` on an account this process
    does not control, and it is then interpolated into a filename, a DB key and
    an FTS row. Containing it at the boundary is the only place that is cheap;
    after that it has already been joined onto the audio directory.
    """

    def setUp(self):
        self.tenant = TenantIngest(self)
        self.outside = os.path.join(os.path.dirname(self.tenant.audio_dir),
                                    "escaped.mp3")
        self.addCleanup(
            lambda: os.path.exists(self.outside) and os.remove(self.outside))

    def archive(self, fid):
        meta = {"name": "Запись", "duration": 14_000,
                "presigned_url": "https://plaud.example/x.mp3"}

        def download(url, path):
            Path(path).write_bytes(b"ID3 escaped")

        with mock.patch.object(self.tenant.archive, "call",
                               side_effect=plaud_tools(meta)), \
                mock.patch.object(self.tenant.archive,
                                  "_download_to_file", side_effect=download):
            return self.tenant.archive.archive_one(
                self.tenant.conn, "sid", fid, False)

    def test_a_traversing_file_id_writes_nothing_anywhere(self):
        """`../../escaped` joined onto the audio directory resolves outside the
        tenant archive entirely — and the temp file, being created in the
        derived directory, escapes with it."""
        with self.assertRaises(self.tenant.archive.UnsafeRecordingId):
            self.archive("../../escaped")

        self.assertFalse(os.path.exists(self.outside))
        self.assertEqual(os.listdir(self.tenant.audio_dir), [])

    def test_a_rejected_id_never_reaches_the_database(self):
        """Rejection happens before any read or write, so a hostile id cannot
        create a recordings row, an FTS entry or a job either."""
        with self.assertRaises(self.tenant.archive.UnsafeRecordingId):
            self.archive("../../escaped")

        self.assertEqual(
            self.tenant.conn.execute("SELECT COUNT(*) FROM recordings")
            .fetchone()[0], 0)
        self.assertEqual(self.tenant.jobs(), [])

    def test_the_shapes_that_are_refused(self):
        refused = ["../../escaped", "a/b", "a\\b", "..", ".", "",
                   "with space", "x" * 200, "nul\x00byte", "a;rm -rf",
                   "%2e%2e/x", "ünïcode"]
        for fid in refused:
            with self.subTest(fid=fid):
                self.assertFalse(self.tenant.archive.is_safe_recording_id(fid))

    def test_real_plaud_ids_are_still_accepted(self):
        """The containment must not reject the ids production actually sees:
        PLAUD's own object ids, and the viewer's `[A-Za-z0-9_-]+` route rule."""
        for fid in ("689e0b1c2d3e4f5061728394", "abc-DEF_123", "0", "a" * 128):
            with self.subTest(fid=fid):
                self.assertTrue(self.tenant.archive.is_safe_recording_id(fid))

    def test_discovery_skips_a_hostile_id_and_keeps_archiving_the_rest(self):
        """One malformed id in a listing must not stop the pass: the other
        recordings in that page are real work."""
        seen = self.tenant.archive.safe_recording_ids(
            ["good1", "../../escaped", "good2"])

        self.assertEqual(seen, ["good1", "good2"])


class EnqueueOnIngestTests(unittest.TestCase):
    """The recording row and its first job are one commit."""

    def setUp(self):
        self.tenant = TenantIngest(self)

    def archive_one(self, rec_id="rec1", transcript="", duration=14_000):
        meta = {"name": "Запись", "duration": duration,
                "presigned_url": "https://plaud.example/x.mp3"}

        def download(url, path):
            Path(path).write_bytes(b"ID3 whole file")

        with mock.patch.object(self.tenant.archive, "call",
                               side_effect=plaud_tools(meta, transcript)), \
                mock.patch.object(self.tenant.archive,
                                  "_download_to_file", side_effect=download):
            return self.tenant.archive.archive_one(
                self.tenant.conn, "sid", rec_id, False)

    def test_archiving_a_recording_commits_its_asr_job(self):
        """No cron between the audio landing and the work being owed."""
        self.archive_one()

        self.assertEqual(self.tenant.jobs(), [("rec1", "asr", "queued")])

    def test_a_plaud_transcript_does_not_complete_asr(self):
        """PLAUD text arrives with the recording and is immediately readable —
        but it is provisional. It still gets a validation ASR job, because a
        truncated or looping PLAUD transcript is exactly what nobody notices."""
        self.archive_one(transcript="Расшифровка от PLAUD.")

        row = self.tenant.conn.execute(
            "SELECT plaud_transcript, COALESCE(asr_transcript,'') "
            "FROM recordings WHERE id='rec1'").fetchone()
        self.assertEqual(row, ("Расшифровка от PLAUD.", ""))
        self.assertEqual(self.tenant.jobs(), [("rec1", "asr", "queued")])

    def test_later_discovery_repairs_metadata_only_recording_before_asr_enqueue(self):
        """The first metadata response has no downloadable URL; the next one
        does. The durable row is retained, but only the second pass that wrote
        tenant-local audio may enqueue ASR."""
        absent_url = {"name": "Запись", "duration": 14_000}
        with mock.patch.object(self.tenant.archive, "call",
                               side_effect=plaud_tools(absent_url)):
            self.tenant.archive.archive_one(self.tenant.conn, "sid", "late", False)
        self.assertFalse(os.path.exists(self.tenant.audio_path("late")))
        self.assertEqual(self.tenant.jobs(), [])

        available_url = {"name": "Запись", "duration": 14_000,
                         "presigned_url": "https://plaud.example/late.mp3"}
        def download(_url, path):
            Path(path).write_bytes(b"ID3 complete")
        with mock.patch.object(self.tenant.archive, "call",
                               side_effect=plaud_tools(available_url)), \
                mock.patch.object(self.tenant.archive,
                                  "_download_to_file", side_effect=download):
            self.tenant.archive.archive_one(self.tenant.conn, "sid", "late", False)
        self.assertTrue(os.path.exists(self.tenant.audio_path("late")))
        self.assertEqual(self.tenant.jobs(), [("late", "asr", "queued")])

    def test_no_mode_enqueues_asr_until_tenant_local_audio_exists(self):
        """The queue never delegates an absent local file to a remote account,
        including deployments that happen to use the owner configuration."""
        metadata = {"name": "No audio", "duration": 14_000}
        with mock.patch.object(self.tenant.archive, "TENANT",
                               mock.Mock(is_owner=True)), \
                mock.patch.object(self.tenant.archive, "call",
                                  side_effect=plaud_tools(metadata)):
            self.tenant.archive.archive_one(self.tenant.conn, "sid", "no-audio", False)
        self.assertFalse(os.path.exists(self.tenant.audio_path("no-audio")))
        self.assertEqual(self.tenant.jobs(), [])

    def test_owner_enqueues_after_local_audio_is_archived(self):
        """Owner mode retains the same local-audio queue contract."""
        with mock.patch.object(self.tenant.archive, "TENANT",
                               mock.Mock(is_owner=True)):
            self.archive_one("owner-local")

        self.assertEqual(self.tenant.jobs(), [("owner-local", "asr", "queued")])

    def test_a_failed_job_write_takes_the_recording_row_with_it(self):
        """Row and job are one transaction. A recording row with no job is a
        recording nothing will ever process, and nothing would notice."""
        broken = FailingConn(self.tenant.conn, on="INSERT INTO pipeline_jobs")
        real_conn, self.tenant.conn = self.tenant.conn, broken
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.archive_one()
        finally:
            self.tenant.conn = real_conn

        self.assertIsNone(self.tenant.conn.execute(
            "SELECT id FROM recordings WHERE id='rec1'").fetchone())
        self.assertEqual(self.tenant.jobs(), [])

    def test_re_archiving_an_already_archived_recording_repairs_its_job(self):
        """The skip path is the one an interrupted pass comes back through.
        It must still leave the queue correct, and must not create a second
        job for work already in flight."""
        self.archive_one()
        self.tenant.conn.execute("DELETE FROM pipeline_jobs")
        self.tenant.conn.commit()

        self.archive_one()   # audio + row already there: the 'skip' path

        self.assertEqual(self.tenant.jobs(), [("rec1", "asr", "queued")])
        self.archive_one()
        self.assertEqual(self.tenant.jobs(), [("rec1", "asr", "queued")])


class StageChainTests(unittest.TestCase):
    """What each durable stage actually does, and what it hands on.

    The doubles here stand in for asr-mcp only. Everything about ordering,
    persistence and what the reader ends up seeing is the real code.
    """

    PLAUD_GOOD = " ".join(f"плауд{i}" for i in range(60)) + "."
    PLAUD_LOOP = " ".join(["да"] * 60)
    LOCAL_GOOD = " ".join(f"локально{i}" for i in range(90)) + "."

    def setUp(self):
        self.tenant = TenantIngest(self)
        self.stages = load_module("stages.py", f"stages_{id(self)}")
        self.asr = load_module(
            "asr_backfill.py", "asr_backfill",
            {"TENANT_ID": "tenant-c", "TENANT_ARCHIVE_DIR": self.tenant.dir,
             "ARCHIVE_DB": None, "ASR_TOKEN_FILE": None,
             "ASR_AUDIO_URL_BASE": None})
        self.summary = load_module(
            "summary_backfill.py", "summary_backfill",
            {"TENANT_ID": "tenant-c", "TENANT_ARCHIVE_DIR": self.tenant.dir,
             "ARCHIVE_DB": None, "ASR_TOKEN_FILE": None})
        self.asr_calls = []
        self.summary_calls = []

    def recording(self, rec_id="r1", plaud="", duration_ms=14_000):
        self.tenant.conn.execute(
            """INSERT INTO recordings(id,name,start_at,archived_at,duration_ms,
                 plaud_transcript,asr_transcript,audio_path)
               VALUES(?,?,?,?,?,?,'',?)""",
            (rec_id, f"rec {rec_id}", "2026-08-10T11:00:00",
             "2026-08-10T11:00:00", duration_ms, plaud,
             self.tenant.audio_path(rec_id)))
        self.tenant.conn.execute(
            "INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)",
            (rec_id, f"rec {rec_id}", plaud))
        self.tenant.conn.commit()
        Path(self.tenant.audio_path(rec_id)).write_bytes(b"ID3fake")
        self.tenant.pipeline.enqueue(self.tenant.conn, rec_id,
                                     self.tenant.pipeline.STAGE_ASR, now=NOW)

    def run_chain_at(self, now, **kwargs):
        return self.run_chain(now=now, **kwargs)

    def run_chain(self, local=LOCAL_GOOD, alternative="", summary_ok=True,
                  max_jobs=None, now=NOW):
        import json

        def asr_call(name, args, sid, **kwargs):
            self.asr_calls.append((name, dict(args)))
            return {"text": local, "engine_used": "large-v3",
                    "meta": {"detected_lang": "ru",
                             "alternative": {"engine": "kyrgyz",
                                             "text": alternative}}}

        def summary_call(name, args, sid, **kwargs):
            self.summary_calls.append((name, dict(args)))
            if not summary_ok:
                raise RuntimeError("summary tool exploded")
            return {"summary": "## Итог\n\nОбсудили планы.",
                    "summary_json": json.dumps(
                        {"overview": "Обсудили планы.",
                         "themes": ["планы"], "action_items": ["позвонить"]},
                        ensure_ascii=False)}

        handlers = self.stages.build_handlers(
            asr_module=self.asr, summary_module=self.summary,
            session=lambda module: "sid")
        with mock.patch.object(self.asr, "call", side_effect=asr_call), \
                mock.patch.object(self.summary, "call", side_effect=summary_call):
            return self.tenant.pipeline.drain(
                self.tenant.conn, handlers, now=now, max_jobs=max_jobs)

    def stored(self, rec_id="r1"):
        return self.tenant.conn.execute(
            "SELECT COALESCE(asr_transcript,''), COALESCE(plaud_transcript,''),"
            " COALESCE(summary,''), COALESCE(summary_json,'') "
            "FROM recordings WHERE id=?", (rec_id,)).fetchone()

    def fts(self, rec_id="r1"):
        return [row[0] for row in self.tenant.conn.execute(
            "SELECT transcript FROM recordings_fts WHERE id=?", (rec_id,))]

    def test_a_textless_recording_runs_the_whole_chain_from_one_wake(self):
        """Nothing advances a clock here. Local ASR publishes the transcript,
        which enqueues the Russian summary, which enqueues the mind map, which
        enqueues the card — each committed by the stage before it."""
        self.recording()

        self.run_chain()

        asr_text, _plaud, summary, summary_json = self.stored()
        self.assertEqual(asr_text, self.LOCAL_GOOD)
        self.assertEqual(self.fts(), [self.LOCAL_GOOD])
        self.assertIn("Итог", summary)
        self.assertIn("overview", summary_json)
        self.assertEqual(self.tenant.jobs(),
                         [("r1", "asr", "done"), ("r1", "summary", "done"),
                          ("r1", "mindmap", "done"), ("r1", "card", "done")])
        self.assertTrue(self.tenant.pipeline.is_ready(self.tenant.conn, "r1"))

    def test_local_asr_reads_the_tenant_file_never_the_plaud_account(self):
        self.recording()

        self.run_chain()

        self.assertEqual([name for name, _ in self.asr_calls], ["transcribe_url"])
        self.assertNotIn("file_id", self.asr_calls[0][1])
        self.assertEqual(self.asr_calls[0][1]["url"],
                         Path(self.tenant.audio_path("r1")).as_uri())

    def test_a_provisional_plaud_transcript_is_validated_and_kept_when_it_wins(self):
        """PLAUD text is readable from the moment it is archived, and stays
        exactly as it is when it survives validation — but it is validated, and
        the losing local hypothesis is kept with the score that rejected it."""
        self.recording(plaud=self.PLAUD_GOOD)

        self.run_chain(local=" ".join(f"локально{i}" for i in range(60)) + ".")

        asr_text, plaud_text, _summary, _json = self.stored()
        self.assertEqual(plaud_text, self.PLAUD_GOOD)   # never overwritten
        self.assertEqual(asr_text, "")                  # the loser is not shown
        self.assertEqual(self.fts(), [self.PLAUD_GOOD])
        state, source, reason, candidates = self.tenant.conn.execute(
            "SELECT state, selected_source, reason, candidates_json "
            "FROM plaud_reviews WHERE id='r1'").fetchone()
        self.assertEqual((state, source, reason),
                         ("reviewed", "plaud", "plaud-adequate"))
        import json
        kept = json.loads(candidates)
        self.assertEqual([c["source"] for c in kept],
                         ["plaud", "local", "local-alternative"])
        # And the chain carried on from the SELECTED transcript.
        self.assertTrue(self.tenant.pipeline.is_ready(self.tenant.conn, "r1"))
        self.assertIn(self.PLAUD_GOOD, self.summary_calls[0][1]["transcript"])

    def test_a_degenerate_plaud_transcript_loses_to_local_asr(self):
        self.recording(plaud=self.PLAUD_LOOP)

        self.run_chain(local=self.LOCAL_GOOD)

        asr_text, plaud_text, _summary, _json = self.stored()
        self.assertEqual(asr_text, self.LOCAL_GOOD)
        self.assertEqual(plaud_text, self.PLAUD_LOOP)   # source artifact kept
        self.assertEqual(self.fts(), [self.LOCAL_GOOD])
        self.assertEqual(
            self.tenant.conn.execute(
                "SELECT selected_source, reason FROM plaud_reviews WHERE id='r1'"
            ).fetchone(),
            ("local", "local-clearly-better"))
        self.assertIn(self.LOCAL_GOOD, self.summary_calls[0][1]["transcript"])

    def test_a_summary_failure_never_sends_the_recording_back_through_asr(self):
        """Stage independence at the real handlers. The transcript is published
        and must stay published; only the summary is owed another try."""
        self.recording()

        self.run_chain(summary_ok=False)

        self.assertEqual(self.stored()[0], self.LOCAL_GOOD)
        self.assertEqual(
            self.tenant.jobs(),
            [("r1", "asr", "done"), ("r1", "summary", "retry_wait")])
        self.assertEqual([name for name, _ in self.asr_calls], ["transcribe_url"])

        # The retry runs the summary, and only the summary. This is the one
        # place a later clock is legitimate: a stage that just failed backs off
        # before its next attempt, which is a retry policy, not a stage gap.
        self.run_chain_at(NOW + 3600)
        self.assertEqual([name for name, _ in self.asr_calls], ["transcribe_url"])
        self.assertTrue(self.tenant.pipeline.is_ready(self.tenant.conn, "r1"))

    def test_a_recording_too_short_to_summarise_still_reaches_ready(self):
        """A ten-second clip has no summary, so it has no mind map and no card.
        It must end the chain cleanly rather than retry a stage that can never
        succeed, or it would sit in retry_wait forever."""
        self.recording()

        self.run_chain(local="Ага.")

        self.assertEqual(self.summary_calls, [])
        self.assertEqual(self.tenant.jobs(),
                         [("r1", "asr", "done"), ("r1", "summary", "done")])
        self.assertTrue(self.tenant.pipeline.is_ready(self.tenant.conn, "r1"))

    def test_the_plaud_grace_period_never_delays_a_queued_job(self):
        """asr_backfill.pending() holds a fresh recording back for an hour in
        case PLAUD delivers a transcript for free. That is a discovery-side
        heuristic; once a job is committed the pipeline owes the work NOW."""
        self.tenant.conn.execute(
            """INSERT INTO recordings(id,name,archived_at,duration_ms,
                 plaud_transcript,asr_transcript,audio_path)
               VALUES('fresh','fresh',?,14000,'','',?)""",
            (time.strftime("%Y-%m-%dT%H:%M:%S"),
             self.tenant.audio_path("fresh")))
        self.tenant.conn.commit()
        Path(self.tenant.audio_path("fresh")).write_bytes(b"ID3fake")
        self.tenant.pipeline.enqueue(self.tenant.conn, "fresh",
                                     self.tenant.pipeline.STAGE_ASR, now=NOW)

        self.assertEqual(self.asr.pending(self.tenant.conn), [],
                         "precondition: the cron heuristic would hold this back")
        self.run_chain()

        self.assertEqual(self.stored("fresh")[0], self.LOCAL_GOOD)

    def test_a_long_job_stops_at_the_window_where_it_loses_its_claim(self):
        """Duplicate GPU work, prevented where it is still preventable.

        Fenced writes stop a stale worker PUBLISHING, but by then it has
        already spent the hours. A long transcription checkpoints between
        windows, and that checkpoint is where it has to learn its claim was
        reissued — otherwise two workers transcribe the same four hours.
        """
        self.recording(duration_ms=90 * 60 * 1000)
        windows = []

        real_record = self.asr.record_segment

        def steal_after_first_window(conn, rid, index, start, duration,
                                     text, engine, meta, alternative=''):
            real_record(conn, rid, index, start, duration, text, engine, meta,
                        alternative)
            windows.append(index)
            if index == 0:
                # Another worker takes the job while window two is being asked
                # for, exactly as an expired lease followed by recover_stale
                # would.
                other = sqlite3.connect(self.tenant.db_path)
                other.execute(
                    "UPDATE pipeline_jobs SET claim_owner='other:1', "
                    "claim_epoch=? WHERE recording_id=? AND stage='asr'",
                    (NOW + 1, rid))
                other.commit()
                other.close()

        with mock.patch.object(self.asr, "SEGMENTS_PER_RUN", 0), \
                mock.patch.object(self.asr, "record_segment",
                                  side_effect=steal_after_first_window):
            self.run_chain()

        self.assertEqual(windows, [0],
                         "the run must stop at the checkpoint, not transcribe "
                         "every remaining window of somebody else's job")
        # Nothing was recorded against the new owner's job.
        self.assertEqual(self.tenant.jobs(), [("r1", "asr", "processing")])
        self.assertEqual(
            self.tenant.conn.execute(
                "SELECT attempts, claim_owner FROM pipeline_jobs").fetchone(),
            (0, "other:1"))
        # The window it did finish is still committed: real work is never lost.
        self.assertEqual(
            self.tenant.conn.execute(
                "SELECT COUNT(*) FROM asr_segments WHERE id='r1' AND "
                "status='complete'").fetchone()[0], 1)

    def test_a_long_recording_checkpoints_and_keeps_its_place(self):
        """Windows are at most half an hour and each one is committed. The job
        hands the queue back between windows without a strike, so a nine-window
        recording cannot look like nine failures, and a crash resumes from the
        last committed window."""
        self.recording(duration_ms=90 * 60 * 1000)

        with mock.patch.object(self.asr, "SEGMENTS_PER_RUN", 1), \
                mock.patch.object(self.asr, "CHAIN_IDLE_SEGMENTS", False):
            self.run_chain()

        windows = self.tenant.conn.execute(
            "SELECT duration_sec, status FROM asr_segments WHERE id='r1' "
            "ORDER BY seg_index").fetchall()
        self.assertEqual(len(windows), 9)
        self.assertTrue(all(duration <= 600 for duration, _s in windows))
        self.assertEqual(windows[0][1], "complete")
        self.assertEqual(self.tenant.jobs(), [("r1", "asr", "queued")])
        self.assertEqual(
            self.tenant.conn.execute(
                "SELECT attempts FROM pipeline_jobs WHERE stage='asr'"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
