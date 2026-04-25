"""Edge-case handler tests for BackendService: diagnostics, audio devices,
clipboard history, storage info, and profile presets."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes (minimal, no numpy dependency beyond what test_backend_service uses)
# ---------------------------------------------------------------------------

class FakeRecorder:
    is_recording = False
    sample_rate = 16000
    last_stop_trim_ms = 0
    last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        import numpy as np
        return np.zeros(16000, dtype="float32"), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.ones(32000, dtype="float32"), 1.0


class FakeEngine:
    """Minimal engine stub required by _handle_get_diagnostics."""
    quality_profile: str = "balanced"
    current_model: str = "fake-model"

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class FakeTranscriber:
    counter = 0

    def __init__(self) -> None:
        self.engine = FakeEngine()

    def transcribe(self, audio_data: Any, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary: Any = None, lang_hint: Any = None,
                   history_context: Any = None, stt_hotwords: Any = None) -> str:
        self.counter += 1
        return f"fake text #{self.counter}"

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> str:
        return f"preview ({quality_profile})"


class FakeTranslator:
    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary: dict[str, str] | None = None) -> TranslationResult:
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

class _BaseHandlersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.service.handle_request(
            {"id": "t1", "method": method, "params": params or {}}
        )


# ---------------------------------------------------------------------------
# 1. handle_get_diagnostics
# ---------------------------------------------------------------------------

class TestGetDiagnostics(_BaseHandlersTest):
    """get_diagnostics returns the required top-level sections."""

    def test_returns_all_required_sections(self) -> None:
        resp = self.request("get_diagnostics")
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        for section in ("system", "stt", "llm", "history", "settings_cache"):
            self.assertIn(section, result, f"Section '{section}' missing")

    def test_system_section_fields(self) -> None:
        result = self.request("get_diagnostics")["result"]
        sys_sec = result["system"]
        self.assertIn("python_version", sys_sec)
        self.assertIn("platform", sys_sec)
        self.assertGreaterEqual(sys_sec["uptime_sec"], 0.0)

    def test_stt_section_fields(self) -> None:
        result = self.request("get_diagnostics")["result"]
        stt = result["stt"]
        self.assertIn("model_balanced", stt)
        self.assertIn("model_max", stt)
        self.assertIn("diarization_enabled", stt)

    def test_history_section_has_data_dir(self) -> None:
        result = self.request("get_diagnostics")["result"]
        hist = result["history"]
        self.assertIn("data_dir", hist)
        self.assertIn("transcripts_dir", hist)
        self.assertGreaterEqual(hist["total_items"], 0)

    def test_settings_cache_section(self) -> None:
        result = self.request("get_diagnostics")["result"]
        cache = result["settings_cache"]
        self.assertIn("ttl_sec", cache)
        self.assertIn("cached", cache)

    def test_llm_section_present(self) -> None:
        result = self.request("get_diagnostics")["result"]
        self.assertIn("llm", result)
        # May be {"enabled": False} if no LLM rewriter configured in test env.
        self.assertIsInstance(result["llm"], dict)


# ---------------------------------------------------------------------------
# 2. handle_get_audio_devices / list_audio_inputs — mock sounddevice
# ---------------------------------------------------------------------------

class TestAudioDeviceHandlers(_BaseHandlersTest):
    """get_audio_devices and list_audio_inputs return device lists."""

    def _inject_fake_devices(self) -> None:
        """Replace _list_audio_inputs on the service with a deterministic stub."""
        self.service._list_audio_inputs = lambda: [  # type: ignore[method-assign]
            {"id": 0, "name": "Built-in Microphone", "is_default": True,
             "max_input_channels": 1, "hostapi": "CoreAudio", "tags": ["mic"]},
            {"id": 1, "name": "BlackHole 2ch", "is_default": False,
             "max_input_channels": 2, "hostapi": "CoreAudio", "tags": ["loopback"]},
        ]

    def test_get_audio_devices_returns_devices_key(self) -> None:
        self._inject_fake_devices()
        resp = self.request("get_audio_devices")
        self.assertTrue(resp["ok"], resp)
        self.assertIn("devices", resp["result"])
        self.assertEqual(len(resp["result"]["devices"]), 2)

    def test_list_audio_inputs_structure(self) -> None:
        self._inject_fake_devices()
        resp = self.request("list_audio_inputs")
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertIn("items", result)
        self.assertIn("count", result)
        self.assertIn("default_input_id", result)
        self.assertEqual(result["count"], 2)

    def test_list_audio_inputs_default_id_resolved(self) -> None:
        self._inject_fake_devices()
        result = self.request("list_audio_inputs")["result"]
        self.assertEqual(result["default_input_id"], 0)

    def test_list_audio_inputs_no_sounddevice_returns_empty(self) -> None:
        """When sounddevice is not importable, _list_audio_inputs returns []."""
        self.service._list_audio_inputs = lambda: []  # type: ignore[method-assign]
        resp = self.request("list_audio_inputs")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["count"], 0)
        self.assertIsNone(resp["result"]["default_input_id"])

    def test_get_audio_devices_empty_list(self) -> None:
        self.service._list_audio_inputs = lambda: []  # type: ignore[method-assign]
        resp = self.request("get_audio_devices")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["devices"], [])


# ---------------------------------------------------------------------------
# 3. handle_get_clipboard_history
# ---------------------------------------------------------------------------

class TestGetClipboardHistory(_BaseHandlersTest):
    """get_clipboard_history returns the in-memory clipboard history list."""

    def _seed_clipboard(self, n: int) -> None:
        hist = self.service._clipboard_history
        for i in range(n):
            hist.append({"text": f"text_{i}", "ts": f"2026-04-22T12:00:{i:02d}", "history_id": f"id_{i}"})

    def test_empty_clipboard_history(self) -> None:
        resp = self.request("get_clipboard_history")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["items"], [])
        self.assertEqual(resp["result"]["count"], 0)

    def test_clipboard_history_returns_items(self) -> None:
        self._seed_clipboard(5)
        resp = self.request("get_clipboard_history")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["count"], 5)
        self.assertEqual(len(resp["result"]["items"]), 5)

    def test_clipboard_history_limit_respected(self) -> None:
        self._seed_clipboard(15)
        resp = self.request("get_clipboard_history", {"limit": 3})
        self.assertTrue(resp["ok"])
        self.assertLessEqual(len(resp["result"]["items"]), 3)
        self.assertEqual(resp["result"]["count"], 15)

    def test_clipboard_history_returns_latest_items(self) -> None:
        self._seed_clipboard(10)
        resp = self.request("get_clipboard_history", {"limit": 2})
        items = resp["result"]["items"]
        # Last 2 items (index 8 and 9).
        self.assertEqual(items[-1]["text"], "text_9")
        self.assertEqual(items[0]["text"], "text_8")

    def test_clipboard_history_limit_capped_at_20(self) -> None:
        self._seed_clipboard(20)
        resp = self.request("get_clipboard_history", {"limit": 100})
        self.assertTrue(resp["ok"])
        self.assertLessEqual(len(resp["result"]["items"]), 20)


# ---------------------------------------------------------------------------
# 4. handle_get_storage_info — mock filesystem
# ---------------------------------------------------------------------------

class TestGetStorageInfo(_BaseHandlersTest):
    """get_storage_info returns filesystem size metrics."""

    def test_returns_required_keys(self) -> None:
        resp = self.request("get_storage_info")
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        for key in (
            "history_bytes",
            "history_file_size_mb",
            "transcripts_count",
            "transcripts_size_mb",
            "reports_count",
            "total_bytes",
            "total_data_mb",
        ):
            self.assertIn(key, result, f"Key '{key}' missing from storage info")

    def test_history_bytes_non_negative(self) -> None:
        result = self.request("get_storage_info")["result"]
        self.assertGreaterEqual(result["history_bytes"], 0)

    def test_transcripts_count_non_negative(self) -> None:
        result = self.request("get_storage_info")["result"]
        self.assertGreaterEqual(result["transcripts_count"], 0)

    def test_total_bytes_non_negative(self) -> None:
        result = self.request("get_storage_info")["result"]
        self.assertGreaterEqual(result["total_bytes"], 0)

    def test_after_writing_history_bytes_increase(self) -> None:
        """Recording a transcription should make history_bytes grow."""
        before = self.request("get_storage_info")["result"]["history_bytes"]
        # Write an item via IPC to trigger history growth.
        self.service._history.store.add_history_item(
            text="Тестовая запись для увеличения размера",
            paste_status="ok",
            source_text="",
            translated_text="",
            translation_mode="off",
            source_lang="",
            target_lang="",
            translation_status="not_requested",
            translation_engine="test",
        )
        after = self.request("get_storage_info")["result"]["history_bytes"]
        self.assertGreaterEqual(after, before)


# ---------------------------------------------------------------------------
# 5. handle_apply_profile_preset — all 4 built-in presets
# ---------------------------------------------------------------------------

class TestApplyProfilePreset(_BaseHandlersTest):
    """apply_profile_preset applies settings for each of the 4 built-in presets."""

    _EXPECTED_KEYS = {
        "default": {
            "quality_profile": "balanced",
            "translation_mode": "off",
            "auto_paste": True,
        },
        "meeting": {
            "quality_profile": "max",
            "cleanup_profile": "strict",
            "auto_paste": False,
        },
        "translation": {
            "quality_profile": "balanced",
            "translation_mode": "auto",
            "translate_and_paste": True,
        },
        "call_recording": {
            "quality_profile": "max",
            "cleanup_profile": "strict",
            "auto_paste": False,
        },
    }

    def test_all_four_presets_apply_without_error(self) -> None:
        for preset in ("default", "meeting", "translation", "call_recording"):
            with self.subTest(preset=preset):
                resp = self.request("apply_profile_preset", {"profile": preset})
                self.assertTrue(resp["ok"], f"Preset '{preset}' failed: {resp}")

    def test_preset_settings_persisted(self) -> None:
        for preset, expected in self._EXPECTED_KEYS.items():
            with self.subTest(preset=preset):
                self.request("apply_profile_preset", {"profile": preset})
                settings_resp = self.request("get_settings")
                self.assertTrue(settings_resp["ok"])
                settings = settings_resp["result"]
                for key, value in expected.items():
                    self.assertEqual(
                        settings.get(key),
                        value,
                        f"Preset '{preset}': expected {key}={value!r}, got {settings.get(key)!r}",
                    )

    def test_unknown_preset_returns_error(self) -> None:
        resp = self.request("apply_profile_preset", {"profile": "nonexistent_preset"})
        self.assertFalse(resp["ok"])
        self.assertIn("error", resp)

    def test_missing_profile_param_returns_error(self) -> None:
        resp = self.request("apply_profile_preset", {})
        self.assertFalse(resp["ok"])

    def test_preset_invalidates_settings_cache(self) -> None:
        """After applying a preset, get_settings should reflect new values."""
        self.request("apply_profile_preset", {"profile": "meeting"})
        settings = self.request("get_settings")["result"]
        self.assertEqual(settings["quality_profile"], "max")
        self.assertFalse(settings["auto_paste"])

    def test_list_profile_presets_covers_all_four(self) -> None:
        resp = self.request("list_profile_presets")
        self.assertTrue(resp["ok"])
        result = resp["result"]
        preset_names = [p["name"] for p in result.get("presets", [])]
        for name in ("default", "meeting", "translation", "call_recording"):
            self.assertIn(name, preset_names)


if __name__ == "__main__":
    unittest.main()
