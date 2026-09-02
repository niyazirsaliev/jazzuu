"""Tests for truthful long-ASR progress derived from archive.asr_segments.

Stdlib only (unittest + sqlite3): the viewer's runtime deps (fastapi) are not
installable in every environment, and this module deliberately has none, so the
progress rules stay testable anywhere.

The source of truth is the production writer, `asr_backfill.segment_progress`
in the plaud-recordings archive. Its classifier, which this module must mirror
exactly:

    status 'complete' AND text IS NOT NULL -> complete   ('' is a silent window)
    status 'processing'                    -> processing
    status 'error'                         -> error
    anything else                          -> pending

Status is the verdict; `last_error` is history the writer leaves on a row it has
already re-queued, and reading it as current evidence turns a queued retry into
a displayed failure. `WriterParity` below pins this down against the real writer
module when it is present, so neither side can drift alone.

Other rules under test (see app/asr_progress.py):
  * derived read-only; never invents progress it cannot prove
  * state = processing > error > pending
  * absent table / absent columns / no rows / finished transcript -> None
  * only safe fields leave the module (no last_error, no paths, no text)
"""

import importlib.util
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import asr_progress  # noqa: E402


# Verbatim from asr_backfill.ensure_segments — fixtures are writer-shaped or
# they are not evidence of anything.
FULL_DDL = """
CREATE TABLE asr_segments(
  id TEXT NOT NULL, seg_index INTEGER NOT NULL,
  start_sec INTEGER NOT NULL, duration_sec INTEGER NOT NULL,
  text TEXT, engine TEXT, meta_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT,
  status TEXT, claim_epoch INTEGER, claim_owner TEXT,
  PRIMARY KEY(id, seg_index));
"""

LEGACY_DDL_NO_STATUS = """
CREATE TABLE asr_segments(
  id TEXT, seg_index INTEGER, start_sec REAL, duration_sec REAL, text TEXT,
  engine TEXT, meta_json TEXT, PRIMARY KEY(id, seg_index));
"""

LEGACY_DDL_NO_TEXT = """
CREATE TABLE asr_segments(
  id TEXT, seg_index INTEGER, status TEXT, PRIMARY KEY(id, seg_index));
"""

# The one path that matters for `text`: NULL is "never written", any string —
# including '' — is a committed result.
_DEFAULT_TEXT = object()


def add_segments(conn, rec_id, statuses, text=_DEFAULT_TEXT, last_error=None):
    """Insert one `asr_segments` row per status.

    `text` defaults to what the writer actually leaves behind: record_segment()
    writes a string only when a window completes, and every other row keeps
    text NULL. Getting this wrong is what makes a fixture lie — under the
    writer's rules a NULL-text row labelled complete is still work to do, while
    an empty string is a finished silent window. Pass a string, None, or a
    per-row list to override deliberately.
    """
    for i, status in enumerate(statuses):
        if text is _DEFAULT_TEXT:
            done = (status or "").strip().lower() == "complete"
            t = "блок текста" if done else None
        elif isinstance(text, list):
            t = text[i]
        else:
            t = text
        conn.execute(
            "INSERT INTO asr_segments"
            "(id,seg_index,start_sec,duration_sec,text,engine,meta_json,"
            " attempts,last_error,updated_at,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (rec_id, i, i * 30, 30, t, "host",
             json.dumps({"audio_path": "/archive/audio/secret.mp3"}),
             1, last_error, "2026-08-08T10:00:00", status),
        )
    conn.commit()


class ProgressCase(unittest.TestCase):
    """Connections are registered for cleanup, so no test leaks a handle."""

    def conn_with(self, ddl=None):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)   # sqlite3.close() is idempotent
        if ddl:
            conn.executescript(ddl)
        return conn


