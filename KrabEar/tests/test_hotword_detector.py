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


class TestHotwordDetectorExtended(unittest.TestCase):
    """Extended test coverage for HotwordDetector focus scenarios."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.detector = HotwordDetector(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # 21. detect present — hotword found in transcript
    def test_detect_hotword_present(self):
        """Test that detector correctly identifies a hotword present in text."""
        self.detector.add_hotword("important")
        matches = self.detector.check_text("This is important news.")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].word, "important")
        self.assertIn("important", matches[0].context)

    # 22. detect absent — hotword not found in transcript
    def test_detect_hotword_absent(self):
        """Test that detector returns empty list when hotword absent."""
        self.detector.add_hotword("urgent")
        matches = self.detector.check_text("This is routine information.")
        self.assertEqual(len(matches), 0)

    # 23. multiple triggers in single text
    def test_multiple_triggers_in_text(self):
        """Test detection of multiple different hotwords in a single transcript."""
        self.detector.add_hotword("warning")
        self.detector.add_hotword("critical")
        self.detector.add_hotword("action")
        text = "WARNING: critical system issue requires immediate action now."
        matches = self.detector.check_text(text)
        self.assertEqual(len(matches), 3)
        words_found = {m.word for m in matches}
        self.assertEqual(words_found, {"warning", "critical", "action"})

    # 24. case-insensitive matching by default
    def test_case_insensitive_default(self):
        """Test case-insensitive matching is default behavior."""
        self.detector.add_hotword("ERROR")
        # Test various case combinations
        matches1 = self.detector.check_text("error detected")
        matches2 = self.detector.check_text("ERROR detected")
        matches3 = self.detector.check_text("Error detected")
        self.assertEqual(len(matches1), 1)
        self.assertEqual(len(matches2), 1)
        self.assertEqual(len(matches3), 1)

    # 25. empty text handling
    def test_empty_text(self):
        """Test that empty text returns no matches."""
        self.detector.add_hotword("trigger")
        matches = self.detector.check_text("")
        self.assertEqual(matches, [])

    # 26. word boundary matching (no partial words)
    def test_word_boundary_matching(self):
        """Test that hotwords are matched as whole words only."""
        self.detector.add_hotword("test")
        # "test" should match, "contest" should not (partial match)
        matches_full = self.detector.check_text("This is a test case.")
        matches_partial = self.detector.check_text("This is a contest now.")
        self.assertEqual(len(matches_full), 1)
        self.assertEqual(len(matches_partial), 0)


class TestHotwordDetectorUnicode(unittest.TestCase):
    """Tests for unicode/accented chars and multiple occurrences of same word."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.detector = HotwordDetector(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # 27. Multiple occurrences of the same hotword in a single text
    def test_multiple_occurrences_same_hotword(self):
        """check_text returns one match per occurrence of the same hotword."""
        self.detector.add_hotword("error")
        text = "error occurred: error in module, error logged"
        matches = self.detector.check_text(text)
        self.assertEqual(len(matches), 3)
        self.assertTrue(all(m.word == "error" for m in matches))
        # Positions should be strictly increasing
        positions = [m.position for m in matches]
        self.assertEqual(positions, sorted(positions))

    # 28. Unicode hotword (Cyrillic)
    def test_unicode_cyrillic_hotword(self):
        """Detector correctly matches Cyrillic hotwords."""
        self.detector.add_hotword("ошибка")
        matches = self.detector.check_text("Произошла ошибка в системе.")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].word, "ошибка")

    # 29. Unicode hotword (Spanish accented)
    def test_unicode_accented_spanish_hotword(self):
        """Detector correctly matches Spanish words with accents."""
        self.detector.add_hotword("atención")
        matches = self.detector.check_text("¡Atención! Se requiere atención inmediata.")
        # case-insensitive → both should match
        self.assertEqual(len(matches), 2)
        self.assertTrue(all(m.word == "atención" for m in matches))

    # 30. Unicode hotword case-insensitive (Cyrillic mixed case)
    def test_unicode_cyrillic_case_insensitive(self):
        """Case-insensitive matching works for Cyrillic."""
        self.detector.add_hotword("КРИТИЧНО", case_sensitive=False)
        matches = self.detector.check_text("критично важно действовать сейчас")
        self.assertEqual(len(matches), 1)

    # 31. Same hotword appears at start and end of text
    def test_same_hotword_at_start_and_end(self):
        """Detects the same hotword at both the start and end of a sentence."""
        self.detector.add_hotword("start")
        text = "start of the process and end at start"
        matches = self.detector.check_text(text)
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0].position, 0)

    # 32. Unicode hotword with no match returns empty list
    def test_unicode_hotword_no_match(self):
        """Returns empty list when unicode hotword is absent."""
        self.detector.add_hotword("предупреждение")
        matches = self.detector.check_text("Всё в порядке, проблем нет.")
        self.assertEqual(matches, [])

    # 33. Position is correct for unicode text
    def test_position_correct_for_unicode_text(self):
        """Match position is byte-index into the unicode string."""
        self.detector.add_hotword("мир")
        text = "Привет мир сегодня"
        matches = self.detector.check_text(text)
        self.assertEqual(len(matches), 1)
        self.assertEqual(text[matches[0].position:matches[0].position + 3], "мир")


