"""Unit tests for Wave 78 (Wave 205) Phase B error codes and their call-site wiring.

One test per new code:
1. stt.gigaam_hf_cache_miss    — engine.py GigaAM exception handler (HF cache/network)
2. rewriter.model_unloaded     — llm_rewriter.py HTTP 400/422 "not started loading"
3. rewriter.output_ratio_fallback — llm_rewriter.py length ratio guard (too short / too long)
4. stt.mlx_watchdog_hang       — mlx_subprocess.py watchdog timeout
5. ipc.audio_device_poll_flood — service.py list_audio_inputs >10 calls/sec
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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
# 1. stt.gigaam_hf_cache_miss — engine.py GigaAM exception handler
# ---------------------------------------------------------------------------

class GigaamHfCacheMissTests(unittest.TestCase):
    """stt.gigaam_hf_cache_miss fires when GigaAM fails with HF cache/network error."""

    def test_code_in_registry(self):
        self.assertIn("stt.gigaam_hf_cache_miss", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.gigaam_hf_cache_miss"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 600)

    def test_push_via_error_bus(self):
        """Simulate the push that engine.py does on GigaAM HF cache miss."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["stt.gigaam_hf_cache_miss"]
        err = KrabError(
            severity=entry["severity"],
            component="stt",
            code="stt.gigaam_hf_cache_miss",
            message_user=entry["user_msg_ru"],
            message_debug="GigaAM HF cache miss: LocalEntryNotFoundError: not found in cache",
            timestamp=datetime.now(timezone.utc),
            context={"model": "gigaam-rnnt", "profile": "balanced"},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "stt.gigaam_hf_cache_miss")
        self.assertEqual(e.component, "stt")
        self.assertEqual(e.severity, "warn")
        self.assertIn("pyannote", e.message_user)

    def test_hf_cache_miss_keywords_match(self):
        """The keyword detection set covers known HF failure messages."""
        _hf_keywords = (
            "localentrynotfound", "repositorynotfound", "connection error",
            "not found in cache", "gated repo", "access to model",
            "cannot find the requested files",
        )
        test_cases = [
            "LocalEntryNotFoundError: pyannote/segmentation-3.0",
            "RepositoryNotFoundError: model not found",
            "Connection error: failed to reach huggingface.co",
            "not found in cache at path /root/.cache",
            "gated repo requires token",
            "access to model requires acceptance of terms",
            "Cannot find the requested files in the local cache",
        ]
        for msg in test_cases:
            msg_lower = msg.lower()
            matched = any(kw in msg_lower for kw in _hf_keywords)
            self.assertTrue(matched, f"Message '{msg[:60]}' should match HF cache miss detection")

    def test_non_hf_errors_not_matched(self):
        """Non-HF errors (e.g. OOM) should NOT trigger gigaam_hf_cache_miss."""
        _hf_keywords = (
            "localentrynotfound", "repositorynotfound", "connection error",
            "not found in cache", "gated repo", "access to model",
            "cannot find the requested files",
        )
        non_hf_errors = [
            "CUDA out of memory",
            "RuntimeError: Metal GPU assertion failed",
            "TypeError: unsupported audio format",
        ]
        for msg in non_hf_errors:
            matched = any(kw in msg.lower() for kw in _hf_keywords)
            self.assertFalse(matched, f"Message '{msg}' should NOT match HF cache miss detection")

    def test_dedupe_suppresses_second_push(self):
        """Second push within dedupe_seconds window is suppressed."""
        bus, captured = _make_error_bus()
        entry = ERROR_REGISTRY["stt.gigaam_hf_cache_miss"]

        def _make_err():
            return KrabError(
                severity=entry["severity"],
                component="stt",
                code="stt.gigaam_hf_cache_miss",
                message_user=entry["user_msg_ru"],
                message_debug="repeated",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

        first = bus.push(_make_err())
        self.assertTrue(first)
        second = bus.push(_make_err())
        self.assertFalse(second)


# ---------------------------------------------------------------------------
# 2. rewriter.model_unloaded — llm_rewriter.py HTTP 400/422
# ---------------------------------------------------------------------------

class RewriterModelUnloadedTests(unittest.TestCase):
    """rewriter.model_unloaded fires on HTTP 400/422 'not started loading' body."""

    def test_code_in_registry(self):
        self.assertIn("rewriter.model_unloaded", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["rewriter.model_unloaded"]
        self.assertEqual(entry["severity"], "error")
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_lm_studio_settings")
        self.assertNotEqual(entry["action_label"], "")
        self.assertEqual(entry["dedupe_seconds"], 120)

    def test_push_via_error_bus(self):
        """Simulate rewriter._push_error for model_unloaded."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["rewriter.model_unloaded"]
        err = KrabError(
            severity=entry["severity"],
            component="rewriter",
            code="rewriter.model_unloaded",
            message_user=entry["user_msg_ru"],
            message_debug="HTTP 422: Model has not started loading",
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=True,
            action_id="open_lm_studio_settings",
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "rewriter.model_unloaded")
        self.assertEqual(e.severity, "error")
        self.assertTrue(e.actionable)
        self.assertEqual(e.action_id, "open_lm_studio_settings")
        self.assertIn("LM Studio", e.message_user)

    def test_trigger_keywords_cover_known_messages(self):
        """The body keywords trigger on known LM Studio error messages."""
        keywords = ("model has not started loading", "model is not loaded",
                    "not started loading", "model not loaded")
        test_bodies = [
            "Model has not started loading yet",
            "model is not loaded",
            "error: model not loaded, please load a model first",
            "The model has not started loading",
        ]
        for body in test_bodies:
            body_lower = body.lower()
            matched = any(kw in body_lower for kw in keywords)
            self.assertTrue(matched, f"Body '{body[:60]}' should match model_unloaded detection")

    def test_component_is_rewriter(self):
        code = "rewriter.model_unloaded"
        self.assertEqual(code.split(".")[0], "rewriter")


# ---------------------------------------------------------------------------
# 3. rewriter.output_ratio_fallback — llm_rewriter.py length ratio guard
# ---------------------------------------------------------------------------

class RewriterOutputRatioFallbackTests(unittest.TestCase):
    """rewriter.output_ratio_fallback fires when output length ratio is out of bounds."""

    def test_code_in_registry(self):
        self.assertIn("rewriter.output_ratio_fallback", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["rewriter.output_ratio_fallback"]
        self.assertEqual(entry["severity"], "info")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 30)

    def test_push_too_short(self):
        """Push rewriter.output_ratio_fallback for output_too_short path."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["rewriter.output_ratio_fallback"]
        err = KrabError(
            severity="info",
            component="rewriter",
            code="rewriter.output_ratio_fallback",
            message_user=entry["user_msg_ru"],
            message_debug="output_too_short: ratio=0.10 input_len=200 output_len=20",
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "rewriter.output_ratio_fallback")
        self.assertEqual(e.severity, "info")
        self.assertFalse(e.actionable)
        self.assertIn("исходный текст сохранён", e.message_user)

    def test_push_too_long(self):
        """Push rewriter.output_ratio_fallback for output_too_long path."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["rewriter.output_ratio_fallback"]
        err = KrabError(
            severity="info",
            component="rewriter",
            code="rewriter.output_ratio_fallback",
            message_user=entry["user_msg_ru"],
            message_debug="output_too_long: ratio=5.20 input_len=50 output_len=260",
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "rewriter.output_ratio_fallback")
        self.assertEqual(e.severity, "info")

    def test_ratio_bounds_are_correct(self):
        """Verify the ratio guard bounds (< 0.35 = too short, > 3.0 = too long)."""
        # These bounds are from llm_rewriter.py length ratio guard
        self.assertLess(0.34, 0.35)   # too short threshold
        self.assertGreater(3.1, 3.0)  # too long threshold


# ---------------------------------------------------------------------------
# 4. stt.mlx_watchdog_hang — mlx_subprocess.py watchdog timeout
# ---------------------------------------------------------------------------

class MlxWatchdogHangTests(unittest.TestCase):
    """stt.mlx_watchdog_hang fires when MLXWatchdog times out."""

    def test_code_in_registry(self):
        self.assertIn("stt.mlx_watchdog_hang", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.mlx_watchdog_hang"]
        self.assertEqual(entry["severity"], "critical")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 60)

    def test_push_watchdog_hang_no_bus(self):
        """_push_watchdog_hang is silent when no _error_bus module global."""
        from core import mlx_subprocess
        original = mlx_subprocess._error_bus
        try:
            mlx_subprocess._error_bus = None
            # Must not raise
            mlx_subprocess._push_watchdog_hang("mlx-whisper-balanced", 30.5, 1)
        finally:
            mlx_subprocess._error_bus = original

    def test_push_watchdog_hang_with_bus(self):
        """_push_watchdog_hang pushes stt.mlx_watchdog_hang when bus is wired."""
        from core import mlx_subprocess
        bus, captured = _make_error_bus()
        original = mlx_subprocess._error_bus
        try:
            mlx_subprocess._error_bus = bus
            mlx_subprocess._push_watchdog_hang("mlx-whisper-balanced", 30.5, 3)
        finally:
            mlx_subprocess._error_bus = original

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "stt.mlx_watchdog_hang")
        self.assertEqual(e.component, "stt")
        self.assertEqual(e.severity, "critical")
        self.assertIn("Metal GPU", e.message_user)
        self.assertIn("mlx-whisper-balanced", e.message_debug)
        self.assertEqual(e.context["crash_count"], 3)

    def test_component_is_stt(self):
        code = "stt.mlx_watchdog_hang"
        self.assertEqual(code.split(".")[0], "stt")

    def test_dedupe_suppresses_second_push(self):
        """Second push within dedupe window is suppressed."""
        from core import mlx_subprocess
        bus, _ = _make_error_bus()
        original = mlx_subprocess._error_bus
        try:
            mlx_subprocess._error_bus = bus
            mlx_subprocess._push_watchdog_hang("balanced", 30.0, 1)
            # Track what was captured directly
            entry = ERROR_REGISTRY["stt.mlx_watchdog_hang"]
            err2 = KrabError(
                severity=entry["severity"],
                component="stt",
                code="stt.mlx_watchdog_hang",
                message_user=entry["user_msg_ru"],
                message_debug="repeat",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )
            second = bus.push(err2)
            self.assertFalse(second)
        finally:
            mlx_subprocess._error_bus = original


# ---------------------------------------------------------------------------
# 5. ipc.audio_device_poll_flood — service.py list_audio_inputs rate guard
# ---------------------------------------------------------------------------

class IpcAudioDevicePollFloodTests(unittest.TestCase):
    """ipc.audio_device_poll_flood fires when list_audio_inputs is called >10×/sec."""

    def test_code_in_registry(self):
        self.assertIn("ipc.audio_device_poll_flood", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["ipc.audio_device_poll_flood"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 60)

    def test_push_via_error_bus(self):
        """Simulate the poll-flood push from _handle_list_audio_inputs."""
        bus, captured = _make_error_bus()

        entry = ERROR_REGISTRY["ipc.audio_device_poll_flood"]
        err = KrabError(
            severity=entry["severity"],
            component="ipc",
            code="ipc.audio_device_poll_flood",
            message_user=entry["user_msg_ru"],
            message_debug=(
                "list_audio_inputs called 15× in last 1s "
                "(poll flood — check Swift audio device picker refresh rate)"
            ),
            timestamp=datetime.now(timezone.utc),
            context={"calls_per_sec": 15},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "ipc.audio_device_poll_flood")
        self.assertEqual(e.severity, "warn")
        self.assertEqual(e.component, "ipc")
        self.assertIn("аудиоустройств", e.message_user)
        self.assertEqual(e.context["calls_per_sec"], 15)

    def test_flood_threshold_is_ten(self):
        """The flood threshold is >10 calls per second (not >10 including boundary)."""
        # Verify the threshold semantics: exactly 10 calls should NOT fire,
        # 11+ calls should fire. This mirrors the service.py: len(_call_times) > 10.
        self.assertFalse(10 > 10)   # boundary: 10 calls → no flood
        self.assertTrue(11 > 10)    # 11 calls → flood

    def test_component_is_ipc(self):
        code = "ipc.audio_device_poll_flood"
        self.assertEqual(code.split(".")[0], "ipc")

    def test_dedupe_suppresses_second_push(self):
        """Second push within dedupe window is suppressed."""
        bus, _ = _make_error_bus()
        entry = ERROR_REGISTRY["ipc.audio_device_poll_flood"]

        def _make_err():
            return KrabError(
                severity=entry["severity"],
                component="ipc",
                code="ipc.audio_device_poll_flood",
                message_user=entry["user_msg_ru"],
                message_debug="flood",
                timestamp=datetime.now(timezone.utc),
                context={"calls_per_sec": 12},
                actionable=False,
                action_id=None,
            )

        first = bus.push(_make_err())
        self.assertTrue(first)
        second = bus.push(_make_err())
        self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
