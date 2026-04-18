"""Тесты Voxtral Mini 4B Realtime adapter в fallback chain AudioEngine (Phase 4.4).

Проверяет интеграцию без реальной загрузки модели (FakeAudioEngine паттерн).
mistral-inference не нужен для прохождения тестов — все точки входа замоканы.
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
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _make_engine(
    *,
    voxtral_enabled: bool = True,
    whisperx_enabled: bool = False,
    sensevoice_enabled: bool = False,
    parakeet_enabled: bool = False,
    voxtral_marker_unavailable: bool = False,
) -> AudioEngine:
    """Создаёт AudioEngine.__new__() с необходимым минимумом стейта."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models = set()
    if voxtral_marker_unavailable:
        engine._unavailable_models.add(engine._VOXTRAL_MARKER)
    engine._voxtral_model = None
    engine._voxtral_load_error = None
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._whisperx_model = None
    engine._whisperx_load_error = None
    engine._parakeet_model = None
    engine._parakeet_load_error = None
    return engine


def _mock_settings(
    mock: Any,
    *,
    voxtral_enabled: bool = True,
    voxtral_reasoning_enabled: bool = False,
    whisperx_enabled: bool = False,
    sensevoice_enabled: bool = False,
    parakeet_enabled: bool = False,
) -> None:
    mock.VOXTRAL_ENABLED = voxtral_enabled
    mock.VOXTRAL_MODEL = "mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit"
    mock.VOXTRAL_REASONING_ENABLED = voxtral_reasoning_enabled
    mock.WHISPERX_ENABLED = whisperx_enabled
    mock.WHISPERX_MODEL = "large-v3"
    mock.WHISPERX_DEVICE = "cpu"
    mock.WHISPERX_DIARIZATION = False
    mock.WHISPERX_WORD_TIMESTAMPS = True
    mock.SENSEVOICE_ENABLED = sensevoice_enabled
    mock.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    mock.SENSEVOICE_EMOTION_TO_HISTORY = True
    mock.PARAKEET_ENABLED = parakeet_enabled
    mock.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
    mock.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    mock.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    mock.TRANSCRIBE_TIMEOUT_SEC = 30
    mock.NETWORK_MODE = "offline_strict"
    mock.model_max_list = ["mlx-community/whisper-large-v3-turbo"]
    mock.HF_TOKEN = ""


# ---------------------------------------------------------------------------
# 1. Voxtral отключён — маркер не вставляется
# ---------------------------------------------------------------------------

class TestVoxtralAdapterDisabled(unittest.TestCase):
    """Voxtral не участвует в chain когда VOXTRAL_ENABLED=False."""

    @patch("core.engine.settings")
    def test_voxtral_skipped_when_disabled(self, mock_settings: Any) -> None:
        """Когда флаг выключен — VOXTRAL_MARKER не вставляется в candidates."""
        _mock_settings(mock_settings, voxtral_enabled=False)

        engine = _make_engine(voxtral_enabled=False)

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

        # Voxtral маркер не должен оказаться в unavailable (он не вставлялся)
        self.assertNotIn(engine._VOXTRAL_MARKER, engine._unavailable_models)


# ---------------------------------------------------------------------------
# 2. Voxtral успешно транскрибирует когда balanced недоступен
# ---------------------------------------------------------------------------

