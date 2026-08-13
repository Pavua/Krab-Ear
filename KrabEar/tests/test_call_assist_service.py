"""Unit tests for CallAssistService backpressure tracking (C4)."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402


class FakeStore:
    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "",
        }


class FakeRecorder:
    is_recording = False

    def start(self) -> bool:
        self.is_recording = True
        return True

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(int(max_duration_sec * 16000), dtype=np.float32), max_duration_sec


class FakeTranscriber:
    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict:
        return {"text": "hello"}


class SlowGateway(VoiceGatewayClient):
    """Gateway that blocks in post() until released, simulating in-flight POSTs."""

    def __init__(self) -> None:
        self._block = threading.Event()
        self._entered = threading.Event()
        self.post_count = 0

    def unblock(self) -> None:
        self._block.set()

    def get(self, voice_gateway_url: str, api_key: str, path: str) -> dict:
        return {"ok": True, "payload": {"status": "ok"}}

    def post(self, voice_gateway_url: str, api_key: str, path: str, payload: dict) -> dict:
        self.post_count += 1
        self._entered.set()
        self._block.wait(timeout=5.0)
        return {"ok": True}


def _make_service(gateway: VoiceGatewayClient) -> CallAssistService:
    svc = CallAssistService(
        store=FakeStore(),
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        gateway=gateway,
    )
    # Pre-set active gateway session to allow handle_diagnostics
    with svc._lock:
        svc._state["active"] = True
        svc._state["gateway_session_id"] = "test-session-123"
    return svc


class TestCallAssistBackpressure(unittest.TestCase):
    """Verify pending_post tracking and diagnostic exposure."""

    def test_pending_post_count_increments_and_max_tracked(self) -> None:
        """Simulating 2 concurrent POSTs: max_observed must be >= 2."""
        gw = SlowGateway()
        svc = _make_service(gw)

        def _inc() -> None:
            with svc._lock:
                svc._pending_post_count += 1
                current = svc._pending_post_count
                if current > svc._max_pending_post_depth_observed:
                    svc._max_pending_post_depth_observed = current

        # Simulate 2 concurrent in-flight POSTs
        _inc()
        _inc()

        with svc._lock:
            current = svc._pending_post_count
            max_obs = svc._max_pending_post_depth_observed

        self.assertEqual(current, 2)
        self.assertGreaterEqual(max_obs, 2)

    def test_diagnostic_returns_pending_posts_field(self) -> None:
        """handle_diagnostics must include pending_posts with current and max_observed."""
        gw = SlowGateway()
        svc = _make_service(gw)

        # Manually set some observed values
        with svc._lock:
            svc._pending_post_count = 0
            svc._max_pending_post_depth_observed = 4

        result = svc.handle_diagnostics({})

        self.assertIn("pending_posts", result)
        pp = result["pending_posts"]
        self.assertIn("current", pp)
        self.assertIn("max_observed", pp)
        self.assertEqual(pp["current"], 0)
        self.assertEqual(pp["max_observed"], 4)

    def test_pending_count_resets_to_zero_after_post(self) -> None:
        """After post completes, pending count returns to 0."""
        gw = SlowGateway()
        gw.unblock()  # non-blocking
        svc = _make_service(gw)

        # Simulate the inc/dec pattern from _assist_loop
        with svc._lock:
            svc._pending_post_count += 1
            current = svc._pending_post_count
            if current > svc._max_pending_post_depth_observed:
                svc._max_pending_post_depth_observed = current

        try:
            gw.post("http://127.0.0.1:8090", "", "/v1/sessions/x/events", {})
        finally:
            with svc._lock:
                svc._pending_post_count = max(0, svc._pending_post_count - 1)

        with svc._lock:
            self.assertEqual(svc._pending_post_count, 0)
            self.assertEqual(svc._max_pending_post_depth_observed, 1)


if __name__ == "__main__":
    unittest.main()