class AbsentAndLegacySchemas(ProgressCase):
    """Requirement 3: old DBs must not break list or detail."""

    def test_absent_table_returns_none(self):
        conn = self.conn_with()
        self.assertIsNone(asr_progress.progress_for(conn, "rec1", False))

    def test_absent_table_returns_empty_batch(self):
        conn = self.conn_with()
        self.assertEqual(asr_progress.progress_map(conn, ["rec1", "rec2"]), {})

    def test_legacy_table_without_status_column_returns_none(self):
        conn = self.conn_with(LEGACY_DDL_NO_STATUS)
        conn.execute(
            "INSERT INTO asr_segments(id,seg_index,text) VALUES('rec1',0,'hi')")
        conn.commit()
        self.assertIsNone(asr_progress.progress_for(conn, "rec1", False))
        self.assertEqual(asr_progress.progress_map(conn, ["rec1"]), {})

    def test_legacy_table_without_text_column_returns_none(self):
        conn = self.conn_with(LEGACY_DDL_NO_TEXT)
        conn.execute(
            "INSERT INTO asr_segments(id,seg_index,status) "
            "VALUES('rec1',0,'complete')")
        conn.commit()
        self.assertIsNone(asr_progress.progress_for(conn, "rec1", False))
        self.assertEqual(asr_progress.progress_map(conn, ["rec1"]), {})

    def test_no_rows_for_recording_returns_none(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "other", ["processing"])
        self.assertIsNone(asr_progress.progress_for(conn, "rec1", False))
        self.assertEqual(asr_progress.progress_map(conn, ["rec1"]), {})

    def test_broken_database_returns_none(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["processing"])
        conn.close()  # every query now raises ProgrammingError
        self.assertIsNone(asr_progress.progress_for(conn, "rec1", False))
        self.assertEqual(asr_progress.progress_map(conn, ["rec1"]), {})

    def test_empty_id_list_does_not_query(self):
        conn = self.conn_with(FULL_DDL)
        self.assertEqual(asr_progress.progress_map(conn, []), {})

    def test_schema_without_last_error_still_counts(self):
        """A mid-migration archive loses nothing: last_error is never read."""
        conn = self.conn_with(
            "CREATE TABLE asr_segments("
            " id TEXT, seg_index INTEGER, text TEXT, status TEXT,"
            " PRIMARY KEY(id, seg_index));"
        )
        rows = [("готово", "complete"), (None, "processing"),
                (None, "error"), (None, "pending")]
        for i, (t, s) in enumerate(rows):
            conn.execute(
                "INSERT INTO asr_segments(id,seg_index,text,status) VALUES(?,?,?,?)",
                ("rec1", i, t, s))
        conn.commit()
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(
            (p["total"], p["complete"], p["processing"], p["error"], p["pending"]),
            (4, 1, 1, 1, 1))
        self.assertEqual(p["state"], "processing")


class TranscriptSuppression(ProgressCase):
    """Requirement 1: a finished transcript replaces progress."""

    def test_final_transcript_suppresses_progress(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["complete", "processing"])
        self.assertIsNone(asr_progress.progress_for(conn, "rec1", True))

    def test_no_transcript_keeps_progress(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["complete", "processing"])
        self.assertIsNotNone(asr_progress.progress_for(conn, "rec1", False))


