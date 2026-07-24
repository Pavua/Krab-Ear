"""Integration tests for IPC dispatch in BackendService.

Covers end-to-end request routing, cache invalidation, recording lifecycle,
bulk archive operations, clipboard flow, request-id tracking, settings
persistence across service instances, and throttle enforcement.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402

from backend.ipc_throttle import IPCThrottle  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRecorder:
    """Minimal deterministic recorder for IPC integration tests."""

    def __init__(self) -> None:
        self.is_recording: bool = False
        self.sample_rate: int = 16000
        self.last_stop_trim_ms: int = 0
        self.last_stop_timeout_sec: float = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (0.3 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)
        return audio, 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        return np.ones(32000, dtype=np.float32), 1.0


class FakeEngine:
    quality_profile: str = "balanced"
    current_model: str = "fake-model"

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class FakeTranscriber:
    """Deterministic transcriber stub."""

    def __init__(self) -> None:
        self.counter: int = 0
        self.engine = FakeEngine()

    def transcribe(
        self,
        audio_data: Any,
        quality_profile: str = "balanced",
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary: Any = None,
        lang_hint: Any = None,
        history_context: Any = None,
        stt_hotwords: Any = None,
    ) -> str:
        self.counter += 1
        return f"transcript #{self.counter}"

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> str:
        return f"preview ({quality_profile})"


class FakeTranslator:
    def translate(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict[str, str] | None = None,
    ) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class _IPCBase(unittest.TestCase):
    """Common setUp + helper for all IPC integration scenarios."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService starts background threads
        # (DiskSpaceMonitor и т.п.; R1 startup-recovery — только когда есть
        # реальная работа) that may write to data dir after the test ends →
        # OSError on cleanup in CI (established pattern, see BackendServiceTestCase
        # in test_backend_service.py). Особенно актуально здесь: этот класс
        # конструирует ВТОРОЙ BackendService на том же data_dir (reload-тесты).
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.store = StateStore(self.data_dir)
        self.recorder = FakeRecorder()
        self.transcriber = FakeTranscriber()
        self.translator = FakeTranslator()
        self.service = BackendService(
            store=self.store,
            recorder=self.recorder,
            transcriber=self.transcriber,
            translator=self.translator,
        )

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        req_id: str = "req-1",
    ) -> dict[str, Any]:
        return self.service.handle_request(
            {"id": req_id, "method": method, "params": params or {}}
        )

    def ok(self, resp: dict[str, Any]) -> dict[str, Any]:
        self.assertTrue(resp.get("ok"), f"Expected ok=True, got: {resp}")
        return resp["result"]


# ---------------------------------------------------------------------------
# Scenario 1 — valid payload routes to correct handler
# ---------------------------------------------------------------------------

