"""Тесты WhisperX adapter в fallback chain AudioEngine (Phase 4.3).

Проверяет интеграцию без реальной загрузки модели (FakeAudioEngine паттерн).
whisperx не нужен для прохождения тестов — все точки входа замоканы.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine
from backend.models import HistoryItem


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

_FAKE_WORD_TIMESTAMPS = [
    {"word": "привет", "start": 0.0, "end": 0.4, "confidence": 0.95},
    {"word": "мир", "start": 0.5, "end": 0.8, "confidence": 0.97},
]

_FAKE_SPEAKER_TURNS = [
    {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
    {"speaker": "SPEAKER_01", "start": 1.1, "end": 2.3},
]


def _make_engine(
    *,
    whisperx_enabled: bool = True,
    sensevoice_enabled: bool = False,
    whisperx_marker_unavailable: bool = False,
    sensevoice_marker_unavailable: bool = False,
) -> AudioEngine:
    """Создаёт AudioEngine.__new__ с необходимым минимумом стейта."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models = {}
    engine._router = None
    if whisperx_marker_unavailable:
        engine._unavailable_models[engine._WHISPERX_MARKER] = __import__("time").monotonic()
    if sensevoice_marker_unavailable:
        engine._unavailable_models[engine._SENSEVOICE_MARKER] = __import__("time").monotonic()
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._whisperx_model = None
    engine._whisperx_load_error = None
    return engine


def _mock_settings(
    mock: Any,
    *,
    whisperx_enabled: bool = True,
    sensevoice_enabled: bool = False,
) -> None:
    mock.WHISPERX_ENABLED = whisperx_enabled
    mock.WHISPERX_MODEL = "large-v3"
    mock.WHISPERX_DEVICE = "cpu"
    mock.WHISPERX_DIARIZATION = False
    mock.WHISPERX_WORD_TIMESTAMPS = True
    mock.SENSEVOICE_ENABLED = sensevoice_enabled
    mock.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    mock.SENSEVOICE_EMOTION_TO_HISTORY = True
    mock.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    mock.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    mock.TRANSCRIBE_TIMEOUT_SEC = 30
    mock.NETWORK_MODE = "offline_strict"
    mock.model_max_list = ["mlx-community/whisper-large-v3-turbo"]
    mock.HF_TOKEN = ""


# ---------------------------------------------------------------------------
# 1. WhisperX отключён — маркер не вставляется
# ---------------------------------------------------------------------------

class TestWhisperXAdapterDisabled(unittest.TestCase):
    """WhisperX не участвует в chain когда WHISPERX_ENABLED=False."""

    @patch("core.engine.settings")
    def test_whisperx_skipped_when_disabled(self, mock_settings: Any) -> None:
        """Когда флаг выключен — WHISPERX_MARKER не вставляется в candidates."""
        _mock_settings(mock_settings, whisperx_enabled=False)

        engine = _make_engine(whisperx_enabled=False)

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                mock_pool_cls.return_value.submit.return_value.result.return_value = {"text": "тест", "segments": [], "language": "ru"}
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        # WhisperX маркер не должен оказаться в unavailable (он не вставлялся)
        self.assertNotIn(engine._WHISPERX_MARKER, engine._unavailable_models)


# ---------------------------------------------------------------------------
# 2. WhisperX успешно транскрибирует когда balanced недоступен
# ---------------------------------------------------------------------------

