"""W947 — Tests for CalendarLinker IPC handlers wired in Wave 947.

Handlers under test (W942 MEDIUM-1 resolution):
  - link_to_calendar_event
  - get_calendar_link
  - search_by_calendar_event

All tests use the BackendService dispatch table via handle_request() to confirm
proper end-to-end wiring, not just method existence.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors existing test files)
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes (same pattern as test_backend_service.py)
# ---------------------------------------------------------------------------

class _FakeEngine:
    def __init__(self):
        self.cleanup_profile = "soft"
        self.quality_profile = "balanced"


class FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.sample_rate = 16000
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.is_recording = False
        return None


class FakeTranscriber:
    def __init__(self):
        self.counter = 0
        self.engine = _FakeEngine()

    def transcribe(self, audio_data, quality_profile="balanced", cleanup_profile="soft",
                   domain="casual", extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None):
        self.counter += 1
        return f"test #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return "preview"


class FakeTranslator:
    def __init__(self):
        self.last_mode = "off"

    def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
        self.last_mode = mode
        return TranslationResult(
            text="", status="not_requested", source_lang="", target_lang="", mode="off", engine="fake"
        )


def _make_service_and_store():
    """Return (service, store, tmp_path) using real BackendService with fakes."""
    tmp = Path(tempfile.mkdtemp())
    from backend.state_store import StateStore
    from backend.service import BackendService
    store = StateStore(data_dir=tmp)
    svc = BackendService(
        store=store,
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        translator=FakeTranslator(),
    )
    return svc, store, tmp


def _capture_dispatch_table(svc):
    """Capture the inline handlers dict from handle_request via frame inspection.

    The BackendService builds its dispatch dict *inside* handle_request at each
    call. We hook sys.settrace to capture the local 'handlers' variable.
    """
    import sys as _sys
    captured = {}
    original = svc.handle_request

    def capturing(payload):
        frame_ref = []

        def tracer(frame, event, arg):
            if event == "call" and frame.f_code is original.__func__.__code__:
                frame_ref.append(frame)
            return tracer

        old = _sys.gettrace()
        _sys.settrace(tracer)
        try:
            result = original(payload)
        finally:
            _sys.settrace(old)
        if frame_ref:
            captured.update(frame_ref[0].f_locals.get("handlers", {}))
        return result

    svc.handle_request = capturing
    svc.handle_request({"id": "probe", "method": "__w947_probe__", "params": {}})
    svc.handle_request = original
    return captured


def _req(svc, method, params=None):
    """Convenience: call handle_request with standard dict format."""
    return svc.handle_request({"id": "t", "method": method, "params": params or {}})


# ---------------------------------------------------------------------------
# Test: dispatch table registration
# ---------------------------------------------------------------------------

class TestCalendarLinkerDispatch(unittest.TestCase):
    """Verify the three new handlers appear in the dispatch table."""

    def setUp(self):
        self.svc, self.store, self.tmp = _make_service_and_store()
        self.handlers = _capture_dispatch_table(self.svc)

    def test_link_to_calendar_event_in_dispatch(self):
        self.assertIn(
            "link_to_calendar_event", self.handlers,
            "link_to_calendar_event must be in the IPC dispatch table",
        )

    def test_get_calendar_link_in_dispatch(self):
        self.assertIn(
            "get_calendar_link", self.handlers,
            "get_calendar_link must be in the IPC dispatch table",
        )

    def test_search_by_calendar_event_in_dispatch(self):
        self.assertIn(
            "search_by_calendar_event", self.handlers,
            "search_by_calendar_event must be in the IPC dispatch table",
        )

    def test_deleted_wave65_handlers_not_reintroduced(self):
        """Regression: Wave 65 batch 3 deleted these — they must NOT reappear."""
        service_path = os.path.join(PROJECT_ROOT, "backend", "service.py")
        with open(service_path, encoding="utf-8") as fh:
            source = fh.read()
        # The _v2 variants use different names; old names must not reappear
        for deleted in ("def _handle_get_calendar_link\n", "def _handle_search_by_calendar_event\n"):
            self.assertNotIn(
                deleted,
                source,
                f"'{deleted.strip()}' was deleted in Wave 65 batch 3 — "
                "new _v2 variants use different names and must not share the old def name.",
            )

    def test_v2_handler_methods_exist_on_service(self):
        """Confirm _v2 methods are defined (not just mapped)."""
        self.assertTrue(hasattr(self.svc, "_handle_link_to_calendar_event"))
        self.assertTrue(hasattr(self.svc, "_handle_get_calendar_link_v2"))
        self.assertTrue(hasattr(self.svc, "_handle_search_by_calendar_event_v2"))


# ---------------------------------------------------------------------------
# Test: link_to_calendar_event handler logic
# ---------------------------------------------------------------------------

class TestLinkToCalendarEvent(unittest.TestCase):

    def setUp(self):
        self.svc, self.store, self.tmp = _make_service_and_store()

    def _call(self, params):
        return self.svc._handle_link_to_calendar_event(params)

    def test_missing_item_id_returns_error(self):
        result = self._call({})
        self.assertFalse(result["ok"])
        self.assertIn("history_item_id", result.get("error", ""))

    def test_privacy_mode_skips_lookup(self):
        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": True,
            "calendar_link_enabled": True,
        }.get(k, d)):
            result = self._call({"history_item_id": "abc123"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "privacy_mode")
        self.assertIsNone(result["calendar_event"])

    def test_feature_flag_disabled_skips_lookup(self):
        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": False,
        }.get(k, d)):
            result = self._call({"history_item_id": "abc123"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "disabled")

    def test_no_active_event_returns_none(self):
        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": True,
        }.get(k, d)), \
             patch.object(self.svc._calendar_linker, "find_active_event", return_value=None):
            result = self._call({"history_item_id": "abc123"})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["calendar_event"])
        self.assertEqual(result["reason"], "no_active_event")

    def test_event_found_and_stored(self):
        """When a Calendar event is found, it must be persisted in StateStore."""
        item = self.store.add_history_item(text="hello")

        fake_event = {
            "title": "Team Sync",
            "start_iso": "2026-05-26T14:00:00",
            "end_iso": "2026-05-26T15:00:00",
            "location": "",
            "calendar_name": "Work",
        }

        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": True,
        }.get(k, d)), \
                patch.object(self.svc._calendar_linker, "find_active_event", return_value=fake_event):  # noqa: E127
            result = self._call({"history_item_id": item.id})

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["calendar_event"]["title"], "Team Sync")

        # Verify persisted in StateStore
        stored = self.store.get_history_item_calendar(item.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["title"], "Team Sync")

    def test_calendar_linker_exception_is_soft_fail(self):
        """CalendarLinker internal crash must not propagate — soft fail."""
        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": True,
        }.get(k, d)), \
                patch.object(self.svc._calendar_linker, "find_active_event",  # noqa: E127
                             side_effect=RuntimeError("osascript gone")):
            result = self._call({"history_item_id": "abc123"})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["calendar_event"])
        self.assertEqual(result["reason"], "error")

    def test_at_time_iso_string_is_parsed(self):
        """at_time param as ISO 8601 string must be parsed and forwarded."""
        captured = {}

        def mock_find(at_time=None):
            captured["at_time"] = at_time
            return None

        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": True,
        }.get(k, d)), \
                patch.object(self.svc._calendar_linker, "find_active_event", side_effect=mock_find):  # noqa: E127
            self._call({"history_item_id": "x", "at_time": "2026-05-26T14:30:00"})

        self.assertIsNotNone(captured.get("at_time"))
        self.assertIsInstance(captured["at_time"], datetime)
        self.assertEqual(captured["at_time"].hour, 14)
        self.assertEqual(captured["at_time"].minute, 30)

    def test_invalid_at_time_falls_back_to_none(self):
        """Bogus at_time string must not raise — falls back to None (CalendarLinker uses now())."""
        captured = {}

        def mock_find(at_time=None):
            captured["at_time"] = at_time
            return None

        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": True,
        }.get(k, d)), \
                patch.object(self.svc._calendar_linker, "find_active_event", side_effect=mock_find):  # noqa: E127
            result = self._call({"history_item_id": "x", "at_time": "not-a-date"})

        self.assertTrue(result["ok"])
        self.assertIsNone(captured.get("at_time"))


# ---------------------------------------------------------------------------
# Test: get_calendar_link handler
# ---------------------------------------------------------------------------

class TestGetCalendarLinkV2(unittest.TestCase):

    def setUp(self):
        self.svc, self.store, self.tmp = _make_service_and_store()

    def _call(self, params):
        return self.svc._handle_get_calendar_link_v2(params)

    def test_missing_item_id_returns_error(self):
        result = self._call({})
        self.assertFalse(result["ok"])

    def test_unknown_item_id_returns_none(self):
        result = self._call({"history_item_id": "does-not-exist"})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["calendar_event"])

    def test_returns_stored_event(self):
        item = self.store.add_history_item(text="test")
        event = {
            "title": "Board Meeting",
            "start_iso": "2026-05-26T09:00:00",
            "end_iso": "2026-05-26T10:00:00",
            "location": "Room A",
            "calendar_name": "Work",
        }
        self.store.update_history_item_calendar(item.id, event)

        result = self._call({"history_item_id": item.id})
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["calendar_event"])
        self.assertEqual(result["calendar_event"]["title"], "Board Meeting")

    def test_via_dispatch_table(self):
        """Confirm get_calendar_link routes through handle_request."""
        outer = _req(self.svc, "get_calendar_link", {"history_item_id": "no-such"})
        self.assertTrue(outer.get("ok"))  # outer envelope ok
        inner = outer.get("result", {})
        self.assertTrue(inner.get("ok"))  # handler ok
        self.assertIsNone(inner.get("calendar_event"))


# ---------------------------------------------------------------------------
# Test: search_by_calendar_event handler
# ---------------------------------------------------------------------------

class TestSearchByCalendarEventV2(unittest.TestCase):

    def setUp(self):
        self.svc, self.store, self.tmp = _make_service_and_store()

    def _call(self, params):
        return self.svc._handle_search_by_calendar_event_v2(params)

    def _add_item_with_event(self, event_title: str) -> str:
        item = self.store.add_history_item(text=f"text for {event_title}")
        self.store.update_history_item_calendar(
            item.id,
            {"title": event_title, "start_iso": "", "end_iso": "", "location": "", "calendar_name": "Cal"},
        )
        return item.id

    def test_empty_title_returns_all(self):
        for i in range(3):
            self._add_item_with_event(f"Event {i}")

        result = self._call({"event_title": ""})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 3)

    def test_filter_by_substring(self):
        alpha_id = self._add_item_with_event("Alpha Review")
        self._add_item_with_event("Beta Stand-up")

        result = self._call({"event_title": "alpha"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["item_id"], alpha_id)

    def test_no_match_returns_empty_list(self):
        result = self._call({"event_title": "nonexistent-xyz"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [])

    def test_result_shape(self):
        """Each result must have item_id and calendar_event keys."""
        iid = self._add_item_with_event("Shape Event")
        result = self._call({"event_title": "Shape"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 1)
        r = result["results"][0]
        self.assertIn("item_id", r)
        self.assertIn("calendar_event", r)
        self.assertEqual(r["calendar_event"]["title"], "Shape Event")
        self.assertEqual(r["item_id"], iid)

    def test_via_dispatch_table(self):
        """Confirm search_by_calendar_event routes through handle_request."""
        outer = _req(self.svc, "search_by_calendar_event", {"event_title": ""})
        self.assertTrue(outer.get("ok"))
        inner = outer.get("result", {})
        self.assertTrue(inner.get("ok"))
        self.assertIsInstance(inner.get("results"), list)


# ---------------------------------------------------------------------------
# Test: via handle_request dispatch (link_to_calendar_event E2E)
# ---------------------------------------------------------------------------

class TestLinkToCalendarEventDispatch(unittest.TestCase):

    def setUp(self):
        self.svc, self.store, self.tmp = _make_service_and_store()

    def test_handle_request_routes_to_handler_disabled(self):
        """handle_request("link_to_calendar_event") works when feature disabled.

        handle_request wraps the handler result in {"id": ..., "ok": True, "result": {...}},
        so we inspect result["result"] for the handler-level fields.
        """
        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": False,
        }.get(k, d)):
            outer = _req(self.svc, "link_to_calendar_event", {"history_item_id": "abc"})
        self.assertTrue(outer.get("ok"))  # outer envelope ok
        inner = outer.get("result", {})
        self.assertTrue(inner.get("ok"))  # handler ok
        self.assertTrue(inner.get("skipped"))  # feature disabled → skipped

    def test_handle_request_full_dict_form(self):
        """handle_request accepts the {id, method, params} dict form."""
        with patch.object(self.svc, "_get_runtime_setting", side_effect=lambda k, d=None: {
            "privacy_mode_enabled": False,
            "calendar_link_enabled": False,
        }.get(k, d)):
            outer = self.svc.handle_request(
                {"id": "w947", "method": "link_to_calendar_event",
                 "params": {"history_item_id": "abc"}}
            )
        self.assertTrue(outer.get("ok"))


if __name__ == "__main__":
    unittest.main()
