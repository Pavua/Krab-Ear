"""Gap-filling tests for BackendService edge-cases not covered elsewhere.

Covers:
- _build_empty_audio_response — all reason combinations
- Handler dispatch: unknown method → error response
- Settings cache invalidation
- handle_ping returns healthy status
- Startup sequence: minimal init completes without crashes
- params non-dict guard
- build_service factory
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService, build_service  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes (keep in-file to avoid import coupling)
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
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        return (0.06 * np.sin(2.0 * np.pi * 210.0 * t)).astype(np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(32000, dtype=np.float32), float(self._snapshot_counter)


class _FakeTranscriber:
    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile="balanced", cleanup_profile="soft",
                   domain="casual", extra_vocabulary=None, lang_hint=None) -> str:
        self.counter += 1
        return f"text#{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced") -> str:
        return f"preview#{self.counter}"


class _FakeTranslator:
    def translate(self, text, mode, network_mode, translation_style="neutral",
                  glossary=None) -> TranslationResult:
        return TranslationResult(
            text="", status="not_requested",
            source_lang="", target_lang="", mode="off", engine="fake",
        )


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

class _ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        store = StateStore(self.data_dir)
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def request(self, method: str, params=None, request_id="t1"):
        return self.service.handle_request(
            {"id": request_id, "method": method, "params": params or {}}
        )


# ---------------------------------------------------------------------------
# 1. _build_empty_audio_response — all reason combinations
# ---------------------------------------------------------------------------

class TestBuildEmptyAudioResponse(_ServiceFixture):
    """_build_empty_audio_response covers all silence/background guard reasons."""

    def _call(self, **kwargs):
        defaults = dict(
            duration_sec=1.5,
            quality_profile="balanced",
            cleanup_profile="soft",
            translation_mode="off",
            translate_and_paste=False,
            stop_tail_trim_ms=180,
        )
        defaults.update(kwargs)
        return self.service._build_empty_audio_response(**defaults)

    def test_schema_base_fields(self):
        resp = self._call()
        self.assertEqual(resp["status"], "empty_audio")
        self.assertEqual(resp["text"], "")
        self.assertEqual(resp["original_text"], "")
        self.assertEqual(resp["translated_text"], "")
        self.assertEqual(resp["translation_status"], "not_requested")
        self.assertIsNone(resp["history_id"])

    def test_silence_detected_reason(self):
        resp = self._call(silence_detected=True, silence_guard_enabled=True)
        self.assertTrue(resp["silence_detected"])
        self.assertTrue(resp["silence_guard_enabled"])
        self.assertFalse(resp["background_guard_rejected"])

    def test_background_guard_reason(self):
        resp = self._call(background_guard_rejected=True)
        self.assertTrue(resp["background_guard_rejected"])
        self.assertFalse(resp["silence_detected"])

    def test_no_guard_triggered(self):
        resp = self._call()
        self.assertFalse(resp["silence_detected"])
        self.assertFalse(resp["background_guard_rejected"])
        self.assertFalse(resp["silence_guard_enabled"])

    def test_duration_and_profile_propagated(self):
        resp = self._call(
            duration_sec=4.2,
            quality_profile="max",
            cleanup_profile="strict",
            translation_mode="ru_to_es",
            translate_and_paste=True,
            stop_tail_trim_ms=300,
        )
        self.assertAlmostEqual(resp["duration_sec"], 4.2)
        self.assertEqual(resp["quality_profile"], "max")
        self.assertEqual(resp["cleanup_profile"], "strict")
        self.assertEqual(resp["translation_mode"], "ru_to_es")
        self.assertTrue(resp["translate_and_paste"])
        self.assertEqual(resp["stop_tail_trim_ms"], 300)

    def test_all_three_reasons_explicit_false(self):
        """All guard flags explicitly False — all should be False in output."""
        resp = self._call(
            silence_detected=False,
            silence_guard_enabled=False,
            background_guard_rejected=False,
        )
        self.assertFalse(resp["silence_detected"])
        self.assertFalse(resp["silence_guard_enabled"])
        self.assertFalse(resp["background_guard_rejected"])


# ---------------------------------------------------------------------------
# 2. Handler dispatch — unknown method → error
# ---------------------------------------------------------------------------

class TestHandlerDispatch(_ServiceFixture):
    """Dispatch table: unknown method returns structured error, not an exception."""

    def test_unknown_method_returns_error(self):
        resp = self.request("not_a_real_method_xyz")
        self.assertFalse(resp.get("ok", True))
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], "unknown_method")

    def test_unknown_method_preserves_request_id(self):
        resp = self.service.handle_request(
            {"id": "my-id-42", "method": "ghost_method", "params": {}}
        )
        self.assertEqual(resp["id"], "my-id-42")
        self.assertFalse(resp.get("ok", True))

    def test_empty_method_returns_error(self):
        resp = self.request("")
        self.assertFalse(resp.get("ok", True))

    def test_params_not_dict_returns_error(self):
        resp = self.service.handle_request(
            {"id": "x", "method": "ping", "params": ["list", "not", "dict"]}
        )
        self.assertFalse(resp.get("ok", True))
        self.assertEqual(resp["error"]["code"], "invalid_params")

    def test_known_method_returns_ok(self):
        resp = self.request("ping")
        self.assertTrue(resp.get("ok"))


# ---------------------------------------------------------------------------
# 3. Settings cache invalidation
# ---------------------------------------------------------------------------

class TestSettingsCacheInvalidation(_ServiceFixture):
    """Cache is refreshed after invalidate_cache() call."""

    def test_cache_populated_after_get_settings(self):
        self.request("get_settings")
        # Internal cache should be populated
        self.assertIsNotNone(self.service._settings_svc._cache)

    def test_invalidate_clears_cache(self):
        self.request("get_settings")
        self.service._invalidate_settings_cache()
        self.assertIsNone(self.service._settings_svc._cache)

    def test_cache_serves_updated_value_after_set(self):
        self.request("set_settings", {"quality_profile": "max"})
        resp = self.request("get_settings")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["quality_profile"], "max")

    def test_cache_ttl_respected(self):
        """Within TTL, a second read does not reset _cache_ts."""
        self.request("get_settings")
        ts1 = self.service._settings_svc._cache_ts
        self.request("get_settings")
        ts2 = self.service._settings_svc._cache_ts
        # ts should not change within TTL window (both reads within milliseconds)
        self.assertAlmostEqual(ts1, ts2, places=2)

    def test_cache_refreshes_after_ttl(self):
        """After TTL expires, cache_ts is updated on next read."""
        self.request("get_settings")
        # Force expiry
        self.service._settings_svc._cache_ts = time.monotonic() - 10.0
        ts_before = self.service._settings_svc._cache_ts
        self.request("get_settings")
        ts_after = self.service._settings_svc._cache_ts
        self.assertGreater(ts_after, ts_before)


# ---------------------------------------------------------------------------
# 4. handle_ping returns healthy status
# ---------------------------------------------------------------------------

class TestHandlePing(_ServiceFixture):
    """_handle_ping / ping IPC method returns healthy service status."""

    def test_ping_ok_status(self):
        resp = self.request("ping")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["status"], "ok")

    def test_ping_service_name(self):
        resp = self.request("ping")
        self.assertEqual(resp["result"]["service"], "krabear-backend")

    def test_ping_version_string(self):
        from KrabEar.__version__ import __version__ as APP_VERSION
        resp = self.request("ping")
        self.assertEqual(resp["result"]["version"], APP_VERSION)

    def test_ping_uptime_non_negative(self):
        resp = self.request("ping")
        self.assertGreaterEqual(resp["result"]["uptime_sec"], 0)

    def test_ping_is_recording_false_initially(self):
        resp = self.request("ping")
        self.assertFalse(resp["result"]["is_recording"])

    def test_ping_is_recording_true_after_start(self):
        self.request("start_recording")
        resp = self.request("ping")
        self.assertTrue(resp["result"]["is_recording"])

    def test_ping_history_count_non_negative(self):
        resp = self.request("ping")
        self.assertGreaterEqual(resp["result"]["history_count"], 0)


# ---------------------------------------------------------------------------
# 5. Startup sequence — build_service factory completes without crash
# ---------------------------------------------------------------------------

class TestStartupSequence(unittest.TestCase):
    """build_service() initializes without side-effects or crashes."""

    def test_build_service_creates_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            svc = build_service(data_dir)
            self.assertIsInstance(svc, BackendService)

    def test_build_service_initializes_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            svc = build_service(data_dir)
            self.assertIsNotNone(svc.store)

    def test_build_service_can_serve_ping(self):
        """After build_service, a ping IPC call should succeed immediately."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            svc = build_service(data_dir)
            resp = svc.handle_request({"id": "init-ping", "method": "ping", "params": {}})
            self.assertTrue(resp.get("ok"))
            self.assertEqual(resp["result"]["status"], "ok")

    def test_build_service_creates_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            build_service(data_dir)
            settings_path = data_dir / "settings.json"
            self.assertTrue(settings_path.exists(), "settings.json must be created on init")

    def test_minimal_init_no_recorder_no_transcriber(self):
        """BackendService with only a StateStore (no recorder/transcriber) must not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "data")
            svc = BackendService(store=store)
            self.assertIsNotNone(svc)
            # ping should still work (recorder is created internally)
            resp = svc.handle_request({"id": "1", "method": "ping", "params": {}})
            self.assertTrue(resp.get("ok"))


if __name__ == "__main__":
    unittest.main()
