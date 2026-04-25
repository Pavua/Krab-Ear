"""Тесты SenseVoice adapter в fallback chain AudioEngine.

Проверяет интеграцию без реальной загрузки модели (FakeAudioEngine паттерн).
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


class TestSenseVoiceAdapterDisabled(unittest.TestCase):
    """SenseVoice не участвует в chain когда SENSEVOICE_ENABLED=False."""

    @patch("core.engine.settings")
    def test_sensevoice_skipped_when_disabled(self, mock_settings: Any) -> None:
        """_transcribe_with_fallback_impl не вставляет SENSEVOICE_MARKER если флаг выключен."""
        mock_settings.SENSEVOICE_ENABLED = False
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]

        engine = AudioEngine.__new__(AudioEngine)
        engine._router = None
        engine.quality_profile = "balanced"
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        engine._unavailable_models = set()
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool_cls.return_value.__enter__ = lambda s: mock_pool
                mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_future = MagicMock()
                mock_future.result.return_value = {"text": "тест", "segments": [], "language": "ru"}
                mock_pool.submit.return_value = mock_future
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        # Проверяем что SENSEVOICE_MARKER не оказался в unavailable (не вставлялся)
        self.assertNotIn(engine._SENSEVOICE_MARKER, engine._unavailable_models)


class TestSenseVoiceAdapterEnabled(unittest.TestCase):
    """SenseVoice участвует в chain когда включён.

    Реализация вставляет SENSEVOICE_MARKER на позицию 1 в candidates:
    [balanced_whisper, SENSEVOICE_MARKER, ...остальные max-кандидаты].
    SenseVoice пробуется только после того, как balanced_whisper недоступен.
    """

    @patch("core.engine.settings")
    def test_sensevoice_reached_when_balanced_unavailable(self, mock_settings: Any) -> None:
        """Когда balanced whisper помечен недоступным — SenseVoice успешно транскрибирует."""
        mock_settings.SENSEVOICE_ENABLED = True
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]

        engine = AudioEngine.__new__(AudioEngine)
        engine._router = None
        engine.quality_profile = "balanced"
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        # Помечаем balanced whisper как недоступный — SenseVoice должен сработать
        engine._unavailable_models = {"mlx-community/whisper-large-v3-turbo"}
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None

        engine._transcribe_sensevoice = lambda *a, **kw: {  # type: ignore[method-assign]
            "text": "привет мир",
            "engine": "sensevoice",
            "emotion": "happy",
            "language": "ru",
            "segments": [],
        }

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(result["text"], "привет мир")
        self.assertEqual(result["engine"], "sensevoice")
        self.assertEqual(result["emotion"], "happy")

    @patch("core.engine.settings")
    def test_sensevoice_marker_inserted_in_candidates(self, mock_settings: Any) -> None:
        """Когда SENSEVOICE_ENABLED=True — SENSEVOICE_MARKER вставляется на позицию 1 в chain."""
        mock_settings.SENSEVOICE_ENABLED = True
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        # max profile: 2 кандидата — marker вставится между ними
        mock_settings.model_max_list = [
            "mlx-community/whisper-large-v3-mlx",
            "mlx-community/whisper-large-v3-turbo",
        ]

        engine = AudioEngine.__new__(AudioEngine)
        engine._router = None
        engine.quality_profile = "max"
        engine.current_model = "mlx-community/whisper-large-v3-mlx"
        engine._unavailable_models = set()
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None

        visited: list[str] = []

        def fake_transcribe_model(audio_data: Any, model_name: str, prompt: str, language: Any = None) -> dict:
            visited.append(model_name)
            raise RuntimeError("VRAM out")  # все whisper-кандидаты падают

        engine._transcribe_model = fake_transcribe_model  # type: ignore[method-assign]
        # SenseVoice тоже падает чтобы не прерывать chain
        engine._transcribe_sensevoice = MagicMock(side_effect=RuntimeError("funasr not installed"))  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.engine._get_available_memory_gb", return_value=16.0):
                with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                    mock_pool = MagicMock()
                    mock_pool_cls.return_value.__enter__ = lambda s: mock_pool
                    mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
                    mock_future = MagicMock()
                    mock_future.result.side_effect = RuntimeError("VRAM out")
                    mock_pool.submit.return_value = mock_future
                    with self.assertRaises(RuntimeError):
                        engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        # SenseVoice marker должен быть помечен недоступным после сбоя
        self.assertIn(engine._SENSEVOICE_MARKER, engine._unavailable_models)

    @patch("core.engine.settings")
    def test_sensevoice_marker_not_retried_after_failure(self, mock_settings: Any) -> None:
        """Если SenseVoice однажды упал — он не вставляется в chain повторно."""
        mock_settings.SENSEVOICE_ENABLED = True
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]

        engine = AudioEngine.__new__(AudioEngine)
        engine._router = None
        engine.quality_profile = "balanced"
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        # Маркер уже помечен недоступным после предыдущего сбоя
        engine._unavailable_models = {engine._SENSEVOICE_MARKER}
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None

        sv_call_count = []
        engine._transcribe_sensevoice = lambda *a, **kw: sv_call_count.append(True) or {}  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool_cls.return_value.__enter__ = lambda s: mock_pool
                mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_future = MagicMock()
                mock_future.result.return_value = {"text": "вискер", "segments": [], "language": "ru"}
                mock_pool.submit.return_value = mock_future
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(len(sv_call_count), 0, "SenseVoice не должен вызываться если маркер уже недоступен")


class TestSenseVoiceLoadNoFunASR(unittest.TestCase):
    """_load_sensevoice_model корректно обрабатывает отсутствие funasr."""

    def test_load_raises_when_funasr_missing(self) -> None:
        """Без funasr _load_sensevoice_model поднимает RuntimeError."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None

        with patch("core.engine._SenseVoiceAutoModel", None):
            with self.assertRaises(RuntimeError) as ctx:
                engine._load_sensevoice_model()
        self.assertIn("funasr", str(ctx.exception).lower())
        # Ошибка кэшируется для последующих вызовов
        self.assertIsNotNone(engine._sensevoice_load_error)

    def test_load_raises_from_cache_on_second_call(self) -> None:
        """После первого сбоя _load_sensevoice_model сразу поднимает RuntimeError без попытки загрузки."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._sensevoice_model = None
        engine._sensevoice_load_error = "funasr не установлен"

        load_attempts = []

        with patch("core.engine._SenseVoiceAutoModel") as mock_cls:
            mock_cls.side_effect = lambda **kw: load_attempts.append(True)
            with self.assertRaises(RuntimeError):
                engine._load_sensevoice_model()

        self.assertEqual(len(load_attempts), 0, "Повторная загрузка не должна происходить")


class TestSenseVoiceEmotionTagParsing(unittest.TestCase):
    """_parse_sensevoice_output корректно извлекает emotion из inline-тегов."""

    def test_emotion_extracted_from_text_tags(self) -> None:
        """FunASR кодирует эмоцию в теги внутри текста; статический парсер должен их извлечь."""
        text = "<|HAPPY|><|ru|><|Speech|>привет мир"
        clean, emotion, lang = AudioEngine._parse_sensevoice_output(text)
        self.assertEqual(clean, "привет мир")
        self.assertEqual(emotion, "happy")
        self.assertEqual(lang, "ru")

    def test_neutral_emotion_tag(self) -> None:
        clean, emotion, lang = AudioEngine._parse_sensevoice_output("<|NEUTRAL|><|en|>hello world")
        self.assertEqual(clean, "hello world")
        self.assertEqual(emotion, "neutral")
        self.assertEqual(lang, "en")

    def test_no_emotion_tag_returns_none(self) -> None:
        clean, emotion, lang = AudioEngine._parse_sensevoice_output("<|ru|>просто текст")
        self.assertEqual(clean, "просто текст")
        self.assertIsNone(emotion)
        self.assertEqual(lang, "ru")

    def test_empty_string_returns_empty(self) -> None:
        clean, emotion, lang = AudioEngine._parse_sensevoice_output("")
        self.assertEqual(clean, "")
        self.assertIsNone(emotion)
        self.assertIsNone(lang)


class TestHistoryItemEmotionField(unittest.TestCase):
    """HistoryItem поддерживает поле emotion без ошибок сериализации."""

    def test_create_with_emotion(self) -> None:
        item = HistoryItem.create(text="тест", emotion="happy")
        self.assertEqual(item.emotion, "happy")

    def test_create_without_emotion_defaults_none(self) -> None:
        item = HistoryItem.create(text="тест")
        self.assertIsNone(item.emotion)

    def test_to_dict_includes_emotion(self) -> None:
        item = HistoryItem.create(text="тест", emotion="neutral")
        d = item.to_dict()
        self.assertIn("emotion", d)
        self.assertEqual(d["emotion"], "neutral")

    def test_from_dict_roundtrip(self) -> None:
        item = HistoryItem.create(text="тест", emotion="sad")
        d = item.to_dict()
        restored = HistoryItem.from_dict(d)
        self.assertEqual(restored.emotion, "sad")

    def test_from_dict_legacy_no_emotion_field(self) -> None:
        """Старые записи без поля emotion загружаются без ошибок (emotion=None)."""
        payload = {
            "id": "abc",
            "ts": "2026-01-01T00:00:00",
            "text": "старая запись",
        }
        item = HistoryItem.from_dict(payload)
        self.assertIsNone(item.emotion)


if __name__ == "__main__":
    unittest.main()