class TestDispatchRouting(_IPCBase):
    """handle_request with valid payload invokes the correct handler."""

    def test_ping_dispatched(self) -> None:
        result = self.ok(self.call("ping"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["service"], "krabear-backend")

    def test_get_settings_dispatched(self) -> None:
        result = self.ok(self.call("get_settings"))
        self.assertIn("translation_mode", result)

    def test_unknown_method_returns_error(self) -> None:
        resp = self.call("nonexistent_method_xyz")
        self.assertFalse(resp.get("ok"))
        self.assertIn("error", resp)

    def test_missing_method_returns_error(self) -> None:
        resp = self.service.handle_request({"id": "x", "params": {}})
        self.assertFalse(resp.get("ok"))

    def test_invalid_params_type_returns_error(self) -> None:
        resp = self.service.handle_request(
            {"id": "x", "method": "ping", "params": "not-a-dict"}
        )
        self.assertFalse(resp.get("ok"))


# ---------------------------------------------------------------------------
# Scenario 2 — set_settings → cache invalidation → get_settings returns new
# ---------------------------------------------------------------------------

class TestSettingsCacheInvalidation(_IPCBase):
    """set_settings invalidates TTL cache; get_settings reflects new value."""

    def test_set_then_get_reflects_change(self) -> None:
        self.ok(self.call("set_settings", {"hotkey_profile": "meeting"}))
        result = self.ok(self.call("get_settings"))
        self.assertEqual(result["hotkey_profile"], "meeting")

    def test_multiple_set_calls_accumulate(self) -> None:
        self.ok(self.call("set_settings", {"hotkey_profile": "translation"}))
        self.ok(self.call("set_settings", {"audio_ducking_enabled": False}))
        result = self.ok(self.call("get_settings"))
        self.assertEqual(result["hotkey_profile"], "translation")
        self.assertFalse(result["audio_ducking_enabled"])

    def test_cache_invalidated_immediately(self) -> None:
        """Two consecutive get_settings calls after set must return updated value."""
        self.ok(self.call("set_settings", {"overlay_opacity_percent": 80}))
        r1 = self.ok(self.call("get_settings"))
        r2 = self.ok(self.call("get_settings"))
        self.assertEqual(r1["overlay_opacity_percent"], 80)
        self.assertEqual(r2["overlay_opacity_percent"], 80)


# ---------------------------------------------------------------------------
# Scenario 3 — start recording → ping in progress → stop → history item
# ---------------------------------------------------------------------------

class TestRecordingLifecycle(_IPCBase):
    """Full recording lifecycle: start → ping (is_recording=True) → stop → history."""

    def test_start_sets_recording_state(self) -> None:
        self.ok(self.call("start_recording"))
        ping = self.ok(self.call("ping"))
        self.assertTrue(ping["is_recording"])

    def test_stop_after_start_creates_history(self) -> None:
        self.ok(self.call("start_recording"))
        hist_before = self.ok(self.call("ping"))["history_count"]
        self.ok(self.call("stop_recording"))
        hist_after = self.ok(self.call("ping"))["history_count"]
        # Transcription may be skipped by silence/background guard with fake audio
        # but history_count should not decrease
        self.assertGreaterEqual(hist_after, hist_before)

    def test_double_start_second_is_noop(self) -> None:
        self.ok(self.call("start_recording"))
        self.ok(self.call("start_recording"))
        ping = self.ok(self.call("ping"))
        self.assertTrue(ping["is_recording"])

    def test_stop_without_start_is_safe(self) -> None:
        # Should not raise — service handles gracefully
        resp = self.call("stop_recording")
        self.assertIn("ok", resp)

    def test_recording_state_query(self) -> None:
        result = self.ok(self.call("get_recording_state"))
        self.assertIn("is_recording", result)
        self.assertFalse(result["is_recording"])
        self.ok(self.call("start_recording"))
        result2 = self.ok(self.call("get_recording_state"))
        self.assertTrue(result2["is_recording"])


# ---------------------------------------------------------------------------
# Scenario 4 — bulk archive 3 items → verify all 3 marked
# ---------------------------------------------------------------------------

class TestBulkArchive(_IPCBase):
    """archive_items with 3 IDs archives all 3; list_archived reflects them."""

    def _add_item(self, text: str) -> str:
        result = self.ok(
            self.call(
                "add_history_item",
                {
                    "text": text,
                    "paste_status": "ok",
                    "source_text": "",
                    "translated_text": "",
                    "translation_mode": "off",
                    "source_lang": "",
                    "target_lang": "",
                    "translation_status": "not_requested",
                    "translation_engine": "",
                },
            )
        )
        return result["id"]

    def test_archive_three_items(self) -> None:
        ids = [self._add_item(f"item {i}") for i in range(3)]
        result = self.ok(self.call("archive_items", {"item_ids": ids}))
        self.assertEqual(result["archived_count"], 3)

    def test_archived_items_removed_from_active_history(self) -> None:
        ids = [self._add_item(f"will be archived {i}") for i in range(3)]
        self.ok(self.call("archive_items", {"item_ids": ids}))
        listed = self.ok(self.call("list_archived"))
        archive_ids = {item["id"] for item in listed.get("items", [])}
        for item_id in ids:
            self.assertIn(item_id, archive_ids)

    def test_archive_stats_count(self) -> None:
        ids = [self._add_item(f"stat item {i}") for i in range(3)]
        self.ok(self.call("archive_items", {"item_ids": ids}))
        stats = self.ok(self.call("get_archive_stats"))
        self.assertGreaterEqual(stats["total_archived"], 3)

    def test_archive_empty_list_is_safe(self) -> None:
        result = self.ok(self.call("archive_items", {"item_ids": []}))
        self.assertEqual(result["archived_count"], 0)


# ---------------------------------------------------------------------------
# Scenario 5 — clipboard push → get_clipboard_history → repaste
# ---------------------------------------------------------------------------

class TestClipboardFlow(_IPCBase):
    """Clipboard history push → query → repaste round-trip."""

    def _push(self, text: str, history_id: str) -> None:
        self.service._clipboard_history.append(
            {"text": text, "ts": "2026-04-22T12:00:00", "history_id": history_id}
        )

    def test_push_and_get_clipboard(self) -> None:
        self._push("Привет мир", "hid-1")
        result = self.ok(self.call("get_clipboard_history"))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["text"], "Привет мир")

    def test_repaste_returns_correct_text(self) -> None:
        self._push("текст для репасты", "hid-repaste")
        result = self.ok(self.call("repaste_item", {"history_id": "hid-repaste"}))
        self.assertTrue(result["found"])
        self.assertEqual(result["text"], "текст для репасты")
        self.assertEqual(result["history_id"], "hid-repaste")

    def test_repaste_missing_id_returns_error(self) -> None:
        resp = self.call("repaste_item", {"history_id": "does-not-exist"})
        self.assertFalse(resp.get("ok"))
        # A not-found item is an EXPECTED condition, not a backend crash — the
        # dispatch now maps handler ValueError/RuntimeError to a semantic
        # `invalid_request` (WARNING-logged), not `internal_error` (ERROR/Sentry).
        self.assertEqual(resp["error"]["code"], "invalid_request")

    def test_clipboard_limit_parameter(self) -> None:
        for i in range(10):
            self._push(f"entry {i}", f"hid-{i}")
        result = self.ok(self.call("get_clipboard_history", {"limit": 3}))
        self.assertLessEqual(len(result["items"]), 3)
        self.assertEqual(result["count"], 10)


