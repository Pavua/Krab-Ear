"""Аудио диктовки переживает ЗАВИСАНИЕ STT, не только краш.

Инцидент 01.08.2026: три диктовки владельца потеряны подряд. Спасательная
запись аудио жила только в `except Exception` вокруг transcribe — при
зависании STT (дедлок mlx_lock, PortAudio-wedge) исключения нет, вотчдог
перезапускает backend, и аудио умирает вместе с процессом.

Покрывает:
  - аудио лежит на диске УЖЕ в момент вызова transcribe (переживает kill -9);
  - после успешной транскрибации временный файл убирается (не копится);
  - при краше STT файл остаётся и путь отдаётся в audio_recovery_path.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore
from tests.test_phase_c_stt_crash_recovery_W1177 import (
    _FakeRecorder,
    _FakeSemanticSearcher,
    _FakeSettingsSvc,
    _FakeTranslator,
)


class _ObservingTranscriber:
    """Фиксирует, какие WAV лежали в failed_recordings/ в момент вызова."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self.seen_files: list[Path] = []
        self.seen_sizes: list[int] = []

    def transcribe(self, audio, **kwargs):
        # Размеры снимаем здесь: после успеха файл удаляется, снаружи его уже нет.
        self.seen_files = sorted(
            (self._data_dir / "failed_recordings").glob("*.wav")
        )
        self.seen_sizes = [p.stat().st_size for p in self.seen_files]
        return {
            "text": "тестовый текст",
            "raw_text": "тестовый текст",
            "cleaned_text": "тестовый текст",
            "llm_applied": False,
            "confidence": 0.9,
            "raw_confidence": 0.9,
            "duration_ms": 100,
            "engine": "fake",
            "model": "fake",
            "language": "ru",
            "segments": [],
            "diarization": None,
            "emotion": None,
        }


class _KeepWavSettings(_FakeSettingsSvc):
    def cached_settings(self):
        return {"debug_keep_dictation_wav": True}


def _make_service(tmp_dir: str, transcriber, settings_svc=None):
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.load.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=_FakeRecorder(),
        transcriber=transcriber,
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=settings_svc or _FakeSettingsSvc(),
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
    ), store


class TestAudioPresaveSurvivesHang(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_audio_on_disk_before_transcribe_starts(self):
        """Главный инвариант: зависший STT ⇒ аудио уже спасено."""
        transcriber = _ObservingTranscriber(Path(self._tmp))
        svc, _ = _make_service(self._tmp, transcriber)
        svc.handle_start_recording({})

        svc.handle_stop_recording({})

        self.assertEqual(
            len(transcriber.seen_files), 1,
            "в момент вызова transcribe аудио должно уже лежать в "
            f"failed_recordings/, найдено: {transcriber.seen_files}",
        )
        self.assertGreater(
            transcriber.seen_sizes[0], 0,
            "спасательный WAV не должен быть пустым",
        )

    def test_presaved_audio_removed_after_success(self):
        """Успешная транскрибация не оставляет мусора на диске."""
        transcriber = _ObservingTranscriber(Path(self._tmp))
        svc, store = _make_service(self._tmp, transcriber)
        svc.handle_start_recording({})

        svc.handle_stop_recording({})

        leftovers = list((Path(store.data_dir) / "failed_recordings").glob("*.wav"))
        self.assertEqual(
            leftovers, [],
            f"после успеха временный WAV должен удаляться, остались: {leftovers}",
        )

    def test_audio_kept_when_stt_crashes(self):
        """Регресс W1177: при краше файл остаётся и путь возвращается."""
        class _Crashing:
            def transcribe(self, audio, **kwargs):
                raise RuntimeError("Simulated hang-then-crash")

        svc, store = _make_service(self._tmp, _Crashing())
        svc.handle_start_recording({})

        result = svc.handle_stop_recording({})

        recovery_rel = result.get("audio_recovery_path")
        self.assertIsNotNone(recovery_rel, "audio_recovery_path должен быть задан")
        self.assertTrue(
            (Path(store.data_dir) / recovery_rel).exists(),
            "спасательный WAV должен пережить краш STT",
        )

    def test_keep_wav_on_success_when_debug_flag(self):
        """W2b: opt-in keep после успеха — WAV в debug_duration_wav/, не в failed_recordings/."""
        transcriber = _ObservingTranscriber(Path(self._tmp))
        svc, store = _make_service(
            self._tmp, transcriber, settings_svc=_KeepWavSettings(),
        )
        svc.handle_start_recording({})
        svc.handle_stop_recording({})

        data_dir = Path(store.data_dir)
        kept = list((data_dir / "debug_duration_wav").glob("*.wav"))
        leftovers = list((data_dir / "failed_recordings").glob("*.wav"))
        self.assertEqual(leftovers, [], leftovers)
        self.assertEqual(len(kept), 1, f"ожидали один WAV в debug_duration_wav/, есть: {kept}")
        sidecar = kept[0].with_suffix(".json")
        self.assertTrue(sidecar.is_file(), f"нет сидкара {sidecar}")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertNotIn("text", payload)
        self.assertNotIn("transcript", payload)
        for key in (
            "vad_total_sec", "chunker_duration_sec",
            "wav_nframes", "sample_rate",
        ):
            self.assertIn(key, payload, key)


if __name__ == "__main__":
    unittest.main()
