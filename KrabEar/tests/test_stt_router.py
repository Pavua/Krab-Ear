"""Тесты для STTRouter (KrabEar/core/stt_router.py).

Проверяет маршрутизацию на языково-специализированные STT модели:
- routing disabled → всегда OTHER_PRIMARY
- hint_language явный → правильная модель
- неизвестный hint → OTHER_PRIMARY
- audio detection через AudioLanguageID (mocked) → использует detected lang
- audio detection → None → placeholder "ru"
- disabled lang id → placeholder "ru"
- пустой/тихий аудио → fallback
- исключение в adapter_factory → fallback OTHER_PRIMARY
- adapter_factory вызывается с правильным model_id
"""

from __future__ import annotations

import sys
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

# Путь к корню проекта чтобы импорты работали из test discovery
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.stt_router import (  # noqa: E402
    STTRouter,
    select_adapter_scored,
)


def _adapter(name: str, languages: "set[str]", available: bool = True) -> SimpleNamespace:
    """Create a minimal duck-typed adapter stub."""
    ns = SimpleNamespace(name=name, supported_languages=languages)
    ns.is_available = lambda: available  # type: ignore[attr-defined]
    return ns


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
    STT_AUDIO_LANG_ID_ENABLED: bool = True
    STT_AUDIO_LANG_ID_PREVIEW_SEC: float = 5.0


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
    """audio detection: AudioLanguageID mocked → использует detected язык."""

    def setUp(self):
        self.settings = _make_settings(
            STT_LANGUAGE_ROUTING_ENABLED=True,
            STT_AUDIO_LANG_ID_ENABLED=True,
        )

    def test_audio_lid_detection_returns_detected_lang(self):
        """AudioLanguageID.detect() возвращает 'en' → router выбирает EN модель."""
        mock_lid = MagicMock()
        mock_lid.detect.return_value = "en"

        router = STTRouter(self.settings)
        router._lang_id = mock_lid

        result = router.select_model(
            audio_data=_speech_audio(seconds=2.0),
            sample_rate=16000,
            hint_language=None,
        )
        self.assertEqual(result, "model/en-specialist")
        mock_lid.detect.assert_called_once()

    def test_audio_lid_detection_none_falls_back_to_placeholder_ru(self):
        """AudioLanguageID.detect() возвращает None → placeholder 'ru' → RU model."""
        mock_lid = MagicMock()
        mock_lid.detect.return_value = None

        router = STTRouter(self.settings)
        router._lang_id = mock_lid

        result = router.select_model(
            audio_data=_speech_audio(seconds=2.0),
            sample_rate=16000,
            hint_language=None,
        )
        self.assertEqual(result, "model/ru-specialist")

    def test_audio_lid_disabled_returns_placeholder_ru(self):
        """STT_AUDIO_LANG_ID_ENABLED=False → LID не вызывается, placeholder 'ru'."""
        settings = _make_settings(
            STT_LANGUAGE_ROUTING_ENABLED=True,
            STT_AUDIO_LANG_ID_ENABLED=False,
        )
        mock_lid = MagicMock()
        mock_lid.detect.return_value = "es"  # не должен быть вызван

        router = STTRouter(settings)
        router._lang_id = mock_lid

        result = router.select_model(
            audio_data=_speech_audio(seconds=2.0),
            sample_rate=16000,
            hint_language=None,
        )
        # LID disabled → placeholder ru (rms > 0 → "ru")
        self.assertEqual(result, "model/ru-specialist")
        # detect() не должен вызываться когда LID отключён
        mock_lid.detect.assert_not_called()

    def test_silence_audio_no_hint_returns_other(self):
        """Near-silence аудио → LID вернёт None (тишина) → placeholder → ru model.

        Примечание: тихое аудио (rms≈0) → AudioLanguageID→None → _resolve_language→ru.
        Но silence_audio < 1e-6 rms → при LID disabled вернёт 'und'.
        С LID mock возвращающим None → также placeholder 'ru' → RU model.
        """
        mock_lid = MagicMock()
        mock_lid.detect.return_value = None  # тишина → None

        router = STTRouter(self.settings)
        router._lang_id = mock_lid

        result = router.select_model(
            audio_data=_silence_audio(seconds=2.0),
            sample_rate=16000,
            hint_language=None,
        )
        # None → placeholder 'ru' → ru-specialist
        self.assertEqual(result, "model/ru-specialist")

    def test_none_audio_no_hint_returns_other(self):
        """audio_data=None + нет hint → fallback на OTHER_PRIMARY (без LID)."""
        router = STTRouter(self.settings)
        result = router.select_model(
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
        """Пустой numpy массив → fallback, нет краша.

        После введения AudioLanguageID: пустое аудио (0 фреймов) < min 1s →
        _resolve_language возвращает placeholder 'ru' → RU model.
        Ключевое требование: НЕТ краша.
        """
        router = STTRouter(self.settings)
        result = router.select_model(
            audio_data=np.array([], dtype=np.float32),
            sample_rate=16000,
            hint_language=None,
        )
        # Пустое аудио слишком короткое → placeholder 'ru' → RU model
        self.assertIn(
            result,
            {"model/other-generalist", "model/ru-specialist"},
        )


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

    def test_real_settings_has_audio_lang_id_attrs(self):
        """Реальный settings содержит STT_AUDIO_LANG_ID_ENABLED и PREVIEW_SEC."""
        from core.config import settings as real_settings
        self.assertTrue(hasattr(real_settings, "STT_AUDIO_LANG_ID_ENABLED"))
        self.assertIsInstance(real_settings.STT_AUDIO_LANG_ID_ENABLED, bool)
        self.assertTrue(hasattr(real_settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC"))
        self.assertIsInstance(real_settings.STT_AUDIO_LANG_ID_PREVIEW_SEC, float)


class TestSTTRouterGetGigaamAdapter(unittest.TestCase):
    """get_gigaam_adapter: enabled (adapter returned) / disabled (None returned)."""

    @staticmethod
    def _fake_gigaam_module():
        """Создаёт модуль-заглушку и записывает параметры всех адаптеров."""
        import types

        class FakeGigaAMAdapter:
            """Минимальный адаптер-заглушка с наблюдаемым закрытием."""

            instances = []

            def __init__(self, *, device, mode, transport, venv_python_path):
                self.device = device
                self.mode = mode
                self.transport = transport
                self.venv_python_path = venv_python_path
                self.close = MagicMock()
                self.__class__.instances.append(self)

        module = types.ModuleType("core.pipeline.stt_gigaam")
        module.GigaAMAdapter = FakeGigaAMAdapter
        return module, FakeGigaAMAdapter

    @staticmethod
    def _gigaam_settings(**overrides):
        """Возвращает включённые настройки GigaAM с явной конфигурацией."""
        settings = _make_settings(
            STT_GIGAAM_ENABLED=True,
            STT_GIGAAM_MODE="rnnt",
            STT_GIGAAM_DEVICE="cpu",
            STT_GIGAAM_TRANSPORT="subprocess",
            STT_GIGAAM_VENV_PYTHON="",
        )
        for key, value in overrides.items():
            setattr(settings, key, value)
        return settings

    def test_get_gigaam_adapter_disabled_returns_none(self):
        """Когда STT_GIGAAM_ENABLED=False → get_gigaam_adapter() возвращает None."""
        settings = _make_settings()
        setattr(settings, "STT_GIGAAM_ENABLED", False)
        router = STTRouter(settings)

        result = router.get_gigaam_adapter()

        self.assertIsNone(result)

    def test_get_gigaam_adapter_enabled_importerror_returns_none(self):
        """Когда STT_GIGAAM_ENABLED=True но gigaam не установлен → None, нет краша."""
        settings = _make_settings()
        setattr(settings, "STT_GIGAAM_ENABLED", True)
        setattr(settings, "STT_GIGAAM_MODE", "rnnt")
        setattr(settings, "STT_GIGAAM_DEVICE", "mps")
        router = STTRouter(settings)

        # Патчим sys.modules чтобы вызвать ImportError при import core.pipeline.stt_gigaam
        import unittest.mock as _mock
        with _mock.patch.dict("sys.modules", {"core.pipeline.stt_gigaam": None}):
            result = router.get_gigaam_adapter()

        # ImportError внутри get_gigaam_adapter → None, не exception
        self.assertIsNone(result)

    def test_unchanged_fingerprint_reuses_cached_adapter(self):
        """Неизменная конфигурация переиспользует один адаптер и subprocess."""
        import unittest.mock as _mock

        module, adapter_class = self._fake_gigaam_module()
        router = STTRouter(self._gigaam_settings())

        with _mock.patch.dict("sys.modules", {"core.pipeline.stt_gigaam": module}):
            first = router.get_gigaam_adapter()
            second = router.get_gigaam_adapter()

        self.assertIs(first, second)
        self.assertEqual(len(adapter_class.instances), 1)
        first.close.assert_not_called()

    def test_mode_change_recreates_adapter_and_closes_old_one(self):
        """Hot reload rnnt→ctc закрывает старый адаптер и создаёт новый."""
        import unittest.mock as _mock

        module, adapter_class = self._fake_gigaam_module()
        settings = self._gigaam_settings()
        router = STTRouter(settings)

        with _mock.patch.dict("sys.modules", {"core.pipeline.stt_gigaam": module}):
            first = router.get_gigaam_adapter()
            settings.STT_GIGAAM_MODE = "ctc"
            second = router.get_gigaam_adapter()

        self.assertIsNot(first, second)
        self.assertEqual([item.mode for item in adapter_class.instances], ["rnnt", "ctc"])
        first.close.assert_called_once_with()

    def test_device_and_transport_changes_recreate_adapter(self):
        """Device и transport входят в fingerprint кэшированного адаптера."""
        import unittest.mock as _mock

        module, adapter_class = self._fake_gigaam_module()
        settings = self._gigaam_settings()
        router = STTRouter(settings)

        with _mock.patch.dict("sys.modules", {"core.pipeline.stt_gigaam": module}):
            first = router.get_gigaam_adapter()
            settings.STT_GIGAAM_DEVICE = "mps"
            second = router.get_gigaam_adapter()
            settings.STT_GIGAAM_TRANSPORT = "in_process"
            third = router.get_gigaam_adapter()

        self.assertEqual(len(adapter_class.instances), 3)
        self.assertEqual((second.device, second.transport), ("mps", "subprocess"))
        self.assertEqual((third.device, third.transport), ("mps", "in_process"))
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_fingerprint_uses_validated_venv_path(self):
        """Кэш сравнивает проверенный venv-путь, а не исходную строку настроек."""
        import unittest.mock as _mock

        module, adapter_class = self._fake_gigaam_module()
        settings = self._gigaam_settings(STT_GIGAAM_VENV_PYTHON="alias-a")
        router = STTRouter(settings)
        validated_paths = {
            "alias-a": "/Users/test/.venvs/gigaam/bin/python3.12",
            "alias-a-equivalent": "/Users/test/.venvs/gigaam/bin/python3.12",
            "alias-b": "/Users/test/.venvs/gigaam-v2/bin/python3.12",
        }

        with (
            _mock.patch.dict("sys.modules", {"core.pipeline.stt_gigaam": module}),
            _mock.patch.object(
                router,
                "_validate_gigaam_venv_python",
                side_effect=lambda raw: validated_paths[raw],
            ),
        ):
            first = router.get_gigaam_adapter()
            settings.STT_GIGAAM_VENV_PYTHON = "alias-a-equivalent"
            same = router.get_gigaam_adapter()
            settings.STT_GIGAAM_VENV_PYTHON = "alias-b"
            second = router.get_gigaam_adapter()

        self.assertIs(first, same)
        self.assertIsNot(first, second)
        self.assertEqual(len(adapter_class.instances), 2)
        self.assertEqual(
            second.venv_python_path,
            "/Users/test/.venvs/gigaam-v2/bin/python3.12",
        )
        first.close.assert_called_once_with()

    def test_toggle_off_closes_adapter_and_clears_fingerprint(self):
        """Выключение GigaAM освобождает адаптер и полностью сбрасывает кэш."""
        import unittest.mock as _mock

        module, _ = self._fake_gigaam_module()
        settings = self._gigaam_settings()
        router = STTRouter(settings)

        with _mock.patch.dict("sys.modules", {"core.pipeline.stt_gigaam": module}):
            adapter = router.get_gigaam_adapter()
            settings.STT_GIGAAM_ENABLED = False
            result = router.get_gigaam_adapter()

        self.assertIsNone(result)
        adapter.close.assert_called_once_with()
        self.assertIsNone(router._gigaam_adapter)
        self.assertIsNone(router._gigaam_adapter_fingerprint)


# ---------------------------------------------------------------------------
# Wave 124 — Required test cases
# ---------------------------------------------------------------------------

class TestSTTRouterRuAudioRoutedToGigaamFirst(unittest.TestCase):
    """test_ru_audio_routed_to_gigaam_first: scored selection picks GigaAM for RU."""

    def test_ru_audio_routed_to_gigaam_first(self):
        """GigaAM exact-language match (score=130) beats whisper-mlx multilingual (score=75)."""
        gigaam = _adapter("gigaam", {"ru", "uk"})
        whisper = _adapter("whisper-mlx", set())   # multilingual

        best = select_adapter_scored("ru", audio_duration_s=5.0, adapters=[gigaam, whisper])
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "gigaam")

    def test_ru_audio_gigaam_returns_before_whisper_in_list_order(self):
        """Even when GigaAM is listed second it still wins due to score."""
        gigaam = _adapter("gigaam", {"ru"})
        whisper = _adapter("whisper-mlx", set())

        best = select_adapter_scored("ru", audio_duration_s=5.0, adapters=[whisper, gigaam])
        self.assertEqual(best.name, "gigaam")


class TestSTTRouterEnAudioRoutedToWhisper(unittest.TestCase):
    """test_en_audio_routed_to_whisper: EN audio → whisper wins over gigaam for EN."""

    def test_en_audio_routed_to_whisper(self):
        """For EN: whisper-mlx (multilingual + quality bonus) beats GigaAM (no EN support)."""
        gigaam = _adapter("gigaam", {"ru", "uk"})   # no EN support → score=0
        whisper = _adapter("whisper-mlx", set())     # multilingual

        best = select_adapter_scored("en", audio_duration_s=5.0, adapters=[gigaam, whisper])
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "whisper-mlx")

    def test_en_hint_returns_en_model(self):
        """STTRouter.select_model with hint_language='en' returns EN specialist model."""
        settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)
        router = STTRouter(settings)
        result = router.select_model(hint_language="en")
        self.assertEqual(result, "model/en-specialist")


