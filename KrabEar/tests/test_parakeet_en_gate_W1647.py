"""W1647 — Parakeet EN-only language gate in STT chain-building.

W1644 F4 MED: до фикса Parakeet вставлялся в chain для ЛЮБОГО языка.
На RU/ES аудио NVIDIA Parakeet-TDT возвращает мусор, что может прервать chain
(нет исключения → chain думает, что успешно завершился). GigaAM имеет корректный
_effective_lang == "ru" gate; Parakeet требует аналогичный _effective_lang == "en".

Тесты проверяют четыре сценария:
  1. test_parakeet_in_chain_for_en_audio       — вставляется только для "en"
  2. test_parakeet_excluded_from_chain_for_ru_audio — исключается для "ru"
  3. test_parakeet_excluded_for_es_audio       — исключается для "es"
  4. test_gigaam_ru_gate_unchanged             — GigaAM по-прежнему только для "ru"

Стратегия: создаём минимальный engine stub через __new__() (без __init__),
вызываем _transcribe_with_fallback_impl с языком, перехватываем список candidates
через side_effect на первом adapter_fn вызове. Паттерн аналогичен
test_parakeet_adapter.py и test_sensevoice_adapter.py.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine() -> AudioEngine:
    """Минимальный engine stub без __init__, все адаптеры выключены по умолчанию."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models: dict[str, float] = {}  # пустой — всё доступно
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._parakeet_model = None
    engine._parakeet_load_error = None
    engine._router = None
    engine._skip_gigaam = True
    return engine


def _make_settings(
    parakeet_enabled: bool = True,
    gigaam_enabled: bool = False,
    language: str = "en",
) -> MagicMock:
    """Mock settings с полями, используемыми в chain-building."""
    s = MagicMock()
    s.PARAKEET_ENABLED = parakeet_enabled
    s.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
    s.SENSEVOICE_ENABLED = False
    s.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    s.SENSEVOICE_EMOTION_TO_HISTORY = True
    s.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    s.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    s.TRANSCRIBE_TIMEOUT_SEC = 30
    s.NETWORK_MODE = "offline_strict"
    s.model_max_list = ["mlx-community/whisper-large-v3-turbo"]
    s.STT_USE_RU_FINETUNE = False
    s.STT_GIGAAM_ENABLED = gigaam_enabled
    s.STT_GIGAAM_MODE = "rnnt"
    s.STT_RU_FINETUNE_MODEL = "antony66/whisper-large-v3-russian"
    s.TRANSCRIBE_LANGUAGE = language
    s.WHISPERX_ENABLED = False
    s.VOXTRAL_ENABLED = False
    s.VOXTRAL_MODEL = "mistralai/Voxtral-Mini-3B-2507"
    s.WHISPERX_MODEL = "guillaumekln/faster-whisper-large-v3"
    return s


def _get_candidates(engine: AudioEngine, mock_settings: Any, language: str) -> list[str]:
    """Возвращает список кандидатов chain, зафиксированный через StopIteration.

    Патчит _profiler.start_span как context-manager, перехватывает первую итерацию
    цикла `for model_name in candidates` через side_effect на _profiler.start_span,
    которая пишет span_name и бросает StopIteration → выход из цикла.

    Альтернативный подход: перехватываем вызов через патч self._transcribe_parakeet /
    _transcribe_gigaam / _transcribe_model — side_effect записывает имя и бросает
    RuntimeError → chain помечает маркер недоступным, продолжает, затем падает
    на _transcribe_model (whisper) и мы видим visited set.

    Здесь используем более прямой подход: патчим _transcribe_with_fallback_impl
    чтобы захватить candidates перед dispatch loop.
    """
    captured: list[str] = []

    original_impl = AudioEngine._transcribe_with_fallback_impl

    def capturing_impl(self_inner: AudioEngine, audio_data: Any, prompt: str, language_arg: str | None = None) -> dict[str, Any]:
        """Вызывает оригинальный impl но перехватывает candidates через side-effect."""
        # Вызываем оригинал — он упадёт, но нас интересует какие адаптеры были вызваны.
        # Вместо этого используем более чистый путь: патчим adapter-функции.
        raise StopIteration("capture_done")

    # Прямой подход: перехватываем вставку в chain через side_effect на каждой из
    # adapter функций, но нам нужно знать какие маркеры вошли в candidates ПЕРЕД
    # dispatch. Лучший способ — патчить сам for-loop через замену _adapter_map.
    #
    # Самый надёжный вариант: вызываем _transcribe_with_fallback_impl с поддельным
    # audio_data и перехватываем обращение к _is_model_unavailable для каждого маркера.

    _PARAKEET_MARKER = AudioEngine._PARAKEET_MARKER
    _GIGAAM_MARKER = AudioEngine._GIGAAM_MARKER

    parakeet_queried = [False]  # был ли PARAKEET_MARKER в candidates

    original_is_unavail = engine._is_model_unavailable

    def tracking_is_unavailable(model_id: str) -> bool:
        if model_id == _PARAKEET_MARKER:
            parakeet_queried[0] = True
        return original_is_unavail(model_id)

    engine._is_model_unavailable = tracking_is_unavailable  # type: ignore[method-assign]

    with patch("core.engine.settings", mock_settings):
        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            try:
                engine._transcribe_with_fallback_impl(b"\x00" * 320, "", language)
            except Exception:
                pass

    # Сбрасываем patched метод
    del engine._is_model_unavailable  # type: ignore[attr-defined]

    if parakeet_queried[0]:
        captured.append(AudioEngine._PARAKEET_MARKER)

    return captured


