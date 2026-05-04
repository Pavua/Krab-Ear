"""Tests for report_hotkey_conflict IPC handler (Phase B.2 followup F9).

Verifies that the handler pushes a hotkey.conflict KrabError to the error bus
ring buffer when the chord is already held by another app.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.service import BackendService
from backend.translator import TranslationResult


# ---------------------------------------------------------------------------
# Minimal fakes (same pattern as test_transcriber_errors.py)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000

    def start(self) -> bool:
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        self.is_recording = False
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        return np.ones(16000, dtype=np.float32), 1.0


class _FakeTranscriber:
    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None) -> str:
        self.counter += 1
        return f"test #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class _FakeTranslator:
    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary: dict | None = None) -> TranslationResult:
        return TranslationResult(
            text="", status="not_requested",
            source_lang="", target_lang="", mode="off", engine="fake",
        )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class ReportHotkeyConflictTests(unittest.TestCase):
    """IPC handler report_hotkey_conflict pushes hotkey.conflict KrabError."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = BackendService(
            store=StateStore(Path(self.tmp.name) / "data"),
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        self.addCleanup(self.service.close)

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    def test_report_hotkey_conflict_returns_ok(self) -> None:
        """Handler returns ok=True for a known chord."""
        resp = self._call("report_hotkey_conflict", {"chord": "right_option"})
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"), msg=f"Result not ok: {result}")

    def test_report_hotkey_conflict_pushes_to_ring_buffer(self) -> None:
        """hotkey.conflict KrabError appears in list_recent_errors ring buffer."""
        self._call("report_hotkey_conflict", {"chord": "right_option"})
        errors_resp = self._call("list_recent_errors", {})
        self.assertTrue(errors_resp.get("ok"), msg=f"list_recent_errors failed: {errors_resp}")
        errors = errors_resp["result"]["errors"]
        codes = [e["code"] for e in errors]
        self.assertIn("hotkey.conflict", codes,
                      msg=f"hotkey.conflict not in ring buffer; codes={codes}")

    def test_report_hotkey_conflict_severity_is_warn(self) -> None:
        """hotkey.conflict error has severity='warn' (from ERROR_REGISTRY)."""
        self._call("report_hotkey_conflict", {"chord": "right_option"})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        conflict_errors = [e for e in errors if e["code"] == "hotkey.conflict"]
        self.assertTrue(conflict_errors, "hotkey.conflict should be in ring buffer")
        self.assertEqual(conflict_errors[0]["severity"], "warn")

    def test_report_hotkey_conflict_stores_chord_in_context(self) -> None:
        """Chord identifier is preserved in the KrabError context."""
        self._call("report_hotkey_conflict", {"chord": "right_option"})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        conflict_errors = [e for e in errors if e["code"] == "hotkey.conflict"]
        self.assertTrue(conflict_errors)
        self.assertEqual(conflict_errors[0]["context"].get("chord"), "right_option")

    def test_report_hotkey_conflict_unknown_chord_still_ok(self) -> None:
        """Unknown chord is treated as metadata only — handler still returns ok=True."""
        resp = self._call("report_hotkey_conflict", {"chord": "completely_unknown_chord"})
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"), msg=f"Result not ok: {result}")
        # Should still push a hotkey.conflict error
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        codes = [e["code"] for e in errors]
        self.assertIn("hotkey.conflict", codes)

    def test_report_hotkey_conflict_empty_chord_still_ok(self) -> None:
        """Missing chord param returns ok=True (chord is optional metadata)."""
        resp = self._call("report_hotkey_conflict", {})
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"), msg=f"Result not ok: {result}")

    def test_report_hotkey_conflict_component_is_hotkey(self) -> None:
        """KrabError component field is 'hotkey'."""
        self._call("report_hotkey_conflict", {"chord": "left_option"})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        conflict_errors = [e for e in errors if e["code"] == "hotkey.conflict"]
        self.assertTrue(conflict_errors)
        self.assertEqual(conflict_errors[0]["component"], "hotkey")

    def test_report_hotkey_conflict_is_actionable(self) -> None:
        """hotkey.conflict error is actionable (from ERROR_REGISTRY)."""
        self._call("report_hotkey_conflict", {"chord": "right_option"})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        conflict_errors = [e for e in errors if e["code"] == "hotkey.conflict"]
        self.assertTrue(conflict_errors)
        self.assertTrue(conflict_errors[0]["actionable"])
        self.assertEqual(conflict_errors[0]["action_id"], "open_hotkey_settings")


if __name__ == "__main__":
    unittest.main()
