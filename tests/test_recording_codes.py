"""Permanent, human-friendly recording codes.

The code is the handle a human — or a bot speaking for one — uses to name a
recording out loud. That makes two properties load-bearing and worth testing
hard: a code is allocated exactly once and never moves afterwards (a rename, a
re-sync or a neighbouring row's deletion must not touch it), and a code is
never DERIVED at read time, because "the 68th row" changes meaning the moment
row 12 is deleted.
"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_codes():
    path = ROOT / "archive" / "recording_codes.py"
    spec = importlib.util.spec_from_file_location("recording_codes_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


codes = load_codes()


def temp_db():
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    return handle.name


def make_conn(path=":memory:"):
    conn = sqlite3.connect(path, timeout=30)
    conn.execute(
        """CREATE TABLE recordings(
           id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
           archived_at TEXT, duration_ms INTEGER)"""
    )
    conn.commit()
    return conn


def insert(conn, rec_id, start_at=None, created_at=None, archived_at=None):
    conn.execute(
        "INSERT INTO recordings(id,start_at,created_at,archived_at) VALUES(?,?,?,?)",
        (rec_id, start_at, created_at, archived_at),
    )
    conn.commit()


def number_of(conn, rec_id):
    row = conn.execute(
        "SELECT recording_number FROM recordings WHERE id=?", (rec_id,)
    ).fetchone()
    return row[0] if row else None


class PrefixTests(unittest.TestCase):
    def env(self, mapping=None):
        return {"RECORDING_CODE_PREFIXES_JSON": json.dumps(
            mapping or {"tenant-alpha": "A", "tenant-beta": "B"})}

    def test_each_tenant_has_its_own_pinned_prefix(self):
        self.assertEqual(codes.prefix_for("tenant-alpha", self.env()), "A")
        self.assertEqual(codes.prefix_for("tenant-beta", self.env()), "B")

    def test_unknown_tenant_fails_closed(self):
        # An unconfigured or misspelled tenant must never fall back to a
        # default prefix: that would file one tenant's recording under another's
        # series.
        for bad in ("", "  ", "tenant-gamma", None):
            with self.assertRaises(codes.UnknownTenantError):
                codes.prefix_for(bad, self.env())

    def test_mapping_is_required_and_prefixes_are_unique(self):
        for env in ({}, self.env({"tenant-alpha": "A", "tenant-beta": "A"})):
            with self.assertRaises(codes.UnknownTenantError):
                codes.prefix_for("tenant-alpha", env)

    def test_codes_are_zero_padded_to_four_digits(self):
        self.assertEqual(codes.format_code("N", 1), "N-0001")
        self.assertEqual(codes.format_code("D", 68), "D-0068")
        self.assertEqual(codes.format_code("B", 1234), "B-1234")
        # Past four digits the code simply grows; it is never truncated or
        # wrapped, because a wrapped code would collide with a live one.
        self.assertEqual(codes.format_code("N", 12345), "N-12345")


class ParseTests(unittest.TestCase):
    def test_canonical_code_is_accepted_case_insensitively(self):
        for text in ("N-0068", "n-0068", "  N-0068  ", "n-68"):
            self.assertEqual(codes.parse_code(text, "N"), "N-0068")

    def test_numeric_shorthand_resolves_against_the_configured_scope(self):
        # A single-tenant process knows which series "68" means. This is the
        # only context in which the prefix may be inferred.
        self.assertEqual(codes.parse_code("68", "D"), "D-0068")
        self.assertEqual(codes.parse_code(" 0068 ", "B"), "B-0068")
        self.assertEqual(codes.parse_code(68, "N"), "N-0068")

    def test_another_tenants_prefix_is_refused_in_this_scope(self):
        # Cross-tenant denial happens before any lookup: a tenant-b-shaped code
        # asked of the owner's archive is refused outright, and the refusal
        # says nothing about whether such a recording exists anywhere.
        with self.assertRaises(codes.InvalidCodeError) as caught:
            codes.parse_code("D-0001", "N")
        message = str(caught.exception)
        self.assertNotIn("tenant-b", message.lower())
        self.assertIn("D-0001", message)

    def test_malformed_codes_are_refused(self):
        for bad in ("", "   ", "N-", "-0001", "NN-0001", "N-abc", "N-0001-2",
                    "N 0001", "0", "-1", "N--1", None, 1.5, "N-99999999"):
            with self.assertRaises(codes.InvalidCodeError):
                codes.parse_code(bad, "N")

    def test_shorthand_requires_a_known_scope(self):
        with self.assertRaises(codes.UnknownTenantError):
            codes.parse_code("68", "")


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_is_idempotent_and_migrates_an_existing_table(self):
        conn = make_conn()
        insert(conn, "rec1")
        codes.ensure_schema(conn)
        codes.ensure_schema(conn)  # a second run must be a no-op
        columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        self.assertIn("recording_number", columns)
        self.assertIsNone(number_of(conn, "rec1"))

    def test_the_column_is_unique(self):
        conn = make_conn()
        insert(conn, "rec1")
        insert(conn, "rec2")
        codes.ensure_schema(conn)
        conn.execute("UPDATE recordings SET recording_number='N-0001' WHERE id='rec1'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE recordings SET recording_number='N-0001' WHERE id='rec2'")

    def test_several_rows_may_still_be_unnumbered(self):
        # The uniqueness rule must not collapse the not-yet-allocated rows into
        # one another; SQLite treats NULLs as distinct, and the migration
        # depends on that.
        conn = make_conn()
        insert(conn, "rec1")
        insert(conn, "rec2")
        codes.ensure_schema(conn)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM recordings WHERE recording_number IS NULL"
            ).fetchone()[0], 2)


class AllocationTests(unittest.TestCase):
    def test_allocation_is_idempotent(self):
        conn = make_conn()
        insert(conn, "rec1")
        codes.ensure_schema(conn)
        first = codes.allocate_number(conn, "N", "rec1")
        second = codes.allocate_number(conn, "N", "rec1")
        self.assertEqual(first, "N-0001")
        self.assertEqual(second, "N-0001")

    def test_numbers_are_never_reused_after_deletion(self):
        # Deleting the newest recording must not hand its number to the next
        # one archived: the code was already spoken out loud.
        conn = make_conn()
        codes.ensure_schema(conn)
        for rec_id in ("rec1", "rec2"):
            insert(conn, rec_id)
            codes.allocate_number(conn, "N", rec_id)
        conn.execute("DELETE FROM recordings WHERE id='rec2'")
        conn.commit()
        insert(conn, "rec3")
        self.assertEqual(codes.allocate_number(conn, "N", "rec3"), "N-0003")

    def test_a_rename_or_resync_leaves_the_number_alone(self):
        conn = make_conn()
        insert(conn, "rec1")
        codes.ensure_schema(conn)
        codes.allocate_number(conn, "N", "rec1")
        conn.execute("UPDATE recordings SET name='переименовано' WHERE id='rec1'")
        conn.commit()
        codes.backfill(conn, "N")
        self.assertEqual(number_of(conn, "rec1"), "N-0001")

    def test_allocating_for_an_unknown_row_fails_rather_than_inventing_one(self):
        conn = make_conn()
        codes.ensure_schema(conn)
        with self.assertRaises(codes.UnknownRecordingError):
            codes.allocate_number(conn, "N", "ghost")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0], 0)

    def test_numbering_never_counts_rows(self):
        # A read-time COUNT(*) would renumber the whole archive the first time
        # a row is deleted. The stored value is the only source of truth.
        conn = make_conn()
        codes.ensure_schema(conn)
        for rec_id in ("rec1", "rec2", "rec3"):
            insert(conn, rec_id)
            codes.allocate_number(conn, "N", rec_id)
        conn.execute("DELETE FROM recordings WHERE id='rec1'")
        conn.commit()
        self.assertEqual(number_of(conn, "rec2"), "N-0002")
        self.assertEqual(number_of(conn, "rec3"), "N-0003")


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db()
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_backfill_orders_deterministically_by_recording_time(self):
        conn = make_conn(self.path)
        codes.ensure_schema(conn)
        # Inserted deliberately out of order, and one row carries no start_at
        # so the created_at fallback is exercised.
        insert(conn, "c", start_at="2026-03-01 09:00:00")
        insert(conn, "a", start_at="2026-01-01 09:00:00")
        insert(conn, "d", start_at=None, created_at="2026-04-01 09:00:00")
        insert(conn, "b", start_at="2026-02-01 09:00:00")
        assigned = codes.backfill(conn, "N")
        self.assertEqual(assigned, 4)
        self.assertEqual(
            [number_of(conn, rid) for rid in ("a", "b", "c", "d")],
            ["N-0001", "N-0002", "N-0003", "N-0004"],
        )

    def test_backfill_is_stable_across_repeated_runs(self):
        conn = make_conn(self.path)
        codes.ensure_schema(conn)
        for i, rec_id in enumerate(("a", "b", "c")):
            insert(conn, rec_id, start_at=f"2026-01-0{i + 1} 09:00:00")
        codes.backfill(conn, "N")
        before = {rid: number_of(conn, rid) for rid in ("a", "b", "c")}
        self.assertEqual(codes.backfill(conn, "N"), 0)  # nothing left to do
        after = {rid: number_of(conn, rid) for rid in ("a", "b", "c")}
        self.assertEqual(before, after)

    def test_backfill_never_reassigns_and_continues_the_series(self):
        conn = make_conn(self.path)
        codes.ensure_schema(conn)
        insert(conn, "old", start_at="2026-01-01 09:00:00")
        codes.backfill(conn, "N")
        insert(conn, "older", start_at="2020-01-01 09:00:00")
        insert(conn, "newer", start_at="2026-06-01 09:00:00")
        self.assertEqual(codes.backfill(conn, "N"), 2)
        # The pre-existing row keeps N-0001 even though an older recording has
        # since appeared; only the new rows take fresh numbers, in order.
        self.assertEqual(number_of(conn, "old"), "N-0001")
        self.assertEqual(number_of(conn, "older"), "N-0002")
        self.assertEqual(number_of(conn, "newer"), "N-0003")

    def test_a_second_process_backfilling_the_same_db_assigns_nothing_twice(self):
        conn = make_conn(self.path)
        codes.ensure_schema(conn)
        for i in range(5):
            insert(conn, f"rec{i}", start_at=f"2026-01-0{i + 1} 09:00:00")
        conn.close()

        results = []
        errors = []
        barrier = threading.Barrier(2)

        def run_backfill():
            worker = sqlite3.connect(self.path, timeout=30)
            try:
                barrier.wait()
                results.append(codes.backfill(worker, "N"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                worker.close()

        threads = [threading.Thread(target=run_backfill) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(results), 5, "each row must be numbered exactly once")
        check = sqlite3.connect(self.path)
        try:
            numbers = [r[0] for r in check.execute(
                "SELECT recording_number FROM recordings ORDER BY recording_number")]
        finally:
            check.close()
        self.assertEqual(numbers,
                         ["N-0001", "N-0002", "N-0003", "N-0004", "N-0005"])


class RaceTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db()
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_concurrent_allocation_hands_out_distinct_numbers(self):
        conn = make_conn(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        codes.ensure_schema(conn)
        ids = [f"rec{i}" for i in range(12)]
        for rec_id in ids:
            insert(conn, rec_id)
        conn.close()

        allocated = []
        errors = []
        barrier = threading.Barrier(len(ids))

        def allocate(rec_id):
            worker = sqlite3.connect(self.path, timeout=30)
            try:
                barrier.wait()
                allocated.append(codes.allocate_number(worker, "N", rec_id))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                worker.close()

        threads = [threading.Thread(target=allocate, args=(rec_id,)) for rec_id in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(allocated), len(ids))
        self.assertEqual(len(set(allocated)), len(ids), "numbers collided")
        self.assertEqual(sorted(allocated),
                         [codes.format_code("N", n) for n in range(1, len(ids) + 1)])

    def test_the_same_recording_allocated_concurrently_gets_one_number(self):
        conn = make_conn(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        codes.ensure_schema(conn)
        insert(conn, "rec1")
        conn.close()

        allocated = []
        errors = []
        barrier = threading.Barrier(6)

        def allocate():
            worker = sqlite3.connect(self.path, timeout=30)
            try:
                barrier.wait()
                allocated.append(codes.allocate_number(worker, "N", "rec1"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                worker.close()

        threads = [threading.Thread(target=allocate) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(set(allocated), {"N-0001"})


class LookupTests(unittest.TestCase):
    def test_lookup_is_by_stored_code_not_by_position(self):
        conn = make_conn()
        codes.ensure_schema(conn)
        for rec_id in ("rec1", "rec2", "rec3"):
            insert(conn, rec_id)
            codes.allocate_number(conn, "N", rec_id)
        conn.execute("DELETE FROM recordings WHERE id='rec1'")
        conn.commit()
        self.assertEqual(codes.resolve(conn, "N-0003", "N"), "rec3")
        self.assertIsNone(codes.resolve(conn, "N-0001", "N"))

    def test_lookup_accepts_shorthand_and_case(self):
        conn = make_conn()
        codes.ensure_schema(conn)
        insert(conn, "rec1")
        codes.allocate_number(conn, "N", "rec1")
        self.assertEqual(codes.resolve(conn, "1", "N"), "rec1")
        self.assertEqual(codes.resolve(conn, "n-0001", "N"), "rec1")


if __name__ == "__main__":
    unittest.main()
