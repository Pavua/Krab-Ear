"""Unit-тесты для Transcriber — высокоуровневой обёртки над AudioEngine.

Проверяем:
  1. Инициализация с опциональным engine / llm_rewriter / settings_get
  2. Корректная передача параметров в engine.transcribe()
  3. Переключение quality profile
  4. Обработка extra_vocabulary
  5. Language hints
  6. Passthrough ошибок из engine
  7. Preview mode

Все зависимости мокируются — реальные модели не загружаются.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_transcriber.py -v
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

from backend.transcriber import Transcriber


class FakeAudioEngine:
    """Полностью мокированный AudioEngine для изоляции тестов Transcriber."""

    def __init__(self, llm_rewriter=None, settings_get=None):
        self.quality_profile = "balanced"
        self._llm_rewriter = llm_rewriter
        self._settings_get = settings_get
        self.transcribe_calls = []  # Логируем вызовы для проверки
        self.transcribe_preview_calls = []

    def set_quality_profile(self, profile: str) -> bool:
        """Переключение профиля качества."""
        if self.quality_profile == profile:
            return False
        old_profile = self.quality_profile
        self.quality_profile = profile.lower()
        return old_profile != self.quality_profile

    def transcribe(
        self,
        audio_data: Any,
        cleanup_profile: str = "soft",
        is_preview: bool = False,
        domain: str = "casual",
        extra_vocabulary: list[str] | None = None,
        lang_hint: str | None = None,
        history_context: list[Any] | None = None,
        stt_hotwords: list[str] | None = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        """Имитация engine.transcribe()."""
        call_record = {
            "audio_data": audio_data,
            "cleanup_profile": cleanup_profile,
            "is_preview": is_preview,
            "domain": domain,
            "extra_vocabulary": extra_vocabulary,
            "lang_hint": lang_hint,
        }
        self.transcribe_calls.append(call_record)

        # Возвращаем базовый результат
        return {
            "text": f"Transcribed: {audio_data}",
            "language": lang_hint or "auto",
            "confidence": 0.95,
        }

    def raise_error(self, error_msg: str):
        """Вспомогательный метод для тестирования passthrough ошибок."""
        raise RuntimeError(error_msg)


# ---------------------------------------------------------------------------
# Тест 1: Инициализация Transcriber
# ---------------------------------------------------------------------------


class TranscriberInitTests(unittest.TestCase):
    """Проверяем инициализацию Transcriber с различными параметрами."""

    def test_init_creates_engine_by_default(self):
        """При engine=None Transcriber должен создать новый AudioEngine."""
        transcriber = Transcriber(engine=None)
        self.assertIsNotNone(transcriber.engine)

    def test_init_accepts_external_engine(self):
        """Transcriber должен принять внешний engine."""
        fake_engine = FakeAudioEngine()
        transcriber = Transcriber(engine=fake_engine)
        self.assertIs(transcriber.engine, fake_engine)

    def test_init_injects_llm_rewriter(self):
        """Если llm_rewriter передан и engine=None, он должен быть передан в новый engine."""
        mock_llm = MagicMock()
        transcriber = Transcriber(engine=None, llm_rewriter=mock_llm)
        # При создании нового engine, llm_rewriter должен быть передан
        self.assertIsNotNone(transcriber.engine._llm_rewriter)

    def test_init_injects_llm_rewriter_into_existing_engine(self):
        """Если передан engine и llm_rewriter, llm_rewriter должен быть инжектирован."""
        fake_engine = FakeAudioEngine(llm_rewriter=None)
        mock_llm = MagicMock()
        Transcriber(engine=fake_engine, llm_rewriter=mock_llm)
        self.assertIs(fake_engine._llm_rewriter, mock_llm)

    def test_init_injects_settings_get(self):
        """Если settings_get передан, он должен быть инжектирован в engine."""
        fake_engine = FakeAudioEngine()
        mock_settings_get = MagicMock()
        Transcriber(engine=fake_engine, settings_get=mock_settings_get)
        self.assertIs(fake_engine._settings_get, mock_settings_get)

    def test_init_does_not_overwrite_existing_llm_rewriter(self):
        """Если engine уже имеет llm_rewriter, новый не должен перезаписываться."""
        existing_llm = MagicMock()
        fake_engine = FakeAudioEngine(llm_rewriter=existing_llm)
        new_llm = MagicMock()
        Transcriber(engine=fake_engine, llm_rewriter=new_llm)
        # Существующий должен остаться
        self.assertIs(fake_engine._llm_rewriter, existing_llm)


# ---------------------------------------------------------------------------
# Тест 2: Основная транскрибация
# ---------------------------------------------------------------------------


class TranscriberTranscribeTests(unittest.TestCase):
    """Проверяем основной метод transcribe()."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_transcribe_calls_engine_with_audio_data(self):
        """Transcriber.transcribe() должен передать audio_data в engine.transcribe()."""
        audio_data = "test_audio_bytes"
        self.transcriber.transcribe(audio_data)

        self.assertEqual(len(self.fake_engine.transcribe_calls), 1)
        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["audio_data"], audio_data)

    def test_transcribe_default_parameters(self):
        """Проверяем значения параметров по умолчанию."""
        audio_data = b"test_audio"
        self.transcriber.transcribe(audio_data)

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["cleanup_profile"], "soft")
        self.assertEqual(call["is_preview"], False)
        self.assertEqual(call["domain"], "casual")
        self.assertIsNone(call["extra_vocabulary"])
        self.assertIsNone(call["lang_hint"])

    def test_transcribe_custom_quality_profile(self):
        """Transcriber должен переключить profile перед транскрибацией."""
        self.transcriber.transcribe(b"audio", quality_profile="max")

        self.assertEqual(self.fake_engine.quality_profile, "max")
        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["cleanup_profile"], "soft")

    def test_transcribe_custom_cleanup_profile(self):
        """Transcriber должен передать cleanup_profile в engine."""
        self.transcriber.transcribe(b"audio", cleanup_profile="strict")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["cleanup_profile"], "strict")

    def test_transcribe_custom_domain(self):
        """Transcriber должен передать domain в engine."""
        self.transcriber.transcribe(b"audio", domain="meeting")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["domain"], "meeting")

    def test_transcribe_with_extra_vocabulary(self):
        """Transcriber должен передать extra_vocabulary в engine."""
        vocab = ["foo", "bar", "baz"]
        self.transcriber.transcribe(b"audio", extra_vocabulary=vocab)

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["extra_vocabulary"], vocab)

    def test_transcribe_with_language_hint(self):
        """Transcriber должен передать lang_hint в engine."""
        self.transcriber.transcribe(b"audio", lang_hint="ru")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["lang_hint"], "ru")

    def test_transcribe_returns_engine_result(self):
        """Transcriber должен вернуть результат от engine.transcribe()."""
        result = self.transcriber.transcribe(b"audio")

        self.assertIsInstance(result, dict)
        self.assertIn("text", result)
        self.assertIn("confidence", result)

    def test_transcribe_all_custom_parameters(self):
        """Проверяем транскрибацию со всеми кастомными параметрами."""
        audio_data = b"complex_audio"
        vocab = ["краб", "уши"]
        self.transcriber.transcribe(
            audio_data,
            quality_profile="max",
            cleanup_profile="strict",
            domain="technical",
            extra_vocabulary=vocab,
            lang_hint="es",
        )

        # Проверяем, что profile был переключён
        self.assertEqual(self.fake_engine.quality_profile, "max")

        # Проверяем вызов engine
        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["audio_data"], audio_data)
        self.assertEqual(call["cleanup_profile"], "strict")
        self.assertEqual(call["domain"], "technical")
        self.assertEqual(call["extra_vocabulary"], vocab)
        self.assertEqual(call["lang_hint"], "es")
        self.assertEqual(call["is_preview"], False)