class TestWhisperXAdapterEnabled(unittest.TestCase):
    """WhisperX участвует в chain когда включён."""

    @patch("core.engine.settings")
    def test_whisperx_reached_when_balanced_unavailable(self, mock_settings: Any) -> None:
        """Когда balanced whisper помечен недоступным — WhisperX успешно транскрибирует."""
        _mock_settings(mock_settings, whisperx_enabled=True)

        engine = _make_engine()
        # Помечаем balanced whisper как недоступный — WhisperX должен сработать
        engine._unavailable_models["mlx-community/whisper-large-v3-turbo"] = __import__("time").monotonic()

        engine._transcribe_whisperx = lambda *a, **kw: {  # type: ignore[method-assign]
            "text": "привет мир",
            "engine": "whisperx",
            "language": "ru",
            "segments": [],
            "word_timestamps": _FAKE_WORD_TIMESTAMPS,
            "speaker_turns": None,
        }

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(result["text"], "привет мир")
        self.assertEqual(result["engine"], "whisperx")
        self.assertIsNotNone(result["word_timestamps"])
        self.assertEqual(len(result["word_timestamps"]), 2)

    @patch("core.engine.settings")
    def test_whisperx_marker_inserted_after_sensevoice(self, mock_settings: Any) -> None:
        """Когда оба адаптера включены — WhisperX маркер идёт ПОСЛЕ SenseVoice маркера."""
        _mock_settings(mock_settings, whisperx_enabled=True, sensevoice_enabled=True)

        engine = _make_engine()
        visited: list[str] = []

        def fake_transcribe_model(audio_data: Any, model_name: str, prompt: str, language: Any = None) -> dict:
            visited.append(model_name)
            raise RuntimeError("unavail")

        engine._transcribe_model = fake_transcribe_model  # type: ignore[method-assign]
        engine._transcribe_sensevoice = MagicMock(side_effect=RuntimeError("funasr not installed"))  # type: ignore[method-assign]
        engine._transcribe_whisperx = MagicMock(side_effect=RuntimeError("whisperx not installed"))  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.engine._get_available_memory_gb", return_value=16.0):
                with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                    mock_pool_cls.return_value.submit.return_value.result.side_effect = RuntimeError("unavail")
                    with self.assertRaises(RuntimeError):
                        engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        # Оба маркера должны быть помечены недоступными (оба упали)
        self.assertIn(engine._SENSEVOICE_MARKER, engine._unavailable_models)
        self.assertIn(engine._WHISPERX_MARKER, engine._unavailable_models)

    @patch("core.engine.settings")
    def test_whisperx_marker_not_retried_after_failure(self, mock_settings: Any) -> None:
        """Если WhisperX однажды упал — он не вставляется в chain повторно."""
        _mock_settings(mock_settings, whisperx_enabled=True)

        engine = _make_engine(whisperx_marker_unavailable=True)

        wx_call_count = []
        engine._transcribe_whisperx = lambda *a, **kw: wx_call_count.append(True) or {}  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                mock_pool_cls.return_value.submit.return_value.result.return_value = {"text": "вискер", "segments": [], "language": "ru"}
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(len(wx_call_count), 0, "WhisperX не должен вызываться если маркер уже недоступен")


# ---------------------------------------------------------------------------
# 3. _load_whisperx_model — graceful при отсутствии библиотеки
# ---------------------------------------------------------------------------

class TestWhisperXLoadNoLibrary(unittest.TestCase):
    """_load_whisperx_model корректно обрабатывает отсутствие whisperx."""

    def test_load_raises_when_whisperx_missing(self) -> None:
        """Без whisperx _load_whisperx_model поднимает RuntimeError."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._whisperx_model = None
        engine._whisperx_load_error = None

        with patch("core.engine._whisperx", None):
            with self.assertRaises(RuntimeError) as ctx:
                engine._load_whisperx_model()
        self.assertIn("whisperx", str(ctx.exception).lower())
        # Ошибка кэшируется
        self.assertIsNotNone(engine._whisperx_load_error)

    def test_load_raises_from_cache_on_second_call(self) -> None:
        """После первого сбоя _load_whisperx_model сразу поднимает RuntimeError без попытки загрузки."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._whisperx_model = None
        engine._whisperx_load_error = "whisperx не установлен"

        load_attempts = []

        with patch("core.engine._whisperx") as mock_wx:
            mock_wx.load_model.side_effect = lambda *a, **kw: load_attempts.append(True)
            with self.assertRaises(RuntimeError):
                engine._load_whisperx_model()

        self.assertEqual(len(load_attempts), 0, "Повторная загрузка не должна происходить")


# ---------------------------------------------------------------------------
# 4. HistoryItem — новые поля word_timestamps и speaker_turns
# ---------------------------------------------------------------------------

