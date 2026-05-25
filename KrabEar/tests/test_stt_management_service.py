"""Unit tests for STTManagementService — Wave 705.

Covers all 6 IPC handlers:
  1. handle_add_stt_hotword
  2. handle_remove_stt_hotword
  3. handle_list_stt_hotwords
  4. handle_warmup_stt
  5. handle_get_stt_routing_decision
  6. handle_select_model
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

from backend.stt_management_service import STTManagementService, _STT_HOTWORDS_MAX


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeSettingsService:
    """Minimal SettingsService stub."""

    def __init__(self, initial: dict | None = None):
        self._data: dict = dict(initial or {})
        self._saved: list[dict] = []

    def cached_settings(self) -> dict:
        return dict(self._data)

    def handle_set_settings(self, params: dict) -> dict:
        self._data.update(params)
        self._saved.append(dict(params))
        return {"ok": True}


class _FakeEngine:
    def warmup(self) -> dict:
        return {"loaded": True, "latency_ms": 12, "model_name": "whisper-base", "error": None}


class _FakeTranscriber:
    def __init__(self, engine_ok: bool = True):
        self.engine = _FakeEngine() if engine_ok else None


# ---------------------------------------------------------------------------
# Tests — STT hotwords CRUD
# ---------------------------------------------------------------------------

class TestAddSttHotword(unittest.TestCase):

    def _make(self, initial: dict | None = None) -> STTManagementService:
        return STTManagementService(
            settings_svc=_FakeSettingsService(initial),
            transcriber=_FakeTranscriber(),
        )

    def test_add_new_word(self):
        svc = self._make()
        res = svc.handle_add_stt_hotword({"word": "Краб"})
        self.assertIn("Краб", res["hotwords"])
        self.assertFalse(res["truncated"])

    def test_add_duplicate_ignored(self):
        svc = self._make({"stt_hotwords": ["Краб"]})
        res = svc.handle_add_stt_hotword({"word": "Краб"})
        self.assertEqual(res["hotwords"].count("Краб"), 1)
        # settings not saved again
        self.assertEqual(svc._settings_svc._saved, [])

    def test_add_empty_word_raises(self):
        svc = self._make()
        with self.assertRaises(ValueError):
            svc.handle_add_stt_hotword({"word": ""})

    def test_add_missing_word_raises(self):
        svc = self._make()
        with self.assertRaises(ValueError):
            svc.handle_add_stt_hotword({})

    def test_truncated_flag_when_limit_exceeded(self):
        # Fill list to limit
        big_list = [f"word{i}" for i in range(_STT_HOTWORDS_MAX)]
        svc = self._make({"stt_hotwords": big_list})
        res = svc.handle_add_stt_hotword({"word": "extra"})
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["hotwords"]), _STT_HOTWORDS_MAX)
        self.assertIn("extra", res["hotwords"])

    def test_corrupted_hotwords_treated_as_empty(self):
        svc = self._make({"stt_hotwords": "not-a-list"})
        res = svc.handle_add_stt_hotword({"word": "test"})
        self.assertIn("test", res["hotwords"])


class TestRemoveSttHotword(unittest.TestCase):

    def _make(self, initial: dict | None = None) -> STTManagementService:
        return STTManagementService(
            settings_svc=_FakeSettingsService(initial),
            transcriber=_FakeTranscriber(),
        )

    def test_remove_existing(self):
        svc = self._make({"stt_hotwords": ["Краб", "OpenClaw"]})
        res = svc.handle_remove_stt_hotword({"word": "Краб"})
        self.assertNotIn("Краб", res["hotwords"])
        self.assertIn("OpenClaw", res["hotwords"])

    def test_remove_nonexistent_noop(self):
        svc = self._make({"stt_hotwords": ["OpenClaw"]})
        res = svc.handle_remove_stt_hotword({"word": "missing"})
        self.assertEqual(res["hotwords"], ["OpenClaw"])
        # settings not saved if nothing changed
        self.assertEqual(svc._settings_svc._saved, [])

    def test_remove_empty_word_raises(self):
        svc = self._make()
        with self.assertRaises(ValueError):
            svc.handle_remove_stt_hotword({"word": "  "})


class TestListSttHotwords(unittest.TestCase):

    def _make(self, initial: dict | None = None) -> STTManagementService:
        return STTManagementService(
            settings_svc=_FakeSettingsService(initial),
            transcriber=_FakeTranscriber(),
        )

    def test_list_returns_sorted(self):
        svc = self._make({"stt_hotwords": ["Zeta", "Alpha", "Mango"]})
        res = svc.handle_list_stt_hotwords({})
        self.assertEqual(res["hotwords"], ["Alpha", "Mango", "Zeta"])
        self.assertTrue(res["enabled"])

    def test_list_disabled_returns_empty(self):
        svc = self._make({"stt_hotwords": ["Краб"], "stt_hotwords_enabled": False})
        res = svc.handle_list_stt_hotwords({})
        self.assertEqual(res["hotwords"], [])
        self.assertFalse(res["enabled"])

    def test_list_empty_store(self):
        svc = self._make()
        res = svc.handle_list_stt_hotwords({})
        self.assertEqual(res["hotwords"], [])
        self.assertTrue(res["enabled"])

    def test_corrupted_store_returns_empty(self):
        svc = self._make({"stt_hotwords": 42})
        res = svc.handle_list_stt_hotwords({})
        self.assertEqual(res["hotwords"], [])


# ---------------------------------------------------------------------------
# Tests — warmup_stt
# ---------------------------------------------------------------------------

class TestWarmupStt(unittest.TestCase):

    def test_warmup_delegates_to_engine(self):
        svc = STTManagementService(
            settings_svc=_FakeSettingsService(),
            transcriber=_FakeTranscriber(engine_ok=True),
        )
        res = svc.handle_warmup_stt({})
        self.assertTrue(res["loaded"])
        self.assertEqual(res["model_name"], "whisper-base")

    def test_warmup_no_transcriber_returns_error(self):
        svc = STTManagementService(
            settings_svc=_FakeSettingsService(),
            transcriber=None,
        )
        res = svc.handle_warmup_stt({})
        self.assertFalse(res["loaded"])
        self.assertIn("error", res)

    def test_warmup_transcriber_no_engine_returns_error(self):
        t = _FakeTranscriber(engine_ok=False)
        t.engine = None  # explicitly no engine
        svc = STTManagementService(settings_svc=_FakeSettingsService(), transcriber=t)
        res = svc.handle_warmup_stt({})
        self.assertFalse(res["loaded"])


# ---------------------------------------------------------------------------
# Tests — get_stt_routing_decision
# ---------------------------------------------------------------------------

class TestGetSttRoutingDecision(unittest.TestCase):

    def _make(self) -> STTManagementService:
        return STTManagementService(
            settings_svc=_FakeSettingsService(),
            transcriber=_FakeTranscriber(),
        )

    def test_returns_required_keys(self):
        svc = self._make()
        res = svc.handle_get_stt_routing_decision({"language": "ru"})
        self.assertIn("selected_engine", res)
        self.assertIn("scores", res)
        self.assertIn("language", res)
        self.assertIn("audio_duration_s", res)

    def test_language_normalised_to_lowercase(self):
        svc = self._make()
        res = svc.handle_get_stt_routing_decision({"language": "RU"})
        self.assertEqual(res["language"], "ru")

    def test_missing_language_defaults_to_und(self):
        svc = self._make()
        res = svc.handle_get_stt_routing_decision({})
        self.assertEqual(res["language"], "und")

    def test_audio_duration_passed_through(self):
        svc = self._make()
        res = svc.handle_get_stt_routing_decision({"language": "en", "audio_duration_s": 30.5})
        self.assertAlmostEqual(res["audio_duration_s"], 30.5)

    def test_scores_is_dict(self):
        svc = self._make()
        res = svc.handle_get_stt_routing_decision({"language": "en"})
        self.assertIsInstance(res["scores"], dict)


# ---------------------------------------------------------------------------
# Tests — handle_select_model
# ---------------------------------------------------------------------------

class TestHandleSelectModel(unittest.TestCase):

    def _make(self) -> STTManagementService:
        return STTManagementService(
            settings_svc=_FakeSettingsService(),
            transcriber=_FakeTranscriber(),
        )

    def test_returns_required_keys(self):
        svc = self._make()
        res = svc.handle_select_model({"duration_sec": 10.0})
        self.assertIn("model_name", res)
        self.assertIn("reason", res)
        self.assertIn("estimated_latency_ms", res)
        self.assertIn("quality_tier", res)

    def test_invalid_duration_raises(self):
        svc = self._make()
        with self.assertRaises(ValueError):
            svc.handle_select_model({"duration_sec": "bad"})

    def test_zero_duration(self):
        svc = self._make()
        res = svc.handle_select_model({"duration_sec": 0.0})
        self.assertIsInstance(res["model_name"], str)

    def test_quality_balanced(self):
        svc = self._make()
        res = svc.handle_select_model({"duration_sec": 5.0, "quality": "balanced"})
        self.assertIn(res["quality_tier"], ("balanced", "max", "preview"))

    def test_invalid_system_load_defaults_to_zero(self):
        """Non-numeric system_load should not raise — defaults to 0.0."""
        svc = self._make()
        res = svc.handle_select_model({"duration_sec": 5.0, "system_load": "heavy"})
        self.assertIn("model_name", res)


# ---------------------------------------------------------------------------
# Integration: STTManagementService is wired in service.py dispatch
# ---------------------------------------------------------------------------

class TestDispatchWiring(unittest.TestCase):
    """Confirm service.py dispatch table references _stt_mgmt_svc for all 6 handlers."""

    SERVICE_PY = os.path.join(KRAB_EAR_ROOT, "backend", "service.py")

    _EXPECTED = {
        "add_stt_hotword",
        "remove_stt_hotword",
        "list_stt_hotwords",
        "warmup_stt",
        "get_stt_routing_decision",
        "select_model",
    }

    def _dispatch_block(self) -> str:
        with open(self.SERVICE_PY, encoding="utf-8") as f:
            src = f.read()
        start = src.index("handlers: dict[str, Callable")
        end = src.index("\n        handler = handlers.get(method)")
        return src[start:end]

    def test_all_handlers_delegated_to_stt_mgmt_svc(self):
        block = self._dispatch_block()
        for handler in self._expected_handlers():
            self.assertIn(handler, block, f"'{handler}' not found in dispatch block")
            self.assertIn("_stt_mgmt_svc", block, "_stt_mgmt_svc not referenced in dispatch")

    def _expected_handlers(self):
        return self._EXPECTED

    def test_stt_management_service_imported(self):
        with open(self.SERVICE_PY, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("from backend.stt_management_service import STTManagementService", src)

    def test_stt_mgmt_svc_instantiated_in_init(self):
        with open(self.SERVICE_PY, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("self._stt_mgmt_svc = STTManagementService(", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
