"""Integration tests for ErrorBus IPC handlers (Phase B.1).

Tests: list_recent_errors, clear_recent_errors, handle_error_action, probe_llm_http.
Uses the same direct-BackendService harness as test_backend_service.py.
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
# Minimal fakes (same pattern as test_backend_service.py)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(16000, dtype=np.float32), float(self._snapshot_counter)


class _FakeTranscriber:
    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None) -> str:
        self.counter += 1
        return f"тест #{self.counter}"

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

class ErrorBusIntegrationTestCase(unittest.TestCase):
    """IPC integration tests for Phase B.1 error bus handlers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t1", "method": method, "params": params or {}}
        )

    # ------------------------------------------------------------------
    # 1. list_recent_errors — fresh backend → empty list
    # ------------------------------------------------------------------

    def test_list_recent_errors_empty(self) -> None:
        """Fresh backend has no errors in ring buffer."""
        resp = self._call("list_recent_errors")
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertIn("errors", result)
        self.assertIsInstance(result["errors"], list)
        self.assertEqual(result["errors"], [])

    # ------------------------------------------------------------------
    # 2. clear_recent_errors — should return cleared count (0 on fresh svc)
    # ------------------------------------------------------------------

    def test_clear_recent_errors(self) -> None:
        """Clearing empty ring buffer returns cleared=0 without error."""
        resp = self._call("clear_recent_errors")
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertIn("cleared", result)
        self.assertIsInstance(result["cleared"], int)
        self.assertGreaterEqual(result["cleared"], 0)

    # ------------------------------------------------------------------
    # 3. handle_error_action — unknown action_id → executed=False
    # ------------------------------------------------------------------

    def test_handle_error_action_unknown(self) -> None:
        """Bogus action_id returns executed=False with a descriptive reason."""
        resp = self._call("handle_error_action", {"action_id": "bogus_nonexistent_action"})
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertIn("executed", result)
        self.assertFalse(result["executed"])
        self.assertIn("reason", result)
        self.assertIsNotNone(result["reason"])
        reason_lower = str(result["reason"]).lower()
        self.assertTrue(
            "unknown" in reason_lower or "not found" in reason_lower or "bogus" in reason_lower,
            msg=f"Expected 'unknown' in reason, got: {result['reason']!r}",
        )

    def test_handle_error_action_missing_action_id(self) -> None:
        """Missing action_id returns executed=False with reason 'missing action_id'."""
        resp = self._call("handle_error_action", {})
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertFalse(result["executed"])
        self.assertIn("missing", str(result.get("reason", "")).lower())

    # ------------------------------------------------------------------
    # 4. probe_llm_http — response shape valid (LM Studio not required)
    # ------------------------------------------------------------------

    def test_probe_llm_http_response_shape(self) -> None:
        """probe_llm_http returns dict with reachable, latency_ms, model keys."""
        resp = self._call("probe_llm_http")
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        # All three keys must be present regardless of LM Studio availability
        self.assertIn("reachable", result)
        self.assertIn("latency_ms", result)
        self.assertIn("model", result)
        # reachable must be a bool
        self.assertIsInstance(result["reachable"], bool)
        # latency_ms must be a number
        self.assertIsInstance(result["latency_ms"], (int, float))


if __name__ == "__main__":
    unittest.main()