class CountsAndStates(ProgressCase):
    """Requirement 2."""

    def progress(self, statuses, text=_DEFAULT_TEXT):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", statuses, text=text)
        return asr_progress.progress_for(conn, "rec1", False)

    def test_counts_and_processing_state(self):
        p = self.progress(["complete"] * 3 + ["processing"] + ["pending"] * 5)
        self.assertEqual(p["state"], "processing")
        self.assertEqual(p["total"], 9)
        self.assertEqual(p["complete"], 3)
        self.assertEqual(p["processing"], 1)
        self.assertEqual(p["error"], 0)
        self.assertEqual(p["pending"], 5)
        self.assertEqual(p["percent"], 33)

    def test_processing_wins_over_error(self):
        p = self.progress(["error", "processing", "complete"])
        self.assertEqual(p["state"], "processing")
        self.assertEqual(p["error"], 1)
        self.assertEqual(p["processing"], 1)

    def test_error_state_when_no_processing(self):
        p = self.progress(["error", "complete", "pending"])
        self.assertEqual(p["state"], "error")

    def test_pending_state_when_neither(self):
        p = self.progress(["pending", "complete"])
        self.assertEqual(p["state"], "pending")

    def test_unknown_and_null_statuses_count_as_pending(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["queued", "", "complete"])
        conn.execute(
            "INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,"
            "text,status) VALUES('rec1',9,0,30,NULL,NULL)")
        conn.commit()
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(p["total"], 4)
        self.assertEqual(p["complete"], 1)
        self.assertEqual(p["pending"], 3)
        self.assertEqual(p["state"], "pending")

    def test_status_matching_ignores_case_and_padding(self):
        # A superset of the writer's exact match: a hand-edited status still
        # reads as what it plainly says rather than silently becoming pending.
        p = self.progress([" Complete ", "PROCESSING"],
                          text=["готово", None])
        self.assertEqual(p["complete"], 1)
        self.assertEqual(p["processing"], 1)
        self.assertEqual(p["state"], "processing")

    def test_counts_always_sum_to_total(self):
        p = self.progress(["complete", "processing", "error", "pending", "?"])
        self.assertEqual(
            p["complete"] + p["processing"] + p["error"] + p["pending"],
            p["total"],
        )

    def test_all_segments_done_before_transcript_is_pending_at_100(self):
        p = self.progress(["complete", "complete"])
        self.assertEqual(p["percent"], 100)
        self.assertEqual(p["state"], "pending")

    def test_percent_is_floored(self):
        self.assertEqual(self.progress(["complete", "pending", "pending"])["percent"], 33)
        self.assertEqual(
            self.progress(["complete", "complete", "pending"])["percent"], 66)
        self.assertEqual(self.progress(["pending"] * 7)["percent"], 0)


