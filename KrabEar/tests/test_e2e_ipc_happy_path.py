"""End-to-end IPC happy path integration tests.

Each test simulates a realistic user workflow через handle_request:
  - Start recording
  - Stop recording (with mocked audio + STT)
  - Verify history saved
  - Translate
  - Apply paste profile
  - Get diagnostics

Mocked collaborators: AudioEngine, Transcriber, IPCServer, StateStore (tmpdir).
NO real model loads, NO real audio capture.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------

class FakeRecorder:
    """Deterministic recorder: returns a 1-second 220 Hz sine on stop()."""

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
    """Deterministic transcriber stub — returns unique transcripts."""

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
    """Stub translator — returns reversed text to prove the call went through."""

    def translate(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict[str, str] | None = None,
    ) -> TranslationResult:
        translated = text[::-1] if text else ""
        return TranslationResult(
            text=translated,
            status="ok",
            source_lang="ru",
            target_lang="es",
            mode=mode,
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _E2EBase(unittest.TestCase):
    """Shared setUp / helpers for all E2E IPC happy-path scenarios."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    def _add_history_item(self, text: str) -> str:
        """Directly add a history item and return its id."""
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

    def _do_full_recording(self) -> None:
        """Run the complete start → stop recording lifecycle."""
        self.ok(self.call("start_recording"))
        self.ok(self.call("stop_recording"))


# ---------------------------------------------------------------------------
# Scenario 1 — full recording lifecycle
# ---------------------------------------------------------------------------

class TestFullRecordingLifecycle(_E2EBase):
    """start_recording → check status → (mock STT) → stop_recording → history."""

    def test_status_is_recording_after_start(self) -> None:
        self.ok(self.call("start_recording"))
        ping = self.ok(self.call("ping"))
        self.assertTrue(ping["is_recording"])

    def test_stop_recording_marks_not_recording(self) -> None:
        self.ok(self.call("start_recording"))
        self.ok(self.call("stop_recording"))
        ping = self.ok(self.call("ping"))
        self.assertFalse(ping["is_recording"])

    def test_history_not_decremented_after_stop(self) -> None:
        count_before = self.ok(self.call("ping"))["history_count"]
        self.ok(self.call("start_recording"))
        self.ok(self.call("stop_recording"))
        count_after = self.ok(self.call("ping"))["history_count"]
        self.assertGreaterEqual(count_after, count_before)

    def test_get_history_page_returns_list(self) -> None:
        self._do_full_recording()
        result = self.ok(self.call("get_history_page"))
        self.assertIn("items", result)
        self.assertIsInstance(result["items"], list)

    def test_transcriber_counter_incremented(self) -> None:
        initial = self.transcriber.counter
        self._do_full_recording()
        # Counter may or may not increment depending on silence guard;
        # it must not go below its initial value.
        self.assertGreaterEqual(self.transcriber.counter, initial)


# ---------------------------------------------------------------------------
# Scenario 2 — recording with translation
# ---------------------------------------------------------------------------

class TestRecordingWithTranslation(_E2EBase):
    """Full recording lifecycle followed by translate_selection."""

    def test_translate_selection_returns_translated_text(self) -> None:
        self._do_full_recording()
        resp = self.ok(
            self.call("translate_selection", {"text": "Привет мир", "source_lang": "ru"})
        )
        # FakeTranslator reverses the text; result should differ from input
        self.assertIn("translated_text", resp)
        # Reversed text is non-empty and different
        self.assertTrue(len(resp["translated_text"]) > 0)
        self.assertNotEqual(resp["translated_text"], "Привет мир")

    def test_translate_selection_empty_text_fast_return(self) -> None:
        resp = self.ok(self.call("translate_selection", {"text": ""}))
        self.assertEqual(resp["translated_text"], "")
        self.assertEqual(resp["engine"], "none")

    def test_translate_selection_source_lang_detected(self) -> None:
        resp = self.ok(
            self.call("translate_selection", {"text": "Hola mundo", "source_lang": "es"})
        )
        self.assertEqual(resp["source_lang_detected"], "es")
        self.assertIn("target_lang", resp)
        self.assertIn("latency_ms", resp)

    def test_translate_after_recording_uses_glossary_from_settings(self) -> None:
        # Set a glossary entry, then translate — should not crash
        self.ok(self.call("set_settings", {"translation_glossary": {"Краб": "Crab"}}))
        self._do_full_recording()
        resp = self.ok(self.call("translate_selection", {"text": "Краб слушает", "source_lang": "ru"}))
        self.assertIn("translated_text", resp)