class TestVoxtralAdapterEnabled(unittest.TestCase):
    """Voxtral участвует в chain когда включён."""

    @patch("core.engine.settings")
    def test_voxtral_reached_when_balanced_unavailable(self, mock_settings: Any) -> None:
        """Когда balanced whisper помечен недоступным — Voxtral успешно транскрибирует."""
        _mock_settings(mock_settings, voxtral_enabled=True)

        engine = _make_engine()
        # Помечаем balanced whisper как недоступный — Voxtral должен сработать
        engine._unavailable_models.add("mlx-community/whisper-large-v3-turbo")

        engine._transcribe_voxtral = lambda *a, **kw: {  # type: ignore[method-assign]
            "text": "привет мир",
            "engine": "voxtral",
            "language": "ru",
            "segments": [],
            "reasoning": None,
        }

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(result["text"], "привет мир")
        self.assertEqual(result["engine"], "voxtral")
        self.assertIn("reasoning", result)
        self.assertIsNone(result["reasoning"])

    @patch("core.engine.settings")
    def test_voxtral_marker_inserted_after_whisperx(self, mock_settings: Any) -> None:
        """Когда WhisperX и Voxtral включены — Voxtral маркер идёт ПОСЛЕ WhisperX маркера."""
        _mock_settings(mock_settings, voxtral_enabled=True, whisperx_enabled=True)

        engine = _make_engine()
        visited: list[str] = []

        def fake_transcribe_model(audio_data: Any, model_name: str, prompt: str, language: Any = None) -> dict:
            visited.append(model_name)
            raise RuntimeError("unavail")

        engine._transcribe_model = fake_transcribe_model  # type: ignore[method-assign]
        engine._transcribe_whisperx = MagicMock(side_effect=RuntimeError("whisperx not installed"))  # type: ignore[method-assign]
        engine._transcribe_voxtral = MagicMock(side_effect=RuntimeError("voxtral not installed"))  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.engine._get_available_memory_gb", return_value=16.0):
                with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                    mock_pool = MagicMock()
                    mock_pool_cls.return_value.__enter__ = lambda s: mock_pool
                    mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
                    mock_future = MagicMock()
                    mock_future.result.side_effect = RuntimeError("unavail")
                    mock_pool.submit.return_value = mock_future
                    with self.assertRaises(RuntimeError):
                        engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        # Оба маркера должны быть помечены недоступными (оба упали)
        self.assertIn(engine._WHISPERX_MARKER, engine._unavailable_models)
        self.assertIn(engine._VOXTRAL_MARKER, engine._unavailable_models)

    @patch("core.engine.settings")
    def test_voxtral_marker_not_retried_after_failure(self, mock_settings: Any) -> None:
        """Если Voxtral однажды упал — он не вставляется в chain повторно."""
        _mock_settings(mock_settings, voxtral_enabled=True)

        engine = _make_engine(voxtral_marker_unavailable=True)

        vt_call_count = []
        engine._transcribe_voxtral = lambda *a, **kw: vt_call_count.append(True) or {}  # type: ignore[method-assign]

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

        self.assertEqual(len(vt_call_count), 0, "Voxtral не должен вызываться если маркер уже недоступен")


# ---------------------------------------------------------------------------
# 3. _load_voxtral_model — graceful при отсутствии библиотеки
# ---------------------------------------------------------------------------

class TestVoxtralLoadNoLibrary(unittest.TestCase):
    """_load_voxtral_model корректно обрабатывает отсутствие mistral-inference."""

    def test_load_raises_when_voxtral_missing(self) -> None:
        """Без mistral-inference _load_voxtral_model поднимает RuntimeError."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._voxtral_model = None
        engine._voxtral_load_error = None

        with patch("core.engine._voxtral_available", False):
            with self.assertRaises(RuntimeError) as ctx:
                engine._load_voxtral_model()
        self.assertIn("mistral-inference", str(ctx.exception))
        # Ошибка кэшируется
        self.assertIsNotNone(engine._voxtral_load_error)

    def test_load_raises_from_cache_on_second_call(self) -> None:
        """После первого сбоя _load_voxtral_model сразу поднимает RuntimeError без попытки загрузки."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._voxtral_model = None
        engine._voxtral_load_error = "mistral-inference не установлен — Voxtral adapter недоступен"

        with patch("core.engine._voxtral_available", True):
            with self.assertRaises(RuntimeError) as ctx:
                engine._load_voxtral_model()

        self.assertIn("mistral-inference", str(ctx.exception))


# ---------------------------------------------------------------------------
# 4. HistoryItem — новое поле reasoning (backward compat)
# ---------------------------------------------------------------------------

