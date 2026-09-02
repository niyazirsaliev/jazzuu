"""The deployed asr-mcp summarize_transcript schema is deliberately narrow."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))
import summary_backfill


def test_summary_call_rejects_domain_error_payload(monkeypatch):
    monkeypatch.setattr(summary_backfill, "_post", lambda *args, **kwargs: ({
        "result": {"structuredContent": {"result": {
            "status": "error", "detail": "private upstream diagnostic"}}},
    }, "session"))

    try:
        summary_backfill.call("summarize_transcript", {"transcript": "private"}, "session")
    except summary_backfill.McpToolError as exc:
        assert str(exc) == "summary MCP tool call failed"
        assert "private upstream diagnostic" not in str(exc)
    else:
        raise AssertionError("domain-level MCP error must not become summary data")


def test_summary_call_unwraps_fastmcp_result_payload(monkeypatch):
    payload = {"status": "ok", "summary_markdown": "# Итог"}
    monkeypatch.setattr(summary_backfill, "_post", lambda *args, **kwargs: ({
        "result": {"structuredContent": {"result": payload}},
    }, "session"))

    assert summary_backfill.call("summarize_transcript", {}, "session") == payload


def test_summary_call_uses_exact_deployed_kwargs(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,asr_transcript TEXT,plaud_transcript TEXT,summary TEXT,summary_json TEXT)")
    conn.execute("INSERT INTO recordings VALUES ('r', 'Title', ?, '', '', '')", ("x" * 200,))
    captured = {}
    monkeypatch.setattr(summary_backfill, "call", lambda name, args, sid: captured.update(name=name, args=args, sid=sid) or {"summary":"# ok", "structured":{"overview":"ok"}})
    presentation = "[Аня] " + "x" * 200
    summary_backfill.backfill_one(conn, "session", "r", force=True, transcript=presentation)
    assert captured == {"name":"summarize_transcript", "args":{"transcript":presentation, "title":"Title", "label_catalogue":[{"id":"business","name":"Работа"},{"id":"idea","name":"Идея"},{"id":"personal","name":"Личное"}]}, "sid":"session"}


def test_summary_categories_publish_auto_labels_without_overwriting_manual_override(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,asr_transcript TEXT,plaud_transcript TEXT,summary TEXT,summary_json TEXT)")
    conn.execute("INSERT INTO recordings VALUES ('r', 'Title', ?, '', '', '')", ("x" * 200,))
    summary_backfill.ensure_label_schema(conn)
    conn.execute("INSERT INTO recording_labels VALUES('r','personal',0,0,'old')")
    monkeypatch.setattr(summary_backfill, "call", lambda *_: {
        "summary": "# ok", "structured": {"overview": "ok", "categories": ["personal", "business"]}})

    summary_backfill.backfill_one(conn, "session", "r", force=True)

    # 'idea' is in the catalogue but not in this recording's categories, so it
    # is recorded as not-auto-assigned rather than omitted.
    assert conn.execute(
        "SELECT label_id,auto_assigned,manual_override FROM recording_labels "
        "WHERE recording_id='r' ORDER BY label_id").fetchall() == [
        ('business', 1, None), ('idea', 0, None), ('personal', 1, 0)]


def test_existing_structured_categories_backfill_without_a_model_call_and_keep_manual_removal():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY,name TEXT,asr_transcript TEXT,plaud_transcript TEXT,summary TEXT,summary_json TEXT)")
    conn.execute("INSERT INTO recordings VALUES ('r', 'Title', '', '', '', ?)", (
        json.dumps({"categories": ["personal", "business"]}),))
    summary_backfill.ensure_label_schema(conn)
    conn.execute("INSERT INTO recording_labels VALUES('r','personal',0,0,'old')")
    summary_backfill.ensure_attempts(conn)
    assert conn.execute(
        "SELECT label_id,auto_assigned,manual_override FROM recording_labels "
        "WHERE recording_id='r' ORDER BY label_id").fetchall() == [
        ('business', 1, None), ('idea', 0, None), ('personal', 1, 0)]


def _category_backfill_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE recordings(
        id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT, asr_transcript TEXT,
        plaud_transcript TEXT, summary TEXT, summary_json TEXT)""")
    summary_backfill.ensure_attempts(conn)
    return conn


