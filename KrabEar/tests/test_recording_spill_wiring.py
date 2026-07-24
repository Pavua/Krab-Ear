"""Проводка RecordingSpillWriter в RecordingCoreService (R1 Task 3).

Фейки-коллабораторы скопированы по образцу setUp/_make_service из
test_recording_core_service.py (worker обязан переиспользовать структуру,
не изобретать свою — см. план). Используется РЕАЛЬНЫЙ RecordingSpillWriter
(Task 1) с временным rescue_dir и РЕАЛЬНЫЙ StateStore для персиста.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes (по образцу test_recording_core_service.py)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    """Фейк-рекордер: сам НЕ трогает spill (append/close) — этим владеет
    RecordingCoreService._active_spill (RecordingCoreService — источник
    правды по discard()/close() согласно Task 3)."""

    sample_rate = 16000
    channels = 1

    def __init__(self, start_ok: bool = True, stop_audio: "tuple | None" = None):
        self.is_recording = False
        self._start_ok = start_ok
        self._stop_audio = stop_audio
        self.received_spill = "__unset__"
        self.start_calls = 0

    def start(self, spill=None):
        self.start_calls += 1
        self.received_spill = spill
        if not self._start_ok:
            return False
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        if self._stop_audio is not None:
            return self._stop_audio
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (np.sin(2.0 * np.pi * 440.0 * t) * 0.3).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _RaisingTranscriber:
    def transcribe(self, audio, **kwargs):
        raise RuntimeError("STT boom")


class _FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "hello world", "confidence": 0.9, "engine": "fake"}


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text,
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
        )


class _FakeSettingsSvc:
    def __init__(self, overrides: dict | None = None):
        self._overrides = dict(overrides or {})

    def cached_settings(self):
        return dict(self._overrides)

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_service(
    tmp_dir, rescue_dir, recorder=None, transcriber=None,
    settings_overrides=None, extra_kwargs=None,
):
    """Utility: construct a RecordingCoreService with minimal fakes + rescue_dir."""
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    # Гарантированно проходим silence/background guard'ы, чтобы дойти до
    # phase_c/d/e детерминированно (иначе тест зависит от порога RMS тона).
    base_settings = {
        "silence_guard_enabled": False,
        "background_guard_enabled": False,
    }
    base_settings.update(settings_overrides or {})
    kwargs = dict(
        recorder=recorder or _FakeRecorder(),
        transcriber=transcriber or _FakeTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_FakeSettingsSvc(base_settings),
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
        rescue_dir=rescue_dir,
    )
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return RecordingCoreService(**kwargs)


def _find_meta_json(rescue_dir: Path) -> dict:
    metas = list(rescue_dir.glob("*.meta.json"))
    assert len(metas) == 1, f"expected exactly one meta.json, found {metas}"
    return json.loads(metas[0].read_text(encoding="utf-8"))


class RecordingSpillWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self._tmp = self._tmp_ctx.name
        self.addCleanup(self._tmp_ctx.cleanup)
        self.rescue_dir = Path(self._tmp) / "rescue"

    def test_start_passes_open_spill_to_recorder(self):
        recorder = _FakeRecorder()
        svc = _make_service(
            self._tmp, self.rescue_dir, recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        result = svc.handle_start_recording({})
        self.assertEqual(result["status"], "recording")
        self.assertIsNotNone(recorder.received_spill)
        self.assertNotEqual(recorder.received_spill, "__unset__")
        self.assertTrue(recorder.received_spill.part_path.exists())
        self.assertIs(svc._active_spill, recorder.received_spill)

    def test_start_with_setting_disabled_passes_none(self):
        recorder = _FakeRecorder()
        svc = _make_service(
            self._tmp, self.rescue_dir, recorder=recorder,
            settings_overrides={"recording_spill_enabled": False},
        )
        result = svc.handle_start_recording({})
        self.assertEqual(result["status"], "recording")
        self.assertIsNone(recorder.received_spill)
        self.assertIsNone(svc._active_spill)
        self.assertFalse(self.rescue_dir.exists() and any(self.rescue_dir.glob("*.part")))

    def test_start_source_param_reaches_meta(self):
        recorder = _FakeRecorder()
        svc = _make_service(
            self._tmp, self.rescue_dir, recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        svc.handle_start_recording({"source": "meeting"})
        meta = _find_meta_json(self.rescue_dir)
        self.assertEqual(meta["source"], "meeting")

    def test_stop_discards_after_persist(self):
        recorder = _FakeRecorder()
        svc = _make_service(
            self._tmp, self.rescue_dir, recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        svc.handle_start_recording({})
        part_path = recorder.received_spill.part_path
        self.assertTrue(part_path.exists())
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result.get("status"), "ok")
        self.assertIsNotNone(result.get("history_id"))
        self.assertFalse(part_path.exists())
        self.assertIsNone(svc._active_spill)

    def test_stop_keeps_spill_when_stt_fails(self):
        recorder = _FakeRecorder()
        svc = _make_service(
            self._tmp, self.rescue_dir, recorder=recorder,
            transcriber=_RaisingTranscriber(),
            settings_overrides={"recording_spill_enabled": True},
        )
        svc.handle_start_recording({})
        part_path = recorder.received_spill.part_path
        self.assertTrue(part_path.exists())
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result.get("status"), "stt_failed")
        # STT упал — аудио ещё нужно для восстановления на следующем старте.
        self.assertTrue(part_path.exists())
        self.assertIsNone(svc._active_spill)

    def test_start_failure_discards_placeholder(self):
        recorder = _FakeRecorder(start_ok=False)
        svc = _make_service(
            self._tmp, self.rescue_dir, recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        result = svc.handle_start_recording({})
        self.assertIn(result["status"], ("already_recording", "recorder_stopping"))
        # start() не удался — placeholder-файл не должен пережить вызов.
        self.assertFalse(any(self.rescue_dir.glob("*.part")) if self.rescue_dir.exists() else False)
        self.assertIsNone(svc._active_spill)


if __name__ == "__main__":
    unittest.main()