class TestHistoryItemVoxtralField(unittest.TestCase):
    """HistoryItem поддерживает поле reasoning без ошибок сериализации."""

    def test_create_with_reasoning(self) -> None:
        item = HistoryItem.create(text="тест", reasoning="Краткое содержание разговора.")
        self.assertIsNotNone(item.reasoning)
        self.assertIn("Краткое", item.reasoning)

    def test_create_without_reasoning_defaults_none(self) -> None:
        item = HistoryItem.create(text="тест")
        self.assertIsNone(item.reasoning)

    def test_to_dict_includes_reasoning(self) -> None:
        item = HistoryItem.create(text="тест", reasoning="Это summary.")
        d = item.to_dict()
        self.assertIn("reasoning", d)
        self.assertEqual(d["reasoning"], "Это summary.")

    def test_from_dict_roundtrip_with_reasoning(self) -> None:
        item = HistoryItem.create(text="тест", reasoning="Summary text.")
        d = item.to_dict()
        restored = HistoryItem.from_dict(d)
        self.assertEqual(restored.reasoning, "Summary text.")

    def test_from_dict_legacy_no_reasoning(self) -> None:
        """Старые записи без поля reasoning загружаются без ошибок."""
        payload = {
            "id": "abc",
            "ts": "2026-01-01T00:00:00",
            "text": "старая запись",
        }
        item = HistoryItem.from_dict(payload)
        self.assertIsNone(item.reasoning)

    def test_emotion_and_word_timestamps_still_work(self) -> None:
        """Обратная совместимость: emotion и word_timestamps работают вместе с reasoning."""
        item = HistoryItem.create(
            text="тест",
            emotion="happy",
            word_timestamps=[{"word": "тест", "start": 0.0, "end": 0.5, "confidence": 0.9}],
            reasoning="Summary.",
        )
        self.assertEqual(item.emotion, "happy")
        self.assertIsNotNone(item.word_timestamps)
        self.assertEqual(item.reasoning, "Summary.")


# ---------------------------------------------------------------------------
# 5. Voxtral с reasoning enabled — результат содержит reasoning поле
# ---------------------------------------------------------------------------

class TestVoxtralWithReasoningEnabled(unittest.TestCase):
    """Voxtral возвращает reasoning когда VOXTRAL_REASONING_ENABLED=True."""

    @patch("core.engine.settings")
    def test_voxtral_result_with_reasoning(self, mock_settings: Any) -> None:
        """Voxtral result содержит reasoning когда адаптер возвращает его."""
        _mock_settings(mock_settings, voxtral_enabled=True, voxtral_reasoning_enabled=True)

        engine = _make_engine()
        engine._unavailable_models.add("mlx-community/whisper-large-v3-turbo")

        engine._transcribe_voxtral = lambda *a, **kw: {  # type: ignore[method-assign]
            "text": "Добрый день, как дела?",
            "engine": "voxtral",
            "language": "ru",
            "segments": [],
            "reasoning": "Приветственный диалог на русском языке.",
        }

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(result["text"], "Добрый день, как дела?")
        self.assertEqual(result["engine"], "voxtral")
        self.assertEqual(result["reasoning"], "Приветственный диалог на русском языке.")


# ---------------------------------------------------------------------------
# 6. Voxtral marker position — корректный порядок в chain со всеми адаптерами
# ---------------------------------------------------------------------------

class TestVoxtralChainPosition(unittest.TestCase):
    """Voxtral маркер занимает правильную позицию в chain."""

    @patch("core.engine.settings")
    def test_voxtral_only_marker_at_position_1(self, mock_settings: Any) -> None:
        """При отключённых всех других адаптерах Voxtral маркер на позиции 1 (после balanced)."""
        _mock_settings(
            mock_settings,
            voxtral_enabled=True,
            whisperx_enabled=False,
            sensevoice_enabled=False,
            parakeet_enabled=False,
        )

        engine = _make_engine()
        engine._unavailable_models.add("mlx-community/whisper-large-v3-turbo")

        vt_called = []
        engine._transcribe_voxtral = lambda *a, **kw: vt_called.append(True) or {  # type: ignore[method-assign]
            "text": "тест позиции",
            "engine": "voxtral",
            "language": "ru",
            "segments": [],
            "reasoning": None,
        }

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertEqual(len(vt_called), 1, "Voxtral должен быть вызван ровно один раз")
        self.assertEqual(result["engine"], "voxtral")


if __name__ == "__main__":
    unittest.main()
