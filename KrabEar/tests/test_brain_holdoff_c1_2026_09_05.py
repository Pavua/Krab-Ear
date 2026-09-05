"""C1 holdoff / no GPU steal (2026-09-05).

Политика владельца: 15+ ГБ слот (тот же, что группы Краба и summary Ear)
не поднимать обратно после ручной выгрузки. Ear не делает `lms load` /
`load_model_async(brain)` на стопе, если preload выключен; rewriter на HTTP 400
не self-heal'ит загрузкой; summarize не автогрузит пустую Studio.
MemoryConductor не выгружает brain (shadow «would evict» в логе — ок).
`cloud_rewriter_enabled` и `memory_conductor_enforce*` этим срезом не включать.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriter  # noqa: E402
from backend.memory_conductor import MemoryConductor  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402

BRAIN_ID = "qwen/qwen3.6-27b"
KRAB_SLOT_ID = "lm-studio-local/gemma-4-26b-a4b-it@4bit"
BASE_URL = "http://localhost:1234/v1"


def _holdoff_settings(**over) -> MagicMock:
    base = {
        "llm_brain_model": BRAIN_ID,
        "llm_brain_lease_enabled": True,
        "llm_brain_lease_ttl_sec": 30.0,
        "llm_brain_preload_on_stop": False,
        "llm_brain_unload_on_recording": False,
        "llm_base_url": BASE_URL,
        "realtime_preview_enabled": False,
        "realtime_partial_enabled": False,
        "realtime_silence_filter_enabled": False,
    }
    base.update(over)
    svc = MagicMock()
    svc.cached_settings.return_value = base
    svc.invalidate_cache = MagicMock()
    return svc


class _FakeRecorder:
    is_recording = False
    sample_rate = 16000

    def start(self, spill=None) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (np.sin(2.0 * np.pi * 440.0 * t) * 0.3).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "hello world", "confidence": 0.9, "engine": "fake"}


class _FakeTranslator:
    def translate(self, text, **kwargs):
        return TranslationResult(
            text=text,
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
        )


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_service(tmp_dir, extra_kwargs=None):
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    kwargs = dict(
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_holdoff_settings(),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=session_tracker,
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return RecordingCoreService(**kwargs)


def _fake_requests_response(status_code: int, text: str = "", json_data=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


def _conductor_settings(**over) -> MagicMock:
    base = {
        "memory_conductor_enabled": True,
        "memory_conductor_enforce": False,
        "memory_conductor_enforce_brain": False,
        "gigaam_idle_unload_sec": 600.0,
        "whisper_idle_unload_sec": 900.0,
        "rewriter_idle_unload_sec": 1800.0,
        "memory_pressure_streak_ticks": 3,
        "memory_evict_cooldown_sec": 600.0,
        "llm_brain_model": BRAIN_ID,
        "llm_model": "gigachat3.1-10b-a1.8b-mlx-oq8",
        "llm_base_url": BASE_URL,
        "mlx_oom_auto_unload_enabled": True,
    }
    base.update(over)
    svc = MagicMock()
    svc.cached_settings.return_value = base
    return svc


def _mk_conductor(*, pressure=4, settings=None, model_loaded=False):
    return MemoryConductor(
        settings_service=settings or _conductor_settings(),
        ledger=MagicMock(),
        is_recording=lambda: False,
        is_meeting_active=lambda: False,
        pressure_fn=lambda: pressure,
        host_stats_fn=lambda: None,
        gigaam_close_if_idle=MagicMock(return_value=True),
        gigaam_idle_sec_fn=lambda: 0.0,
        last_stt_activity_ts_fn=lambda: time.monotonic(),
        tick_sec=0.05,
        unload_model_fn=MagicMock(),
        load_model_fn=MagicMock(),
        model_loaded_fn=MagicMock(return_value=model_loaded),
        lease_holder_fn=lambda: None,
        verify_timeout_sec=0.2,
        verify_poll_sec=0.02,
    )


class StopRecordingDoesNotStealGpuTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_stop_with_preload_false_does_not_acquire_or_load_brain(self) -> None:
        svc = _make_service(
            self._tmp,
            extra_kwargs={"settings_svc": _holdoff_settings()},
        )
        svc.handle_start_recording({})
        with patch("backend.brain_lease.acquire_brain_lease") as acquire, patch(
            "backend.lm_studio_lifecycle.load_model_async"
        ) as load_async, patch(
            "backend.lm_studio_lifecycle.load_model_sync"
        ) as load_sync:
            svc.handle_stop_recording({"quality_profile": "balanced"})
        acquire.assert_not_called()
        load_async.assert_not_called()
        load_sync.assert_not_called()

    def test_stop_with_preload_true_still_acquires_and_loads(self) -> None:
        svc = _make_service(
            self._tmp,
            extra_kwargs={
                "settings_svc": _holdoff_settings(llm_brain_preload_on_stop=True),
            },
        )
        svc.handle_start_recording({})
        with patch("backend.brain_lease.acquire_brain_lease") as acquire, patch(
            "backend.lm_studio_lifecycle.load_model_async"
        ) as load_async:
            svc.handle_stop_recording({"quality_profile": "balanced"})
        acquire.assert_called_once()
        load_async.assert_called_once()
        self.assertEqual(load_async.call_args.args[1], BRAIN_ID)

    def test_start_releases_lease_and_does_not_load_brain(self) -> None:
        svc = _make_service(
            self._tmp,
            extra_kwargs={"settings_svc": _holdoff_settings()},
        )
        with patch("backend.brain_lease.release_brain_lease") as release, patch(
            "backend.lm_studio_lifecycle.load_model_async"
        ) as load_async:
            result = svc.handle_start_recording({})
        self.assertEqual(result["status"], "recording")
        release.assert_called_once()
        load_async.assert_not_called()


class RewriterNoLmsLoadOnEmptyStudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rewriter = LLMRewriter(
            base_url=BASE_URL,
            api_key="",
            model="gigachat3.1-10b-a1.8b-mlx-oq8",
            timeout_sec=5.0,
            circuit_fail_threshold=3,
            idle_keepalive_enabled=False,
        )

    def test_rewrite_400_no_models_does_not_call_load_model_sync(self) -> None:
        self.rewriter._session.post = MagicMock(
            return_value=_fake_requests_response(400, text="No models loaded")
        )
        with patch("backend.lm_studio_lifecycle.load_model_sync") as load_sync, patch(
            "backend.lm_studio_lifecycle.load_model_async"
        ) as load_async:
            result = self.rewriter.rewrite("raw transcript text")
        load_sync.assert_not_called()
        load_async.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIsNone(result.text)
        self.assertIn("400", result.fallback_reason or "")
        self.assertEqual(self.rewriter._session.post.call_count, 1)


@pytest.mark.llm_network_live
class SummarizeDoesNotAutoloadEmptyStudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rewriter = LLMRewriter(
            base_url=BASE_URL,
            api_key="",
            model="gigachat3.1-10b-a1.8b-mlx-oq8",
            timeout_sec=5.0,
            circuit_fail_threshold=3,
            idle_keepalive_enabled=False,
        )

    def test_summarize_400_does_not_lms_load(self) -> None:
        self.rewriter._session.post = MagicMock(
            return_value=_fake_requests_response(400, text="No models loaded")
        )
        with patch("backend.lm_studio_lifecycle.load_model_sync") as load_sync, patch(
            "backend.lm_studio_lifecycle.load_model_async"
        ) as load_async:
            result = self.rewriter.summarize("long enough transcript for a summary " * 8)
        load_sync.assert_not_called()
        load_async.assert_not_called()
        self.assertFalse(result.ok)
        self.assertIsNone(result.text)

    def test_summarize_known_empty_studio_skips_completion(self) -> None:
        self.rewriter._session.post = MagicMock()
        with patch(
            "backend.lm_studio_lifecycle.probe_loaded_chat_models",
            return_value=[],
        ) as probe, patch(
            "backend.lm_studio_lifecycle.load_model_sync"
        ) as load_sync:
            result = self.rewriter.summarize("long enough transcript for a summary " * 8)
        probe.assert_called()
        self.rewriter._session.post.assert_not_called()
        load_sync.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "studio_empty_no_autoload")

    def test_summarize_uses_already_loaded_slot_not_rewriter_id(self) -> None:
        self.rewriter._session.post = MagicMock(
            return_value=_fake_requests_response(
                200,
                json_data={"choices": [{"message": {"content": "Краткое резюме."}}]},
            )
        )
        with patch(
            "backend.lm_studio_lifecycle.probe_loaded_chat_models",
            return_value=[KRAB_SLOT_ID],
        ):
            result = self.rewriter.summarize("long enough transcript for a summary " * 8)
        self.assertTrue(result.ok)
        payload = self.rewriter._session.post.call_args.kwargs.get("json")
        if payload is None:
            payload = self.rewriter._session.post.call_args[1].get("json")
        self.assertEqual(payload["model"], KRAB_SLOT_ID)


class ProbeLoadedChatModelsTest(unittest.TestCase):
    def test_empty_catalog_is_known_empty_list(self) -> None:
        from backend.lm_studio_lifecycle import probe_loaded_chat_models

        body = json.dumps({"object": "list", "data": []}).encode()
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            self.assertEqual(probe_loaded_chat_models(BASE_URL), [])

    def test_loaded_chat_id_returned_embeddings_skipped(self) -> None:
        from backend.lm_studio_lifecycle import probe_loaded_chat_models

        body = json.dumps({
            "object": "list",
            "data": [
                {"id": "text-embedding-nomic", "state": "loaded", "type": "embeddings"},
                {"id": KRAB_SLOT_ID, "state": "loaded", "type": "llm"},
            ],
        }).encode()
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = body
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER.open", return_value=resp):
            self.assertEqual(probe_loaded_chat_models(BASE_URL), [KRAB_SLOT_ID])


class ConductorDoesNotEvictBrainTest(unittest.TestCase):
    def test_oom_legacy_flag_does_not_unload_brain(self) -> None:
        c = _mk_conductor(pressure=4, settings=_conductor_settings(
            memory_conductor_enforce=False,
            mlx_oom_auto_unload_enabled=True,
        ))
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        c.wait_workers(2.0)
        c.unload_model_fn.assert_not_called()
        self.assertGreater(c.get_diagnostics()["residents"]["brain"]["would"], 0)

    def test_pressure_ticks_never_unload_brain_even_if_enforce_true(self) -> None:
        c = _mk_conductor(pressure=4, settings=_conductor_settings(
            memory_conductor_enforce=True,
            memory_conductor_enforce_brain=True,
        ))
        for _ in range(3):
            c.tick_once()
        c.unload_model_fn.assert_not_called()
        self.assertGreater(c.get_diagnostics()["residents"]["brain"]["would"], 0)


class SchemaHoldoffDefaultsTest(unittest.TestCase):
    def test_preload_on_stop_default_false(self) -> None:
        self.assertIn("llm_brain_preload_on_stop", DEFAULT_SETTINGS)
        self.assertIs(DEFAULT_SETTINGS["llm_brain_preload_on_stop"], False)

    def test_brain_model_key_present(self) -> None:
        self.assertIn("llm_brain_model", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["llm_brain_model"], BRAIN_ID)

    def test_cloud_rewriter_stays_off(self) -> None:
        self.assertIs(DEFAULT_SETTINGS["cloud_rewriter_enabled"], False)

    def test_rewrite_stays_off(self) -> None:
        self.assertIs(DEFAULT_SETTINGS["llm_rewrite_enabled"], False)

    def test_enforce_flags_stay_false(self) -> None:
        for key in (
            "memory_conductor_enforce",
            "memory_conductor_enforce_brain",
            "memory_conductor_enforce_recording_sequence",
            "memory_conductor_enforce_rewriter",
            "memory_conductor_enforce_gigaam",
            "memory_conductor_enforce_whisper",
        ):
            self.assertIs(DEFAULT_SETTINGS[key], False, key)

    def test_pydantic_preload_default_false(self) -> None:
        from core.config import Settings
        self.assertIs(Settings.model_fields["LLM_BRAIN_PRELOAD_ON_STOP"].default, False)

    def test_bool_fields_coerce_preload_string_false(self) -> None:
        from backend.settings_validator import SettingsValidator
        result = SettingsValidator().validate({"llm_brain_preload_on_stop": "false"})
        self.assertIs(result.fixed["llm_brain_preload_on_stop"], False)

    def test_bool_fields_coerce_cloud_rewriter_string_false(self) -> None:
        from backend.settings_validator import SettingsValidator
        result = SettingsValidator().validate({"cloud_rewriter_enabled": "false"})
        self.assertIs(result.fixed["cloud_rewriter_enabled"], False)


if __name__ == "__main__":
    unittest.main()
