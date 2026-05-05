"""Tests for Phase C C.2 — IPC reconnect protocol.

Covers:
  - handle_handshake: returns backend_version, phase_b_capable, phase_c_capable
  - handle_report_reconnect: pushes ipc.reconnect KrabError to error bus
  - ipc.reconnect in ERROR_REGISTRY and Component Literal
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
# Minimal fakes
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


def _build_service(tmp_dir: str) -> BackendService:
    return BackendService(
        store=StateStore(Path(tmp_dir) / "data"),
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )


# ---------------------------------------------------------------------------
# Test: handle_handshake
# ---------------------------------------------------------------------------

class HandshakeTests(unittest.TestCase):
    """handle_handshake returns backend capabilities on connect."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = _build_service(self.tmp.name)
        self.addCleanup(self.service.close)

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    def test_handshake_returns_ok(self) -> None:
        resp = self._call("handshake", {"swift_agent_version": "1.0.0"})
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"), msg=f"Result not ok: {result}")

    def test_handshake_returns_phase_b_capable(self) -> None:
        resp = self._call("handshake", {"swift_agent_version": "1.0.0"})
        result = resp.get("result", {})
        self.assertTrue(result.get("phase_b_capable"), msg=f"Missing phase_b_capable: {result}")

    def test_handshake_returns_phase_c_capable(self) -> None:
        resp = self._call("handshake", {"swift_agent_version": "1.0.0"})
        result = resp.get("result", {})
        self.assertTrue(result.get("phase_c_capable"), msg=f"Missing phase_c_capable: {result}")

    def test_handshake_returns_backend_version(self) -> None:
        resp = self._call("handshake", {"swift_agent_version": "1.0.0"})
        result = resp.get("result", {})
        self.assertIn("backend_version", result)
        self.assertIsInstance(result["backend_version"], str)
        self.assertTrue(result["backend_version"], "backend_version should not be empty")

    def test_handshake_acks_swift_version(self) -> None:
        """swift_version_ack echoes the sent version."""
        resp = self._call("handshake", {"swift_agent_version": "2.3.1"})
        result = resp.get("result", {})
        self.assertEqual(result.get("swift_version_ack"), "2.3.1")

    def test_handshake_missing_version_still_ok(self) -> None:
        """Handshake with no params returns ok (graceful degradation)."""
        resp = self._call("handshake", {})
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")

    def test_handshake_with_capabilities_ok(self) -> None:
        resp = self._call("handshake", {
            "swift_agent_version": "1.0.0",
            "capabilities": ["error_bus_consumer", "live_subs"],
        })
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"), msg=f"Result not ok: {result}")


# ---------------------------------------------------------------------------
# Test: handle_report_reconnect
# ---------------------------------------------------------------------------

class ReportReconnectTests(unittest.TestCase):
    """handle_report_reconnect pushes ipc.reconnect KrabError to error bus."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = _build_service(self.tmp.name)
        self.addCleanup(self.service.close)

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    def test_report_reconnect_returns_ok(self) -> None:
        resp = self._call("report_reconnect", {"attempts": 3, "duration_ms": 1500})
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"), msg=f"Result not ok: {result}")

    def test_report_reconnect_pushes_ipc_reconnect_code(self) -> None:
        self._call("report_reconnect", {"attempts": 3, "duration_ms": 1500})
        errors_resp = self._call("list_recent_errors", {})
        self.assertTrue(errors_resp.get("ok"), msg=f"list_recent_errors failed: {errors_resp}")
        errors = errors_resp["result"]["errors"]
        codes = [e["code"] for e in errors]
        self.assertIn("ipc.reconnect", codes, msg=f"ipc.reconnect not in ring buffer; codes={codes}")

    def test_report_reconnect_severity_is_info(self) -> None:
        self._call("report_reconnect", {"attempts": 1, "duration_ms": 250})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        reconnect_errors = [e for e in errors if e["code"] == "ipc.reconnect"]
        self.assertTrue(reconnect_errors, "ipc.reconnect should be in ring buffer")
        self.assertEqual(reconnect_errors[0]["severity"], "info")

    def test_report_reconnect_component_is_ipc(self) -> None:
        self._call("report_reconnect", {"attempts": 2, "duration_ms": 750})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        reconnect_errors = [e for e in errors if e["code"] == "ipc.reconnect"]
        self.assertTrue(reconnect_errors)
        self.assertEqual(reconnect_errors[0]["component"], "ipc")

    def test_report_reconnect_stores_context(self) -> None:
        self._call("report_reconnect", {"attempts": 4, "duration_ms": 3750})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        reconnect_errors = [e for e in errors if e["code"] == "ipc.reconnect"]
        self.assertTrue(reconnect_errors)
        ctx = reconnect_errors[0]["context"]
        self.assertEqual(ctx["attempts"], 4)
        self.assertEqual(ctx["duration_ms"], 3750)

    def test_report_reconnect_not_actionable(self) -> None:
        self._call("report_reconnect", {"attempts": 1, "duration_ms": 250})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        reconnect_errors = [e for e in errors if e["code"] == "ipc.reconnect"]
        self.assertTrue(reconnect_errors)
        self.assertFalse(reconnect_errors[0]["actionable"])
        self.assertIsNone(reconnect_errors[0]["action_id"])

    def test_report_reconnect_no_params_still_ok(self) -> None:
        """Handler is resilient to missing params (defaults to 0)."""
        resp = self._call("report_reconnect", {})
        self.assertTrue(resp.get("ok"), msg=f"IPC error: {resp}")
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        codes = [e["code"] for e in errors]
        self.assertIn("ipc.reconnect", codes)


# ---------------------------------------------------------------------------
# Test: ERROR_REGISTRY and Component Literal
# ---------------------------------------------------------------------------

class IpcReconnectRegistryTests(unittest.TestCase):
    """ipc.reconnect entry exists in ERROR_REGISTRY with correct shape."""

    def test_ipc_reconnect_in_registry(self) -> None:
        from backend.error_codes import ERROR_REGISTRY
        self.assertIn("ipc.reconnect", ERROR_REGISTRY)

    def test_ipc_reconnect_severity_is_info(self) -> None:
        from backend.error_codes import ERROR_REGISTRY
        self.assertEqual(ERROR_REGISTRY["ipc.reconnect"]["severity"], "info")

    def test_ipc_reconnect_not_actionable(self) -> None:
        from backend.error_codes import ERROR_REGISTRY
        entry = ERROR_REGISTRY["ipc.reconnect"]
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])

    def test_ipc_reconnect_dedupe_seconds_positive(self) -> None:
        from backend.error_codes import ERROR_REGISTRY
        self.assertGreater(ERROR_REGISTRY["ipc.reconnect"]["dedupe_seconds"], 0)

    def test_component_literal_includes_ipc(self) -> None:
        """Component Literal in error_bus.py must include 'ipc'."""
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY
        from datetime import datetime, timezone
        # Constructing a KrabError with component="ipc" must not raise ValidationError
        entry = ERROR_REGISTRY["ipc.reconnect"]
        err = KrabError(
            severity=entry["severity"],
            component="ipc",
            code="ipc.reconnect",
            message_user=entry["user_msg_ru"],
            message_debug="test reconnect",
            timestamp=datetime.now(timezone.utc),
            context={"attempts": 1, "duration_ms": 250},
            actionable=False,
            action_id=None,
        )
        self.assertEqual(err.component, "ipc")


if __name__ == "__main__":
    unittest.main()
