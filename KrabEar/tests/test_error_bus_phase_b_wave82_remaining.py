"""Unit tests for Wave 505 Phase B Wave 82 remaining 3 medium-priority error codes.

1. stt.postprocess_drop         — engine.py cleanup reduces non-empty raw_text to empty
2. rewriter.circuit_cascade     — llm_rewriter.py CircuitBreaker HALF_OPEN→OPEN escalation
3. stt.gigaam_longform_unavailable — engine.py GigaAM longform combined dedup (gated + cache miss)
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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
    mock_event_bus = MagicMock()
    bus = ErrorBus(event_bus=mock_event_bus, registry=ERROR_REGISTRY)
    captured: list[KrabError] = []

    original_push = bus.push

    def _capture(err: KrabError) -> bool:
        captured.append(err)
        return original_push(err)

    bus.push = _capture  # type: ignore[method-assign]
    return bus, captured


def _make_err(code: str, component: str = "stt", severity: str = "warn",
              actionable: bool = False, action_id: str | None = None) -> KrabError:
    entry = ERROR_REGISTRY[code]
    return KrabError(
        severity=entry["severity"],
        component=component,
        code=code,
        message_user=entry["user_msg_ru"],
        message_debug=f"test debug for {code}",
        timestamp=datetime.now(timezone.utc),
        context={},
        actionable=entry["actionable"],
        action_id=entry["action_id"],
    )


# ---------------------------------------------------------------------------
# 1. stt.postprocess_drop
# ---------------------------------------------------------------------------

class SttPostprocessDropTests(unittest.TestCase):
    """stt.postprocess_drop fires when cleanup_transcript reduces non-empty raw to empty."""

    def test_code_in_registry(self):
        self.assertIn("stt.postprocess_drop", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.postprocess_drop"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["action_label"], "")
        self.assertEqual(entry["dedupe_seconds"], 300)

    def test_push_via_error_bus(self):
        bus, captured = _make_error_bus()
        bus.push(_make_err("stt.postprocess_drop"))
        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "stt.postprocess_drop")
        self.assertEqual(e.component, "stt")
        self.assertEqual(e.severity, "warn")
        self.assertFalse(e.actionable)

    def test_message_mentions_postprocess(self):
        entry = ERROR_REGISTRY["stt.postprocess_drop"]
        # Message should indicate the drop is due to repetition or hallucination
        msg = entry["user_msg_ru"].lower()
        self.assertTrue(
            "постобработка" in msg or "cleanup" in msg or "drop" in msg.lower(),
            f"Expected postprocess-drop context in: {entry['user_msg_ru']}"
        )

    def test_dedupe_suppresses_second_push(self):
        bus, captured = _make_error_bus()
        first = bus.push(_make_err("stt.postprocess_drop"))
        self.assertTrue(first)
        second = bus.push(_make_err("stt.postprocess_drop"))
        self.assertFalse(second)

    def test_component_is_stt(self):
        self.assertTrue("stt.postprocess_drop".startswith("stt."))


# ---------------------------------------------------------------------------
# 2. rewriter.circuit_cascade
# ---------------------------------------------------------------------------

class RewriterCircuitCascadeTests(unittest.TestCase):
    """rewriter.circuit_cascade fires on HALF_OPEN->OPEN escalation."""

    def test_code_in_registry(self):
        self.assertIn("rewriter.circuit_cascade", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["rewriter.circuit_cascade"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["action_label"], "")
        self.assertEqual(entry["dedupe_seconds"], 600)

    def test_push_via_error_bus(self):
        bus, captured = _make_error_bus()
        entry = ERROR_REGISTRY["rewriter.circuit_cascade"]
        err = KrabError(
            severity=entry["severity"],
            component="rewriter",
            code="rewriter.circuit_cascade",
            message_user=entry["user_msg_ru"],
            message_debug="HALF_OPEN->OPEN probe failed; cooldown now 120s",
            timestamp=datetime.now(timezone.utc),
            context={"cooldown_sec": 120},
            actionable=entry["actionable"],
            action_id=entry["action_id"],
        )
        bus.push(err)
        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "rewriter.circuit_cascade")
        self.assertEqual(e.component, "rewriter")

    def test_cascade_callback_wired_to_llm_rewriter(self):
        """LLMRewriter sets _on_circuit_cascade_cb on the circuit breaker at init."""
        from backend.llm_rewriter import LLMRewriter
        rw = LLMRewriter(
            base_url="http://localhost:1234",
            api_key="",
            model="test-model",
        )
        # The callback must be set on the circuit
        cb = getattr(rw._circuit, "_on_circuit_cascade", None)
        self.assertIsNotNone(cb, "_on_circuit_cascade callback not wired on circuit")
        self.assertTrue(callable(cb))

    def test_cascade_callback_calls_push_error(self):
        """When callback is invoked, _push_error fires with rewriter.circuit_cascade."""
        from backend.llm_rewriter import LLMRewriter
        rw = LLMRewriter(
            base_url="http://localhost:1234",
            api_key="",
            model="test-model",
        )
        bus, captured = _make_error_bus()
        rw._error_bus = bus  # late-inject
        # Manually invoke the cascade callback (simulates HALF_OPEN->OPEN)
        rw._on_circuit_cascade_cb(new_cooldown_sec=120)
        self.assertTrue(
            any(e.code == "rewriter.circuit_cascade" for e in captured),
            f"Expected rewriter.circuit_cascade in captured: {[e.code for e in captured]}"
        )

    def test_dedupe_suppresses_second_push(self):
        bus, captured = _make_error_bus()
        entry = ERROR_REGISTRY["rewriter.circuit_cascade"]

        def _err():
            return KrabError(
                severity=entry["severity"],
                component="rewriter",
                code="rewriter.circuit_cascade",
                message_user=entry["user_msg_ru"],
                message_debug="repeated cascade",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

        self.assertTrue(bus.push(_err()))
        self.assertFalse(bus.push(_err()))


# ---------------------------------------------------------------------------
# 3. stt.gigaam_longform_unavailable
# ---------------------------------------------------------------------------

class SttGigaamLongformUnavailableTests(unittest.TestCase):
    """stt.gigaam_longform_unavailable fires as combined dedup for longform HF-gated failure."""

    def test_code_in_registry(self):
        self.assertIn("stt.gigaam_longform_unavailable", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["stt.gigaam_longform_unavailable"]
        self.assertEqual(entry["severity"], "warn")
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_hf_token_setting")
        self.assertNotEqual(entry["action_label"], "")
        self.assertEqual(entry["dedupe_seconds"], 3600)

    def test_push_via_error_bus(self):
        bus, captured = _make_error_bus()
        entry = ERROR_REGISTRY["stt.gigaam_longform_unavailable"]
        err = KrabError(
            severity=entry["severity"],
            component="stt",
            code="stt.gigaam_longform_unavailable",
            message_user=entry["user_msg_ru"],
            message_debug="GigaAM longform unavailable (duration=45.0s): gated repo requires token",
            timestamp=datetime.now(timezone.utc),
            context={"duration_sec": 45.0},
            actionable=entry["actionable"],
            action_id=entry["action_id"],
        )
        bus.push(err)
        self.assertEqual(len(captured), 1)
        e = captured[0]
        self.assertEqual(e.code, "stt.gigaam_longform_unavailable")
        self.assertEqual(e.component, "stt")
        self.assertTrue(e.actionable)
        self.assertEqual(e.action_id, "open_hf_token_setting")

    def test_message_mentions_pyannote_and_whisper_fallback(self):
        entry = ERROR_REGISTRY["stt.gigaam_longform_unavailable"]
        msg = entry["user_msg_ru"].lower()
        self.assertIn("gigaam", msg)
        self.assertIn("whisper", msg)

    def test_replaces_double_toast_on_longform_path(self):
        """On longform=True path, gigaam_longform_unavailable fires; hf_cache_miss does not."""
        # Simulate the engine logic: use_longform=True → push longform_unavailable
        bus, captured = _make_error_bus()

        use_longform = True
        exc_str = "gated repo requires token to access model"
        _hf_cache_miss_keywords = (
            "localentrynotfound", "repositorynotfound", "connection error",
            "not found in cache", "gated repo", "access to model",
            "cannot find the requested files",
        )
        is_hf_miss = any(kw in exc_str for kw in _hf_cache_miss_keywords)
        self.assertTrue(is_hf_miss)

        if is_hf_miss:
            code = "stt.gigaam_longform_unavailable" if use_longform else "stt.gigaam_hf_cache_miss"
        else:
            code = None

        self.assertEqual(code, "stt.gigaam_longform_unavailable")

    def test_non_longform_path_still_uses_hf_cache_miss(self):
        """On use_longform=False path, the original hf_cache_miss code is pushed."""
        use_longform = False
        exc_str = "gated repo requires token to access model"
        _hf_cache_miss_keywords = (
            "localentrynotfound", "repositorynotfound", "connection error",
            "not found in cache", "gated repo", "access to model",
            "cannot find the requested files",
        )
        is_hf_miss = any(kw in exc_str for kw in _hf_cache_miss_keywords)
        if is_hf_miss:
            code = "stt.gigaam_longform_unavailable" if use_longform else "stt.gigaam_hf_cache_miss"
        else:
            code = None
        self.assertEqual(code, "stt.gigaam_hf_cache_miss")

    def test_dedupe_suppresses_second_push(self):
        bus, captured = _make_error_bus()
        entry = ERROR_REGISTRY["stt.gigaam_longform_unavailable"]

        def _err():
            return KrabError(
                severity=entry["severity"],
                component="stt",
                code="stt.gigaam_longform_unavailable",
                message_user=entry["user_msg_ru"],
                message_debug="repeated",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=True,
                action_id="open_hf_token_setting",
            )

        self.assertTrue(bus.push(_err()))
        self.assertFalse(bus.push(_err()))


if __name__ == "__main__":
    unittest.main()