def _parakeet_in_chain(engine: AudioEngine, mock_settings: Any, language: str) -> bool:
    """True если PARAKEET_MARKER вошёл в candidates chain для данного языка."""
    # Используем самый прямой способ: вызываем _transcribe_with_fallback_impl,
    # перехватываем _transcribe_parakeet (если она будет вызвана, маркер был в chain).
    parakeet_called = [False]

    def fake_parakeet(*a: Any, **kw: Any) -> dict:
        parakeet_called[0] = True
        # Возвращаем успешный результат → chain завершается здесь
        return {"text": "hello en", "engine": "parakeet", "language": "en", "segments": []}

    engine._transcribe_parakeet = fake_parakeet  # type: ignore[method-assign]
    # Маркируем whisper как "недоступный через свежий timestamp"
    engine._unavailable_models = {
        "mlx-community/whisper-large-v3-turbo": time.monotonic(),
    }

    with patch("core.engine.settings", mock_settings):
        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            try:
                engine._transcribe_with_fallback_impl(b"\x00" * 320, "", language)
            except Exception:
                pass

    return parakeet_called[0]


def _gigaam_in_chain(engine: AudioEngine, mock_settings: Any, language: str) -> bool:
    """True если GIGAAM_MARKER вошёл в candidates chain для данного языка."""
    gigaam_called = [False]

    def fake_gigaam(*a: Any, **kw: Any) -> dict:
        gigaam_called[0] = True
        return {"text": "привет ру", "engine": "gigaam", "language": "ru", "segments": []}

    engine._transcribe_gigaam = fake_gigaam  # type: ignore[method-assign]
    engine._unavailable_models = {
        "mlx-community/whisper-large-v3-turbo": time.monotonic(),
    }

    with patch("core.engine.settings", mock_settings):
        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            try:
                engine._transcribe_with_fallback_impl(b"\x00" * 320, "", language)
            except Exception:
                pass

    return gigaam_called[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParakeetInChainForEnAudio(unittest.TestCase):
    """Parakeet должен вставляться в chain только если язык "en"."""

    def test_parakeet_in_chain_for_en_audio(self) -> None:
        engine = _make_engine()
        settings = _make_settings(parakeet_enabled=True, language="en")

        result = _parakeet_in_chain(engine, settings, language="en")

        self.assertTrue(
            result,
            "PARAKEET_MARKER должен входить в chain когда _effective_lang='en' и PARAKEET_ENABLED=True",
        )


class TestParakeetExcludedFromChainForRuAudio(unittest.TestCase):
    """Parakeet НЕ должен вставляться в chain для языка "ru" — EN-only модель."""

    def test_parakeet_excluded_from_chain_for_ru_audio(self) -> None:
        engine = _make_engine()
        settings = _make_settings(parakeet_enabled=True, language="ru")

        result = _parakeet_in_chain(engine, settings, language="ru")

        self.assertFalse(
            result,
            "PARAKEET_MARKER НЕ должен входить в chain для 'ru' — Parakeet EN-only, "
            "на русской речи возвращает мусор и молча останавливает chain (W1644 F4 MED)",
        )


class TestParakeetExcludedForEsAudio(unittest.TestCase):
    """Parakeet НЕ должен вставляться в chain для языка "es"."""

    def test_parakeet_excluded_for_es_audio(self) -> None:
        engine = _make_engine()
        settings = _make_settings(parakeet_enabled=True, language="es")

        result = _parakeet_in_chain(engine, settings, language="es")

        self.assertFalse(
            result,
            "PARAKEET_MARKER НЕ должен входить в chain для 'es' — Parakeet EN-only, "
            "на испанской речи возвращает мусор (W1644 F4 MED)",
        )


class TestGigaAmRuGateUnchanged(unittest.TestCase):
    """GigaAM gate (_effective_lang == 'ru') должен оставаться нетронутым.

    Проверяем что:
    - GigaAM вставляется для "ru" (если router возвращает адаптер)
    - GigaAM НЕ вставляется для "en"
    """

    def test_gigaam_inserted_for_ru(self) -> None:
        """GigaAM должен входить в chain для языка 'ru' если enabled и adapter доступен."""
        engine = _make_engine()
        engine._skip_gigaam = False  # разрешаем GigaAM

        # Создаём mock router с get_gigaam_adapter()
        mock_router = MagicMock()
        mock_gigaam_adapter = MagicMock()
        mock_router.get_gigaam_adapter.return_value = mock_gigaam_adapter
        engine._router = mock_router

        settings = _make_settings(gigaam_enabled=True, parakeet_enabled=False, language="ru")

        result = _gigaam_in_chain(engine, settings, language="ru")

        self.assertTrue(
            result,
            "GIGAAM_MARKER должен входить в chain для 'ru' когда STT_GIGAAM_ENABLED=True и router.get_gigaam_adapter() != None",
        )

    def test_gigaam_excluded_for_en(self) -> None:
        """GigaAM НЕ должен входить в chain для языка 'en' — RU-only модель."""
        engine = _make_engine()
        engine._skip_gigaam = False  # не блокируем GigaAM (gate по языку должен сам не пустить)

        mock_router = MagicMock()
        mock_gigaam_adapter = MagicMock()
        mock_router.get_gigaam_adapter.return_value = mock_gigaam_adapter
        engine._router = mock_router

        settings = _make_settings(gigaam_enabled=True, parakeet_enabled=False, language="en")

        result = _gigaam_in_chain(engine, settings, language="en")

        self.assertFalse(
            result,
            "GIGAAM_MARKER НЕ должен входить в chain для 'en' — GigaAM RU-only gate должен быть нетронутым",
        )


if __name__ == "__main__":
    unittest.main()