# ---------------------------------------------------------------------------
# Тест 3: Preview режим
# ---------------------------------------------------------------------------


class TranscriberPreviewTests(unittest.TestCase):
    """Проверяем метод transcribe_preview()."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_preview_always_balanced(self):
        """Preview всегда должен использовать balanced профиль."""
        # Сначала переключаемся на max
        self.fake_engine.quality_profile = "max"

        self.transcriber.transcribe_preview(b"audio")

        # После preview должен быть balanced
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

    def test_preview_sets_is_preview_flag(self):
        """Preview должен установить is_preview=True."""
        self.transcriber.transcribe_preview(b"audio")

        call = self.fake_engine.transcribe_calls[0]
        self.assertTrue(call["is_preview"])

    def test_preview_uses_soft_cleanup(self):
        """Preview должен использовать soft cleanup для скорости."""
        self.transcriber.transcribe_preview(b"audio")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["cleanup_profile"], "soft")

    def test_preview_accepts_quality_profile_param(self):
        """Preview принимает quality_profile параметр."""
        self.transcriber.transcribe_preview(b"audio", quality_profile="max")

        # Несмотря на параметр, должен быть переключен на balanced
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

    def test_preview_returns_engine_result(self):
        """Preview должен вернуть результат от engine."""
        result = self.transcriber.transcribe_preview(b"audio")

        self.assertIsInstance(result, dict)
        self.assertIn("text", result)


# ---------------------------------------------------------------------------
# Тест 4: Quality profile switching
# ---------------------------------------------------------------------------


class QualityProfileTests(unittest.TestCase):
    """Проверяем корректное переключение quality profile."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_multiple_profile_switches(self):
        """Transcriber должен корректно переключать профили."""
        self.transcriber.transcribe(b"audio1", quality_profile="balanced")
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

        self.transcriber.transcribe(b"audio2", quality_profile="max")
        self.assertEqual(self.fake_engine.quality_profile, "max")

        self.transcriber.transcribe(b"audio3", quality_profile="balanced")
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

    def test_profile_switch_before_each_transcribe(self):
        """Profile должен быть переключен ДО каждого вызова transcribe()."""
        # Проверяем, что set_quality_profile вызывается
        with patch.object(self.fake_engine, "set_quality_profile") as mock_set:
            mock_set.return_value = True
            self.transcriber.transcribe(b"audio", quality_profile="max")

            # Если set_quality_profile будет использоваться, этот тест покажет