class StatusIsAuthoritative(ProgressCase):
    """The writer's classifier, mirrored: status decides, text only proves it.

    These cases pin down every place a plausible-looking shortcut would
    disagree with asr_backfill.segment_progress, so a future edit cannot
    quietly drift back to guessing from row contents.
    """

    def test_completed_silent_window_counts(self):
        # The whole reason the writer stores '' rather than NULL: half an hour
        # of silence is a finished half hour.
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["complete", "complete"],
                     text=["слышный блок", ""])
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(p["complete"], 2, "an empty completed window is done")
        self.assertEqual(p["pending"], 0)
        self.assertEqual(p["percent"], 100)

    def test_complete_with_null_text_is_pending(self):
        # Labelled complete, never written: still work to do (done_segments()
        # refuses to splice it, so the writer will redo the window).
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["complete"], text=None)
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(p["complete"], 0)
        self.assertEqual(p["pending"], 1)
        self.assertEqual(p["state"], "pending")
        self.assertEqual(p["percent"], 0)

    def test_text_under_an_unfinished_status_is_not_completion(self):
        # Every status a half-written row could hold, all carrying text. Only
        # the status decides; the text is not a second opinion.
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["pending", "processing", "error", "queued", ""],
                     text="частичный текст")
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(p["complete"], 0, "text alone never means finished")
        self.assertEqual(p["processing"], 1)
        self.assertEqual(p["error"], 1)
        self.assertEqual(p["pending"], 3)
        self.assertEqual(p["percent"], 0)

    def test_failed_window_stays_failed_even_holding_text(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["error"], text="частично распознано",
                     last_error="ffmpeg exited 1")
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(p["error"], 1)
        self.assertEqual(p["complete"], 0)
        self.assertEqual(p["state"], "error")

    def test_historical_last_error_does_not_override_pending(self):
        # fail_segment() leaves last_error behind; the row is re-queued as
        # pending and the diagnosis stays as history. Reading it as current
        # evidence is how a queued retry gets displayed as a failure.
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["pending"], last_error="CUDA out of memory")
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(p["error"], 0)
        self.assertEqual(p["pending"], 1)
        self.assertEqual(p["state"], "pending")

    def test_running_window_with_old_error_is_processing(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", ["processing"], last_error="previous attempt died")
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(p["processing"], 1)
        self.assertEqual(p["error"], 0)
        self.assertEqual(p["state"], "processing")

    def test_silent_completion_plus_pending_retry(self):
        """The audit's reproduction, on writer-valid rows.

        A silent window that completed, and a window re-queued after a failure
        whose last_error is still on the row. The writer reads this as
        complete=1, error=0, pending=1, percent=50. Anything else displays a
        false error state and undercounts a legitimately finished window.
        """
        conn = self.conn_with(FULL_DDL)
        conn.executescript(
            "INSERT INTO asr_segments"
            "(id,seg_index,start_sec,duration_sec,text,engine,attempts,"
            " last_error,status) "
            "VALUES('rec1',0,0,1800,'','host',0,NULL,'complete');"
            "INSERT INTO asr_segments"
            "(id,seg_index,start_sec,duration_sec,text,engine,attempts,"
            " last_error,status) "
            "VALUES('rec1',1,1800,1800,NULL,NULL,1,"
            "'asr-mcp timed out after 7200s','pending');"
        )
        conn.commit()
        p = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(
            (p["complete"], p["processing"], p["error"], p["pending"]),
            (1, 0, 0, 1))
        self.assertEqual(p["percent"], 50)
        self.assertEqual(p["state"], "pending")
        self.assertEqual(p["label_ru"], "Ожидает расшифровки")

    def test_query_never_reads_last_error(self):
        """Structural guard: the column is history and must stay unread."""
        self.assertNotIn("last_error", asr_progress.COUNT_SQL)
        self.assertNotIn("last_error", asr_progress.REQUIRED_COLUMNS)


# Review ledger, verbatim from asr_backfill.ensure_reviews.
REVIEWS_DDL = """
CREATE TABLE plaud_reviews(
  id TEXT PRIMARY KEY, state TEXT NOT NULL, eligible_epoch INTEGER,
  selected_source TEXT, reason TEXT, candidates_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  claim_epoch INTEGER, claim_owner TEXT, updated_at TEXT);
"""

ATTEMPTS_DDL = """
CREATE TABLE asr_attempts(
  id TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0,
  last_at TEXT, last_error TEXT);
"""


def record(rec_id="rec1", local=False, plaud=False, engine=None):
    """One row of what the API already knows before it asks for a status.

    Booleans, never the transcripts: the list endpoint must not copy megabytes
    of text out of SQLite to answer "is there any".
    """
    return {"id": rec_id, "has_local_transcript": local,
            "has_plaud_transcript": plaud, "engine": engine}


class TranscriptStatus(ProgressCase):
    """queued -> processing -> ready/error, for rows with no blocks at all.

    Block progress only ever existed for long recordings. A 14-second one, and
    a recording whose PLAUD text is being validated against local ASR, showed
    the reader nothing whatsoever — no state, no source, no way to tell a
    finished transcript from one that never started.
    """

    def setUp(self):
        self.conn = self.conn_with(FULL_DDL + REVIEWS_DDL + ATTEMPTS_DDL)

    def review(self, rec_id, state, source=None, reason=None, candidates=None):
        self.conn.execute(
            "INSERT INTO plaud_reviews(id,state,selected_source,reason,"
            "candidates_json,attempts,last_error) VALUES(?,?,?,?,?,?,?)",
            (rec_id, state, source, reason,
             json.dumps(candidates or [{"source": "local",
                                        "text": "тайная гипотеза"}]),
             0, "Traceback: /archive/audio/secret.mp3"))
        self.conn.commit()

    def attempts(self, rec_id, count):
        self.conn.execute(
            "INSERT INTO asr_attempts(id,attempts,last_error) VALUES(?,?,?)",
            (rec_id, count, "ValueError: decode failed /archive/x.mp3"))
        self.conn.commit()

    def status(self, **kwargs):
        return asr_progress.status_for(self.conn, record(**kwargs))

    def test_a_textless_recording_with_no_blocks_is_queued(self):
        self.assertEqual(self.status()["state"], "queued")

    def test_a_recording_being_transcribed_in_blocks_is_processing(self):
        add_segments(self.conn, "rec1", ["complete", "processing", "pending"])
        self.assertEqual(self.status()["state"], "processing")

    def test_a_finished_segmented_recording_is_ready(self):
        add_segments(self.conn, "rec1", ["complete", "complete"])
        got = self.status(local=True, engine="large-v3")
        self.assertEqual((got["state"], got["source"], got["engine"]),
                         ("ready", "local", "large-v3"))

    def test_a_failed_block_is_an_error(self):
        add_segments(self.conn, "rec1", ["complete", "error"])
        self.assertEqual(self.status()["state"], "error")

    def test_a_local_transcript_with_no_blocks_is_ready(self):
        got = self.status(local=True, engine="kyrgyz")
        self.assertEqual((got["state"], got["source"], got["engine"]),
                         ("ready", "local", "kyrgyz"))

    def test_plaud_text_in_an_archive_with_no_review_ledger_is_ready(self):
        """Legacy archives have no plaud_reviews table at all."""
        conn = self.conn_with(FULL_DDL)
        got = asr_progress.status_for(conn, record(plaud=True))
        self.assertEqual((got["state"], got["source"]), ("ready", "plaud"))

    def test_an_enrolled_plaud_recording_is_queued_for_validation(self):
        self.review("rec1", "queued")
        got = self.status(plaud=True)
        self.assertEqual((got["state"], got["source"]), ("queued", "plaud"))

    def test_a_validation_in_flight_is_processing(self):
        self.review("rec1", "processing")
        got = self.status(plaud=True)
        self.assertEqual((got["state"], got["source"]), ("processing", "plaud"))

    def test_a_reviewed_plaud_winner_is_ready_and_says_so(self):
        self.review("rec1", "reviewed", source="plaud", reason="plaud-adequate")
        got = self.status(plaud=True)
        self.assertEqual((got["state"], got["source"]), ("ready", "plaud"))

    def test_a_reviewed_local_winner_is_ready_from_local(self):
        self.review("rec1", "reviewed", source="local",
                    reason="local-clearly-better")
        got = self.status(local=True, plaud=True, engine="large-v3")
        self.assertEqual((got["state"], got["source"], got["engine"]),
                         ("ready", "local", "large-v3"))

    def test_a_failed_review_is_an_error_that_still_names_the_visible_text(self):
        self.review("rec1", "error")
        got = self.status(plaud=True)
        self.assertEqual((got["state"], got["source"]), ("error", "plaud"))

    def test_a_recording_that_ran_out_of_retries_is_an_error(self):
        self.attempts("rec1", asr_progress.MAX_ATTEMPTS)
        self.assertEqual(self.status()["state"], "error")

    def test_a_recording_still_inside_its_retry_budget_stays_queued(self):
        self.attempts("rec1", asr_progress.MAX_ATTEMPTS - 1)
        self.assertEqual(self.status()["state"], "queued")

    def test_only_safe_fields_ever_leave_the_module(self):
        self.review("rec1", "processing")
        add_segments(self.conn, "rec1", ["processing"])
        self.attempts("rec1", 1)
        got = self.status(plaud=True)
        self.assertEqual(set(got), set(asr_progress.STATUS_FIELDS))
        blob = json.dumps(got, ensure_ascii=False)
        for leak in ("тайная", "Traceback", "/archive", ".mp3", "candidates",
                     "last_error", "decode failed"):
            self.assertNotIn(leak, blob, f"leaked {leak!r}")

    def test_a_page_of_recordings_is_derived_in_one_pass(self):
        self.review("a", "processing")
        self.review("b", "reviewed", source="local", reason="local-clearly-better")
        add_segments(self.conn, "c", ["complete", "pending"])
        got = asr_progress.status_map(self.conn, [
            record("a", plaud=True), record("b", local=True, plaud=True),
            record("c"), record("d", local=True),
        ])
        self.assertEqual({key: value["state"] for key, value in got.items()},
                         {"a": "processing", "b": "ready", "c": "processing",
                          "d": "ready"})

    def test_labels_are_concise_russian_and_name_the_source(self):
        self.review("rec1", "queued")
        self.assertEqual(self.status(plaud=True)["label_ru"],
                         "Текст PLAUD · ожидает проверки")
        self.conn.execute("UPDATE plaud_reviews SET state='processing'")
        self.conn.commit()
        self.assertEqual(self.status(plaud=True)["label_ru"],
                         "Текст PLAUD · идёт проверка")
        self.conn.execute(
            "UPDATE plaud_reviews SET state='reviewed', selected_source='plaud'")
        self.conn.commit()
        self.assertEqual(self.status(plaud=True)["label_ru"], "Текст PLAUD")
        self.conn.execute(
            "UPDATE plaud_reviews SET selected_source='local'")
        self.conn.commit()
        self.assertEqual(self.status(local=True, plaud=True)["label_ru"],
                         "Расшифровка Barston ASR")

    def test_the_queued_label_for_a_textless_recording_is_unchanged(self):
        self.assertEqual(self.status()["label_ru"], "Ожидает расшифровки")

    def test_labels_never_promise_a_time(self):
        for state in ("queued", "processing", "error"):
            self.conn.execute("DELETE FROM plaud_reviews")
            self.review("rec1", state)
            label = self.status(plaud=True)["label_ru"]
            for word in ("сек", "мин", "час", "осталось", "~"):
                self.assertNotIn(word, label, label)


WRITER_PATH = os.environ.get(
    "ASR_BACKFILL_PATH",
    "archive/asr_backfill.py",
)

# The writer this checkout ships, as opposed to whatever is deployed beside it.
LOCAL_WRITER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "archive", "asr_backfill.py")


