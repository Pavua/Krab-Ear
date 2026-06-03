"""test_hotword_purge_inmemory.py — wave-26 MED privacy purge test.

Scenario: add a hotword → call purge_all_data (disk wipe) → verify that
in-memory _hotwords/_patterns are ALSO cleared so check_text returns no
matches (not just after restart).

Covers:
  - HotwordDetector.clear() method contract
  - BackendService._handle_purge_all_data wires clear() after history purge
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from backend.hotword_detector import HotwordDetector  # noqa: E402


class TestHotwordDetectorClear(unittest.TestCase):
    """Unit tests for HotwordDetector.clear() method (wave-26)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.detector = HotwordDetector(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clear_empties_hotwords(self):
        """clear() removes all in-memory hotwords."""
        self.detector.add_hotword("secret_name")
        self.detector.add_hotword("confidential_term")
        self.assertGreater(len(self.detector.get_hotwords()), 0)

        self.detector.clear()

        self.assertEqual(self.detector.get_hotwords(), [])

    def test_clear_empties_patterns(self):
        """clear() removes all compiled regex patterns from _patterns dict."""
        self.detector.add_hotword("pattern_word")
        self.assertGreater(len(self.detector._patterns), 0)

        self.detector.clear()

        self.assertEqual(len(self.detector._patterns), 0)

    def test_clear_stops_detection(self):
        """After clear(), check_text returns no matches for previously added hotword."""
        hotword = "private_term"
        self.detector.add_hotword(hotword)

        # Confirm it matches before clear
        matches_before = self.detector.check_text(f"text with {hotword} inside")
        self.assertEqual(len(matches_before), 1, "Expected match before clear")

        # Purge in-memory state
        self.detector.clear()

        # After clear — no match
        matches_after = self.detector.check_text(f"text with {hotword} inside")
        self.assertEqual(
            len(matches_after), 0,
            "Expected zero matches after clear() — in-memory purge gap should be closed",
        )

    def test_clear_on_empty_detector_is_safe(self):
        """clear() on a detector with no hotwords does not raise."""
        self.assertEqual(self.detector.get_hotwords(), [])
        self.detector.clear()  # Should not raise
        self.assertEqual(self.detector.get_hotwords(), [])

    def test_clear_is_idempotent(self):
        """Calling clear() twice does not raise and leaves detector empty."""
        self.detector.add_hotword("word")
        self.detector.clear()
        self.detector.clear()
        self.assertEqual(self.detector.get_hotwords(), [])

    def test_clear_multiple_hotwords(self):
        """clear() wipes all registered hotwords at once."""
        for word in ["alpha", "beta", "gamma", "delta"]:
            self.detector.add_hotword(word)
        self.assertEqual(len(self.detector.get_hotwords()), 4)

        self.detector.clear()

        self.assertEqual(len(self.detector.get_hotwords()), 0)
        # check_text returns nothing for any of them
        for word in ["alpha", "beta", "gamma", "delta"]:
            matches = self.detector.check_text(f"word is {word} here")
            self.assertEqual(len(matches), 0, f"Expected no match for {word!r} after clear()")

    def test_clear_does_not_delete_disk_file(self):
        """clear() is an in-memory operation only; it does NOT delete hotwords.json."""
        self.detector.add_hotword("on_disk_word")
        disk_path = Path(self.tmp.name) / "hotwords.json"
        self.assertTrue(disk_path.exists(), "hotwords.json should exist after add")

        self.detector.clear()

        # File still exists — disk deletion is HistoryService's responsibility
        self.assertTrue(
            disk_path.exists(),
            "clear() must not delete hotwords.json — disk wipe is HistoryService's job",
        )

    def test_add_after_clear_works(self):
        """Detector is fully functional after clear() — can add and detect again."""
        self.detector.add_hotword("before_clear")
        self.detector.clear()

        self.detector.add_hotword("after_clear")
        matches = self.detector.check_text("text with after_clear keyword")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].word, "after_clear")

    def test_clear_with_case_sensitive_hotword(self):
        """clear() removes case-sensitive hotwords from both dicts."""
        self.detector.add_hotword("CaseSensitive", case_sensitive=True)
        self.assertGreater(len(self.detector._hotwords), 0)

        self.detector.clear()

        self.assertEqual(len(self.detector._hotwords), 0)
        self.assertEqual(len(self.detector._patterns), 0)
        matches = self.detector.check_text("text with CaseSensitive word")
        self.assertEqual(len(matches), 0)

    def test_purge_flow_add_purge_detect(self):
        """Full privacy purge flow: add hotword → purge (clear) → detect returns nothing.

        This is the primary wave-26 scenario: user registers a hotword containing
        their name or private term, a privacy purge is triggered, and the in-memory
        state must be wiped so subsequent IPC check_hotwords calls return no matches.
        """
        private_word = "ИванИванов"  # Cyrillic name — realistic PII
        self.detector.add_hotword(private_word, category="private")

        # Confirm detection works before purge
        pre_purge = self.detector.check_text(f"Встреча с {private_word} состоялась.")
        self.assertEqual(len(pre_purge), 1, "Should detect hotword before purge")

        # Simulate privacy purge: disk file is deleted by HistoryService,
        # then BackendService calls clear() on the detector.
        disk_path = Path(self.tmp.name) / "hotwords.json"
        disk_path.unlink(missing_ok=True)  # simulate HistoryService disk wipe
        self.detector.clear()             # simulate BackendService.clear() call

        # After purge — no detection
        post_purge = self.detector.check_text(f"Встреча с {private_word} состоялась.")
        self.assertEqual(
            len(post_purge), 0,
            "In-memory hotwords must be cleared after purge — wave-26 MED purge gap",
        )

        # Disk file is gone (done by HistoryService above)
        self.assertFalse(disk_path.exists())

        # A new HotwordDetector from the same data_dir loads nothing
        reloaded = HotwordDetector(data_dir=self.tmp.name)
        self.assertEqual(reloaded.get_hotwords(), [])
        reload_matches = reloaded.check_text(f"Встреча с {private_word} состоялась.")
        self.assertEqual(len(reload_matches), 0)