class TestSTTRouterEsAudioRoutedToWhisper(unittest.TestCase):
    """test_es_audio_routed_to_whisper: ES audio → whisper beats gigaam (no ES)."""

    def test_es_audio_routed_to_whisper(self):
        """For ES: GigaAM has no ES support → score=0, whisper wins."""
        gigaam = _adapter("gigaam", {"ru", "uk"})
        whisper = _adapter("whisper-mlx", set())

        best = select_adapter_scored("es", audio_duration_s=5.0, adapters=[gigaam, whisper])
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "whisper-mlx")

    def test_es_hint_returns_es_model(self):
        """STTRouter.select_model with hint_language='es' returns ES specialist model."""
        settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)
        router = STTRouter(settings)
        result = router.select_model(hint_language="es")
        self.assertEqual(result, "model/es-specialist")


class TestSTTRouterAdapterUnavailableFallsBack(unittest.TestCase):
    """test_adapter_unavailable_falls_back: unavailable adapters score=0, next best wins."""

    def test_unavailable_gigaam_falls_back_to_whisper(self):
        """Unavailable GigaAM → select_adapter_scored returns whisper."""
        gigaam_unavail = _adapter("gigaam", {"ru", "uk"}, available=False)
        whisper = _adapter("whisper-mlx", set())

        best = select_adapter_scored("ru", audio_duration_s=5.0, adapters=[gigaam_unavail, whisper])
        self.assertIsNotNone(best)
        self.assertEqual(best.name, "whisper-mlx")

    def test_all_unavailable_returns_none(self):
        """All adapters unavailable → None returned."""
        a = _adapter("gigaam", {"ru"}, available=False)
        b = _adapter("whisper-mlx", set(), available=False)

        best = select_adapter_scored("ru", audio_duration_s=5.0, adapters=[a, b])
        self.assertIsNone(best)

    def test_adapter_factory_exception_falls_back(self):
        """adapter_factory exception on selected model → fallback OTHER_PRIMARY."""
        settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)

        def bad_factory(model_id: str):
            raise RuntimeError("model load failed")

        router = STTRouter(settings, adapter_factory=bad_factory)
        result = router.select_model(hint_language="ru")
        self.assertEqual(result, "model/other-generalist")