def _load_local_writer():
    spec = importlib.util.spec_from_file_location(
        "asr_backfill_local_ref", LOCAL_WRITER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.path.exists(LOCAL_WRITER_PATH),
                     f"writer not present at {LOCAL_WRITER_PATH}")
class ReviewStateParity(ProgressCase):
    """The status model quotes the writer's vocabulary; pin both to one source.

    A writer that renames `reviewed` leaves the viewer reporting every finished
    review as an unknown state, which reads as "queued" — a recording the
    reader is told is still waiting, forever.
    """

    def test_the_state_literals_are_the_writers(self):
        writer = _load_local_writer()
        self.assertEqual(asr_progress.REVIEW_QUEUED, writer.RV_QUEUED)
        self.assertEqual(asr_progress.REVIEW_PROCESSING, writer.RV_PROCESSING)
        self.assertEqual(asr_progress.REVIEW_REVIEWED, writer.RV_REVIEWED)
        self.assertEqual(asr_progress.REVIEW_ERROR, writer.RV_ERROR)

    def test_the_retry_budget_is_the_writers(self):
        self.assertEqual(asr_progress.MAX_ATTEMPTS, _load_local_writer().MAX_ATTEMPTS)


def _load_writer():
    spec = importlib.util.spec_from_file_location("asr_backfill_ref", WRITER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.path.exists(WRITER_PATH),
                     f"production writer not present at {WRITER_PATH}")