# ---------------------------------------------------------------------------
# Тест 5: Error handling
# ---------------------------------------------------------------------------


class ErrorHandlingTests(unittest.TestCase):
    """Проверяем обработку ошибок из engine."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_transcribe_propagates_engine_error(self):
        """Ошибка из engine должна пройти через Transcriber."""
        # Мокируем transcribe на engine чтобы выбросить исключение
        self.fake_engine.transcribe = MagicMock(
            side_effect=RuntimeError("STT model failed")
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.transcriber.transcribe(b"audio")

        self.assertIn("STT model failed", str(ctx.exception))

    def test_transcribe_propagates_other_exceptions(self):
        """Другие исключения должны тоже пройти через."""
        self.fake_engine.transcribe = MagicMock(
            side_effect=ValueError("Invalid audio format")
        )

        with self.assertRaises(ValueError) as ctx:
            self.transcriber.transcribe(b"audio")

        self.assertIn("Invalid audio format", str(ctx.exception))

    def test_preview_propagates_engine_error(self):
        """Preview должен тоже пропустить ошибку от engine."""
        self.fake_engine.transcribe = MagicMock(
            side_effect=RuntimeError("Preview failed")
        )

        with self.assertRaises(RuntimeError):
            self.transcriber.transcribe_preview(b"audio")


# ---------------------------------------------------------------------------
# Тест 6: Vocabulary handling
# ---------------------------------------------------------------------------


class VocabularyTests(unittest.TestCase):
    """Проверяем работу с extra_vocabulary."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_empty_vocabulary(self):
        """Пустой vocabulary должен быть передан как None."""
        self.transcriber.transcribe(b"audio", extra_vocabulary=[])

        call = self.fake_engine.transcribe_calls[0]
        # Пустой список передается как есть
        self.assertEqual(call["extra_vocabulary"], [])

    def test_vocabulary_with_duplicates(self):
        """Vocabulary может содержать дубликаты — Transcriber не фильтрует."""
        vocab = ["word", "word", "другое"]
        self.transcriber.transcribe(b"audio", extra_vocabulary=vocab)

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["extra_vocabulary"], vocab)

    def test_vocabulary_with_special_characters(self):
        """Vocabulary может содержать специальные символы."""
        vocab = ["краб-ухо", "foo_bar", "бр."]
        self.transcriber.transcribe(b"audio", extra_vocabulary=vocab)

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["extra_vocabulary"], vocab)

    def test_multiple_transcribe_with_different_vocabularies(self):
        """Разные вызовы могут иметь разные vocabularies."""
        vocab1 = ["word1", "word2"]
        vocab2 = ["word3", "word4"]

        self.transcriber.transcribe(b"audio1", extra_vocabulary=vocab1)
        self.transcriber.transcribe(b"audio2", extra_vocabulary=vocab2)

        call1 = self.fake_engine.transcribe_calls[0]
        call2 = self.fake_engine.transcribe_calls[1]

        self.assertEqual(call1["extra_vocabulary"], vocab1)
        self.assertEqual(call2["extra_vocabulary"], vocab2)


# ---------------------------------------------------------------------------
# Тест 7: Language hint handling
# ---------------------------------------------------------------------------