class TestHotwordDetectorWave92(unittest.TestCase):
    """Wave 92 required test names with explicit task-spec identifiers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.detector = HotwordDetector(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_simple_word_match(self):
        """Simple hotword found verbatim in text."""
        self.detector.add_hotword("краб")
        matches = self.detector.check_text("Это краб.")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].word, "краб")

    def test_word_boundary(self):
        """Hotword 'test' does NOT match inside 'contest' (word boundary enforced)."""
        self.detector.add_hotword("test")
        matches = self.detector.check_text("The contest continues.")
        self.assertEqual(len(matches), 0)
        matches2 = self.detector.check_text("Run the test now.")
        self.assertEqual(len(matches2), 1)

    def test_unicode_hotword(self):
        """Unicode hotwords (Cyrillic + emoji label) are matched correctly."""
        self.detector.add_hotword("ошибка", category="error")
        # Emoji in category label is fine; word itself is pure Cyrillic
        matches = self.detector.check_text("Критическая ошибка системы.")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].word, "ошибка")
        self.assertEqual(matches[0].category, "error")

    def test_case_insensitive(self):
        """Default matching ignores case for ASCII and Cyrillic."""
        self.detector.add_hotword("Warning")
        for variant in ("warning", "WARNING", "Warning", "WaRnInG"):
            matches = self.detector.check_text(f"A {variant} was issued.")
            self.assertEqual(len(matches), 1, f"Failed for variant: {variant!r}")

    def test_multiple_hotwords_in_text(self):
        """All registered hotwords are detected in a single scan."""
        self.detector.add_hotword("alpha")
        self.detector.add_hotword("beta")
        self.detector.add_hotword("gamma")
        text = "alpha beta gamma"
        matches = self.detector.check_text(text)
        self.assertEqual(len(matches), 3)
        found = {m.word for m in matches}
        self.assertEqual(found, {"alpha", "beta", "gamma"})

    def test_empty_transcript(self):
        """Empty string produces zero matches without error."""
        self.detector.add_hotword("anything")
        self.assertEqual(self.detector.check_text(""), [])
        # Also verify IPC wrapper handles empty
        result = self.detector.handle_check_hotwords({"text": ""})
        self.assertEqual(result["count"], 0)

    def test_hotword_list_reload(self):
        """Hotwords registered at runtime persist and are reloaded from file."""
        self.detector.add_hotword("volatile", category="runtime")
        self.detector.add_hotword("persistent", category="store")

        # Simulate reload (same data_dir, new instance)
        detector2 = HotwordDetector(data_dir=self.tmp.name)
        words = {w["word"] for w in detector2.get_hotwords()}
        self.assertIn("volatile", words)
        self.assertIn("persistent", words)

        # Runtime add on new instance also works
        detector2.add_hotword("live_add", category="live")
        matches = detector2.check_text("live_add detected here")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].word, "live_add")


if __name__ == "__main__":
    unittest.main()
