import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ViewerApiTests(unittest.TestCase):
    def make_db(self, include_new=True):
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp.close()
        conn = sqlite3.connect(temp.name)
        conn.execute(
            """CREATE TABLE recordings(
               id TEXT PRIMARY KEY,name TEXT,start_at TEXT,created_at TEXT,
               duration_ms INTEGER,lang TEXT,asr_engine TEXT,asr_transcript TEXT,
               plaud_transcript TEXT,plaud_segments_json TEXT,summary TEXT)"""
        )
        if include_new:
            conn.execute("ALTER TABLE recordings ADD COLUMN summary_json TEXT")
            conn.execute("ALTER TABLE recordings ADD COLUMN semantic_title TEXT")
            conn.execute("ALTER TABLE recordings ADD COLUMN asr_meta_json TEXT")
            conn.execute("ALTER TABLE recordings ADD COLUMN asr_alternative_transcript TEXT")
        conn.commit()
        return temp.name, conn

    def test_detail_safely_parses_summary_and_route_observability(self):
        from viewer.app import main

        path, conn = self.make_db()
        structured = {"overview": "Короткий итог", "decisions": ["Запустить пилот"]}
        route = {
            "selected_engine": "whisper-large",
            "requested_engine": "auto",
            "route_reason": "detected Russian speech",
            "language": "ru",
            "private": "must not leak",
        }
        conn.execute(
            """INSERT INTO recordings(
               id,name,asr_engine,asr_transcript,summary,summary_json,asr_meta_json
               ) VALUES(?,?,?,?,?,?,?)""",
            ("rec1", "Встреча", "whisper-large", "text", "markdown",
             json.dumps(structured, ensure_ascii=False), json.dumps(route)),
        )
        conn.commit()
        conn.close()

        def open_db():
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            return db

        with mock.patch.object(main, "db", side_effect=open_db), \
             mock.patch.object(main, "AUDIO_DIR", tempfile.mkdtemp()):
            result = main.get_recording("rec1")

        self.assertEqual(result["summary_data"], structured)
        self.assertEqual(
            result["asr_route"],
            {
                "selected_engine": "whisper-large",
                "requested_engine": "auto",
                "route_reason": "detected Russian speech",
                "language": "ru",
            },
        )
        self.assertTrue(result["has_summary_card"])

    def test_detail_prefers_semantic_title_and_keeps_source_name(self):
        from viewer.app import main

        path, conn = self.make_db()
        conn.execute(
            "INSERT INTO recordings(id,name,semantic_title,start_at) VALUES(?,?,?,?)",
            ("rec-title", "Official PLAUD filename", "Обсуждение бюджета", "2026-08-08 10:00:00"),
        )
        conn.commit(); conn.close()

        def open_db():
            db = sqlite3.connect(path); db.row_factory = sqlite3.Row
            return db

        with mock.patch.object(main, "db", side_effect=open_db), \
             mock.patch.object(main, "AUDIO_DIR", tempfile.mkdtemp()):
            detail = main.get_recording("rec-title")
            listing = main.list_recordings()

        self.assertEqual(detail["name"], "Обсуждение бюджета")
        self.assertEqual(detail["source_name"], "Official PLAUD filename")
        self.assertEqual(listing[0]["name"], "Обсуждение бюджета")
        self.assertEqual(listing[0]["source_name"], "Official PLAUD filename")

    def test_detail_remains_compatible_when_new_columns_are_absent(self):
        from viewer.app import main

        path, conn = self.make_db(include_new=False)
        conn.execute(
            "INSERT INTO recordings(id,name,summary) VALUES('legacy','Legacy','old summary')"
        )
        conn.commit()
        conn.close()

        def open_db():
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            return db

        with mock.patch.object(main, "db", side_effect=open_db), \
             mock.patch.object(main, "AUDIO_DIR", tempfile.mkdtemp()):
            result = main.get_recording("legacy")
        self.assertIsNone(result["summary_data"])
        self.assertEqual(result["asr_route"], {})
        self.assertFalse(result["has_summary_card"])

    def test_read_only_wal_database_opens_after_wal_files_are_checkpointed_away(self):
        from viewer.app import main

        root = tempfile.mkdtemp()
        path = os.path.join(root, "archive.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO recordings VALUES('r1','working')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        self.assertFalse(os.path.exists(path + "-wal"))
        os.chmod(root, 0o555)
        try:
            with mock.patch.object(main, "DB_PATH", path), \
                 mock.patch.object(main, "_snap", {"key": None, "path": None}):
                opened = main.db()
                try:
                    self.assertEqual(opened.execute(
                        "SELECT name FROM recordings WHERE id='r1'").fetchone()[0],
                        "working")
                finally:
                    opened.close()
        finally:
            os.chmod(root, 0o755)


class SummaryCardTests(unittest.TestCase):
    def test_card_source_prefers_semantic_title_over_timestamp_source_name(self):
        from viewer.app import main

        path, conn = ViewerApiTests().make_db()
        conn.execute(
            """INSERT INTO recordings(id,name,semantic_title,summary_json)
               VALUES(?,?,?,?)""",
            ("semantic-card", "2026-08-11 15:03:22", "Поставка панелей",
             json.dumps({"overview": "Итог"}, ensure_ascii=False)),
        )
        conn.commit(); conn.close()

        def open_db():
            result = sqlite3.connect(path); result.row_factory = sqlite3.Row
            return result

        with mock.patch.object(main, "db", side_effect=open_db):
            title, data = main._summary_card_source("semantic-card")
        self.assertEqual(title, "Поставка панелей")
        self.assertEqual(data["overview"], "Итог")

    def test_renderer_creates_nonempty_1080px_png(self):
        from PIL import Image
        from viewer.app import summary_card

        data = {
            "overview": "Команда согласовала план запуска нового продукта.",
            "themes": [
                {"title": "Запуск", "summary": "Пилот стартует в сентябре"},
                {"title": "Клиенты", "summary": "Первая группа — 100 пользователей"},
            ],
            "key_facts": [
                {"label": "Бюджет", "value": "₽2 млн"},
                {"label": "Срок", "value": "6 недель"},
            ],
            "decisions": ["Запустить пилот", "Проверять метрики еженедельно"],
            "risks": ["Сжатые сроки", "Зависимость от поставщика"],
            "action_items": [
                {"task": "Подготовить план", "owner": "Анна", "due": "15 августа"}
            ],
        }
        output = io.BytesIO()
        summary_card.render_summary_card(data, "План запуска", output)
        raw = output.getvalue()

        self.assertGreater(len(raw), 20_000)
        image = Image.open(io.BytesIO(raw))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (summary_card.WIDTH, summary_card.HEIGHT))
        self.assertEqual(
            image.crop((0, 0, image.width, summary_card.SAFE_AREA_TOP)).getextrema(),
            ((255, 255), (255, 255), (255, 255)),
        )
        colors = image.convert("RGB").getcolors(maxcolors=1_000_000)
        self.assertIsNotNone(colors)
        self.assertGreater(len(colors), 20)

    def test_authenticated_endpoint_returns_cached_png(self):
        from fastapi.testclient import TestClient
        from PIL import Image
        from viewer.app import main, summary_card

        path, conn = ViewerApiTests().make_db()
        conn.execute(
            """INSERT INTO recordings(id,name,summary_json) VALUES(?,?,?)""",
            ("card1", "Карточка", json.dumps({"overview": "Итог", "decisions": ["Да"]})),
        )
        conn.commit()
        conn.close()

        def open_db():
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            return db

        cache = tempfile.mkdtemp()
        with mock.patch.object(main, "db", side_effect=open_db), \
             mock.patch.object(main, "SECRET", "test-secret"), \
             mock.patch.object(main, "SUMMARY_CARD_DIR", cache):
            client = TestClient(main.app)
            self.assertEqual(
                client.get("/api/recordings/card1/summary-card.png").status_code, 401
            )
            response = client.get(
                "/api/recordings/card1/summary-card.png",
                cookies={main.COOKIE_NAME: main.expected_token()},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertEqual(
                Image.open(io.BytesIO(response.content)).size,
                (summary_card.WIDTH, summary_card.HEIGHT),
            )
            self.assertEqual(len(list(Path(cache).glob("card1.*.png"))), 1)


if __name__ == "__main__":
    unittest.main()
