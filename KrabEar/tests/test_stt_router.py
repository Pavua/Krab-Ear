"""Тесты для STTRouter (KrabEar/core/stt_router.py).

Проверяет маршрутизацию на языково-специализированные STT модели:
- routing disabled → всегда OTHER_PRIMARY
- hint_language явный → правильная модель
- неизвестный hint → OTHER_PRIMARY
- audio detection placeholder → RU model
- пустой/тихий аудио → fallback
- исключение в adapter_factory → fallback OTHER_PRIMARY
- adapter_factory вызывается с правильным model_id
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock

import numpy as np

# Путь к корню проекта чтобы импорты работали из test discovery
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.stt_router import STTRouter  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательный stub Settings
# ---------------------------------------------------------------------------

class _FakeSettings:
    """Duck-typed stub конфига для тестов."""

    STT_LANGUAGE_ROUTING_ENABLED: bool = False
    STT_RU_PRIMARY_MODEL: str = "model/ru-specialist"
    STT_EN_PRIMARY_MODEL: str = "model/en-specialist"
    STT_ES_PRIMARY_MODEL: str = "model/es-specialist"
    STT_OTHER_PRIMARY_MODEL: str = "model/other-generalist"


def _make_settings(**overrides) -> _FakeSettings:
    s = _FakeSettings()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _silence_audio(seconds: float = 1.0, sr: int = 16000) -> np.ndarray:
    """Генерирует тишину (near-zero PCM)."""
    return np.zeros(int(seconds * sr), dtype=np.float32)


def _speech_audio(seconds: float = 1.0, sr: int = 16000) -> np.ndarray:
    """Генерирует синус-сигнал имитирующий ненулевое аудио."""
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    return (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestSTTRouterDisabled(unittest.TestCase):
    """routing disabled → всегда OTHER_PRIMARY."""

    def setUp(self):
        self.settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=False)
        self.router = STTRouter(self.settings)

    def test_disabled_no_hint_returns_other(self):
        result = self.router.select_model(
            audio_data=_speech_audio(), sample_rate=16000, hint_language=None
        )
        self.assertEqual(result, "model/other-generalist")

    def test_disabled_with_ru_hint_still_returns_other(self):
        """Когда routing выключен, hint_language игнорируется."""
        result = self.router.select_model(
            audio_data=_speech_audio(), sample_rate=16000, hint_language="ru"
        )
        self.assertEqual(result, "model/other-generalist")

    def test_disabled_with_none_audio_returns_other(self):
        result = self.router.select_model(
            audio_data=None, sample_rate=16000, hint_language=None
        )
        self.assertEqual(result, "model/other-generalist")


class TestSTTRouterHintLanguage(unittest.TestCase):
    """hint_language явный → правильная модель."""

    def setUp(self):
        self.settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)
        self.router = STTRouter(self.settings)

    def test_hint_ru_returns_ru_model(self):
        result = self.router.select_model(hint_language="ru")
        self.assertEqual(result, "model/ru-specialist")

    def test_hint_en_returns_en_model(self):
        result = self.router.select_model(hint_language="en")
        self.assertEqual(result, "model/en-specialist")

    def test_hint_es_returns_es_model(self):
        result = self.router.select_model(hint_language="es")
        self.assertEqual(result, "model/es-specialist")

    def test_hint_unknown_lang_returns_other(self):
        """Неизвестный язык (ja, de, fr, ...) → OTHER_PRIMARY."""
        for lang in ("ja", "de", "fr", "zh", "ar", "ko"):
            with self.subTest(lang=lang):
                result = self.router.select_model(hint_language=lang)
                self.assertEqual(
                    result,
                    "model/other-generalist",
                    msg=f"Expected OTHER_PRIMARY for lang={lang!r}",
                )

    def test_hint_case_insensitive(self):
        """hint_language нечувствителен к регистру."""
        result = self.router.select_model(hint_language="RU")
        self.assertEqual(result, "model/ru-specialist")

    def test_hint_with_whitespace(self):
        """Пробелы вокруг hint_language обрезаются."""
        result = self.router.select_model(hint_language="  en  ")
        self.assertEqual(result, "model/en-specialist")

    def test_hint_uk_maps_to_ru_model(self):
        """Украинский (uk) маппится на RU модель как ближайшую."""
        result = self.router.select_model(hint_language="uk")
        self.assertEqual(result, "model/ru-specialist")


class TestSTTRouterAudioDetection(unittest.TestCase):
    """audio detection: placeholder возвращает 'ru' для ненулевого аудио."""

    def setUp(self):
        self.settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)
        self.router = STTRouter(self.settings)

    def test_speech_audio_no_hint_returns_ru(self):
        """Ненулевое аудио + нет hint → placeholder detection → RU model."""
        result = self.router.select_model(
            audio_data=_speech_audio(seconds=2.0),
            sample_rate=16000,
            hint_language=None,
        )
        self.assertEqual(result, "model/ru-specialist")

    def test_silence_audio_no_hint_returns_other(self):
        """Near-silence аудио → 'und' → OTHER_PRIMARY (нет смысла угадывать язык)."""
        result = self.router.select_model(
            audio_data=_silence_audio(seconds=2.0),
            sample_rate=16000,
            hint_language=None,
        )
        self.assertEqual(result, "model/other-generalist")

    def test_none_audio_no_hint_returns_other(self):
        """audio_data=None + нет hint → fallback на OTHER_PRIMARY."""
        result = self.router.select_model(
            audio_data=None,
            sample_rate=16000,
            hint_language=None,
        )
        self.assertEqual(result, "model/other-generalist")


class TestSTTRouterDetectionFailure(unittest.TestCase):
    """Ошибка при audio detection → fallback OTHER_PRIMARY, нет краша."""

    def setUp(self):
        self.settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)

    def test_nan_audio_does_not_crash(self):
        """NaN в аудио не вызывает исключение."""
        bad_audio = np.full(16000, float("nan"), dtype=np.float32)
        router = STTRouter(self.settings)
        # Не должно падать
        result = router.select_model(
            audio_data=bad_audio, sample_rate=16000, hint_language=None
        )
        # Либо OTHER_PRIMARY (если nan → fallback) либо RU (если nan RMS > threshold)
        self.assertIn(
            result,
            {"model/other-generalist", "model/ru-specialist"},
        )

    def test_empty_audio_array_does_not_crash(self):
        """Пустой numpy массив → fallback, нет краша."""
        router = STTRouter(self.settings)
        result = router.select_model(
            audio_data=np.array([], dtype=np.float32),
            sample_rate=16000,
            hint_language=None,
        )
        # near-zero rms → und → OTHER_PRIMARY
        self.assertEqual(result, "model/other-generalist")


class TestSTTRouterAdapterFactory(unittest.TestCase):
    """adapter_factory вызывается с правильным model_id."""

    def setUp(self):
        self.settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)

    def test_adapter_factory_called_with_correct_model_id(self):
        """select_model вызывает adapter_factory с выбранным model_id."""
        factory_mock = MagicMock(return_value=None)
        router = STTRouter(self.settings, adapter_factory=factory_mock)

        router.select_model(hint_language="ru")

        factory_mock.assert_called_once_with("model/ru-specialist")

    def test_adapter_factory_called_for_en(self):
        factory_mock = MagicMock(return_value=None)
        router = STTRouter(self.settings, adapter_factory=factory_mock)

        router.select_model(hint_language="en")

        factory_mock.assert_called_once_with("model/en-specialist")

    def test_adapter_factory_exception_falls_back_to_other(self):
        """Если adapter_factory бросает исключение → fallback OTHER_PRIMARY, нет краша."""
        def failing_factory(model_id: str):
            raise RuntimeError(f"Cannot load {model_id}")

        router = STTRouter(self.settings, adapter_factory=failing_factory)
        result = router.select_model(hint_language="ru")

        self.assertEqual(result, "model/other-generalist")

    def test_adapter_factory_not_called_when_routing_disabled(self):
        """Когда routing выключен, factory не вызывается."""
        settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=False)
        factory_mock = MagicMock()
        router = STTRouter(settings, adapter_factory=factory_mock)

        router.select_model(hint_language="ru")

        factory_mock.assert_not_called()


class TestSTTRouterConfigDefaults(unittest.TestCase):
    """Router корректно читает значения из реального core.config.settings."""

    def test_real_settings_routing_disabled_by_default(self):
        """Реальный settings: STT_LANGUAGE_ROUTING_ENABLED=False по умолчанию."""
        from core.config import settings as real_settings
        router = STTRouter(real_settings)

        # При routing=False возвращает STT_OTHER_PRIMARY_MODEL
        result = router.select_model(hint_language="ru")
        self.assertEqual(result, real_settings.STT_OTHER_PRIMARY_MODEL)

    def test_real_settings_has_all_language_models(self):
        """Реальный settings содержит все 4 language-model атрибута."""
        from core.config import settings as real_settings
        for attr in (
            "STT_RU_PRIMARY_MODEL",
            "STT_EN_PRIMARY_MODEL",
            "STT_ES_PRIMARY_MODEL",
            "STT_OTHER_PRIMARY_MODEL",
        ):
            self.assertTrue(
                hasattr(real_settings, attr),
                msg=f"settings missing attribute {attr}",
            )
            self.assertIsInstance(getattr(real_settings, attr), str)


if __name__ == "__main__":
    unittest.main()