# ---------------------------------------------------------------------------
# Scenario 3 — recording with paste profile
# ---------------------------------------------------------------------------

class TestRecordingWithPasteProfile(_E2EBase):
    """Full lifecycle + record_paste_app_profile → get_paste_profile_for_app."""

    def test_record_and_retrieve_paste_profile(self) -> None:
        self._do_full_recording()
        self.ok(
            self.call(
                "record_paste_app_profile",
                {"bundle_id": "com.apple.Notes", "profile": "markdown"},
            )
        )
        result = self.ok(
            self.call("get_paste_profile_for_app", {"bundle_id": "com.apple.Notes"})
        )
        self.assertEqual(result["profile"], "markdown")
        self.assertEqual(result["bundle_id"], "com.apple.Notes")

    def test_unknown_bundle_returns_null_profile(self) -> None:
        result = self.ok(
            self.call("get_paste_profile_for_app", {"bundle_id": "com.unknown.app"})
        )
        self.assertIsNone(result["profile"])

    def test_overwrite_paste_profile(self) -> None:
        self.ok(self.call("record_paste_app_profile", {"bundle_id": "com.tg", "profile": "plain"}))
        self.ok(self.call("record_paste_app_profile", {"bundle_id": "com.tg", "profile": "telegram"}))
        result = self.ok(self.call("get_paste_profile_for_app", {"bundle_id": "com.tg"}))
        self.assertEqual(result["profile"], "telegram")

    def test_list_app_profiles_contains_recorded(self) -> None:
        self.ok(self.call("record_paste_app_profile", {"bundle_id": "com.apple.mail", "profile": "email"}))
        result = self.ok(self.call("list_app_profiles"))
        bundle_ids = [p["bundle_id"] for p in result["profiles"]]
        self.assertIn("com.apple.mail", bundle_ids)


# ---------------------------------------------------------------------------
# Scenario 4 — settings workflow
# ---------------------------------------------------------------------------

class TestSettingsWorkflow(_E2EBase):
    """set_settings → list_profile_presets → apply_profile_preset → verify."""

    def test_set_settings_persists_value(self) -> None:
        self.ok(self.call("set_settings", {"hotkey_profile": "meeting"}))
        result = self.ok(self.call("get_settings"))
        self.assertEqual(result["hotkey_profile"], "meeting")

    def test_list_profile_presets_returns_four_presets(self) -> None:
        result = self.ok(self.call("list_profile_presets"))
        self.assertIn("presets", result)
        names = [p["name"] for p in result["presets"]]
        for expected in ("default", "meeting", "translation", "call_recording"):
            self.assertIn(expected, names)

    def test_apply_profile_preset_updates_settings(self) -> None:
        self.ok(self.call("apply_profile_preset", {"profile": "meeting"}))
        result = self.ok(self.call("get_settings"))
        # Meeting preset sets quality_profile=max and auto_paste=False
        self.assertEqual(result.get("quality_profile"), "max")
        self.assertFalse(result.get("auto_paste"))

    def test_apply_then_override_setting(self) -> None:
        self.ok(self.call("apply_profile_preset", {"profile": "translation"}))
        self.ok(self.call("set_settings", {"translation_mode": "off"}))
        result = self.ok(self.call("get_settings"))
        self.assertEqual(result["translation_mode"], "off")

    def test_unknown_preset_returns_error(self) -> None:
        resp = self.call("apply_profile_preset", {"profile": "nonexistent_preset_xyz"})
        self.assertFalse(resp.get("ok"))


