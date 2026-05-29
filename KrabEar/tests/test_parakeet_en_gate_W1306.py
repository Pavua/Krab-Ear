"""W1306 — Parakeet language gate: вставляется только для "en" / "auto".

W1303 F3 MED: до фикса Parakeet вставлялся в chain безусловно, независимо
от языка. Для RU/ES это возвращает мусорный текст и молча останавливает chain
(нет исключения → "успешно"). Фикс добавляет гейт аналогичный GigaAM:
  _effective_lang in {"en", "auto"}

Тесты проверяют четыре сценария по ТЗ:
  1. test_parakeet_inserted_for_en_audio        — вставляется для "en"
  2. test_parakeet_inserted_for_auto_lang        — вставляется для "auto"
  3. test_parakeet_skipped_for_ru_audio          — пропускается для "ru"
  4. test_parakeet_skipped_for_es_audio          — пропускается для "es"

Паттерн: AudioEngine.__new__() без __init__, прямой вызов
_transcribe_with_fallback_impl — воспроизводит существующий
test_parakeet_adapter.py / test_sensevoice_adapter.py.
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


def _make_engine(parakeet_enabled: bool = True) -> AudioEngine:
    """Создаёт минимальный engine-stub без __init__."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models = {}
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._parakeet_model = None
    engine._parakeet_load_error = None
    engine._router = None  # GigaAM adapter gate: router=None → GigaAM skipped
    engine._skip_gigaam = True  # дополнительная защита от GigaAM
    return engine


def _make_settings(parakeet_enabled: bool = True) -> MagicMock:
    """Создаёт mock settings с минимально нужными полями."""
    mock_settings = MagicMock()
    mock_settings.PARAKEET_ENABLED = parakeet_enabled
    mock_settings.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
    mock_settings.SENSEVOICE_ENABLED = False
    mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
    mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    mock_settings.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
    mock_settings.NETWORK_MODE = "offline_strict"
    mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]
    mock_settings.STT_USE_RU_FINETUNE = False
    mock_settings.STT_GIGAAM_ENABLED = False
    mock_settings.TRANSCRIBE_LANGUAGE = "auto"
    mock_settings.WHISPERX_ENABLED = False
    mock_settings.VOXTRAL_ENABLED = False
    mock_settings.VOXTRAL_MODEL = "mistralai/Voxtral-Mini-3B-2507"
    return mock_settings


def _candidates_for_lang(engine: AudioEngine, mock_settings: Any, language: str) -> list[str]:
    """Прогоняет _transcribe_with_fallback_impl до ThreadPoolExecutor и возвращает
    зафиксированный список кандидатов через перехват вызова адаптера.

    Стратегия: помечаем balanced whisper «недоступным» и ставим
    _transcribe_parakeet = stub. Если Parakeet вставлен — stub вызывается и
    мы фиксируем "parakeet" в visited. Если не вставлен — не вызывается.
    """
    visited: list[str] = []

    def fake_parakeet(*a: Any, **kw: Any) -> dict:
        visited.append("parakeet")
        return {"text": "hello", "engine": "parakeet", "language": "en", "segments": []}

    engine._transcribe_parakeet = fake_parakeet  # type: ignore[method-assign]
    # Помечаем balanced whisper недоступным — chain вынужден пробовать адаптеры
    engine._unavailable_models = {"mlx-community/whisper-large-v3-turbo"}

    with patch("core.engine.settings", mock_settings):
        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            try:
                engine._transcribe_with_fallback_impl(b"\x00" * 320, "", language)
            except Exception:
                pass  # ожидаем падение (нет реального whisper) — нас интересует только visited

    return visited


class TestParakeetInsertedForEnAudio(unittest.TestCase):
    """test_parakeet_inserted_for_en_audio — Parakeet должен войти в chain для "en"."""

    @patch("core.engine.settings")
    def test_parakeet_inserted_for_en_audio(self, mock_settings_global: Any) -> None:
        mock_settings = _make_settings(parakeet_enabled=True)
        engine = _make_engine()

        visited = _candidates_for_lang(engine, mock_settings, language="en")

        self.assertIn(
            "parakeet",
            visited,
            "PARAKEET_MARKER должен вставляться в chain когда язык='en'",
        )


class TestParakeetInsertedForAutoLang(unittest.TestCase):
    """test_parakeet_inserted_for_auto_lang — Parakeet должен войти в chain для "auto"."""

    @patch("core.engine.settings")
    def test_parakeet_inserted_for_auto_lang(self, mock_settings_global: Any) -> None:
        mock_settings = _make_settings(parakeet_enabled=True)
        mock_settings.TRANSCRIBE_LANGUAGE = "auto"
        engine = _make_engine()

        visited = _candidates_for_lang(engine, mock_settings, language="auto")

        self.assertIn(
            "parakeet",
            visited,
            "PARAKEET_MARKER должен вставляться в chain когда язык='auto'",
        )


class TestParakeetSkippedForRuAudio(unittest.TestCase):
    """test_parakeet_skipped_for_ru_audio — Parakeet НЕ должен входить в chain для "ru"."""

    @patch("core.engine.settings")
    def test_parakeet_skipped_for_ru_audio(self, mock_settings_global: Any) -> None:
        mock_settings = _make_settings(parakeet_enabled=True)
        engine = _make_engine()

        visited = _candidates_for_lang(engine, mock_settings, language="ru")

        self.assertNotIn(
            "parakeet",
            visited,
            "PARAKEET_MARKER НЕ должен вставляться в chain для языка 'ru' — "
            "Parakeet EN-only, возвращает мусор на русской речи (W1303 F3)",
        )


class TestParakeetSkippedForEsAudio(unittest.TestCase):
    """test_parakeet_skipped_for_es_audio — Parakeet НЕ должен входить в chain для "es"."""

    @patch("core.engine.settings")
    def test_parakeet_skipped_for_es_audio(self, mock_settings_global: Any) -> None:
        mock_settings = _make_settings(parakeet_enabled=True)
        engine = _make_engine()

        visited = _candidates_for_lang(engine, mock_settings, language="es")

        self.assertNotIn(
            "parakeet",
            visited,
            "PARAKEET_MARKER НЕ должен вставляться в chain для языка 'es' — "
            "Parakeet EN-only, возвращает мусор на испанской речи (W1303 F3)",
        )


if __name__ == "__main__":
    unittest.main()