class TestHistoryItemWhisperXFields(unittest.TestCase):
    """HistoryItem поддерживает поля word_timestamps и speaker_turns без ошибок сериализации."""

    def test_create_with_word_timestamps(self) -> None:
        item = HistoryItem.create(text="тест", word_timestamps=_FAKE_WORD_TIMESTAMPS)
        self.assertIsNotNone(item.word_timestamps)
        self.assertEqual(len(item.word_timestamps), 2)
        self.assertEqual(item.word_timestamps[0]["word"], "привет")

    def test_create_with_speaker_turns(self) -> None:
        item = HistoryItem.create(text="тест", speaker_turns=_FAKE_SPEAKER_TURNS)
        self.assertIsNotNone(item.speaker_turns)
        self.assertEqual(item.speaker_turns[0]["speaker"], "SPEAKER_00")

    def test_create_without_new_fields_defaults_none(self) -> None:
        item = HistoryItem.create(text="тест")
        self.assertIsNone(item.word_timestamps)
        self.assertIsNone(item.speaker_turns)

    def test_to_dict_includes_new_fields(self) -> None:
        item = HistoryItem.create(
            text="тест",
            word_timestamps=_FAKE_WORD_TIMESTAMPS,
            speaker_turns=_FAKE_SPEAKER_TURNS,
        )
        d = item.to_dict()
        self.assertIn("word_timestamps", d)
        self.assertIn("speaker_turns", d)
        self.assertEqual(len(d["word_timestamps"]), 2)
        self.assertEqual(d["speaker_turns"][1]["speaker"], "SPEAKER_01")

    def test_from_dict_roundtrip_with_new_fields(self) -> None:
        item = HistoryItem.create(
            text="тест",
            word_timestamps=_FAKE_WORD_TIMESTAMPS,
            speaker_turns=_FAKE_SPEAKER_TURNS,
        )
        d = item.to_dict()
        restored = HistoryItem.from_dict(d)
        self.assertEqual(restored.word_timestamps, _FAKE_WORD_TIMESTAMPS)
        self.assertEqual(restored.speaker_turns, _FAKE_SPEAKER_TURNS)

    def test_from_dict_legacy_no_new_fields(self) -> None:
        """Старые записи без полей word_timestamps/speaker_turns загружаются без ошибок."""
        payload = {
            "id": "abc",
            "ts": "2026-01-01T00:00:00",
            "text": "старая запись",
        }
        item = HistoryItem.from_dict(payload)
        self.assertIsNone(item.word_timestamps)
        self.assertIsNone(item.speaker_turns)

    def test_emotion_field_still_works_after_schema_change(self) -> None:
        """Обратная совместимость: emotion поле работает вместе с новыми полями."""
        item = HistoryItem.create(text="тест", emotion="happy", word_timestamps=_FAKE_WORD_TIMESTAMPS)
        self.assertEqual(item.emotion, "happy")
        self.assertIsNotNone(item.word_timestamps)


# ---------------------------------------------------------------------------
# 5. WhisperX без SenseVoice — только один дополнительный маркер
# ---------------------------------------------------------------------------

class TestWhisperXOnlyNoSenseVoice(unittest.TestCase):
    """WhisperX-only конфиг: SenseVoice выключен, WhisperX включён."""

    @patch("core.engine.settings")
    def test_whisperx_only_marker_at_position_1(self, mock_settings: Any) -> None:
        """При SenseVoice=False WhisperX маркер вставляется на позицию 1 (после balanced)."""
        _mock_settings(mock_settings, whisperx_enabled=True, sensevoice_enabled=False)

        engine = _make_engine()
        # Balanced недоступен чтобы дойти до WhisperX
        engine._unavailable_models["mlx-community/whisper-large-v3-turbo"] = __import__("time").monotonic()

        wx_called = []
        engine._transcribe_whisperx = lambda *a, **kw: wx_called.append(True) or {  # type: ignore[method-assign]
            "text": "тест",
            "engine": "whisperx",
            "language": "ru",
            "segments": [],
            "word_timestamps": None,
            "speaker_turns": None,
        }

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(len(wx_called), 1, "WhisperX должен быть вызван ровно один раз")
        self.assertEqual(result["engine"], "whisperx")


if __name__ == "__main__":
    unittest.main()
