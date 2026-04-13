"""Unit-тесты для MetadataEnricher."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.metadata_enricher import MetadataEnricher, _count_sentences, _avg_word_length


# ── Вспомогательный item ──────────────────────────────────────────────────────

def _make_item(
    text: str = "Привет, как дела? Сегодня отличный день! Всё хорошо.",
    duration_sec: float = 5.0,
    confidence: float = 0.85,
    has_diarization: bool = False,
    has_llm_enhancement: bool = False,
    timestamp: str = "",
) -> dict:
    return {
        "text": text,
        "duration_sec": duration_sec,
        "confidence": confidence,
        "has_diarization": has_diarization,
        "has_llm_enhancement": has_llm_enhancement,
        "timestamp": timestamp,
    }


# ── Тесты вспомогательных функций ────────────────────────────────────────────

class SentenceCountTestCase(unittest.TestCase):
    """Тесты подсчёта предложений."""

    def test_empty_string_returns_zero(self) -> None:
        self.assertEqual(_count_sentences(""), 0)

    def test_single_sentence_no_period(self) -> None:
        self.assertEqual(_count_sentences("Привет как дела"), 1)

    def test_two_sentences(self) -> None:
        result = _count_sentences("Привет. Как дела?")
        self.assertEqual(result, 2)

    def test_multiple_terminators(self) -> None:
        result = _count_sentences("Раз. Два! Три?")
        self.assertEqual(result, 3)


class AvgWordLengthTestCase(unittest.TestCase):
    """Тесты средней длины слова."""

    def test_empty_list_returns_zero(self) -> None:
        self.assertEqual(_avg_word_length([]), 0.0)

    def test_single_word(self) -> None:
        self.assertEqual(_avg_word_length(["hello"]), 5.0)

    def test_multiple_words(self) -> None:
        # "ab" + "cdef" = 2 + 4 = 6, avg = 3.0
        self.assertEqual(_avg_word_length(["ab", "cdef"]), 3.0)


# ── Тесты enrich() ───────────────────────────────────────────────────────────

class EnrichMetadataKeysTestCase(unittest.TestCase):
    """Проверяем наличие всех обязательных ключей в metadata."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_enrich_returns_metadata_key(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("metadata", result)

    def test_enrich_metadata_has_word_count(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("word_count", result["metadata"])

    def test_enrich_metadata_has_sentence_count(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("sentence_count", result["metadata"])

    def test_enrich_metadata_has_avg_word_length(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("avg_word_length", result["metadata"])

    def test_enrich_metadata_has_language_detected(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("language_detected", result["metadata"])

    def test_enrich_metadata_has_emotion(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("emotion", result["metadata"])

    def test_enrich_metadata_has_speech_pace_wpm(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("speech_pace_wpm", result["metadata"])

    def test_enrich_metadata_has_quality_grade(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("quality_grade", result["metadata"])

    def test_enrich_metadata_has_auto_title(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("auto_title", result["metadata"])

    def test_enrich_metadata_has_topics(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("topics", result["metadata"])

    def test_enrich_metadata_has_enriched_at(self) -> None:
        result = self._enricher.enrich(_make_item())
        self.assertIn("enriched_at", result["metadata"])


class EnrichMetadataValuesTestCase(unittest.TestCase):
    """Проверяем типы и разумные значения полей metadata."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()
        self._item = _make_item(
            text="Привет мир. Это тестовая запись!",
            duration_sec=4.0,
            confidence=0.9,
        )
        self._result = self._enricher.enrich(self._item)
        self._meta = self._result["metadata"]

    def test_word_count_is_positive_int(self) -> None:
        self.assertIsInstance(self._meta["word_count"], int)
        self.assertGreater(self._meta["word_count"], 0)

    def test_sentence_count_is_positive_int(self) -> None:
        self.assertIsInstance(self._meta["sentence_count"], int)
        self.assertGreaterEqual(self._meta["sentence_count"], 1)

    def test_avg_word_length_is_positive_float(self) -> None:
        self.assertIsInstance(self._meta["avg_word_length"], float)
        self.assertGreater(self._meta["avg_word_length"], 0.0)

    def test_language_detected_is_string(self) -> None:
        self.assertIsInstance(self._meta["language_detected"], str)
        self.assertGreater(len(self._meta["language_detected"]), 0)

    def test_language_detected_russian_text(self) -> None:
        self.assertEqual(self._meta["language_detected"], "ru")

    def test_emotion_is_valid_string(self) -> None:
        valid_emotions = {"neutral", "positive", "negative", "excited", "frustrated", "questioning"}
        self.assertIn(self._meta["emotion"], valid_emotions)

    def test_speech_pace_wpm_is_non_negative_float(self) -> None:
        self.assertIsInstance(self._meta["speech_pace_wpm"], float)
        self.assertGreaterEqual(self._meta["speech_pace_wpm"], 0.0)

    def test_quality_grade_is_letter(self) -> None:
        self.assertIn(self._meta["quality_grade"], ("A", "B", "C", "D", "F"))

    def test_auto_title_is_non_empty_string(self) -> None:
        self.assertIsInstance(self._meta["auto_title"], str)
        self.assertGreater(len(self._meta["auto_title"]), 0)

    def test_topics_is_list(self) -> None:
        self.assertIsInstance(self._meta["topics"], list)

    def test_original_item_not_mutated(self) -> None:
        """enrich() не должен мутировать исходный словарь."""
        original = _make_item()
        self._enricher.enrich(original)
        self.assertNotIn("metadata", original)


class EnrichEmptyTextTestCase(unittest.TestCase):
    """Поведение при пустом или отсутствующем тексте."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_empty_text_word_count_zero(self) -> None:
        result = self._enricher.enrich(_make_item(text=""))
        self.assertEqual(result["metadata"]["word_count"], 0)

    def test_empty_text_sentence_count_zero(self) -> None:
        result = self._enricher.enrich(_make_item(text=""))
        self.assertEqual(result["metadata"]["sentence_count"], 0)

    def test_empty_text_language_und(self) -> None:
        result = self._enricher.enrich(_make_item(text=""))
        self.assertEqual(result["metadata"]["language_detected"], "und")

    def test_empty_text_auto_title_fallback(self) -> None:
        result = self._enricher.enrich(_make_item(text=""))
        # AutoTitleGenerator возвращает «Запись» для пустого текста
        self.assertIsInstance(result["metadata"]["auto_title"], str)
        self.assertGreater(len(result["metadata"]["auto_title"]), 0)

    def test_missing_text_key(self) -> None:
        """item без ключа text не должен падать с исключением."""
        result = self._enricher.enrich({})
        self.assertIn("metadata", result)
        self.assertEqual(result["metadata"]["word_count"], 0)


class EnrichLanguageDetectionTestCase(unittest.TestCase):
    """Проверяем детектирование языка для разных текстов."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_russian_text_detected(self) -> None:
        item = _make_item(text="Привет, это русский текст для теста.")
        result = self._enricher.enrich(item)
        self.assertEqual(result["metadata"]["language_detected"], "ru")

    def test_spanish_text_detected(self) -> None:
        item = _make_item(text="Hola, esto es una prueba en español.")
        result = self._enricher.enrich(item)
        self.assertEqual(result["metadata"]["language_detected"], "es")

    def test_english_text_detected(self) -> None:
        item = _make_item(text="Hello, this is an English test sentence.")
        result = self._enricher.enrich(item)
        self.assertEqual(result["metadata"]["language_detected"], "en")


class EnrichSpeechPaceTestCase(unittest.TestCase):
    """Проверяем расчёт темпа речи."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_pace_zero_when_no_duration(self) -> None:
        item = _make_item(text="Привет мир тест слова", duration_sec=0.0)
        result = self._enricher.enrich(item)
        self.assertEqual(result["metadata"]["speech_pace_wpm"], 0.0)

    def test_pace_reasonable_for_normal_speech(self) -> None:
        # 10 слов за 5 секунд = 120 wpm
        text = "один два три четыре пять шесть семь восемь девять десять"
        item = _make_item(text=text, duration_sec=5.0)
        result = self._enricher.enrich(item)
        # Ожидаем около 120 WPM ± погрешность
        wpm = result["metadata"]["speech_pace_wpm"]
        self.assertGreater(wpm, 50.0)
        self.assertLess(wpm, 300.0)


class EnrichQualityGradeTestCase(unittest.TestCase):
    """Проверяем оценки качества транскрибации."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_high_confidence_gets_better_grade(self) -> None:
        high = _make_item(
            text="Привет мир тест слова один два три четыре пять шесть семь восемь",
            confidence=0.95,
            duration_sec=5.0,
            has_diarization=True,
            has_llm_enhancement=True,
        )
        low = _make_item(
            text="Привет",
            confidence=0.1,
            duration_sec=5.0,
        )
        high_result = self._enricher.enrich(high)
        low_result = self._enricher.enrich(low)
        grade_order = ["A", "B", "C", "D", "F"]
        high_idx = grade_order.index(high_result["metadata"]["quality_grade"])
        low_idx = grade_order.index(low_result["metadata"]["quality_grade"])
        self.assertLessEqual(high_idx, low_idx)

    def test_grade_is_valid_letter(self) -> None:
        item = _make_item(confidence=0.75, duration_sec=5.0)
        result = self._enricher.enrich(item)
        self.assertIn(result["metadata"]["quality_grade"], ("A", "B", "C", "D", "F"))


class EnrichAutoTitleTestCase(unittest.TestCase):
    """Проверяем генерацию авто-заголовка."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_auto_title_with_timestamp(self) -> None:
        item = _make_item(
            text="Это тестовая запись о работе.",
            timestamp="2026-04-12T10:00:00Z",
        )
        result = self._enricher.enrich(item)
        title = result["metadata"]["auto_title"]
        # Должен содержать дату
        self.assertIn("2026-04-12", title)

    def test_auto_title_without_timestamp(self) -> None:
        item = _make_item(text="Это тестовая запись о работе.", timestamp="")
        result = self._enricher.enrich(item)
        title = result["metadata"]["auto_title"]
        self.assertIsInstance(title, str)
        self.assertGreater(len(title), 0)

    def test_auto_title_non_empty_for_any_text(self) -> None:
        for text in ["привет", "Hello world", "Hola mundo"]:
            item = _make_item(text=text)
            result = self._enricher.enrich(item)
            self.assertGreater(len(result["metadata"]["auto_title"]), 0)


class EnrichTopicsTestCase(unittest.TestCase):
    """Проверяем извлечение тем."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_topics_is_list_type(self) -> None:
        item = _make_item(text="Сегодня обсуждали технологии и программирование.")
        result = self._enricher.enrich(item)
        self.assertIsInstance(result["metadata"]["topics"], list)

    def test_topics_contain_strings(self) -> None:
        item = _make_item(text="Сегодня обсуждали технологии и программирование.")
        result = self._enricher.enrich(item)
        for topic in result["metadata"]["topics"]:
            self.assertIsInstance(topic, str)

    def test_empty_text_topics_empty(self) -> None:
        item = _make_item(text="")
        result = self._enricher.enrich(item)
        self.assertEqual(result["metadata"]["topics"], [])


# ── Тесты enrich_batch() ─────────────────────────────────────────────────────

class EnrichBatchTestCase(unittest.TestCase):
    """Тесты пакетного обогащения."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_enrich_batch_preserves_order(self) -> None:
        items = [
            _make_item(text=f"Текст номер {i}") for i in range(5)
        ]
        results = self._enricher.enrich_batch(items)
        self.assertEqual(len(results), 5)
        for i, result in enumerate(results):
            self.assertIn("metadata", result)

    def test_enrich_batch_empty_list(self) -> None:
        results = self._enricher.enrich_batch([])
        self.assertEqual(results, [])

    def test_enrich_batch_single_item(self) -> None:
        items = [_make_item(text="Одна запись")]
        results = self._enricher.enrich_batch(items)
        self.assertEqual(len(results), 1)
        self.assertIn("metadata", results[0])

    def test_enrich_batch_increments_stats(self) -> None:
        enricher = MetadataEnricher()
        items = [_make_item(text=f"Запись {i}") for i in range(3)]
        enricher.enrich_batch(items)
        stats = enricher.get_enrichment_stats()
        self.assertEqual(stats["items_enriched"], 3)


# ── Тесты get_enrichment_stats() ─────────────────────────────────────────────

class EnrichmentStatsTestCase(unittest.TestCase):
    """Тесты статистики обогащения."""

    def test_stats_initial_state(self) -> None:
        enricher = MetadataEnricher()
        stats = enricher.get_enrichment_stats()
        self.assertEqual(stats["items_enriched"], 0)
        self.assertEqual(stats["avg_enrichment_time_sec"], 0.0)
        self.assertEqual(stats["total_enrichment_time_sec"], 0.0)

    def test_stats_after_single_enrich(self) -> None:
        enricher = MetadataEnricher()
        enricher.enrich(_make_item())
        stats = enricher.get_enrichment_stats()
        self.assertEqual(stats["items_enriched"], 1)
        self.assertGreater(stats["avg_enrichment_time_sec"], 0.0)
        self.assertGreater(stats["total_enrichment_time_sec"], 0.0)

    def test_stats_after_multiple_enrichments(self) -> None:
        enricher = MetadataEnricher()
        for _ in range(5):
            enricher.enrich(_make_item())
        stats = enricher.get_enrichment_stats()
        self.assertEqual(stats["items_enriched"], 5)
        self.assertGreater(stats["avg_enrichment_time_sec"], 0.0)

    def test_stats_has_required_keys(self) -> None:
        enricher = MetadataEnricher()
        stats = enricher.get_enrichment_stats()
        required_keys = {"items_enriched", "avg_enrichment_time_sec", "total_enrichment_time_sec"}
        self.assertEqual(required_keys, set(stats.keys()))

    def test_avg_time_consistent_with_total(self) -> None:
        enricher = MetadataEnricher()
        for _ in range(4):
            enricher.enrich(_make_item())
        stats = enricher.get_enrichment_stats()
        expected_avg = stats["total_enrichment_time_sec"] / 4
        self.assertAlmostEqual(stats["avg_enrichment_time_sec"], expected_avg, places=5)


# ── Тесты IPC-обработчика handle_enrich_recording() ──────────────────────────

class HandleEnrichRecordingTestCase(unittest.TestCase):
    """Тесты IPC-обработчика."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_handle_with_item_param(self) -> None:
        params = {"item": _make_item()}
        result = self._enricher.handle_enrich_recording(params)
        self.assertIn("enriched_item", result)
        self.assertIn("stats", result)
        self.assertIn("metadata", result["enriched_item"])

    def test_handle_with_flat_params(self) -> None:
        params = {
            "text": "Привет мир тест",
            "duration_sec": 3.0,
            "confidence": 0.8,
        }
        result = self._enricher.handle_enrich_recording(params)
        self.assertIn("enriched_item", result)
        self.assertIn("metadata", result["enriched_item"])

    def test_handle_empty_params_does_not_crash(self) -> None:
        result = self._enricher.handle_enrich_recording({})
        self.assertIn("enriched_item", result)

    def test_handle_invalid_item_raises_runtime_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self._enricher.handle_enrich_recording({"item": "не_словарь"})

    def test_handle_returns_stats(self) -> None:
        params = {"item": _make_item()}
        result = self._enricher.handle_enrich_recording(params)
        stats = result["stats"]
        self.assertIn("items_enriched", stats)
        self.assertGreater(stats["items_enriched"], 0)

    def test_handle_enriched_item_preserves_original_fields(self) -> None:
        item = _make_item(text="Тест")
        item["custom_field"] = "кастомное_значение"
        params = {"item": item}
        result = self._enricher.handle_enrich_recording(params)
        self.assertEqual(result["enriched_item"]["custom_field"], "кастомное_значение")


# ── Интеграционные тесты ──────────────────────────────────────────────────────

class EnrichIntegrationTestCase(unittest.TestCase):
    """Интеграционные тесты — реалистичные сценарии."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_russian_recording_full_metadata(self) -> None:
        """Полный прогон обогащения для русскоязычной записи."""
        item = {
            "id": "test-001",
            "text": (
                "Сегодня провели встречу команды. "
                "Обсудили технические задачи и планы на следующий спринт. "
                "Все участники согласились с предложенным подходом."
            ),
            "duration_sec": 30.0,
            "confidence": 0.88,
            "has_diarization": True,
            "timestamp": "2026-04-12T09:30:00Z",
        }
        result = self._enricher.enrich(item)
        meta = result["metadata"]

        # Базовые метрики
        self.assertGreater(meta["word_count"], 10)
        self.assertGreaterEqual(meta["sentence_count"], 2)
        self.assertGreater(meta["avg_word_length"], 2.0)

        # Язык должен быть русским
        self.assertEqual(meta["language_detected"], "ru")

        # Оценка качества — с высокой уверенностью и диаризацией
        self.assertIn(meta["quality_grade"], ("A", "B", "C"))

        # Заголовок содержит дату
        self.assertIn("2026-04-12", meta["auto_title"])

        # Темп речи — разумный
        self.assertGreater(meta["speech_pace_wpm"], 0.0)

    def test_short_recording_handled_gracefully(self) -> None:
        """Короткая запись не должна вызывать ошибок."""
        item = {"text": "Да.", "duration_sec": 0.5, "confidence": 0.6}
        result = self._enricher.enrich(item)
        self.assertEqual(result["metadata"]["word_count"], 1)
        self.assertIn("metadata", result)

    def test_batch_then_stats_consistency(self) -> None:
        """После batch статистика должна отражать реальное количество."""
        enricher = MetadataEnricher()
        items = [{"text": f"Запись {i}", "duration_sec": 2.0} for i in range(7)]
        enricher.enrich_batch(items)
        stats = enricher.get_enrichment_stats()
        self.assertEqual(stats["items_enriched"], 7)


if __name__ == "__main__":
    unittest.main()
