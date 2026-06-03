"""Wave-33 regression tests:
  C1 (HIGH) — generate_daily_digest privacy gate
  C2 (LOW)  — HotwordDetector._save atomic write (corruption-safe)
"""

from __future__ import annotations

import json
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
from backend.state_store import StateStore  # noqa: E402


def _make_backend_service(tmp_dir: str):
    recorder = MagicMock()
    recorder.is_recording = False
    transcriber = MagicMock()
    translator = MagicMock()

    from backend.service import BackendService
    store = StateStore(Path(tmp_dir) / "data")
    svc = BackendService(
        store=store,
        recorder=recorder,
        transcriber=transcriber,
        translator=translator,
    )
    return svc, store


class TestGenerateDailyDigestPrivacyGate(unittest.TestCase):
    """C1 HIGH — generate_daily_digest must be blocked when privacy_mode_enabled=True."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.store = _make_backend_service(self.tmp.name)

    def _set_privacy(self, enabled: bool) -> None:
        """Helper: persist privacy setting and invalidate cache."""
        self.store.save_settings({"privacy_mode_enabled": enabled})
        self.svc._invalidate_settings_cache()

    # ------------------------------------------------------------------
    # privacy_mode = True → blocked
    # ------------------------------------------------------------------

    def test_privacy_mode_true_returns_ok_false(self) -> None:
        """privacy_mode_enabled=True → ok:False, reason:privacy_mode_active."""
        self._set_privacy(True)
        result = self.svc._handle_generate_daily_digest({"date": "2024-01-15"})
        self.assertFalse(result.get("ok"), msg=f"Expected ok=False, got {result}")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_true_via_ipc(self) -> None:
        """IPC route also blocks when privacy_mode_enabled=True."""
        self._set_privacy(True)
        resp = self.svc.handle_request(
            {"id": "1", "method": "generate_daily_digest", "params": {"date": "2024-01-15"}}
        )
        # handle_request wraps the handler result inside resp["result"]
        result = resp.get("result", {})
        self.assertFalse(result.get("ok"), msg=f"Expected ok=False in result, got {result}")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    # ------------------------------------------------------------------
    # privacy_mode = False → passes through
    # ------------------------------------------------------------------

    def test_privacy_mode_false_returns_digest(self) -> None:
        """privacy_mode_enabled=False → normal digest returned."""
        self._set_privacy(False)
        result = self.svc._handle_generate_daily_digest({"date": "2024-01-15"})
        # Should have normal digest keys (not an error dict)
        self.assertIn("date", result, msg=f"Missing 'date' in result: {result}")
        self.assertIn("total_recordings", result)
        self.assertIn("markdown", result)

    def test_privacy_mode_absent_returns_digest(self) -> None:
        """privacy_mode_enabled not set (default False) → normal digest returned."""
        # no save_settings call → default False
        result = self.svc._handle_generate_daily_digest({"date": "2024-01-15"})
        self.assertIn("date", result, msg=f"Missing 'date' in result: {result}")
        self.assertIn("total_recordings", result)

    def test_privacy_mode_no_history_leak(self) -> None:
        """privacy_mode=True must not return any transcript/topic data."""
        self._set_privacy(True)
        result = self.svc._handle_generate_daily_digest({})
        # Our gate returns ONLY {ok: False, reason: ...} — no history fields
        for forbidden in ("highlights", "top_topics", "languages_used", "markdown"):
            self.assertNotIn(
                forbidden, result,
                msg=f"Forbidden field '{forbidden}' leaked in privacy mode: {result}",
            )


class TestHotwordDetectorAtomicSave(unittest.TestCase):
    """C2 LOW — HotwordDetector._save must use atomic write to avoid corruption."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.detector = HotwordDetector(data_dir=self.tmp.name)
        self.path = Path(self.tmp.name) / "hotwords.json"

    # ------------------------------------------------------------------
    # Basic atomicity: tmp file must not persist after successful save
    # ------------------------------------------------------------------

    def test_no_tmp_file_left_after_save(self) -> None:
        """After _save(), no .json.tmp file must remain on disk."""
        self.detector.add_hotword("test")
        tmp_path = self.path.with_suffix(".json.tmp")
        self.assertFalse(tmp_path.exists(), f".json.tmp unexpectedly present: {tmp_path}")

    def test_hotwords_json_written_correctly(self) -> None:
        """hotwords.json contains the expected entries after add_hotword."""
        self.detector.add_hotword("alpha", category="security")
        self.assertTrue(self.path.exists())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        words = [e["word"] for e in data]
        self.assertIn("alpha", words)

    # ------------------------------------------------------------------
    # Round-trip: save → fresh load → same words
    # ------------------------------------------------------------------

    def test_roundtrip_save_and_reload(self) -> None:
        """Words survive a save → new HotwordDetector reload cycle."""
        self.detector.add_hotword("roundtrip_word", category="info")
        # Create new instance to force reload from disk
        detector2 = HotwordDetector(data_dir=self.tmp.name)
        words = [e["word"] for e in detector2.get_hotwords()]
        self.assertIn("roundtrip_word", words)

    def test_multiple_words_roundtrip(self) -> None:
        """Multiple words all survive a reload cycle."""
        for w in ("alpha", "beta", "gamma"):
            self.detector.add_hotword(w, category="test")
        detector2 = HotwordDetector(data_dir=self.tmp.name)
        words = {e["word"] for e in detector2.get_hotwords()}
        self.assertGreaterEqual(words, {"alpha", "beta", "gamma"})

    # ------------------------------------------------------------------
    # Corruption-safety: existing file stays intact if write is interrupted
    # (simulated by making parent dir read-only)
    # ------------------------------------------------------------------

    def test_existing_file_preserved_on_save_error(self) -> None:
        """If _save raises, the original hotwords.json must not be truncated."""
        import os

        # First successful write
        self.detector.add_hotword("original_word")
        self.assertTrue(self.path.exists())

        # Corrupt the tmp path by making it a directory so write fails
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.mkdir(exist_ok=True)
        try:
            # add_hotword calls _save internally; it should log exception, not raise
            try:
                self.detector.add_hotword("new_word_that_fails")
            except Exception:
                pass  # exception caught inside _save is expected
            # Original file must still be readable and contain previous data
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                words = [e["word"] for e in data]
                self.assertIn("original_word", words)
        finally:
            # Clean up the directory we created
            try:
                tmp_path.rmdir()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
