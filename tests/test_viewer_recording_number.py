"""The viewer exposes the permanent recording code the archive allocated.

Two layers are covered separately on purpose. The read helper is pure and is
tested against real SQLite, including an archive written before codes existed,
because that is the case that would otherwise 500 the feed for everyone. The
endpoints themselves are tested through FastAPI where FastAPI is installed, and
their contract — that both of them actually hand the field to the client — is
asserted from the source so it holds even where it is not.
"""
import ast
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from viewer.app import recording_number  # noqa: E402  (path set above)

MAIN_PY = ROOT / "viewer" / "app" / "main.py"

try:
    importlib.import_module("fastapi")
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_FASTAPI = False


def make_db(with_number=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE recordings(
           id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
           duration_ms INTEGER, lang TEXT, asr_engine TEXT, asr_transcript TEXT,
           plaud_transcript TEXT, summary TEXT)"""
    )
    if with_number:
        conn.execute("ALTER TABLE recordings ADD COLUMN recording_number TEXT")
    conn.commit()
    return conn


class ReadHelperTests(unittest.TestCase):
    def test_the_field_is_selected_when_the_archive_has_it(self):
        conn = make_db()
        conn.execute(
            "INSERT INTO recordings(id,name,recording_number) VALUES('r1','Имя','N-0042')")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        row = conn.execute(
            f"SELECT id,{recording_number.select_expr(columns)} FROM recordings"
        ).fetchone()
        self.assertEqual(row["recording_number"], "N-0042")

    def test_an_archive_written_before_codes_still_answers(self):
        # The viewer is deployed independently of the archive writer, so it
        # will meet a database that has no such column. Falling over there
        # would take the whole feed down for a cosmetic field.
        conn = make_db(with_number=False)
        conn.execute("INSERT INTO recordings(id,name) VALUES('r1','Имя')")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        row = conn.execute(
            f"SELECT id,{recording_number.select_expr(columns)} FROM recordings"
        ).fetchone()
        self.assertIsNone(row["recording_number"])

    def test_public_value_is_the_canonical_code_or_nothing(self):
        self.assertEqual(recording_number.public("N-0042"), "N-0042")
        self.assertEqual(recording_number.public("  d-0042 "), "D-0042")
        for empty in (None, "", "   "):
            self.assertIsNone(recording_number.public(empty))

    def test_public_value_never_invents_a_code(self):
        # Anything that is not a code shape is dropped rather than shown: a
        # half-rendered code on screen is worse than none.
        for junk in ("42", "N42", "/archive/audio/rec1.mp3", "N-0042 extra", 42):
            self.assertIsNone(recording_number.public(junk))


class EndpointContractTests(unittest.TestCase):
    """Both endpoints must hand the field to the client, on any schema."""

    def setUp(self):
        self.tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        self.sources = {}
        source = MAIN_PY.read_text(encoding="utf-8")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                self.sources[node.name] = ast.get_source_segment(source, node) or ""

    def test_the_feed_returns_the_code(self):
        body = self.sources["list_recordings"]
        self.assertIn('"recording_number"', body)
        self.assertIn("select_expr", body)

    def test_the_detail_view_returns_the_code(self):
        body = self.sources["get_recording"]
        self.assertIn('"recording_number"', body)
        self.assertIn("select_expr", body)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi is not installed in this environment")
class EndpointTests(unittest.TestCase):
    """Both endpoints, driven exactly as the app drives them.

    Each endpoint opens a connection and closes it in a `finally`, because in
    production `db()` hands out a NEW connection every call. The fixture has to
    behave the same way: handing the same connection to both calls would leave
    the second one reading a database the first had already closed — a fault in
    the test, not in the viewer.
    """

    def make_archive(self, with_number=True, row=None):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.addCleanup(
            lambda: os.path.exists(handle.name) and os.remove(handle.name))
        conn = sqlite3.connect(handle.name)
        conn.execute(
            """CREATE TABLE recordings(
               id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
               duration_ms INTEGER, lang TEXT, asr_engine TEXT,
               asr_transcript TEXT, plaud_transcript TEXT, summary TEXT)"""
        )
        if with_number:
            conn.execute("ALTER TABLE recordings ADD COLUMN recording_number TEXT")
        conn.execute(*row)
        conn.commit()
        conn.close()

        def open_db():
            fresh = sqlite3.connect(handle.name)
            fresh.row_factory = sqlite3.Row
            return fresh

        from viewer.app import main
        return main, mock.patch.object(main, "db", side_effect=open_db)

    def test_feed_and_detail_expose_the_code(self):
        main, patched = self.make_archive(row=(
            "INSERT INTO recordings(id,name,start_at,recording_number) "
            "VALUES('r1','Имя','2026-01-01 09:00:00','N-0042')",))
        with patched:
            feed = main.list_recordings()
            detail = main.get_recording("r1")
        self.assertEqual(feed[0]["recording_number"], "N-0042")
        self.assertEqual(detail["recording_number"], "N-0042")

    def test_feed_and_detail_survive_an_archive_without_the_column(self):
        # The viewer ships independently of the archive writer, so it will meet
        # a database migrated later — or never. Both endpoints must answer.
        main, patched = self.make_archive(with_number=False, row=(
            "INSERT INTO recordings(id,name,start_at) "
            "VALUES('r1','Имя','2026-01-01 09:00:00')",))
        with patched:
            feed = main.list_recordings()
            detail = main.get_recording("r1")
        self.assertIsNone(feed[0]["recording_number"])
        self.assertIsNone(detail["recording_number"])
        # The rest of the payload is unaffected: backward compatibility means
        # the feed still works, not merely that it does not raise.
        self.assertEqual(feed[0]["name"], "Имя")
        self.assertEqual(detail["name"], "Имя")


if __name__ == "__main__":
    unittest.main()
