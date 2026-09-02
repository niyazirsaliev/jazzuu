"""Idea dictations: recognised, labelled, and never mistaken for empty noise.

ideas recorded on the move are short. Two mechanisms must agree
about them: the summariser must still title them, and the empty-recording
sweeper must never archive them. Both are pinned here against the real
representative transcripts that motivated the change.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "archive"))
import summary_backfill


# Verbatim openings from the owner archive (N-0162, N-0256, N-0210, N-0198).
IDEA_162 = ("идея игрок бота под капотом был этот гермес чтобы было тоже очень "
            "легко нативно нажал кнопочку диктом сработал")
IDEA_256 = ("идея запартнериться слов и чтобы сделать такой я и найти в сервис "
            "где они потом обрабатывают письма")
HALLUCINATION = "Thank you."
REAL_SPEECH = "[Speaker 1] Э, вот этим как пользоваться, кстати? Вот это надо нажать, да?"


class IdeaSummaryThresholdTests(unittest.TestCase):
    def test_a_dictated_idea_clears_the_bar_that_left_it_untitled(self):
        # N-0162 is 138 chars: under the 200 floor, so it kept a raw timestamp
        # as its title in production.
        self.assertLess(len(IDEA_162), summary_backfill.SUMMARY_MIN_CHARS)
        self.assertGreaterEqual(len(IDEA_162),
                                summary_backfill._min_summary_chars(IDEA_162))

    def test_the_cue_is_read_from_the_opening_not_the_whole_transcript(self):
        buried = "a" * 400 + " идея"
        self.assertEqual(summary_backfill._min_summary_chars(buried),
                         summary_backfill.SUMMARY_MIN_CHARS)

    def test_speaker_tags_never_hide_the_cue(self):
        self.assertEqual(
            summary_backfill._min_summary_chars("[Speaker 1] Идея: сделать бота"),
            summary_backfill.IDEA_MIN_CHARS)

    def test_silence_hallucinations_keep_the_full_floor(self):
        for noise in (HALLUCINATION, "Thanks for watching!", "Well,", ""):
            self.assertEqual(summary_backfill._min_summary_chars(noise),
                             summary_backfill.SUMMARY_MIN_CHARS, noise)


class IdeaLabelTests(unittest.TestCase):
    def test_idea_joins_the_existing_system_catalogue(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY)")
        summary_backfill.ensure_label_schema(conn)
        rows = dict(conn.execute("SELECT id,name FROM label_definitions"))
        self.assertEqual(rows.get("idea"), "Идея")
        # Existing labels must survive the addition.
        self.assertEqual(rows.get("business"), "Работа")
        self.assertEqual(rows.get("personal"), "Личное")
        conn.close()


# The sweeper's rule, mirrored here so a regression in either copy is visible.
# Trailing punctuation includes the comma: production N-0210 is exactly "Well,".
JUNK = re.compile(
    r"^(?:thank you|thanks for watching|you|bye|\[?music\]?|well|torsdagsfilm"
    r"|i.m sorry|okay|\[неразборчиво\])[.!?,\s]*$", re.I)


def would_archive(transcript):
    text = re.sub(r"\[Speaker \d+\]", "", transcript or "").strip()
    return not text or (len(text.split()) <= 4 and bool(JUNK.match(text)))


class EmptySweepSafetyTests(unittest.TestCase):
    def test_dictated_ideas_are_never_swept(self):
        for idea in (IDEA_162, IDEA_256):
            self.assertFalse(would_archive(idea), idea[:40])

    def test_short_real_speech_survives(self):
        # 7 seconds long, but real content — length alone must never decide.
        self.assertFalse(would_archive(REAL_SPEECH))

    def test_only_silence_hallucinations_are_swept(self):
        for noise in ("Thank you.", "Thanks for watching!", "Well,",
                      "Torsdagsfilm", "I'm sorry.", ""):
            self.assertTrue(would_archive(noise), noise)


if __name__ == "__main__":
    unittest.main()