class TestSTTRouterLegacyOrderMode(unittest.TestCase):
    """test_legacy_order_mode: when routing disabled, adapter order from engine preserved."""

    def test_routing_disabled_uses_other_primary_regardless_of_hint(self):
        """Legacy mode (routing=False) always returns OTHER_PRIMARY."""
        settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=False)
        router = STTRouter(settings)
        for lang in ("ru", "en", "es", "ja"):
            with self.subTest(lang=lang):
                result = router.select_model(hint_language=lang)
                self.assertEqual(result, "model/other-generalist")

    def test_select_adapter_scored_preserves_order_on_equal_score(self):
        """When two adapters have the same score, first in list wins."""
        a = _adapter("whisper-mlx-a", set())   # score 75
        b = _adapter("whisper-mlx-b", set())   # score 75

        best = select_adapter_scored("fr", audio_duration_s=5.0, adapters=[a, b])
        # Both multilingual, equal score → first wins
        self.assertEqual(best.name, "whisper-mlx-a")

    def test_empty_adapters_list_returns_none(self):
        """Empty adapter list → None (no crash)."""
        best = select_adapter_scored("ru", audio_duration_s=5.0, adapters=[])
        self.assertIsNone(best)


class TestSTTRouterConcurrentRoute(unittest.TestCase):
    """test_concurrent_route: concurrent calls to select_model do not crash."""

    def test_concurrent_route(self):
        """10 threads calling select_model concurrently — no exception, consistent results."""
        settings = _make_settings(STT_LANGUAGE_ROUTING_ENABLED=True)
        router = STTRouter(settings)

        results = []
        errors = []

        def call_router(lang):
            try:
                r = router.select_model(hint_language=lang)
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        langs = ["ru", "en", "es", "ru", "en", "es", "ru", "en", "es", "ru"]
        threads = [threading.Thread(target=call_router, args=(lang,)) for lang in langs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent route errors: {errors}")
        self.assertEqual(len(results), 10)
        # ru → ru-specialist, en → en-specialist, es → es-specialist
        for res in results:
            self.assertIn(
                res,
                {"model/ru-specialist", "model/en-specialist", "model/es-specialist"},
            )


class TestSTTRouterHandlesAllAdaptersFailed(unittest.TestCase):
    """test_handles_all_adapters_failed: when all adapters score 0, None returned."""

    def test_handles_all_adapters_failed(self):
        """All adapters have no language support for requested language → None."""
        a = _adapter("gigaam", {"ru"})    # no 'zh' support
        b = _adapter("parakeet", {"en"})  # no 'zh' support

        best = select_adapter_scored("zh", audio_duration_s=5.0, adapters=[a, b])
        self.assertIsNone(best)

    def test_handles_all_adapters_unavailable(self):
        """All adapters marked unavailable → None (no crash)."""
        adapters = [
            _adapter("gigaam", {"ru"}, available=False),
            _adapter("whisper-mlx", set(), available=False),
            _adapter("parakeet", {"en"}, available=False),
        ]
        best = select_adapter_scored("ru", audio_duration_s=5.0, adapters=adapters)
        self.assertIsNone(best)

    def test_adapter_is_available_exception_treated_as_unavailable(self):
        """Adapter whose is_available() raises → treated as unavailable (score=0)."""
        bad = SimpleNamespace(name="bad-adapter", supported_languages={"ru"})

        def raising_available():
            raise OSError("device gone")

        bad.is_available = raising_available

        whisper = _adapter("whisper-mlx", set())
        best = select_adapter_scored("ru", audio_duration_s=5.0, adapters=[bad, whisper])
        # bad adapter raises → score=0; whisper wins
        self.assertEqual(best.name, "whisper-mlx")


if __name__ == "__main__":
    unittest.main()
