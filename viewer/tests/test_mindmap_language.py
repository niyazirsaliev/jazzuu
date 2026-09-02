import json
import sqlite3

from app import main, mindtree


RUSSIAN_SUMMARY = {
    "title": "Координация встречи и пожарная безопасность",
    "overview": "Обсуждение места встречи, дороги и состояния пожарной сигнализации.",
    "brief_summary": "Участники выбрали место встречи и обсудили передачу документов.",
    "themes": [
        {
            "title": "Организация встречи",
            "points": ["Выбрали кафе", "Передали номер сотрудника"],
        },
        {
            "title": "Пожарная безопасность",
            "points": ["Нужно восстановить документацию", "Сигнализацию следует проверить"],
        },
    ],
    "decisions": ["Связаться с подрядчиком"],
    "action_items": [{"task": "Проверить систему", "owner": "Энергия"}],
    "risks": ["Нет доступа к интернету"],
}


def _database(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE recordings("
        "id TEXT PRIMARY KEY,name TEXT,summary TEXT,summary_json TEXT,"
        "plaud_transcript TEXT,asr_transcript TEXT)"
    )
    conn.execute(
        "INSERT INTO recordings VALUES(?,?,?,?,?,?)",
        (
            "rec1",
            "Запись",
            "## Meetup Logistics\nThe group selected a meeting place and discussed traffic.",
            json.dumps(RUSSIAN_SUMMARY, ensure_ascii=False),
            "The original transcript is English and must only be a fallback.",
            "",
        ),
    )
    conn.commit()
    conn.close()


def test_structured_tree_uses_russian_generated_summary():
    tree = mindtree.build_structured_tree("Запись", RUSSIAN_SUMMARY)
    assert "Организация встречи" in tree
    assert "Пожарная безопасность" in tree
    assert "Проверить систему" in tree
    assert "Meetup Logistics" not in tree


def test_mindmap_source_prefers_summary_json_over_original_plaud_markdown(tmp_path, monkeypatch):
    path = tmp_path / "archive.db"
    _database(path)

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(main, "db", connect)
    tree = main._mindmap_source("rec1")
    assert "Организация встречи" in tree
    assert "Meetup Logistics" not in tree
    assert "original transcript" not in tree


def test_derived_revision_changes_when_structured_summary_changes():
    raw = json.dumps(RUSSIAN_SUMMARY, ensure_ascii=False)
    first = main._derived_revision(main.MINDMAP_VERSION, "Запись", raw)
    changed = main._derived_revision(main.MINDMAP_VERSION, "Запись", raw + " ")
    assert first != changed
    assert len(first) == 12