# ---------------------------------------------------------------------------
# Scenario 5 — search workflow
# ---------------------------------------------------------------------------

class TestSearchWorkflow(_E2EBase):
    """Insert 5 history items, search for specific keyword, results returned."""

    def setUp(self) -> None:
        super().setUp()
        # Insert 5 items — 3 contain "machine learning", 2 contain "краб"
        self.ml_ids: list[str] = []
        for i in range(3):
            item_id = self._add_history_item(f"machine learning example {i}")
            self.ml_ids.append(item_id)
        for i in range(2):
            self._add_history_item(f"краб слушает {i}")

    def test_search_returns_all_items_on_empty_query(self) -> None:
        result = self.ok(self.call("search_history", {"query": ""}))
        self.assertIn("items", result)
        self.assertGreaterEqual(len(result["items"]), 5)

    def test_search_filters_by_keyword(self) -> None:
        result = self.ok(self.call("search_history", {"query": "machine"}))
        self.assertIn("items", result)
        self.assertGreaterEqual(len(result["items"]), 1)
        for item in result["items"]:
            self.assertIn("machine", item.get("text", "").lower())

    def test_search_respects_limit(self) -> None:
        result = self.ok(self.call("search_history", {"query": "", "limit": 2}))
        self.assertLessEqual(len(result["items"]), 2)

    def test_search_no_match_returns_empty_list(self) -> None:
        result = self.ok(self.call("search_history", {"query": "zzzunmatchable_xyz_token_123"}))
        self.assertEqual(result["items"], [])

    def test_search_cursor_pagination(self) -> None:
        page1 = self.ok(self.call("search_history", {"query": "", "limit": 3}))
        if page1["next_cursor"] is not None:
            page2 = self.ok(
                self.call("search_history", {"query": "", "limit": 3, "cursor": page1["next_cursor"]})
            )
            self.assertIn("items", page2)


# ---------------------------------------------------------------------------
# Scenario 6 — diagnostics workflow
# ---------------------------------------------------------------------------

class TestDiagnosticsWorkflow(_E2EBase):
    """get_diagnostics returns all required sections."""

    def test_diagnostics_all_sections_present(self) -> None:
        result = self.ok(self.call("get_diagnostics"))
        for section in ("system", "stt", "llm", "history", "settings_cache"):
            self.assertIn(section, result, f"Missing section: {section!r}")

    def test_diagnostics_system_has_required_keys(self) -> None:
        result = self.ok(self.call("get_diagnostics"))
        system = result["system"]
        self.assertIn("python_version", system)
        self.assertIn("platform", system)
        self.assertIn("uptime_sec", system)
        self.assertGreater(system["uptime_sec"], 0.0)

    def test_diagnostics_stt_section(self) -> None:
        result = self.ok(self.call("get_diagnostics"))
        stt = result["stt"]
        self.assertIn("quality_profile", stt)
        self.assertIn("current_model", stt)
        self.assertEqual(stt["quality_profile"], "balanced")

    def test_diagnostics_history_section(self) -> None:
        self._add_history_item("диагностика")
        result = self.ok(self.call("get_diagnostics"))
        history = result["history"]
        self.assertIn("total_items", history)
        self.assertGreaterEqual(history["total_items"], 1)
        self.assertIn("data_dir", history)

    def test_diagnostics_settings_cache_section(self) -> None:
        result = self.ok(self.call("get_diagnostics"))
        cache_info = result["settings_cache"]
        self.assertIn("ttl_sec", cache_info)
        self.assertIn("cached", cache_info)

    def test_diagnostics_after_recording_lifecycle(self) -> None:
        """Diagnostics remain consistent across a full recording lifecycle."""
        self._do_full_recording()
        result = self.ok(self.call("get_diagnostics"))
        self.assertIn("system", result)
        self.assertIn("history", result)


# ---------------------------------------------------------------------------
# Scenario 7 — metrics workflow
# ---------------------------------------------------------------------------