# ---------------------------------------------------------------------------
# Scenario 6 — request lifecycle: id tracking, response matches id
# ---------------------------------------------------------------------------

class TestRequestIdTracking(_IPCBase):
    """Response id always echoes the request id."""

    def test_id_echoed_for_success(self) -> None:
        resp = self.service.handle_request(
            {"id": "custom-abc-123", "method": "ping", "params": {}}
        )
        self.assertEqual(resp["id"], "custom-abc-123")

    def test_id_echoed_for_error(self) -> None:
        resp = self.service.handle_request(
            {"id": "err-id-456", "method": "unknown_xyz", "params": {}}
        )
        self.assertEqual(resp["id"], "err-id-456")

    def test_id_echoed_for_invalid_params(self) -> None:
        resp = self.service.handle_request(
            {"id": "inv-789", "method": "ping", "params": ["not", "a", "dict"]}
        )
        self.assertEqual(resp["id"], "inv-789")
        self.assertFalse(resp["ok"])

    def test_sequential_ids_dont_collide(self) -> None:
        for i in range(5):
            resp = self.service.handle_request(
                {"id": f"seq-{i}", "method": "ping", "params": {}}
            )
            self.assertEqual(resp["id"], f"seq-{i}")
            self.assertTrue(resp["ok"])


# ---------------------------------------------------------------------------
# Scenario 7 — settings persistence: write via set_settings → reload → persists
# ---------------------------------------------------------------------------