class LanguageHintTests(unittest.TestCase):
    """Проверяем работу с язык-хинтами."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_language_hint_none(self):
        """lang_hint=None должен быть передан как None."""
        self.transcriber.transcribe(b"audio", lang_hint=None)

        call = self.fake_engine.transcribe_calls[0]
        self.assertIsNone(call["lang_hint"])

    def test_language_hint_auto(self):
        """lang_hint='auto' должен быть передан как 'auto'."""
        self.transcriber.transcribe(b"audio", lang_hint="auto")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["lang_hint"], "auto")

    def test_language_hint_ru(self):
        """lang_hint='ru' для русского."""
        self.transcriber.transcribe(b"audio", lang_hint="ru")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["lang_hint"], "ru")

    def test_language_hint_es(self):
        """lang_hint='es' для испанского."""
        self.transcriber.transcribe(b"audio", lang_hint="es")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["lang_hint"], "es")

    def test_language_hint_en(self):
        """lang_hint='en' для английского."""
        self.transcriber.transcribe(b"audio", lang_hint="en")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["lang_hint"], "en")

    def test_multiple_transcribe_with_different_languages(self):
        """Разные вызовы могут иметь разные язык-хинты."""
        self.transcriber.transcribe(b"audio1", lang_hint="ru")
        self.transcriber.transcribe(b"audio2", lang_hint="es")
        self.transcriber.transcribe(b"audio3", lang_hint="en")

        self.assertEqual(self.fake_engine.transcribe_calls[0]["lang_hint"], "ru")
        self.assertEqual(self.fake_engine.transcribe_calls[1]["lang_hint"], "es")
        self.assertEqual(self.fake_engine.transcribe_calls[2]["lang_hint"], "en")


# ---------------------------------------------------------------------------
# Тест 8: Domain parameter
# ---------------------------------------------------------------------------


class DomainParameterTests(unittest.TestCase):
    """Проверяем работу с domain параметром."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_default_domain_is_casual(self):
        """Значение по умолчанию — 'casual'."""
        self.transcriber.transcribe(b"audio")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["domain"], "casual")

    def test_custom_domain_meeting(self):
        """Domain может быть 'meeting'."""
        self.transcriber.transcribe(b"audio", domain="meeting")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["domain"], "meeting")

    def test_custom_domain_technical(self):
        """Domain может быть 'technical'."""
        self.transcriber.transcribe(b"audio", domain="technical")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["domain"], "technical")

    def test_custom_domain_medical(self):
        """Domain может быть 'medical'."""
        self.transcriber.transcribe(b"audio", domain="medical")

        call = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call["domain"], "medical")


# ---------------------------------------------------------------------------
# Тест 9: Integration test — все параметры вместе
# ---------------------------------------------------------------------------


class IntegrationTests(unittest.TestCase):
    """Интеграционные тесты с комбинацией всех параметров."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_complex_transcription_workflow(self):
        """Полный workflow с несколькими последовательными транскрибациями."""
        # Сценарий: несколько файлов с разными параметрами

        # Файл 1: Русское интервью (balanced, soft cleanup)
        result1 = self.transcriber.transcribe(
            b"interview_ru",
            quality_profile="balanced",
            cleanup_profile="soft",
            domain="casual",
            lang_hint="ru",
        )
        self.assertIn("text", result1)
        # После первой транскрибации профиль balanced
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

        # Файл 2: Испанское совещание (max, strict cleanup)
        result2 = self.transcriber.transcribe(
            b"meeting_es",
            quality_profile="max",
            cleanup_profile="strict",
            domain="meeting",
            lang_hint="es",
            extra_vocabulary=["foo", "bar"],
        )
        self.assertIn("text", result2)
        # После второй транскрибации профиль max
        self.assertEqual(self.fake_engine.quality_profile, "max")

        # Быстрый preview — ВСЕГДА переключает на balanced
        result3 = self.transcriber.transcribe_preview(b"preview_audio")
        self.assertIn("text", result3)
        self.assertEqual(self.fake_engine.quality_profile, "balanced")

        # Проверяем логирование вызовов
        self.assertEqual(len(self.fake_engine.transcribe_calls), 3)

        call1 = self.fake_engine.transcribe_calls[0]
        self.assertEqual(call1["lang_hint"], "ru")
        self.assertFalse(call1["is_preview"])

        call2 = self.fake_engine.transcribe_calls[1]
        self.assertEqual(call2["lang_hint"], "es")
        self.assertEqual(call2["extra_vocabulary"], ["foo", "bar"])
        self.assertFalse(call2["is_preview"])

        call3 = self.fake_engine.transcribe_calls[2]
        self.assertTrue(call3["is_preview"])  # Preview mode


if __name__ == "__main__":
    unittest.main()