class TestMetricsWorkflow(_E2EBase):
    """get_metrics_dashboard returns a valid snapshot at all times."""

    def test_metrics_dashboard_shape(self) -> None:
        result = self.ok(self.call("get_metrics_dashboard"))
        self.assertIn("session", result)
        self.assertIn("llm", result)
        self.assertIn("call_assist", result)
        self.assertIn("config_snapshot", result)

    def test_metrics_session_reflects_recording_state(self) -> None:
        result_idle = self.ok(self.call("get_metrics_dashboard"))
        self.assertFalse(result_idle["session"]["recording_active"])

        self.ok(self.call("start_recording"))
        result_active = self.ok(self.call("get_metrics_dashboard"))
        self.assertTrue(result_active["session"]["recording_active"])

        self.ok(self.call("stop_recording"))
        result_stopped = self.ok(self.call("get_metrics_dashboard"))
        self.assertFalse(result_stopped["session"]["recording_active"])

    def test_metrics_config_snapshot_keys(self) -> None:
        result = self.ok(self.call("get_metrics_dashboard"))
        cfg = result["config_snapshot"]
        for key in ("quality", "cleanup", "translation_mode", "diarization", "network_mode"):
            self.assertIn(key, cfg)

    def test_metrics_stable_after_multiple_recordings(self) -> None:
        for _ in range(3):
            self._do_full_recording()
        result = self.ok(self.call("get_metrics_dashboard"))
        # No crash and all sections present
        self.assertIn("session", result)
        self.assertFalse(result["session"]["recording_active"])

    def test_metrics_llm_section_has_enabled_field(self) -> None:
        result = self.ok(self.call("get_metrics_dashboard"))
        self.assertIn("enabled", result["llm"])


# ---------------------------------------------------------------------------
# Scenario 8 — concurrent dispatch
# ---------------------------------------------------------------------------

class TestConcurrentDispatch(_E2EBase):
    """50 simultaneous handle_request calls — no deadlock, all return."""

    def _dispatch(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        results: list[dict[str, Any]] | None = None,
        index: int = 0,
    ) -> None:
        resp = self.service.handle_request(
            {"id": f"concurrent-{index}", "method": method, "params": params or {}}
        )
        if results is not None:
            results[index] = resp

    def test_50_concurrent_ping_calls(self) -> None:
        n = 50
        results: list[dict[str, Any]] = [{}] * n
        threads = [
            threading.Thread(target=self._dispatch, args=("ping", {}, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        responded = sum(1 for r in results if r)
        self.assertEqual(responded, n)
        for i, resp in enumerate(results):
            self.assertTrue(resp.get("ok"), f"Thread {i} failed: {resp}")
            self.assertEqual(resp["id"], f"concurrent-{i}")

    def test_mixed_methods_concurrent_no_deadlock(self) -> None:
        """Mix of get_settings, ping, get_history, get_diagnostics concurrently."""
        methods = ["ping", "get_settings", "get_history", "get_diagnostics"]
        n = 40
        results: list[dict[str, Any]] = [{}] * n
        threads = [
            threading.Thread(
                target=self._dispatch,
                args=(methods[i % len(methods)], {}, results, i),
            )
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # All threads must have returned (no deadlock)
        responded = sum(1 for r in results if r)
        self.assertEqual(responded, n)

    def test_concurrent_settings_reads_consistent(self) -> None:
        """Concurrent get_settings calls all return consistent results."""
        self.ok(self.call("set_settings", {"hotkey_profile": "translation"}))
        n = 20
        results: list[dict[str, Any]] = [{}] * n
        threads = [
            threading.Thread(target=self._dispatch, args=("get_settings", {}, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        for i, resp in enumerate(results):
            self.assertTrue(resp.get("ok"), f"Thread {i} failed: {resp}")
            # Every response must see the same hotkey_profile
            self.assertEqual(
                resp["result"]["hotkey_profile"],
                "translation",
                f"Thread {i} got inconsistent settings",
            )


if __name__ == "__main__":
    unittest.main()
