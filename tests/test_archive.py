import builtins
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(filename, module_name, env=None):
    """Import an archive/ module, optionally under a controlled environment.

    These modules read their paths and credentials at import time, so `env` is
    the only way to see what a run would actually do in a tenant container. A
    value of None removes the variable, which is how a test says "the host does
    not export this" — the machine running the suite may well export it.
    """
    path = ROOT / "archive" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    env = env or {}
    with mock.patch.dict(
            os.environ, {k: v for k, v in env.items() if v is not None},
            clear=False):
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
        spec.loader.exec_module(module)
    return module


def load_archive_module():
    path = ROOT / "archive" / "archive_recording.py"
    spec = importlib.util.spec_from_file_location("archive_recording_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    real_open = builtins.open

    def token_open(name, *args, **kwargs):
        if str(name).endswith(".plaud_token"):
            return mock.mock_open(read_data="test-token").return_value
        return real_open(name, *args, **kwargs)

    with mock.patch("builtins.open", side_effect=token_open), \
         mock.patch.dict(os.environ, {
             "RECORDING_CODE_PREFIXES_JSON": '{"default":"N"}'}, clear=False):
        spec.loader.exec_module(module)
    return module


class ArchiveSchemaTests(unittest.TestCase):
    def test_init_db_migrates_existing_recordings_with_derived_columns(self):
        archive = load_archive_module()
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, name TEXT, summary TEXT)")

        archive.init_db(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        self.assertTrue(
            {"summary_json", "asr_meta_json", "asr_alternative_transcript"} <= columns
        )

    def test_force_rearchive_preserves_derived_asr_and_summary_fields(self):
        archive = load_archive_module()
        conn = sqlite3.connect(":memory:")
        archive.init_db(conn)
        conn.execute(
            """INSERT INTO recordings(
                id,name,asr_engine,asr_transcript,summary,summary_json,
                asr_meta_json,asr_alternative_transcript,audio_path
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "rec1", "Old", "whisper", "chosen transcript", "Generated summary",
                '{"overview":"Structured"}', '{"route_reason":"language"}',
                "alternative transcript", "/missing.mp3",
            ),
        )
        conn.commit()
        responses = {
            "get_file": json.dumps({"name": "New", "duration": 1234}),
            "get_transcript": json.dumps({"source_list": []}),
            "get_note": json.dumps({"note_list": []}),
        }
        with mock.patch.object(archive, "call", side_effect=lambda name, args, sid: responses[name]), \
             mock.patch.object(archive, "AUDIO", tempfile.mkdtemp()):
            archive.archive_one(conn, "sid", "rec1", force=True)

        row = conn.execute(
            """SELECT asr_engine,asr_transcript,summary,summary_json,
                      asr_meta_json,asr_alternative_transcript
               FROM recordings WHERE id='rec1'"""
        ).fetchone()
        self.assertEqual(
            row,
            (
                "whisper", "chosen transcript", "Generated summary",
                '{"overview":"Structured"}', '{"route_reason":"language"}',
                "alternative transcript",
            ),
        )
        fts = conn.execute(
            "SELECT transcript FROM recordings_fts WHERE id='rec1'"
        ).fetchone()[0]
        self.assertEqual(fts, "chosen transcript")


class AsrBackfillTests(unittest.TestCase):
    def test_transcribe_stores_meta_alternative_and_indexes_chosen_transcript(self):
        archive = load_archive_module()
        asr = load_module("asr_backfill.py", "asr_backfill_test")
        conn = sqlite3.connect(":memory:")
        archive.init_db(conn)
        asr.ensure_attempts(conn)
        conn.execute("INSERT INTO recordings(id,name) VALUES('rec1','Meeting')")
        conn.commit()
        result = {
            "text": "chosen transcript",
            "engine_used": "whisper-large",
            "meta": {
                "detected_lang": "ru",
                "selected_engine": "whisper-large",
                "route_reason": "cyrillic detected",
                "alternative": {"engine": "kyrgyz", "text": "alternative transcript"},
            },
        }

        with mock.patch.object(asr, "local_audio_path", return_value="/tmp/rec1.mp3"), \
             mock.patch.object(asr, "call", return_value=result):
            asr.transcribe(conn, "sid", "rec1", "auto")

        row = conn.execute(
            """SELECT asr_transcript,asr_engine,lang,asr_meta_json,
                      asr_alternative_transcript FROM recordings WHERE id='rec1'"""
        ).fetchone()
        self.assertEqual(row[:3], ("chosen transcript", "whisper-large", "ru"))
        self.assertEqual(json.loads(row[3]), {
            "detected_lang": "ru",
            "selected_engine": "whisper-large",
            "route_reason": "cyrillic detected",
            "alternative": {"engine": "kyrgyz"},
        })
        self.assertEqual(row[4], "alternative transcript")
        self.assertEqual(
            conn.execute("SELECT transcript FROM recordings_fts WHERE id='rec1'").fetchone()[0],
            "chosen transcript",
        )


class AsrRuntimePathTests(unittest.TestCase):
    """Where a standalone run reads, locks and authenticates.

    connector.py hands asr_backfill its own connection, but the script is also
    run directly inside a tenant container — cron, a manual rerun of one id.
    Those runs used the paths compiled into the module, which are the owner's:
    the owner's archive.db got the tenant's transcripts, the owner's lockfile
    serialised an unrelated container, and the owner's ASR caller token
    authenticated somebody else's account.
    """

    HERE = ROOT / "archive"

    def test_default_endpoint_is_the_russian_summary_service(self):
        asr = load_module(
            "asr_backfill.py", "asr_backfill_default_url",
            {"ASR_MCP_URL": None},
        )
        self.assertEqual(asr.URL, "http://127.0.0.1:62362/mcp")

    def paths(self, env):
        asr = load_module("asr_backfill.py", "asr_backfill_paths")
        return asr.runtime_paths(env)

    def test_owner_layout_is_unchanged_when_no_path_env_is_set(self):
        db, lock, token = self.paths({})
        self.assertEqual(db, str(self.HERE / "archive.db"))
        self.assertEqual(lock, str(self.HERE / ".connector.lock"))
        self.assertEqual(token, str(self.HERE / ".asr_token"))

    def test_explicit_paths_win_individually(self):
        db, lock, token = self.paths({
            "ARCHIVE_DB": "/tenants/tenant-c/archive.db",
            "ASR_LOCK_PATH": "/run/asr-tenant-c.lock",
            "ASR_TOKEN_FILE": "/tenants/tenant-c/asr-token",
        })
        self.assertEqual(db, "/tenants/tenant-c/archive.db")
        self.assertEqual(lock, "/run/asr-tenant-c.lock")
        self.assertEqual(token, "/tenants/tenant-c/asr-token")

    def test_tenant_env_names_move_all_three_paths_together(self):
        # The connector deployment sets these and nothing else; the lock and
        # the caller token must follow the archive, not stay behind with the
        # owner's, or two tenants share one lock and one credential.
        db, lock, token = self.paths({
            "TENANT_ID": "tenant-b", "TENANT_ARCHIVE_DIR": "/tenants/tenant-b",
        })
        self.assertEqual(db, "/tenants/tenant-b/archive.db")
        self.assertEqual(lock, "/tenants/tenant-b/.connector.lock")
        self.assertEqual(token, "/tenants/tenant-b/.asr_token")
        for path in (db, lock, token):
            self.assertNotIn(str(self.HERE), path)

    def test_an_explicit_db_alone_still_moves_the_lock_and_token_with_it(self):
        db, lock, token = self.paths({"ARCHIVE_DB": "/tenants/tenant-d/archive.db"})
        self.assertEqual(lock, "/tenants/tenant-d/.connector.lock")
        self.assertEqual(token, "/tenants/tenant-d/.asr_token")
        self.assertEqual(db, "/tenants/tenant-d/archive.db")

    def test_a_tenant_without_an_archive_dir_refuses_to_use_the_owner_archive(self):
        # Defaulting here would write one family member's transcripts into the
        # owner's archive, and nothing about that is repairable by a rerun.
        with self.assertRaises(SystemExit) as caught:
            self.paths({"TENANT_ID": "tenant-b"})
        self.assertIn("TENANT_ARCHIVE_DIR", str(caught.exception))

    def test_the_paths_the_run_actually_uses_come_from_the_environment(self):
        # main() locks LOCK and opens DB; runtime_paths is only interesting if
        # those module-level values are what it resolved.
        tenant = tempfile.mkdtemp()
        Path(tenant, ".asr_token").write_text("tenant-caller-token")
        asr = load_module(
            "asr_backfill.py", "asr_backfill_tenant_paths",
            {"TENANT_ID": "tenant-b", "TENANT_ARCHIVE_DIR": tenant},
        )
        self.assertEqual(asr.DB, os.path.join(tenant, "archive.db"))
        self.assertEqual(asr.LOCK, os.path.join(tenant, ".connector.lock"))
        self.assertEqual(asr.TOKEN_FILE, os.path.join(tenant, ".asr_token"))
        # And the token really is read from there, not from the owner's file.
        self.assertEqual(asr.TOKEN, "tenant-caller-token")

    def test_the_owner_run_still_resolves_beside_the_scripts(self):
        asr = load_module("asr_backfill.py", "asr_backfill_owner_paths")
        self.assertEqual(asr.DB, str(self.HERE / "archive.db"))
        self.assertEqual(asr.LOCK, str(self.HERE / ".connector.lock"))
        self.assertEqual(asr.TOKEN_FILE, str(self.HERE / ".asr_token"))


class SummaryBackfillTests(unittest.TestCase):
    def test_tenant_paths_and_token_follow_its_archive_not_owner_environment(self):
        tenant = tempfile.mkdtemp()
        Path(tenant, ".asr_token").write_text("tenant-b-caller-token")
        summary = load_module(
            "summary_backfill.py", "summary_tenant_isolation",
            {"TENANT_ID": "tenant-b", "TENANT_ARCHIVE_DIR": tenant,
             "ARCHIVE_DB": None, "SUMMARY_LOCK": None,
             "ASR_TOKEN_FILE": None, "ASR_MCP_TOKEN": "owner-token"},
        )
        self.assertEqual(summary.DB, os.path.join(tenant, "archive.db"))
        self.assertEqual(summary.LOCK, os.path.join(tenant, ".connector.lock"))
        self.assertEqual(summary.TOKEN_FILE, os.path.join(tenant, ".asr_token"))
        self.assertEqual(summary.TOKEN, "tenant-b-caller-token")

    def test_tenant_without_its_own_token_ignores_owner_token(self):
        tenant = tempfile.mkdtemp()
        summary = load_module(
            "summary_backfill.py", "summary_tenant_no_token",
            {"TENANT_ID": "tenant-b", "TENANT_ARCHIVE_DIR": tenant,
             "ARCHIVE_DB": None, "SUMMARY_LOCK": None,
             "ASR_TOKEN_FILE": None, "ASR_MCP_TOKEN": "owner-token"},
        )
        self.assertEqual(summary.TOKEN, "")

    def test_tenant_without_archive_dir_fails_closed(self):
        with self.assertRaises(SystemExit):
            load_module(
                "summary_backfill.py", "summary_tenant_missing_archive",
                {"TENANT_ID": "tenant-b", "TENANT_ARCHIVE_DIR": None,
                 "ARCHIVE_DB": None},
            )

    def test_default_endpoint_is_the_russian_summary_service(self):
        summary = load_module(
            "summary_backfill.py", "summary_backfill_default_url",
            {"ASR_MCP_URL": None},
        )
        self.assertEqual(summary.URL, "http://127.0.0.1:62362/mcp")

    def test_runtime_paths_can_target_an_isolated_tenant_archive(self):
        summary = load_module("summary_backfill.py", "summary_backfill_paths_test")
        db, lock, token = summary.runtime_paths({
            "ARCHIVE_DB": "/tenants/tenant-c/archive.db",
            "SUMMARY_LOCK": "/tenants/tenant-c/.summary.lock",
            "ASR_TOKEN_FILE": "/tenants/tenant-c/asr-token",
        })
        self.assertEqual(db, "/tenants/tenant-c/archive.db")
        self.assertEqual(lock, "/tenants/tenant-c/.summary.lock")
        self.assertEqual(token, "/tenants/tenant-c/asr-token")

    def test_backfill_stores_markdown_and_structured_json(self):
        archive = load_archive_module()
        summary = load_module("summary_backfill.py", "summary_backfill_test")
        conn = sqlite3.connect(":memory:")
        archive.init_db(conn)
        transcript = "Важное обсуждение проекта. " * 12
        conn.execute(
            "INSERT INTO recordings(id,name,asr_transcript) VALUES(?,?,?)",
            ("rec1", "План проекта", transcript),
        )
        conn.commit()
        structured = {
            "overview": "Обсудили запуск",
            "themes": [{"title": "Сроки", "summary": "Запуск в сентябре"}],
            "key_facts": [{"label": "Бюджет", "value": "₽2 млн"}],
            "decisions": ["Запустить пилот"],
            "risks": ["Сжатые сроки"],
            "action_items": [{"task": "Подготовить план", "owner": "Анна"}],
        }
        with mock.patch.object(
            summary, "call", return_value={"result": {"summary": "# Итоги\nЗапуск согласован", "summary_json": structured}}
        ) as invoke:
            summary.backfill_one(conn, "sid", "rec1")

        args = invoke.call_args.args[1]
        self.assertEqual(args["transcript"], transcript.strip())
        self.assertEqual(args["title"], "План проекта")
        row = conn.execute(
            "SELECT summary,summary_json FROM recordings WHERE id='rec1'"
        ).fetchone()
        self.assertEqual(row[0], "# Итоги\nЗапуск согласован")
        self.assertEqual(json.loads(row[1]), structured)

    def test_pending_requires_200_chars_and_any_missing_summary_field(self):
        archive = load_archive_module()
        summary = load_module("summary_backfill.py", "summary_backfill_pending_test")
        conn = sqlite3.connect(":memory:")
        archive.init_db(conn)
        summary.ensure_attempts(conn)
        long_text = "x" * 200
        rows = [
            ("short", "Short", "x" * 199, None, None),
            ("missing_both", "Both", long_text, None, None),
            ("missing_json", "JSON", long_text, "markdown", None),
            ("complete", "Complete", long_text, "markdown", '{}'),
        ]
        conn.executemany(
            "INSERT INTO recordings(id,name,asr_transcript,summary,summary_json) VALUES(?,?,?,?,?)",
            rows,
        )
        conn.commit()

        self.assertEqual(
            [row[0] for row in summary.pending(conn)],
            ["missing_both", "missing_json"],
        )

    def test_structured_backfill_preserves_existing_plaud_markdown(self):
        archive = load_archive_module()
        summary = load_module("summary_backfill.py", "summary_backfill_preserve_test")
        conn = sqlite3.connect(":memory:")
        archive.init_db(conn)
        transcript = "Содержательная расшифровка встречи. " * 10
        conn.execute(
            "INSERT INTO recordings(id,name,asr_transcript,summary) VALUES(?,?,?,?)",
            ("rec1", "Встреча", transcript, "Исходное резюме PLAUD"),
        )
        conn.commit()
        with mock.patch.object(
            summary, "call", return_value={
                "summary_markdown": "Новое резюме LLM",
                "structured": {"overview": "Структурированные данные"},
            },
        ):
            summary.backfill_one(conn, "sid", "rec1")

        row = conn.execute(
            "SELECT summary,summary_json FROM recordings WHERE id='rec1'"
        ).fetchone()
        self.assertEqual(row[0], "Исходное резюме PLAUD")
        self.assertEqual(json.loads(row[1]), {"overview": "Структурированные данные"})


class TransientFailureTests(unittest.TestCase):
    """An asr-mcp restart during a long job must not retire the recording.

    A 4h22m recording burned all three attempts on `unexpected mcp response:
    null` — the container was recreated mid-call. Once attempts hit the cap,
    pending() skips the row forever and no cron pass ever retries it, so a
    perfectly transcribable recording is lost to an unrelated redeploy.
    """

    def setUp(self):
        self.mod = load_module("asr_backfill.py", "asr_backfill_transient")
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript('''
            CREATE TABLE recordings(
              id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
              duration_ms INTEGER, plaud_transcript TEXT, asr_transcript TEXT,
              archived_at TEXT);
            CREATE TABLE asr_attempts(
              id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
              last_at TEXT, last_error TEXT);
        ''')
        # Long, old enough to be eligible, no transcript from either source.
        self.conn.execute(
            "INSERT INTO recordings(id,name,start_at,duration_ms,"
            "plaud_transcript,asr_transcript,archived_at) "
            "VALUES('long','4h meeting','2026-08-05T21:47:52',15744000,'','',"
            "'2026-08-05T22:00:00')")
        self.conn.commit()

    def attempts(self):
        row = self.conn.execute(
            "SELECT attempts FROM asr_attempts WHERE id='long'").fetchone()
        return row[0] if row else 0

    def test_infrastructure_errors_do_not_exhaust_attempts(self):
        for _ in range(5):
            self.mod.bump(self.conn, 'long',
                          'RuntimeError: unexpected mcp response: null')
        self.assertEqual(self.attempts(), 0)
        # Still queued for retry, which is the whole point.
        self.assertIn('long', [r[0] for r in self.mod.pending(self.conn)])

    def test_real_failures_still_exhaust_attempts(self):
        for _ in range(self.mod.MAX_ATTEMPTS):
            self.mod.bump(self.conn, 'long', 'ValueError: audio is silent')
        self.assertEqual(self.attempts(), self.mod.MAX_ATTEMPTS)
        self.assertNotIn('long', [r[0] for r in self.mod.pending(self.conn)])

    def test_shorter_eligible_work_runs_before_multi_hour_audio(self):
        self.conn.execute(
            "INSERT INTO recordings(id,name,start_at,duration_ms,"
            "plaud_transcript,asr_transcript,archived_at) "
            "VALUES('shorter','32m meeting','2026-08-07T07:09:24',1936000,'','',"
            "'2026-08-05T22:00:00')")
        self.conn.commit()
        ids = [row[0] for row in self.mod.pending(self.conn)]
        self.assertEqual(ids, ['shorter', 'long'])

    def test_classifier_covers_the_observed_failure_modes(self):
        for err in ('RuntimeError: unexpected mcp response: null',
                    'ConnectionResetError: [Errno 104] Connection reset by peer',
                    'urllib.error.URLError: <urlopen error timed out>',
                    'http.client.RemoteDisconnected: Remote end closed connection',
                    'HTTP 502 Bad Gateway'):
            self.assertTrue(self.mod.is_transient(err), err)
        for err in ('ValueError: audio is silent',
                    'quality gate rejected transcript',
                    'ffprobe: invalid data found'):
            self.assertFalse(self.mod.is_transient(err), err)

    def test_rate_limits_and_server_faults_are_infrastructure_too(self):
        """429 and 500 are "come back later", not "this audio is unusable".

        Both reached the classifier as plain HTTPError text and counted as
        verdicts about the recording, so an afternoon of rate limiting — or one
        asr-mcp process falling over mid-call — spent the whole retry budget
        and retired a recording nothing was wrong with.
        """
        for err in ('HTTPError: HTTP Error 429: Too Many Requests',
                    'HTTPError: HTTP Error 500: Internal Server Error',
                    'urllib.error.HTTPError: HTTP Error 500: ',
                    'RuntimeError: asr-mcp replied HTTP 429',
                    'RuntimeError: upstream status 500',
                    'RuntimeError: proxy said HTTP/1.1 500',
                    'RuntimeError: gateway code=429'):
            self.assertTrue(self.mod.is_transient(err), err)

    def test_a_status_shaped_number_in_a_verdict_is_not_a_status(self):
        """The audio failures keep costing an attempt.

        A recording that cannot be decoded fails identically forever. If a
        number that merely looks like a status let it read as transient, it
        would be eligible on every single pass, re-spend GPU time on the
        windows before the broken one, and starve the queue behind it.
        """
        for err in ('ValueError: decode failed at offset 500',
                    'RuntimeError: unsupported codec, 429 frames read',
                    'HTTPError: HTTP Error 415: Unsupported Media Type',
                    'HTTPError: HTTP Error 400: Bad Request',
                    'RuntimeError: unsupported sample rate 44100'):
            self.assertFalse(self.mod.is_transient(err), err)

    def test_rate_limited_runs_keep_their_budget_and_stay_queued(self):
        for _ in range(self.mod.MAX_ATTEMPTS + 2):
            self.mod.bump(self.conn, 'long',
                          'HTTPError: HTTP Error 429: Too Many Requests')
        for _ in range(self.mod.MAX_ATTEMPTS + 2):
            self.mod.bump(self.conn, 'long',
                          'HTTPError: HTTP Error 500: Internal Server Error')
        self.assertEqual(self.attempts(), 0)
        self.assertIn('long', [r[0] for r in self.mod.pending(self.conn)])

    def test_permanent_decode_and_unsupported_errors_still_retire_a_recording(self):
        for _ in range(self.mod.MAX_ATTEMPTS):
            self.mod.bump(self.conn, 'long',
                          'RuntimeError: decode failed: unsupported codec')
        self.assertEqual(self.attempts(), self.mod.MAX_ATTEMPTS)
        self.assertNotIn('long', [r[0] for r in self.mod.pending(self.conn)])


class SummaryStatelessMcpTests(unittest.TestCase):
    def test_summary_client_omits_none_session_header(self):
        module = load_module("summary_backfill.py", "summary_stateless_test")
        sent_headers = []

        class FakeResp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            sent_headers.append(dict(req.headers))
            return FakeResp()

        with mock.patch.object(module, "_post", return_value=({}, None)), \
             mock.patch.object(module.urllib.request, "urlopen", fake_urlopen):
            self.assertIsNone(module.mcp_connect())

        for headers in sent_headers:
            self.assertNotIn("Mcp-session-id", headers)
            self.assertNotIn("Mcp-Session-Id", headers)


class CheckpointedLongRecordingTests(unittest.TestCase):
    """A 4h22m recording must survive being cut in half by an infra blip.

    One un-chunked call spends five hours in flight; anything that interrupts
    it — an asr-mcp redeploy, a dropped socket — throws away every minute of
    GPU work. Segmenting the job and committing each finished segment turns
    that into resumable progress: a rerun only pays for what is still missing.
    """

    # 4:22:27, the real recording that kept losing whole runs.
    DURATION_SEC = 4 * 3600 + 22 * 60 + 27

    def setUp(self):
        self.archive = load_archive_module()
        self.asr = load_module("asr_backfill.py", "asr_backfill_chunked")
        self.tmp = tempfile.mkdtemp()
        audio_dir = Path(self.tmp) / "audio"
        audio_dir.mkdir()
        (audio_dir / "long.mp3").write_bytes(b"ID3-local-test-audio")
        self.asr.AUDIO_DIR = str(audio_dir)
        self.db_path = str(Path(self.tmp) / "archive.db")
        self.conn = sqlite3.connect(self.db_path)
        self.archive.init_db(self.conn)
        self.asr.ensure_attempts(self.conn)
        self.conn.execute(
            """INSERT INTO recordings(id,name,start_at,duration_ms,
                 plaud_transcript,asr_transcript,archived_at)
               VALUES('long','4h22m meeting','2026-08-05T21:47:52',?,'','',
                      '2026-08-05T22:00:00')""",
            (self.DURATION_SEC * 1000,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def unbounded(self):
        """Let one call walk the whole plan.

        Production does ASR_SEGMENTS_PER_RUN windows per run and stops; the
        tests below are about what a finished plan looks like, and the quantum
        itself is pinned separately in SegmentQuantumTests.
        """
        return mock.patch.object(self.asr, "SEGMENTS_PER_RUN", 0)

    def segment_call(self, fail_at_start=None, error="ValueError: decode failed"):
        """Fake local-file transcription that answers per requested window."""
        seen = []

        def fake_call(name, args, sid, **kwargs):
            seen.append((name, args))
            if fail_at_start is not None and args.get("start_sec") == fail_at_start:
                raise RuntimeError(error)
            return {
                "text": f"words at {args.get('start_sec')}",
                "engine_used": "whisper-large",
                "meta": {"detected_lang": "ru", "route_reason": "cyrillic detected"},
            }

        return seen, fake_call

    def stored_segments(self, conn=None):
        conn = conn or self.conn
        return conn.execute(
            """SELECT seg_index,start_sec,duration_sec,text
               FROM asr_segments WHERE id='long' AND COALESCE(text,'')<>''
               ORDER BY seg_index"""
        ).fetchall()

    def test_segment_checkpoint_separates_alternative_body_from_public_meta(self):
        self.asr.ensure_segments(self.conn)
        self.asr.record_segment(
            self.conn, "long", 0, 0, 600, "selected", "mixed",
            {"route_reason": "mixed", "alternative": {"text": "PRIVATE-ALT"}},
            alternative="PRIVATE-ALT",
        )

        alternative, meta_json = self.conn.execute(
            "SELECT alternative_text,meta_json FROM asr_segments "
            "WHERE id='long' AND seg_index=0"
        ).fetchone()
        self.assertEqual(alternative, "PRIVATE-ALT")
        self.assertNotIn("PRIVATE-ALT", meta_json)

    # ---- boundaries -------------------------------------------------

    def test_plan_covers_the_recording_with_a_short_final_segment(self):
        plan = self.asr.plan_segments(self.DURATION_SEC)

        self.assertEqual(len(plan), 27)
        self.assertEqual(plan[0], (0, 0, 600))
        self.assertEqual(plan[1], (1, 600, 600))
        self.assertEqual(plan[-1], (26, 15600, 147))
        self.assertEqual(sum(duration for _, _, duration in plan), self.DURATION_SEC)
        starts = [start for _, start, _ in plan]
        self.assertEqual(starts, sorted(starts))
        for (_, start, duration), (_, next_start, _) in zip(plan, plan[1:]):
            self.assertEqual(start + duration, next_start)

    def test_anything_past_thirty_minutes_is_segmented(self):
        self.assertEqual(self.asr.SEGMENT_THRESHOLD_SEC, 600)
        self.assertEqual(self.asr.SEGMENT_SEC, 600)
        # Exactly ten minutes is still a whole-file call; a second more is not.
        self.assertFalse(self.asr.needs_segmentation(600))
        self.assertTrue(self.asr.needs_segmentation(601))
        self.assertFalse(self.asr.needs_segmentation(600))

    def test_threshold_and_window_are_configurable(self):
        with mock.patch.object(self.asr, "MAX_SEGMENT_SEC", 3600):
            self.assertEqual(
                self.asr.plan_segments(7200, chunk_sec=3600),
                [(0, 0, 3600), (1, 3600, 3600)],
            )
        with mock.patch.object(self.asr, "SEGMENT_THRESHOLD_SEC", 5400):
            self.assertFalse(self.asr.needs_segmentation(5400))
            self.assertTrue(self.asr.needs_segmentation(5401))

    def test_no_window_can_exceed_what_the_server_accepts(self):
        """asr-mcp refuses duration_sec > ASR_MAX_SEGMENT_SECONDS (600) with a
        SegmentRequestError — a permanent refusal. A window over the cap is not
        a slow request, it is a recording that can never finish, so the cap
        wins over any configured window size."""
        self.assertEqual(self.asr.MAX_SEGMENT_SEC, 600)
        for plan in (self.asr.plan_segments(7200, chunk_sec=3600),
                     self.asr.plan_segments(self.DURATION_SEC),
                     self.asr.plan_segments(3602),
                     self.asr.plan_segments(1_800_999 // 1000 + 1)):
            self.assertTrue(plan)
            for _index, _start, duration in plan:
                self.assertLessEqual(duration, self.asr.MAX_SEGMENT_SEC)
                self.assertGreater(duration, 0)
        self.assertEqual(len(self.asr.plan_segments(7200, chunk_sec=3600)), 12)

    def test_a_thirty_two_minute_recording_splits_into_six_hundred_second_windows(self):
        self.assertEqual(
            self.asr.plan_segments(32 * 60),
            [(0, 0, 600), (1, 600, 600), (2, 1200, 600), (3, 1800, 120)],
        )

    def test_a_sliver_of_a_tail_borrows_from_its_neighbour(self):
        # A 2-second trailing chunk is below what ASR will accept and would
        # block assembly forever. Folding it into the previous window used to
        # produce a 1802-second request, which the server rejects permanently;
        # move time backwards into the tail instead so both windows are legal.
        plan = self.asr.plan_segments(3602)
        self.assertEqual(plan, [(0, 0, 600), (1, 600, 600), (2, 1200, 600),
                                (3, 1800, 600), (4, 2400, 600), (5, 3000, 599),
                                (6, 3599, 3)])
        self.assertEqual(sum(duration for _, _, duration in plan), 3602)
        for (_, start, duration), (_, next_start, _) in zip(plan, plan[1:]):
            self.assertEqual(start + duration, next_start)

    def test_the_tail_minimum_is_measured_in_whole_seconds_rounded_up(self):
        # A 3500 ms minimum floored to 3 s would plan a 3-second window the
        # service then rejects on every attempt.
        with mock.patch.object(self.asr, "MIN_DURATION_MS", 3500):
            self.assertEqual(self.asr.min_segment_sec(), 4)
            plan = self.asr.plan_segments(3603)
            self.assertEqual(plan[-1], (6, 3599, 4))
        self.assertEqual(self.asr.min_segment_sec(), 3)

    def test_milliseconds_decide_the_ten_minute_boundary(self):
        """Flooring ms to seconds put 10:00.001 through 10:00.999 on the
        whole-file path — the exact all-or-nothing call segmentation exists to
        avoid — and dropped the sub-second tail from the plan entirely."""
        self.assertFalse(self.asr.needs_segmentation_ms(600_000))
        self.assertTrue(self.asr.needs_segmentation_ms(600_001))
        self.assertTrue(self.asr.needs_segmentation_ms(600_999))

        # The seconds every caller converts to agree with that decision.
        self.assertEqual(self.asr.duration_sec_from_ms(600_000), 600)
        self.assertEqual(self.asr.duration_sec_from_ms(600_001), 601)
        self.assertEqual(self.asr.duration_sec_from_ms(600_999), 601)
        self.assertFalse(
            self.asr.needs_segmentation(self.asr.duration_sec_from_ms(600_000)))
        for milliseconds in (600_001, 600_999):
            self.assertTrue(
                self.asr.needs_segmentation(
                    self.asr.duration_sec_from_ms(milliseconds)))
            plan = self.asr.plan_segments(
                self.asr.duration_sec_from_ms(milliseconds))
            # Nothing is dropped: the plan covers the whole recording, and the
            # 1-second remainder is legal because it borrows from its neighbour.
            self.assertEqual(sum(d for _, _, d in plan), 601)
            self.assertEqual(plan, [(0, 0, 598), (1, 598, 3)])

    def test_the_duration_conversion_never_truncates_the_tail(self):
        self.assertEqual(self.asr.duration_sec_from_ms(0), 0)
        self.assertEqual(self.asr.duration_sec_from_ms(None), 0)
        self.assertEqual(self.asr.duration_sec_from_ms(1), 1)
        self.assertEqual(self.asr.duration_sec_from_ms(2999), 3)
        self.assertEqual(
            self.asr.duration_sec_from_ms(self.DURATION_SEC * 1000 + 400),
            self.DURATION_SEC + 1,
        )

    # ---- durability -------------------------------------------------

    def test_each_finished_segment_is_committed_before_the_next_call(self):
        seen, fake_call = self.segment_call(fail_at_start=3 * 600)
        with self.unbounded(), mock.patch.object(
                self.asr, "call", side_effect=fake_call):
            with self.assertRaises(self.asr.SegmentIncomplete):
                self.asr.transcribe(
                    self.conn, "sid", "long", "auto", self.DURATION_SEC
                )

        # A separate connection sees only committed rows.
        other = sqlite3.connect(self.db_path)
        try:
            rows = self.stored_segments(other)
            failed = other.execute(
                """SELECT seg_index,attempts,text FROM asr_segments
                   WHERE id='long' AND COALESCE(text,'')='' ORDER BY seg_index"""
            ).fetchall()
        finally:
            other.close()
        self.assertEqual(
            rows,
            [
                (0, 0, 600, "words at 0"),
                (1, 600, 600, "words at 600"),
                (2, 1200, 600, "words at 1200"),
            ],
        )
        # Windows past the failure are materialised as pending rows, so the
        # failed one is identified by its attempt count, not by being alone.
        self.assertEqual([row for row in failed if row[1]], [(3, 1, None)])
        # Stop at the first failure rather than re-hitting a sick service.
        self.assertEqual(len(seen), 4)
        self.assertEqual(
            [args["start_sec"] for _, args in seen], [0, 600, 1200, 1800]
        )
        for _, args in seen:
            self.assertTrue(args["url"].endswith("/long.mp3"))
            self.assertEqual(args["duration_sec"], 600)

    def test_a_rerun_skips_finished_segments_and_retries_only_the_missing(self):
        plan = self.asr.plan_segments(self.DURATION_SEC)
        for index, start, duration in plan:
            if index in (3, 7):
                continue
            self.asr.record_segment(
                self.conn, "long", index, start, duration,
                f"words at {start}", "whisper-large", {"detected_lang": "ru"},
            )
        seen, fake_call = self.segment_call()
        with self.unbounded(), mock.patch.object(
                self.asr, "call", side_effect=fake_call):
            self.asr.transcribe(self.conn, "sid", "long", "auto", self.DURATION_SEC)

        self.assertEqual([args["start_sec"] for _, args in seen], [1800, 4200])
        self.assertEqual(len(self.stored_segments()), 27)
        transcript = self.conn.execute(
            "SELECT asr_transcript FROM recordings WHERE id='long'"
        ).fetchone()[0]
        self.assertIn("words at 1800", transcript)
        self.assertIn("words at 4200", transcript)

    def test_checkpoints_from_a_different_chunk_size_are_not_reused(self):
        # Re-tuning ASR_CHUNK_SEC must not splice text captured on the old
        # boundaries into the new plan under a wrong timestamp.
        self.asr.record_segment(
            self.conn, "long", 1, 3600, 3600, "stale hour", "whisper-large", {},
        )
        seen, fake_call = self.segment_call()
        with self.unbounded(), mock.patch.object(
                self.asr, "call", side_effect=fake_call):
            self.asr.transcribe(self.conn, "sid", "long", "auto", self.DURATION_SEC)

        self.assertEqual(len(seen), 27)
        transcript = self.conn.execute(
            "SELECT asr_transcript FROM recordings WHERE id='long'"
        ).fetchone()[0]
        self.assertNotIn("stale hour", transcript)
        self.assertEqual(
            self.conn.execute(
                "SELECT start_sec,duration_sec FROM asr_segments "
                "WHERE id='long' AND seg_index=1"
            ).fetchone(),
            (600, 600),
        )

    def run_until_failure(self, error):
        """One run of the long recording, failing window 1, bumped like main()."""
        seen, fake_call = self.segment_call(fail_at_start=600, error=error)
        with self.unbounded(), mock.patch.object(
                self.asr, "call", side_effect=fake_call):
            try:
                self.asr.transcribe(
                    self.conn, "sid", "long", "auto", self.DURATION_SEC
                )
            except Exception as exc:  # exactly what main() does
                self.asr.bump(self.conn, "long", exc)
        return seen

    def attempts(self, rid="long"):
        row = self.conn.execute(
            "SELECT COALESCE(attempts,0) FROM asr_attempts WHERE id=?",
            (rid,)).fetchone()
        return row[0] if row else 0

    def test_a_transient_segment_failure_leaves_the_recording_unpunished(self):
        # An asr-mcp restart mid-window must not spend a retry: the finished
        # windows are committed and the next run picks up where this stopped.
        for _ in range(self.asr.MAX_ATTEMPTS + 2):
            self.run_until_failure("IncompleteRead(4096 bytes read, 8192 more expected)")

        self.assertEqual(self.attempts(), 0)
        self.assertIn("long", [row[0] for row in self.asr.pending(self.conn)])

    def test_a_permanent_segment_failure_spends_the_retry_budget(self):
        """A window that cannot be decoded fails identically on every attempt.

        Wrapping it in SegmentIncomplete and treating every partial run as
        unpunished left the recording eligible forever: it re-entered the queue
        on every pass, spent GPU time on the windows before the broken one, and
        starved everything behind it. Permanent failures spend the budget.
        """
        self.run_until_failure("ValueError: decode failed")
        self.assertEqual(self.attempts(), 1)
        self.assertIn("long", [row[0] for row in self.asr.pending(self.conn)])

        for _ in range(self.asr.MAX_ATTEMPTS):
            self.run_until_failure("ValueError: decode failed")

        self.assertGreaterEqual(self.attempts(), self.asr.MAX_ATTEMPTS)
        self.assertNotIn("long", [row[0] for row in self.asr.pending(self.conn)])
        # The committed windows are still on disk for a forced rerun.
        self.assertEqual([row[0] for row in self.stored_segments()], [0])

    def test_a_rate_limited_or_faulting_window_is_not_a_verdict_on_the_audio(self):
        # The service saying "slow down" (429) or falling over (500) mid-window
        # is the same class of event as a redeploy: the finished windows are
        # committed and the next run resumes, so no strike is owed.
        for error in ('HTTPError: HTTP Error 429: Too Many Requests',
                      'HTTPError: HTTP Error 500: Internal Server Error'):
            for _ in range(self.asr.MAX_ATTEMPTS + 1):
                self.run_until_failure(error)

        self.assertEqual(self.attempts(), 0)
        self.assertIn("long", [row[0] for row in self.asr.pending(self.conn)])

    def test_an_undecodable_window_still_spends_the_budget(self):
        for _ in range(self.asr.MAX_ATTEMPTS):
            self.run_until_failure("RuntimeError: decode failed: unsupported codec")

        self.assertGreaterEqual(self.attempts(), self.asr.MAX_ATTEMPTS)
        self.assertNotIn("long", [row[0] for row in self.asr.pending(self.conn)])

    def test_a_failed_segment_leaves_the_recording_pending_with_its_windows(self):
        seen = self.run_until_failure("ValueError: decode failed")
        self.assertEqual(len(seen), 2)

        self.assertIn("long", [row[0] for row in self.asr.pending(self.conn)])
        self.assertEqual(
            self.conn.execute(
                "SELECT COALESCE(asr_transcript,'') FROM recordings WHERE id='long'"
            ).fetchone()[0],
            "",
        )
        # The per-segment row keeps the diagnosis for the failed window.
        attempts, error = self.conn.execute(
            "SELECT attempts,last_error FROM asr_segments WHERE id='long' AND seg_index=1"
        ).fetchone()
        self.assertEqual(attempts, 1)
        self.assertIn("decode failed", error)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM recordings_fts WHERE id='long'"
            ).fetchone()[0],
            0,
        )

    # ---- assembly ---------------------------------------------------

    def test_marker_formats_seconds_as_wall_clock_offsets(self):
        self.assertEqual(self.asr.format_marker(0), "00:00:00")
        self.assertEqual(self.asr.format_marker(600), "00:10:00")
        self.assertEqual(self.asr.format_marker(self.DURATION_SEC), "04:22:27")

    def test_full_success_assembles_a_marked_transcript_into_the_canonical_store(self):
        seen, fake_call = self.segment_call()
        with self.unbounded(), mock.patch.object(
                self.asr, "call", side_effect=fake_call):
            self.asr.transcribe(self.conn, "sid", "long", "auto", self.DURATION_SEC)

        transcript, engine, lang, meta_json = self.conn.execute(
            """SELECT asr_transcript,asr_engine,lang,asr_meta_json
               FROM recordings WHERE id='long'"""
        ).fetchone()
        markers = re.findall(r"\[\d{2}:\d{2}:\d{2}\]", transcript)
        self.assertEqual(
            markers,
            [f"[{self.asr.format_marker(i * 600)}]" for i in range(27)],
        )
        self.assertLess(transcript.index("words at 0"), transcript.index("words at 14400"))
        self.assertEqual(engine, "whisper-large")
        self.assertEqual(lang, "ru")
        meta = json.loads(meta_json)
        self.assertTrue(meta.get("segmented"))
        self.assertEqual(meta.get("segment_count"), 27)
        self.assertEqual(len(meta.get("segments") or []), 27)

        self.assertEqual(
            self.conn.execute(
                "SELECT transcript FROM recordings_fts WHERE id='long'"
            ).fetchone()[0],
            transcript,
        )
        # Checkpoints stay on disk after success: they are the audit trail.
        self.assertEqual(len(self.stored_segments()), 27)

    def test_segmented_meta_aggregates_unresolved_windows_for_review_safety(self):
        _seen, fake_call = self.segment_call()

        def one_unresolved(tool, args, sid):
            result = fake_call(tool, args, sid)
            result["meta"]["unresolved_count"] = (
                1 if args.get("start_sec") == 600 else 0
            )
            return result

        with self.unbounded(), mock.patch.object(
                self.asr, "call", side_effect=one_unresolved):
            self.asr.transcribe(self.conn, "sid", "long", "mixed", self.DURATION_SEC)

        meta_json = self.conn.execute(
            "SELECT asr_meta_json FROM recordings WHERE id='long'"
        ).fetchone()[0]
        self.assertEqual(json.loads(meta_json)["unresolved_count"], 1)

    # ---- unchanged behaviour ---------------------------------------

    def test_short_recordings_still_take_the_single_call_path(self):
        self.conn.execute(
            "INSERT INTO recordings(id,name,duration_ms) VALUES('short','Standup',600000)"
        )
        (Path(self.asr.AUDIO_DIR) / "short.mp3").write_bytes(b"ID3-short-test-audio")
        self.conn.commit()
        seen, fake_call = self.segment_call()
        with mock.patch.object(self.asr, "call", side_effect=fake_call):
            self.asr.transcribe(self.conn, "sid", "short", "auto", 600)

        self.assertEqual(len(seen), 1)
        _, args = seen[0]
        self.assertNotIn("start_sec", args)
        self.assertNotIn("duration_sec", args)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM asr_segments WHERE id='short'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT asr_transcript FROM recordings WHERE id='short'"
            ).fetchone()[0],
            "words at None",
        )

    def test_incomplete_read_is_infrastructure_not_bad_audio(self):
        error = "IncompleteRead: IncompleteRead(4096 bytes read, 8192 more expected)"
        self.assertTrue(self.asr.is_transient(error))
        for _ in range(self.asr.MAX_ATTEMPTS + 2):
            self.asr.bump(self.conn, "long", error)
        self.assertEqual(
            self.conn.execute(
                "SELECT attempts FROM asr_attempts WHERE id='long'"
            ).fetchone()[0],
            0,
        )
        self.assertIn("long", [row[0] for row in self.asr.pending(self.conn)])

    def test_explicit_ids_take_their_duration_from_the_archive(self):
        """A forced id is planned from its real duration, and an id this
        archive has never seen is dropped rather than defaulted to 0.

        Defaulting would send a four-hour recording down the single-call path
        purely because it was named on the command line, and transcribing an
        unknown id would publish a transcript plus an FTS row for a recording
        that does not exist.
        """
        targets = self.asr.explicit_targets(self.conn, ["long", "ghost"])

        self.assertEqual(
            targets, [("long", "4h22m meeting", self.DURATION_SEC * 1000)]
        )
        self.assertTrue(
            self.asr.needs_segmentation_ms(targets[0][2]),
            "a forced long id must still take the segmented path",
        )


class SegmentProgressTests(unittest.TestCase):
    """The UI must be able to say "3 of 9 done" and be telling the truth.

    Checkpoints alone answer "what is left to transcribe", not "how far along
    is this recording" — nothing exists for a window until it succeeds, so a
    four-hour job looks identical at minute one and minute two hundred. Every
    window of the current plan is written up front, and each one carries a
    status that changes at the moment the work does.
    """

    DURATION_SEC = 4 * 3600 + 22 * 60 + 27

    def setUp(self):
        self.archive = load_archive_module()
        self.asr = load_module("asr_backfill.py", "asr_backfill_progress")
        self.tmp = tempfile.mkdtemp()
        audio_dir = Path(self.tmp) / "audio"
        audio_dir.mkdir()
        (audio_dir / "long.mp3").write_bytes(b"ID3-local-test-audio")
        self.asr.AUDIO_DIR = str(audio_dir)
        self.db_path = str(Path(self.tmp) / "archive.db")
        self.conn = sqlite3.connect(self.db_path)
        self.archive.init_db(self.conn)
        self.asr.ensure_attempts(self.conn)
        self.conn.execute(
            """INSERT INTO recordings(id,name,start_at,duration_ms,
                 plaud_transcript,asr_transcript,archived_at)
               VALUES('long','4h22m meeting','2026-08-05T21:47:52',?,'','',
                      '2026-08-05T22:00:00')""",
            (self.DURATION_SEC * 1000,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def committed_statuses(self):
        """What a reader outside this process sees, ordered by window."""
        other = sqlite3.connect(self.db_path)
        try:
            return other.execute(
                """SELECT seg_index,start_sec,duration_sec,status
                   FROM asr_segments WHERE id='long' ORDER BY seg_index"""
            ).fetchall()
        finally:
            other.close()

    def committed_progress(self):
        other = sqlite3.connect(self.db_path)
        try:
            return self.asr.segment_progress(other, "long")
        finally:
            other.close()

    def test_startup_releases_only_processing_segments_of_recovered_jobs(self):
        plan = self.asr.plan_segments(self.DURATION_SEC)
        self.asr.materialize_plan(self.conn, "long", plan)
        self.asr.mark_processing(self.conn, "long", 0, plan[0][1], plan[0][2])
        self.asr.record_segment(
            self.conn, "long", 1, plan[1][1], plan[1][2],
            "finished", "large-v3", {},
        )
        self.asr.materialize_plan(self.conn, "other", plan[:1])
        self.asr.mark_processing(
            self.conn, "other", 0, plan[0][1], plan[0][2])

        released = self.asr.release_processing_segments(self.conn, ["long"])

        self.assertEqual(released, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT seg_index,status FROM asr_segments WHERE id='long' "
                "ORDER BY seg_index LIMIT 2").fetchall(),
            [(0, self.asr.ST_PENDING), (1, self.asr.ST_COMPLETE)],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM asr_segments WHERE id='other'"
            ).fetchone()[0],
            self.asr.ST_PROCESSING,
        )

    def turn(self):
        """One scheduler turn. True once the recording is finished.

        A run does ASR_SEGMENTS_PER_RUN windows and yields, so the states these
        tests are about are reached over several turns, exactly as cron reaches
        them. A pause is not an outcome to assert on — anything else (a window
        that will not decode, a finished plan) still propagates.
        """
        try:
            self.asr.transcribe(self.conn, "sid", "long", "auto", self.DURATION_SEC)
        except self.asr.SegmentPaused:
            return False
        return True

    def run_turns(self, limit=20):
        """Turn the scheduler until the recording finishes or something raises."""
        for _ in range(limit):
            if self.turn():
                return
        self.fail(f"still unfinished after {limit} turns: {self.committed_progress()}")

    def test_idle_queue_chains_segments_without_waiting_for_the_next_cron(self):
        calls = []

        def fake_call(name, args, sid, **kwargs):
            calls.append(args["start_sec"])
            return {"text": f"words at {args['start_sec']}",
                    "engine_used": "large-v3", "meta": {}}

        with mock.patch.object(self.asr, "call", side_effect=fake_call):
            self.asr.transcribe(
                self.conn, "sid", "long", "auto", self.DURATION_SEC
            )

        self.assertEqual(calls, [i * 600 for i in range(27)])
        self.assertEqual(self.committed_progress()["complete"], 27)

    def test_competing_recording_still_forces_a_yield_after_one_segment(self):
        calls = []

        def fake_call(name, args, sid, **kwargs):
            calls.append(args["start_sec"])
            # Simulate ingestion committing a fresh short recording while the
            # long window is in flight. The worker must see it on its next
            # statement after record_segment commits the checkpoint.
            other = sqlite3.connect(self.db_path)
            try:
                other.execute(
                    """INSERT INTO recordings(id,name,start_at,duration_ms,
                         plaud_transcript,asr_transcript,archived_at)
                       VALUES('short','short note','2026-08-07T12:00:00',
                              120000,'','', '2026-08-07T12:01:00')"""
                )
                other.commit()
            finally:
                other.close()
            return {"text": "first window", "engine_used": "large-v3", "meta": {}}

        with mock.patch.object(self.asr, "call", side_effect=fake_call):
            with self.assertRaises(self.asr.SegmentPaused):
                self.asr.transcribe(
                    self.conn, "sid", "long", "auto", self.DURATION_SEC
                )

        self.assertEqual(calls, [0])
        self.assertEqual(self.committed_progress()["complete"], 1)
        self.assertIn("short", [row[0] for row in self.asr.pending(self.conn)])

    def test_forced_main_yields_without_a_retry_strike_when_queue_competes(self):
        self.conn.execute(
            """INSERT INTO recordings(id,name,start_at,duration_ms,
                 plaud_transcript,asr_transcript,archived_at)
               VALUES('short','short note','2026-08-07T12:00:00',120000,'','',
                      '2026-08-07T12:01:00')"""
        )
        self.conn.commit()
        calls = []

        def fake_call(name, args, sid, **kwargs):
            calls.append((name, Path(args["url"]).name, args["start_sec"]))
            return {"text": "first window", "engine_used": "large-v3", "meta": {}}

        lock_path = str(Path(self.tmp) / "main.lock")
        argv = ["asr_backfill.py", "long"]
        with mock.patch.object(self.asr, "DB", self.db_path), \
             mock.patch.object(self.asr, "LOCK", lock_path), \
             mock.patch.object(self.asr, "TOKEN", "test-token"), \
             mock.patch.object(self.asr, "mcp_connect", return_value="sid"), \
             mock.patch.object(self.asr, "call", side_effect=fake_call), \
             mock.patch("sys.argv", argv):
            self.asr.main()

        self.assertEqual(calls, [("transcribe_url", "long.mp3", 0)])
        self.assertEqual(
            self.conn.execute(
                "SELECT COALESCE(attempts,0) FROM asr_attempts WHERE id='long'"
            ).fetchone(),
            None,
        )
        self.assertEqual(self.committed_progress()["complete"], 1)

    # ---- the plan is known before any GPU time is spent ----------------

    def test_the_whole_plan_is_pending_and_committed_before_the_first_call(self):
        seen_at_first_call = {}

        def fake_call(name, args, sid, **kwargs):
            if not seen_at_first_call:
                seen_at_first_call.update(self.committed_progress())
                seen_at_first_call["rows"] = len(self.committed_statuses())
            return {"text": f"words at {args.get('start_sec')}",
                    "engine_used": "whisper-large", "meta": {}}

        with mock.patch.object(self.asr, "call", side_effect=fake_call):
            self.run_turns()

        # The total is knowable from the very first call, not discovered late.
        self.assertEqual(seen_at_first_call["rows"], 27)
        self.assertEqual(seen_at_first_call["total"], 27)
        self.assertEqual(seen_at_first_call["complete"], 0)
        self.assertEqual(seen_at_first_call["processing"], 1)
        self.assertEqual(seen_at_first_call["pending"], 26)
        self.assertEqual(seen_at_first_call["percent"], 0.0)

    def test_materialising_the_plan_keeps_valid_checkpoints_and_rewrites_others(self):
        self.asr.record_segment(
            self.conn, "long", 0, 0, 600, "words at 0", "whisper-large", {},
        )
        # Captured under a different window size: the text cannot be trusted.
        self.asr.record_segment(
            self.conn, "long", 1, 3600, 3600, "stale hour", "whisper-large", {},
        )
        plan = self.asr.plan_segments(self.DURATION_SEC)

        self.asr.materialize_plan(self.conn, "long", plan)

        rows = self.committed_statuses()
        self.assertEqual(len(rows), 27)
        self.assertEqual(rows[0], (0, 0, 600, "complete"))
        self.assertEqual(rows[1], (1, 600, 600, "pending"))
        self.assertEqual(
            [status for *_rest, status in rows[2:]], ["pending"] * 25
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT text FROM asr_segments WHERE id='long' AND seg_index=1"
            ).fetchone()[0]
        )
        # Re-materialising the same plan changes nothing.
        self.asr.materialize_plan(self.conn, "long", plan)
        self.assertEqual(self.committed_statuses(), rows)

    # ---- status tracks the work as it happens --------------------------

    def test_a_window_is_processing_during_its_call_and_complete_after(self):
        during = []

        def fake_call(name, args, sid, **kwargs):
            during.append({index: status for index, _s, _d, status
                           in self.committed_statuses()})
            return {"text": f"words at {args.get('start_sec')}",
                    "engine_used": "whisper-large", "meta": {}}

        with mock.patch.object(self.asr, "call", side_effect=fake_call):
            self.run_turns()

        # Each call sees its own window processing and the previous ones done.
        self.assertEqual(len(during), 27)
        self.assertEqual(during[0][0], "processing")
        self.assertEqual(during[1][0], "complete")
        self.assertEqual(during[1][1], "processing")
        self.assertEqual(during[26][26], "processing")
        self.assertEqual(
            [status for *_rest, status in self.committed_statuses()],
            ["complete"] * 27,
        )
        progress = self.committed_progress()
        self.assertEqual(progress["complete"], 27)
        self.assertEqual(progress["percent"], 100.0)

    def test_a_failing_window_is_left_as_error_with_its_diagnosis(self):
        def fake_call(name, args, sid, **kwargs):
            if args.get("start_sec") == 3600:
                raise RuntimeError("decode failed")
            return {"text": f"words at {args.get('start_sec')}",
                    "engine_used": "whisper-large", "meta": {}}

        with mock.patch.object(self.asr, "call", side_effect=fake_call):
            with self.assertRaises(self.asr.SegmentIncomplete) as caught:
                self.run_turns()

        # A window that will not decode is a real stop, not a yielded quantum.
        self.assertNotIsInstance(caught.exception, self.asr.SegmentPaused)
        self.assertEqual(
            [status for *_rest, status in self.committed_statuses()],
            ["complete"] * 6 + ["error"] + ["pending"] * 20,
        )
        attempts, last_error = self.conn.execute(
            """SELECT attempts,last_error FROM asr_segments
               WHERE id='long' AND seg_index=6"""
        ).fetchone()
        self.assertEqual(attempts, 1)
        self.assertIn("decode failed", last_error)
        progress = self.committed_progress()
        self.assertEqual(
            (progress["complete"], progress["error"], progress["pending"],
             progress["processing"]),
            (6, 1, 20, 0),
        )

    def test_a_process_killed_mid_window_retries_it_on_the_next_run(self):
        plan = self.asr.plan_segments(self.DURATION_SEC)
        # What a SIGKILL during window 4 leaves behind: claimed, never finished.
        self.asr.mark_processing(self.conn, "long", 4, 2400, 600)
        for index, start, duration in plan[:4]:
            self.asr.record_segment(
                self.conn, "long", index, start, duration,
                f"words at {start}", "whisper-large", {},
            )
        self.assertEqual(self.committed_progress()["processing"], 1)

        seen = []

        def fake_call(name, args, sid, **kwargs):
            seen.append(args["start_sec"])
            return {"text": f"words at {args.get('start_sec')}",
                    "engine_used": "whisper-large", "meta": {}}

        with mock.patch.object(self.asr, "call", side_effect=fake_call):
            self.run_turns()

        # The stale window is retried, not silently accepted as finished.
        self.assertIn(2400, seen)
        self.assertEqual(seen, [i * 600 for i in range(4, 27)])
        self.assertIn(
            "words at 2400",
            self.conn.execute(
                "SELECT asr_transcript FROM recordings WHERE id='long'"
            ).fetchone()[0],
        )

    # ---- migration ------------------------------------------------------

    def test_migration_labels_rows_written_before_status_existed(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE asr_segments(
                 id TEXT NOT NULL, seg_index INTEGER NOT NULL,
                 start_sec INTEGER NOT NULL, duration_sec INTEGER NOT NULL,
                 text TEXT, engine TEXT, meta_json TEXT,
                 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                 updated_at TEXT, PRIMARY KEY(id, seg_index))"""
        )
        conn.executemany(
            """INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,
                 text,attempts,last_error) VALUES(?,?,?,?,?,?,?)""",
            [
                ("old", 0, 0, 600, "words at 0", 0, None),
                ("old", 1, 600, 600, None, 2, "RuntimeError: decode failed"),
                ("old", 2, 3600, 600, None, 0, None),
                ("old", 3, 5400, 600, "", 0, None),
            ],
        )
        conn.commit()

        self.asr.ensure_segments(conn)
        first = conn.execute(
            "SELECT seg_index,status FROM asr_segments ORDER BY seg_index"
        ).fetchall()
        self.asr.ensure_segments(conn)  # idempotent: a second run is a no-op
        second = conn.execute(
            "SELECT seg_index,status FROM asr_segments ORDER BY seg_index"
        ).fetchall()

        self.assertEqual(
            first,
            [(0, "complete"), (1, "error"), (2, "pending"), (3, "pending")],
        )
        self.assertEqual(second, first)
        progress = self.asr.segment_progress(conn, "old")
        self.assertEqual(
            (progress["total"], progress["complete"], progress["error"],
             progress["pending"]),
            (4, 1, 1, 2),
        )
        conn.close()

    # ---- the counts cannot lie ------------------------------------------

    def test_progress_reports_counts_only_and_never_transcript_text(self):
        plan = self.asr.plan_segments(self.DURATION_SEC)
        self.asr.materialize_plan(self.conn, "long", plan)
        for index, start, duration in plan[:3]:
            self.asr.record_segment(
                self.conn, "long", index, start, duration,
                f"words at {start}", "whisper-large", {},
            )
        self.asr.mark_processing(self.conn, "long", 3, 1800, 600)
        self.asr.fail_segment(self.conn, "long", 4, 2400, 600, "RuntimeError: boom")

        progress = self.asr.segment_progress(self.conn, "long")

        self.assertEqual(progress["total"], 27)
        self.assertEqual(progress["complete"], 3)
        self.assertEqual(progress["processing"], 1)
        self.assertEqual(progress["error"], 1)
        self.assertEqual(progress["pending"], 22)
        self.assertEqual(
            progress["complete"] + progress["processing"] + progress["error"]
            + progress["pending"],
            progress["total"],
        )
        self.assertEqual(progress["percent"], 11.1)
        blob = json.dumps(progress)
        self.assertNotIn("words at", blob)
        self.assertNotIn("boom", blob)
        # Reading progress must not write anything.
        self.assertEqual(
            self.asr.segment_progress(self.conn, "long"), progress
        )

    def test_a_recording_with_no_segments_reports_an_empty_plan(self):
        self.assertEqual(
            self.asr.segment_progress(self.conn, "ghost"),
            {"id": "ghost", "total": 0, "complete": 0, "processing": 0,
             "stale": 0, "error": 0, "pending": 0, "percent": 0.0},
        )

    def test_a_complete_status_without_text_is_not_progress(self):
        # Status is a UI hint; only committed text is evidence of work done.
        plan = self.asr.plan_segments(self.DURATION_SEC)
        self.asr.materialize_plan(self.conn, "long", plan)
        self.conn.execute(
            "UPDATE asr_segments SET status='complete' WHERE id='long' AND seg_index=0"
        )
        self.conn.commit()

        self.assertEqual(self.asr.segment_progress(self.conn, "long")["complete"], 0)
        self.assertEqual(self.asr.done_segments(self.conn, "long", plan), {})

    def test_done_segments_ignores_text_captured_on_other_boundaries(self):
        plan = self.asr.plan_segments(self.DURATION_SEC)
        self.asr.record_segment(
            self.conn, "long", 0, 0, 600, "words at 0", "whisper-large", {},
        )
        self.asr.record_segment(
            self.conn, "long", 1, 3600, 3600, "stale hour", "whisper-large", {},
        )

        self.assertEqual(
            sorted(self.asr.done_segments(self.conn, "long", plan)), [0]
        )
        # Without a plan the helper still refuses empty text.
        self.assertEqual(
            sorted(self.asr.done_segments(self.conn, "long")), [0, 1]
        )


class RecordingNumberWiringTests(unittest.TestCase):
    """Ingest gives every archived recording its permanent code.

    The numbering rules themselves are covered in test_recording_codes.py; what
    matters here is that an ordinary ingest pass actually applies them, that an
    archive predating codes is migrated on the next run, and that a family
    tenant writes ITS series rather than the owner's.
    """

    RESPONSES = {
        "get_file": json.dumps({"name": "Запись", "duration": 60000}),
        "get_transcript": json.dumps({"source_list": []}),
        "get_note": json.dumps({"note_list": []}),
    }

    def archive_one(self, archive, conn, rec_id):
        with mock.patch.object(
                archive, "call", side_effect=lambda name, args, sid: self.RESPONSES[name]), \
             mock.patch.object(archive, "AUDIO", tempfile.mkdtemp()):
            archive.archive_one(conn, "sid", rec_id, force=False)

    def test_init_db_numbers_an_archive_that_predates_codes(self):
        archive = load_archive_module()
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE recordings(id TEXT PRIMARY KEY, name TEXT, "
            "start_at TEXT, summary TEXT)")
        conn.execute("INSERT INTO recordings(id,start_at) VALUES('b','2026-02-01')")
        conn.execute("INSERT INTO recordings(id,start_at) VALUES('a','2026-01-01')")
        conn.commit()

        archive.init_db(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        self.assertIn("recording_number", columns)
        numbered = dict(conn.execute("SELECT id,recording_number FROM recordings"))
        self.assertEqual(numbered, {"a": "N-0001", "b": "N-0002"})

        # A second run must not renumber anything.
        archive.init_db(conn)
        self.assertEqual(
            dict(conn.execute("SELECT id,recording_number FROM recordings")),
            numbered)

    def test_archiving_allocates_a_code_that_survives_a_forced_rearchive(self):
        archive = load_archive_module()
        conn = sqlite3.connect(":memory:")
        archive.init_db(conn)

        self.archive_one(archive, conn, "rec1")
        first = conn.execute(
            "SELECT recording_number FROM recordings WHERE id='rec1'").fetchone()[0]
        self.assertEqual(first, "N-0001")

        self.archive_one(archive, conn, "rec2")
        self.assertEqual(
            conn.execute("SELECT recording_number FROM recordings WHERE id='rec2'")
            .fetchone()[0], "N-0002")

        # A re-sync rewrites name, transcript and metadata; the code is not
        # metadata from PLAUD and must not move.
        with mock.patch.object(
                archive, "call", side_effect=lambda name, args, sid: self.RESPONSES[name]), \
             mock.patch.object(archive, "AUDIO", tempfile.mkdtemp()):
            archive.archive_one(conn, "sid", "rec1", force=True)
        self.assertEqual(
            conn.execute("SELECT recording_number FROM recordings WHERE id='rec1'")
            .fetchone()[0], "N-0001")

    def test_a_second_tenant_writes_its_own_series(self):
        directory = tempfile.mkdtemp()
        archive = load_module(
            "archive_recording.py", "archive_recording_tenant_b",
            env={
                "TENANT_ID": "tenant-beta",
                "TENANT_ARCHIVE_DIR": directory,
                "PLAUD_MCP_TOKEN_FILE": os.path.join(directory, "token"),
                "PLAUD_MCP_TENANT_URLS_JSON": '{"tenant-beta":"https://mcp-beta.example/mcp"}',
                "RECORDING_CODE_PREFIXES_JSON": '{"tenant-beta":"B"}',
            },
        )
        conn = sqlite3.connect(":memory:")
        archive.init_db(conn)
        self.archive_one(archive, conn, "rec1")
        self.assertEqual(
            conn.execute("SELECT recording_number FROM recordings WHERE id='rec1'")
            .fetchone()[0], "B-0001")

    def test_an_unpinned_tenant_cannot_archive(self):
        """Identity routing is the isolation boundary: an unreviewed tenant
        must fail before it can write an archive, even with a private URL."""
        directory = tempfile.mkdtemp()
        with self.assertRaises(SystemExit) as caught:
            load_module(
                "archive_recording.py", "archive_recording_unknown",
                env={
                    "TENANT_ID": "tenant-gamma",
                    "TENANT_ARCHIVE_DIR": directory,
                    "PLAUD_MCP_TOKEN_FILE": os.path.join(directory, "token"),
                    "PLAUD_MCP_EXPECTED_URL": "http://127.0.0.1:62399/mcp",
                    "PLAUD_MCP_TENANT_URLS_JSON": '{"tenant-alpha":"https://mcp-alpha.example/mcp"}',
                },
            )
        self.assertIn("not present", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
