"""Тесты для HotwordDetector."""

from __future__ import annotations
from backend.hotword_detector import HotwordDetector

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


class TestHotwordDetectorBasic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.detector = HotwordDetector(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # 1. add + get
    def test_add_and_get_hotword(self):
        self.detector.add_hotword("password", category="security")
        words = self.detector.get_hotwords()
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0]["word"], "password")
        self.assertEqual(words[0]["category"], "security")

    # 2. remove existing
    def test_remove_hotword(self):
        self.detector.add_hotword("secret")
        removed = self.detector.remove_hotword("secret")
        self.assertTrue(removed)
        self.assertEqual(self.detector.get_hotwords(), [])

    # 3. remove non-existing
    def test_remove_nonexistent_returns_false(self):
        removed = self.detector.remove_hotword("ghost")
        self.assertFalse(removed)

    # 4. check_text — basic match
    def test_check_text_finds_match(self):
        self.detector.add_hotword("alert")
        matches = self.detector.check_text("This is an alert message.")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].word, "alert")
        self.assertEqual(matches[0].category, "alert")

    # 5. check_text — no match
    def test_check_text_no_match(self):
        self.detector.add_hotword("danger")
        matches = self.detector.check_text("Everything is fine.")
        self.assertEqual(matches, [])

    # 6. case-insensitive by default
    def test_case_insensitive_by_default(self):
        self.detector.add_hotword("alert", case_sensitive=False)
        matches = self.detector.check_text("ALERT! Something happened.")
        self.assertEqual(len(matches), 1)

    # 7. case-sensitive mode
    def test_case_sensitive(self):
        self.detector.add_hotword("Alert", case_sensitive=True)
        matches_lower = self.detector.check_text("alert fires")
        matches_upper = self.detector.check_text("Alert fires")
        self.assertEqual(len(matches_lower), 0)
        self.assertEqual(len(matches_upper), 1)

    # 8. context snippet
    def test_context_snippet(self):
        self.detector.add_hotword("boom")
        text = "Something went boom right now"
        matches = self.detector.check_text(text)
        self.assertEqual(len(matches), 1)
        self.assertIn("boom", matches[0].context)

    # 9. position is correct
    def test_match_position(self):
        self.detector.add_hotword("word")
        text = "first word here"
        matches = self.detector.check_text(text)
        self.assertEqual(matches[0].position, text.index("word"))

    # 10. persistence — reload from file
    def test_persistence(self):
        self.detector.add_hotword("persist", category="test")
        # Create new instance pointing to same dir
        detector2 = HotwordDetector(data_dir=self.tmp.name)
        words = detector2.get_hotwords()
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0]["word"], "persist")

    # 11. multiple matches
    def test_multiple_hotwords_in_text(self):
        self.detector.add_hotword("fire")
        self.detector.add_hotword("smoke")
        matches = self.detector.check_text("There is fire and smoke everywhere.")
        words_found = {m.word for m in matches}
        self.assertIn("fire", words_found)
        self.assertIn("smoke", words_found)

    # 12. add overwrites existing
    def test_add_overwrites(self):
        self.detector.add_hotword("key", category="old")
        self.detector.add_hotword("key", category="new")
        words = self.detector.get_hotwords()
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0]["category"], "new")

    # 13. empty word raises
    def test_empty_word_raises(self):
        with self.assertRaises(ValueError):
            self.detector.add_hotword("  ")

    # 14. IPC handle_add_hotword
    def test_ipc_add_hotword(self):
        result = self.detector.handle_add_hotword({"word": "trigger", "category": "ipc"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["word"], "trigger")

    # 15. IPC handle_check_hotwords
    def test_ipc_check_hotwords(self):
        self.detector.add_hotword("critical")
        result = self.detector.handle_check_hotwords({"text": "A critical error occurred."})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["word"], "critical")

    # 16. IPC handle_get_hotwords
    def test_ipc_get_hotwords(self):
        self.detector.add_hotword("x")
        result = self.detector.handle_get_hotwords({})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["hotwords"]), 1)

    # 17. IPC handle_remove_hotword
    def test_ipc_remove_hotword(self):
        self.detector.add_hotword("remove_me")
        result = self.detector.handle_remove_hotword({"word": "remove_me"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["removed"])

    # 18. IPC add with missing word
    def test_ipc_add_missing_word(self):
        result = self.detector.handle_add_hotword({})
        self.assertFalse(result["ok"])

    # 19. check_text empty string
    def test_check_text_empty_string(self):
        self.detector.add_hotword("test")
        matches = self.detector.check_text("")
        self.assertEqual(matches, [])

    # 20. matches sorted by position
    def test_matches_sorted_by_position(self):
        self.detector.add_hotword("beta")
        self.detector.add_hotword("alpha")
        text = "alpha comes before beta"
        matches = self.detector.check_text(text)
        self.assertEqual(len(matches), 2)
        self.assertLess(matches[0].position, matches[1].position)
        self.assertEqual(matches[0].word, "alpha")


if __name__ == "__main__":
    unittest.main()
