"""Unit tests: Sentry breadcrumbs fired by CallAssistService top-3 methods.

Verifies handle_summary, handle_timeline_to_history, handle_cost_estimate
all call add_breadcrumb with the correct category/message and privacy-safe
metadata (no transcript text, only counts/durations/cost).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch, call

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeStore:
    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "",
        }

    def add_history_item(self, **kwargs: Any):
        class _Item:
            id = "hist_abc123"
        return _Item()


class FakeRecorder:
    is_recording = False


class FakeTranscriber:
    pass


class FakeSummaryGateway(VoiceGatewayClient):
    """Returns a canned summary payload."""
    def post(self, voice_gateway_url, api_key, path, payload):
        if "/summary" in path:
            return {"ok": True, "payload": {"summary": "ok", "tasks": []}}
        return {"ok": True, "payload": {}}

    def get(self, voice_gateway_url, api_key, path):
        return {"ok": True, "payload": {}}


class FakeTimelineGateway(VoiceGatewayClient):
    """Returns export content + empty summary/stats."""
    def get(self, voice_gateway_url, api_key, path):
        if "/export" in path:
            return {"ok": True, "payload": {"content": "line1\nline2"}}
        if "/summary" in path:
            return {"ok": True, "payload": {"summary": "s", "tasks": []}}
        if "/stats" in path:
            return {"ok": True, "payload": {"stats": {"count": 2, "text_chars": 10}}}
        return {"ok": True, "payload": {}}

    def post(self, voice_gateway_url, api_key, path, payload):
        return {"ok": True, "payload": {}}


class FakeCostGateway(VoiceGatewayClient):
    """Returns a canned cost estimate payload."""
    def get(self, voice_gateway_url, api_key, path):
        if "/cost/estimate" in path:
            return {"ok": True, "payload": {"total_usd": 1.23, "country": "ES"}}
        return {"ok": True, "payload": {}}

    def post(self, voice_gateway_url, api_key, path, payload):
        return {"ok": True, "payload": {}}


def _make_service(gateway: VoiceGatewayClient) -> CallAssistService:
    svc = CallAssistService(
        store=FakeStore(),
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        gateway=gateway,
    )
    # Put a fake active session in state so gateway_session_id is populated
    svc._state["gateway_session_id"] = "gw_test_session"
    svc._state["active"] = True
    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandleSummaryBreadcrumb(unittest.TestCase):
    def test_breadcrumb_fired_on_success(self):
        svc = _make_service(FakeSummaryGateway())
        with patch("backend.call_assist_service.add_breadcrumb") as mock_bc:
            svc.handle_summary({"max_items": 10})

        mock_bc.assert_called_once()
        _, kwargs = mock_bc.call_args
        args = mock_bc.call_args[1] if mock_bc.call_args[1] else {}
        # positional call style: check via args list
        call_args = mock_bc.call_args
        kw = call_args.kwargs if call_args.kwargs else {}
        pos = call_args.args
        # Support both positional and keyword invocations
        category = kw.get("category") or (pos[0] if pos else None)
        message = kw.get("message") or (pos[1] if len(pos) > 1 else None)
        data = kw.get("data") or (pos[3] if len(pos) > 3 else {})
        self.assertEqual(category, "call")
        self.assertEqual(message, "call_assist_summary")
        self.assertIn("ok", data)
        self.assertIn("duration_ms", data)
        self.assertIn("max_items", data)
        # Privacy: no transcript text
        self.assertNotIn("text", data)
        self.assertNotIn("summary", data)

    def test_breadcrumb_level_warning_on_failure(self):
        class FailGateway(VoiceGatewayClient):
            def post(self, *a, **kw):
                return {"ok": False, "error": "timeout"}
            def get(self, *a, **kw):
                return {"ok": True, "payload": {}}

        svc = _make_service(FailGateway())
        with patch("backend.call_assist_service.add_breadcrumb") as mock_bc:
            with self.assertRaises(RuntimeError):
                svc.handle_summary({})

        call_kw = mock_bc.call_args.kwargs if mock_bc.call_args.kwargs else {}
        call_pos = mock_bc.call_args.args
        level = call_kw.get("level") or (call_pos[2] if len(call_pos) > 2 else None)
        self.assertEqual(level, "warning")
        data = call_kw.get("data") or {}
        self.assertFalse(data.get("ok"))


class TestHandleTimelineToHistoryBreadcrumb(unittest.TestCase):
    def test_breadcrumb_fired_with_char_count(self):
        svc = _make_service(FakeTimelineGateway())
        with patch("backend.call_assist_service.add_breadcrumb") as mock_bc:
            result = svc.handle_timeline_to_history({"format": "md", "limit": 100})

        # May be called multiple times if other methods also fire; check last call
        calls = mock_bc.call_args_list
        timeline_calls = [
            c for c in calls
            if (c.kwargs.get("message") or (c.args[1] if len(c.args) > 1 else "")) == "call_assist_timeline_to_history"
        ]
        self.assertEqual(len(timeline_calls), 1)
        c = timeline_calls[0]
        data = c.kwargs.get("data") or {}
        self.assertEqual(data["format"], "md")
        self.assertGreater(data["chars"], 0)
        self.assertIn("summary_included", data)
        self.assertIn("stats_included", data)
        # Privacy: no transcript text content
        self.assertNotIn("content", data)
        self.assertNotIn("text", data)


class TestHandleCostEstimateBreadcrumb(unittest.TestCase):
    def test_breadcrumb_fired_with_cost_value(self):
        svc = _make_service(FakeCostGateway())
        with patch("backend.call_assist_service.add_breadcrumb") as mock_bc:
            svc.handle_cost_estimate({"country": "ES"})

        calls = mock_bc.call_args_list
        cost_calls = [
            c for c in calls
            if (c.kwargs.get("message") or (c.args[1] if len(c.args) > 1 else "")) == "call_assist_cost_estimate"
        ]
        self.assertEqual(len(cost_calls), 1)
        c = cost_calls[0]
        data = c.kwargs.get("data") or {}
        self.assertIn("total_usd", data)
        self.assertIn("country", data)
        self.assertIn("duration_ms", data)
        self.assertTrue(data.get("ok"))
        # Privacy: no phone numbers, no transcript text
        self.assertNotIn("phone", data)
        self.assertNotIn("text", data)

    def test_breadcrumb_level_warning_on_failure(self):
        class FailCostGateway(VoiceGatewayClient):
            def get(self, *a, **kw):
                return {"ok": False, "error": "gateway_down"}
            def post(self, *a, **kw):
                return {"ok": True, "payload": {}}

        svc = _make_service(FailCostGateway())
        with patch("backend.call_assist_service.add_breadcrumb") as mock_bc:
            with self.assertRaises(RuntimeError):
                svc.handle_cost_estimate({"country": "MX"})

        cost_calls = [
            c for c in mock_bc.call_args_list
            if (c.kwargs.get("message") or (c.args[1] if len(c.args) > 1 else "")) == "call_assist_cost_estimate"
        ]
        self.assertEqual(len(cost_calls), 1)
        c = cost_calls[0]
        level = c.kwargs.get("level") or (c.args[2] if len(c.args) > 2 else None)
        self.assertEqual(level, "warning")


if __name__ == "__main__":
    unittest.main()