def _summary_job(conn, rid):
    return conn.execute("SELECT stage,state,force_replace FROM pipeline_jobs WHERE recording_id=?", (rid,)).fetchall()


def test_historical_summary_without_categories_enqueues_one_forced_summary_replacement_only():
    conn = _category_backfill_db()
    conn.execute("""INSERT INTO recordings(
        id,name,start_at,asr_transcript,plaud_transcript,summary,summary_json)
        VALUES ('old','Old','2026-01-01','x','','# old',?)""", (json.dumps({"overview": "old"}),))

    assert summary_backfill.enqueue_missing_category_summaries(conn, batch_size=10) == 1
    assert _summary_job(conn, "old") == [('summary', 'queued', 1)]
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE recording_id='old' AND stage='asr'").fetchone()[0] == 0
    assert summary_backfill.enqueue_missing_category_summaries(conn, batch_size=10) == 0
    assert _summary_job(conn, "old") == [('summary', 'queued', 1)]


def test_historical_category_backfill_skips_present_empty_invalid_and_blank_summaries():
    conn = _category_backfill_db()
    rows = [
        ('empty', 'empty', json.dumps({"categories": []})),
        ('present', 'present', json.dumps({"overview": "ok", "categories": ["personal"]})),
        ('malformed', 'malformed', '{not-json'),
        ('array', 'array', '[]'),
        ('blank', 'blank', '   '),
        ('null', 'null', 'null'),
    ]
    conn.executemany("""INSERT INTO recordings(
        id,name,start_at,asr_transcript,plaud_transcript,summary,summary_json)
        VALUES (?,?,'2026-01-01','x','','# old',?)""", rows)

    assert summary_backfill.enqueue_missing_category_summaries(conn, batch_size=10) == 0
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0


def test_historical_category_backfill_is_bounded_and_includes_archived_rows():
    conn = _category_backfill_db()
    for rid, date in (("archived", "2026-01-01"), ("next", "2026-01-02"), ("later", "2026-01-03")):
        conn.execute("""INSERT INTO recordings(
            id,name,start_at,asr_transcript,plaud_transcript,summary,summary_json)
            VALUES (?,?,?,?,?,?,?)""", (
            rid, rid, date, 'x', '', '# old', json.dumps({"overview": rid})))

    assert summary_backfill.enqueue_missing_category_summaries(conn, batch_size=2) == 2
    assert [row[0] for row in conn.execute("SELECT recording_id FROM pipeline_jobs ORDER BY seq")] == ['archived', 'next']
    assert summary_backfill.enqueue_missing_category_summaries(conn, batch_size=2) == 1
    assert [row[0] for row in conn.execute("SELECT recording_id FROM pipeline_jobs ORDER BY seq")] == ['archived', 'next', 'later']


def test_catalog_revision_admission_is_bounded_and_custom_auto_preserves_manual():
    conn = _category_backfill_db()
    for rid in ("archived", "next", "blank"):
        text = "x" * 200 if rid != "blank" else ""
        conn.execute("INSERT INTO recordings(id,name,start_at,asr_transcript,plaud_transcript,summary,summary_json) VALUES(?,?, '2026-01-01', ?, '', '# old', ?)",
                     (rid, rid, text, json.dumps({"categories": ["personal"]})))
    import control, pipeline
    control.ensure_schema(conn)
    result = control.create_label(conn, "Клиенты")
    custom = result["label"]["id"]
    assert result["classification_queued"] is True
    assert conn.execute("SELECT revision FROM label_catalog_state").fetchone()[0] == 2
    # create action admits a bounded first batch; queued work has no ASR stage.
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE stage='asr'").fetchone()[0] == 0
    assert pipeline.admit_label_reclassification(conn, limit=1) <= 1
    summary_backfill.publish_auto_labels(conn, "next", {"categories": [custom]})
    control.apply(conn, "set_label", "next", label_id=custom, active=False)
    summary_backfill.publish_auto_labels(conn, "next", {"categories": [custom]})
    assert conn.execute("SELECT auto_assigned,manual_override FROM recording_labels WHERE recording_id='next' AND label_id=?", (custom,)).fetchone() == (1, 0)
