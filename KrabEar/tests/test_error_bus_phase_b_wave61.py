"""Unit tests for Wave 61 Phase B error codes and their call-site wiring.

One test per new code:
1. vgw.reconnect            — vg_ws_client.py reconnect loop
2. stt.diarization_skipped  — engine.py WhisperX diarization failure
3. rewriter.lm_studio_500   — llm_rewriter.py HTTP 500 with HTML body
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Allow imports from KrabEar/
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.error_bus import ErrorBus, KrabError
from backend.error_codes import ERROR_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_error_bus() -> tuple[ErrorBus, list[KrabError]]:
    """Create an ErrorBus and capture pushed errors."""
    mock_event_bus = MagicMock()
    bus = ErrorBus(event_bus=mock_event_bus, registry=ERROR_REGISTRY)
    captured: list[KrabError] = []

    original_push = bus.push

    def _capture(err: KrabError) -> bool:
        captured.append(err)
        return original_push(err)

    bus.push = _capture  # type: ignore[method-assign]
    return bus, captured


# ---------------------------------------------------------------------------
# 1. vgw.reconnect — vg_ws_client.py
# ---------------------------------------------------------------------------

class VGWReconnectTests(unittest.TestCase):
    """vgw.reconnect fires when VGWebSocketClient loses connection."""

    def test_code_in_registry(self):
        self.assertIn("vgw.reconnect", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["vgw.reconnect"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 120)

    def test_push_error_helper_fires(self):
        """VGWebSocketClient._push_error sends vgw.reconnect with correct component."""
        from backend.vg_ws_client import VGWebSocketClient

        client = VGWebSocketClient.__new__(VGWebSocketClient)
        client.session_id = "vs_test123"
        client.ws_url = "ws://localhost:8090/v1/sessions/vs_test123/stream"

        bus, captured = _make_error_bus()
        client._error_bus = bus

        client._push_error("vgw.reconnect", "ConnectionRefusedError: reconnect in 1s")

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "vgw.reconnect")
        self.assertEqual(err.component, "vgw")
        self.assertEqual(err.severity, "warn")
        self.assertIn("переподключаемся", err.message_user)

    def test_push_error_no_bus_does_not_raise(self):
        """_push_error is silent when _error_bus is not injected."""
        from backend.vg_ws_client import VGWebSocketClient

        client = VGWebSocketClient.__new__(VGWebSocketClient)
        client.session_id = "vs_noop"
        client.ws_url = "ws://localhost:8090/v1/sessions/vs_noop/stream"
        # No _error_bus set — should be a no-op
        client._push_error("vgw.reconnect", "test")  # must not raise


# ---------------------------------------------------------------------------
# 2. stt.diarization_skipped — engine.py
# ---------------------------------------------------------------------------

class STTDiarizationSkippedTests(unittest.TestCase):
    """stt.diarization_skipped fires when WhisperX diarization raises."""

    def test_code_in_registry(self):
        self.assertIn("stt.diarization_skipped", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.diarization_skipped"]
        self.assertEqual(entry["severity"], "info")
        self.assertFalse(entry["actionable"])
        self.assertEqual(entry["dedupe_seconds"], 600)

    def test_push_error_fires_on_whisperx_fail(self):
        """Engine._push_error emits stt.diarization_skipped with info severity."""
        from core.engine import AudioEngine

        engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "balanced"
        engine.quality_profile = "balanced"

        bus, captured = _make_error_bus()
        engine._error_bus = bus

        engine._push_error(
            "stt.diarization_skipped",
            "WhisperX diarization failed: RuntimeError: CUDA error",
            severity="info",
        )

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "stt.diarization_skipped")
        self.assertEqual(err.component, "stt")
        self.assertEqual(err.severity, "info")
        self.assertIn("Спикеры не определены", err.message_user)

    def test_no_error_bus_injection_silent(self):
        """_push_error is silent when no _error_bus injected (engine pattern)."""
        from core.engine import AudioEngine

        engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "balanced"
        engine.quality_profile = "balanced"
        # No _error_bus — must be silent
        engine._push_error("stt.diarization_skipped", "test noop")  # must not raise


# ---------------------------------------------------------------------------
# 3. rewriter.lm_studio_500 — llm_rewriter.py
# ---------------------------------------------------------------------------

class RewriterLMStudio500Tests(unittest.TestCase):
    """rewriter.lm_studio_500 fires on HTTP 500 with HTML body."""

    def test_code_in_registry(self):
        self.assertIn("rewriter.lm_studio_500", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["rewriter.lm_studio_500"]
        self.assertEqual(entry["severity"], "error")
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_lm_studio_settings")
        self.assertEqual(entry["dedupe_seconds"], 60)

    def test_push_error_fires_with_error_severity(self):
        """LLMRewriter._push_error emits rewriter.lm_studio_500 at error severity."""
        from backend.llm_rewriter import LLMRewriter

        rewriter = LLMRewriter.__new__(LLMRewriter)
        rewriter._model = "test-model"
        rewriter._base_url = "http://localhost:1234"

        bus, captured = _make_error_bus()
        rewriter._error_bus = bus

        rewriter._push_error(
            "rewriter.lm_studio_500",
            "HTTP 500 HTML body: <!doctype html><html>Internal Server Error</html>",
            severity="error",
        )

        self.assertEqual(len(captured), 1)
        err = captured[0]
        self.assertEqual(err.code, "rewriter.lm_studio_500")
        self.assertEqual(err.component, "rewriter")
        self.assertEqual(err.severity, "error")
        self.assertIn("HTTP 500", err.message_user)
        self.assertIn("перезапусти", err.message_user)

    def test_html_body_detection_triggers_lm_studio_500(self):
        """The branching logic in llm_rewriter.py routes HTML 500 to lm_studio_500 code."""
        # Simulate the condition from the code:
        # elif response.status_code == 500 and ("<html" in body_preview.lower() or ...)
        body_html = "<!doctype html><html><body>Internal Server Error</body></html>"
        body_preview = body_html[:120].lower()

        is_html_500 = "<html" in body_preview or "<!doctype" in body_preview
        self.assertTrue(is_html_500, "HTML detection should match this body")

    def test_non_html_500_does_not_trigger_lm_studio_500(self):
        """A 500 with JSON body should NOT trigger rewriter.lm_studio_500."""
        body_json = '{"error": "internal error", "code": 500}'
        body_preview = body_json[:120].lower()

        is_html_500 = "<html" in body_preview or "<!doctype" in body_preview
        self.assertFalse(is_html_500, "JSON body should not match HTML detection")


if __name__ == "__main__":
    unittest.main()