class WriterParity(ProgressCase):
    """Differential test against the real asr_backfill.segment_progress.

    The viewer's numbers are a claim about what the writer is doing. This runs
    both classifiers over the same rows and fails if they ever disagree — the
    only check that cannot rot into implementation-agrees-with-its-own-fixture.
    """

    # Every (status, text) pair the writer can produce or leave behind, plus
    # the malformed ones an interrupted run can strand. Statuses are the
    # writer's exact literals: the viewer additionally tolerates case and
    # padding, which is a documented superset and tested separately.
    MATRIX = [
        ("complete", "распознанный текст"),
        ("complete", ""),                     # silent window: finished
        ("complete", None),                   # labelled, never written
        ("processing", None),
        ("processing", "частичный"),
        ("error", None),
        ("error", "частичный"),
        ("pending", None),
        ("pending", "частичный"),
        ("", None),
        (None, None),
        ("queued", None),
    ]

    def test_classifiers_agree_row_for_row(self):
        writer = _load_writer()
        for i, (status, text) in enumerate(self.MATRIX):
            with self.subTest(status=status, text=text):
                conn = self.conn_with(FULL_DDL)
                conn.execute(
                    "INSERT INTO asr_segments(id,seg_index,start_sec,"
                    "duration_sec,text,status,last_error) "
                    "VALUES('rec1',?,?,30,?,?,'старая диагностика')",
                    (i, i * 30, text, status))
                conn.commit()
                theirs = writer.segment_progress(conn, "rec1")
                ours = asr_progress.progress_for(conn, "rec1", False)
                for bucket in ("total", "complete", "processing", "error", "pending"):
                    self.assertEqual(ours[bucket], theirs[bucket], bucket)

    def test_classifiers_agree_on_a_whole_recording(self):
        writer = _load_writer()
        conn = self.conn_with(FULL_DDL)
        for i, (status, text) in enumerate(self.MATRIX):
            conn.execute(
                "INSERT INTO asr_segments(id,seg_index,start_sec,duration_sec,"
                "text,status,last_error) "
                "VALUES('rec1',?,?,30,?,?,'старая диагностика')",
                (i, i * 30, text, status))
        conn.commit()
        theirs = writer.segment_progress(conn, "rec1")
        ours = asr_progress.progress_for(conn, "rec1", False)
        for bucket in ("total", "complete", "processing", "error", "pending"):
            self.assertEqual(ours[bucket], theirs[bucket], bucket)
        # The viewer floors to a whole percent on purpose (the bar and the badge
        # are integers); the writer keeps one decimal. Same numerator either way.
        self.assertEqual(ours["percent"], int(theirs["percent"]))

    def test_silent_completion_plus_pending_retry_matches_the_writer(self):
        writer = _load_writer()
        conn = self.conn_with(FULL_DDL)
        conn.executescript(
            "INSERT INTO asr_segments"
            "(id,seg_index,start_sec,duration_sec,text,status,last_error) "
            "VALUES('rec1',0,0,1800,'','complete',NULL);"
            "INSERT INTO asr_segments"
            "(id,seg_index,start_sec,duration_sec,text,status,last_error) "
            "VALUES('rec1',1,1800,1800,NULL,'pending','timed out');"
        )
        conn.commit()
        theirs = writer.segment_progress(conn, "rec1")
        ours = asr_progress.progress_for(conn, "rec1", False)
        self.assertEqual(
            (theirs["complete"], theirs["error"], theirs["pending"]), (1, 0, 1))
        self.assertEqual(
            (ours["complete"], ours["error"], ours["pending"]), (1, 0, 1))
        self.assertEqual(ours["percent"], 50)