class TestBackendServiceHotwordPurgeWiring(unittest.TestCase):
    """Integration test: BackendService._handle_purge_all_data calls clear()."""

    def test_purge_calls_hotword_detector_clear(self):
        """_handle_purge_all_data must call self._hotword_detector.clear() when purge succeeds."""
        # Build a minimal BackendService-like object with just the fields needed
        # for _handle_purge_all_data, to avoid heavy imports (mlx, sounddevice, etc.)
        class FakeHistoryService:
            def handle_purge_all_data(self, params):
                return {"ok": True, "history_deleted": 0}

        class FakeAutoBackup:
            def set_purged(self):
                pass

            def clear_purged(self):
                pass

        # Import the real _handle_purge_all_data method and bind it to our fake obj
        with tempfile.TemporaryDirectory() as tmpdir:
            detector = HotwordDetector(data_dir=tmpdir)
            detector.add_hotword("test_word")
            self.assertEqual(len(detector.get_hotwords()), 1)

            fake_svc = MagicMock()
            fake_svc._auto_backup = FakeAutoBackup()
            fake_svc._history = FakeHistoryService()
            fake_svc._hotword_detector = detector

            # Bind the real method
            import backend.service as svc_module
            bound = svc_module.BackendService._handle_purge_all_data.__get__(
                fake_svc, svc_module.BackendService
            )
            result = bound({"confirm": True})

            self.assertTrue(result.get("ok"), f"Purge should succeed, got: {result}")
            # After purge the detector must have been cleared
            self.assertEqual(
                detector.get_hotwords(), [],
                "_handle_purge_all_data must call _hotword_detector.clear() on successful purge",
            )
            matches = detector.check_text("text with test_word inside")
            self.assertEqual(
                len(matches), 0,
                "check_text should return no matches after purge (in-memory cleared)",
            )

    def test_purge_no_clear_on_missing_confirm(self):
        """If confirm is missing, hotwords must NOT be cleared (purge didn't run)."""

        class FakeHistoryService:
            def handle_purge_all_data(self, params):
                # Simulates the real handler returning error without deleting anything
                return {"ok": False, "error": "confirmation_required"}

        class FakeAutoBackup:
            def set_purged(self):
                pass

            def clear_purged(self):
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            detector = HotwordDetector(data_dir=tmpdir)
            detector.add_hotword("should_survive")

            fake_svc = MagicMock()
            fake_svc._auto_backup = FakeAutoBackup()
            fake_svc._history = FakeHistoryService()
            fake_svc._hotword_detector = detector

            import backend.service as svc_module
            bound = svc_module.BackendService._handle_purge_all_data.__get__(
                fake_svc, svc_module.BackendService
            )
            result = bound({})  # no confirm

            self.assertFalse(result.get("ok"))
            # Detector should NOT be cleared — purge never ran
            self.assertEqual(
                len(detector.get_hotwords()), 1,
                "Hotwords must survive when purge is rejected (no confirm)",
            )


if __name__ == "__main__":
    unittest.main()