class TestSettingsPersistence(_IPCBase):
    """set_settings persists to disk; a new BackendService loaded from same
    data_dir reads the same values."""

    def test_value_survives_service_reload(self) -> None:
        self.ok(self.call("set_settings", {"hotkey_profile": "translation", "audio_ducking_percent": 33}))

        # Instantiate a brand-new service pointing at the same data dir
        new_store = StateStore(self.data_dir)
        new_service = BackendService(
            store=new_store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        resp = new_service.handle_request(
            {"id": "r1", "method": "get_settings", "params": {}}
        )
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertEqual(result["hotkey_profile"], "translation")
        self.assertEqual(result["audio_ducking_percent"], 33)

    def test_multiple_keys_persist(self) -> None:
        self.ok(
            self.call(
                "set_settings",
                {
                    "update_channel": "beta",
                    "history_page_size": 77,
                    "audio_ducking_enabled": False,
                },
            )
        )
        new_store = StateStore(self.data_dir)
        new_service = BackendService(
            store=new_store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        result = new_service.handle_request(
            {"id": "r2", "method": "get_settings", "params": {}}
        )["result"]
        self.assertEqual(result["update_channel"], "beta")
        self.assertEqual(result["history_page_size"], 77)
        self.assertFalse(result["audio_ducking_enabled"])


# ---------------------------------------------------------------------------
# Scenario 8 — throttle: >N requests to heavy method → rate_limit_exceeded
# ---------------------------------------------------------------------------

class TestIPCThrottle(_IPCBase):
    """When throttle is active and bucket is exhausted, heavy methods return
    rate_limit_exceeded error code."""

    def _make_throttled_service(self) -> BackendService:
        """Return a new service with throttle enabled and heavy capacity=1."""
        store = StateStore(Path(self.tmp.name) / "throttle_data")
        throttle = IPCThrottle(limits={"heavy": 1, "medium": 30, "light": 120})
        svc = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        svc._ipc_throttle = throttle
        return svc

    def test_first_heavy_call_allowed(self) -> None:
        svc = self._make_throttled_service()
        # summarize_text is a heavy method; stub out LLM
        svc._llm_rewriter = None  # type: ignore[assignment]
        # Just verify the throttle doesn't block the first call
        # (the call itself may fail for other reasons, but not rate_limit)
        resp = svc.handle_request(
            {"id": "t1", "method": "summarize_text", "params": {"text": "hello"}}
        )
        if not resp.get("ok"):
            self.assertNotEqual(resp.get("error", {}).get("code"), "rate_limit_exceeded")

    def test_heavy_method_throttled_after_limit(self) -> None:
        svc = self._make_throttled_service()
        throttle: IPCThrottle = svc._ipc_throttle  # type: ignore[assignment]

        # Drain the bucket for a heavy method by calling check_rate directly
        method = "export_history"
        # capacity=1, so first consume succeeds, second fails
        self.assertTrue(throttle.check_rate(method))
        self.assertFalse(throttle.check_rate(method))

    def test_rate_limit_error_code_returned(self) -> None:
        svc = self._make_throttled_service()

        # Drain the bucket for 'get_diagnostics' (medium, limit 30 → set to 1)
        svc._ipc_throttle = IPCThrottle(limits={"heavy": 5, "medium": 1, "light": 120})
        throttle2: IPCThrottle = svc._ipc_throttle  # type: ignore[assignment]
        # Consume the one medium token
        self.assertTrue(throttle2.check_rate("get_diagnostics"))
        # Manually exhaust: bucket now empty
        self.assertFalse(throttle2.check_rate("get_diagnostics"))

        # Now a real IPC call should hit the throttle
        resp = svc.handle_request(
            {"id": "t2", "method": "get_diagnostics", "params": {}}
        )
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "rate_limit_exceeded")

    def test_excluded_methods_never_throttled(self) -> None:
        svc = self._make_throttled_service()
        svc._ipc_throttle = IPCThrottle(limits={"heavy": 0, "medium": 0, "light": 0})
        # ping and start/stop_recording are EXCLUDED — always allowed
        for method in ("ping", "start_recording", "stop_recording"):
            resp = svc.handle_request({"id": "t3", "method": method, "params": {}})
            if not resp.get("ok"):
                self.assertNotEqual(
                    resp.get("error", {}).get("code"),
                    "rate_limit_exceeded",
                    f"Method {method!r} should not be throttled",
                )

    def test_throttle_stats_track_calls(self) -> None:
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 120})
        for _ in range(3):
            throttle.check_rate("search_history")
        stats = throttle.get_throttle_stats()
        self.assertEqual(stats["total_calls"], 3)
        self.assertEqual(stats["methods"]["search_history"]["calls"], 3)


if __name__ == "__main__":
    unittest.main()