class SafeFieldsOnly(ProgressCase):
    """Requirement 1: never expose last_error, paths or segment text."""

    def payload(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(
            conn, "rec1", ["error", "complete"],
            text=["совершенно секретный текст", "ещё секретный текст"],
            last_error="Traceback: /archive/audio/rec1.mp3 exploded",
        )
        return asr_progress.progress_for(conn, "rec1", False)

    def test_exact_key_set(self):
        self.assertEqual(set(self.payload()), set(asr_progress.SAFE_FIELDS))

    def test_no_secret_substrings_anywhere(self):
        blob = json.dumps(self.payload(), ensure_ascii=False)
        for leak in ("секретный", "Traceback", "/archive", ".mp3", "host",
                     "audio_path", "meta_json", "last_error", "updated_at"):
            self.assertNotIn(leak, blob, f"leaked {leak!r}")

    def test_values_are_json_safe_scalars(self):
        for key, value in self.payload().items():
            self.assertIsInstance(value, (str, int), key)
            self.assertNotIsInstance(value, bool, key)


class RussianLabels(ProgressCase):
    """Requirement 4: truthful Russian labels, no invented elapsed time."""

    def progress(self, statuses):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "rec1", statuses)
        return asr_progress.progress_for(conn, "rec1", False)

    def test_processing_label(self):
        p = self.progress(["complete"] * 3 + ["processing"] + ["pending"] * 5)
        self.assertEqual(p["label_ru"], "Расшифровывается · 3 из 9 блоков")

    def test_error_label(self):
        p = self.progress(["error", "pending"])
        self.assertEqual(p["label_ru"], "Ошибка блока · повторится автоматически")

    def test_pending_label(self):
        p = self.progress(["pending", "pending"])
        self.assertEqual(p["label_ru"], "Ожидает расшифровки")

    def test_block_plural_forms(self):
        # The count sits after "из", so the noun is genitive: "из 1 блока",
        # "из 9 блоков" — not the nominative counting form "1 блок".
        self.assertEqual(
            self.progress(["processing"])["label_ru"],
            "Расшифровывается · 0 из 1 блока",
        )
        self.assertEqual(
            self.progress(["processing", "pending"])["label_ru"],
            "Расшифровывается · 0 из 2 блоков",
        )
        self.assertEqual(asr_progress.blocks_genitive_ru(1), "1 блока")
        self.assertEqual(asr_progress.blocks_genitive_ru(2), "2 блоков")
        self.assertEqual(asr_progress.blocks_genitive_ru(4), "4 блоков")
        self.assertEqual(asr_progress.blocks_genitive_ru(5), "5 блоков")
        self.assertEqual(asr_progress.blocks_genitive_ru(11), "11 блоков")
        self.assertEqual(asr_progress.blocks_genitive_ru(21), "21 блока")
        self.assertEqual(asr_progress.blocks_genitive_ru(22), "22 блоков")
        self.assertEqual(asr_progress.blocks_genitive_ru(112), "112 блоков")

    def test_labels_never_promise_a_time(self):
        for statuses in (["processing", "pending"], ["error"], ["pending"]):
            label = self.progress(statuses)["label_ru"]
            for word in ("сек", "мин", "час", "осталось", "~"):
                self.assertNotIn(word, label, label)


class BatchMap(ProgressCase):
    """The list endpoint derives every card's progress in one pass."""

    def test_map_covers_only_recordings_with_rows(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "a", ["complete", "processing"])
        add_segments(conn, "b", ["error"])
        got = asr_progress.progress_map(conn, ["a", "b", "c"])
        self.assertEqual(set(got), {"a", "b"})
        self.assertEqual(got["a"]["state"], "processing")
        self.assertEqual(got["a"]["percent"], 50)
        self.assertEqual(got["b"]["state"], "error")

    def test_map_payloads_carry_only_safe_fields(self):
        conn = self.conn_with(FULL_DDL)
        add_segments(conn, "a", ["processing"], last_error="boom /archive/x")
        blob = json.dumps(asr_progress.progress_map(conn, ["a"]),
                          ensure_ascii=False)
        self.assertNotIn("boom", blob)
        self.assertNotIn("/archive", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
