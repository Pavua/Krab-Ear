"""Тесты для EmotionDetector — эвристического детектора эмоций в тексте.

Покрывает:
- EmotionResult dataclass поля
- Нейтральный текст
- Позитивные слова → positive
- Негативные слова → negative
- Восклицательные знаки → excited
- Вопросительные знаки → questioning
- ALL CAPS → frustrated
- Пустой/пробельный ввод
- Испанский язык
- caps_ratio вычисление
- Комбинированные сигналы (восклицание + позитив)
- IPC-метод detect_emotion через BackendService
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.emotion_detector import EmotionDetector, EmotionResult


class TestEmotionDetectorBasic(unittest.TestCase):
    """Базовые юнит-тесты EmotionDetector."""

    def setUp(self) -> None:
        self.detector = EmotionDetector()

    # ------------------------------------------------------------------
    # 1. Нейтральный текст
    # ------------------------------------------------------------------

    def test_neutral_plain_text(self) -> None:
        result = self.detector.detect("Сегодня было собрание по проекту")
        self.assertEqual(result.primary_emotion, "neutral")
        self.assertIsInstance(result.confidence, float)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)

    # ------------------------------------------------------------------
    # 2. Позитивные слова → positive
    # ------------------------------------------------------------------

    def test_positive_words_trigger_positive(self) -> None:
        result = self.detector.detect("Отлично, всё работает здорово!")
        self.assertIn(result.primary_emotion, ("positive", "excited"))
        self.assertGreater(result.confidence, 0.0)

    def test_positive_single_word(self) -> None:
        result = self.detector.detect("Хорошо")
        self.assertIn(result.primary_emotion, ("positive", "neutral"))

    # ------------------------------------------------------------------
    # 3. Негативные слова → negative
    # ------------------------------------------------------------------

    def test_negative_words_trigger_negative(self) -> None:
        result = self.detector.detect("Это ужасно, всё плохо и не работает")
        self.assertEqual(result.primary_emotion, "negative")
        self.assertGreater(result.confidence, 0.3)

    def test_negative_single_word(self) -> None:
        result = self.detector.detect("Ужасно")
        self.assertIn(result.primary_emotion, ("negative", "neutral"))

    # ------------------------------------------------------------------
    # 4. Восклицательные знаки → excited
    # ------------------------------------------------------------------

    def test_exclamation_marks_trigger_excited(self) -> None:
        result = self.detector.detect("Это невероятно!!!")
        self.assertEqual(result.primary_emotion, "excited")
        self.assertEqual(result.exclamation_count, 3)
        self.assertGreater(result.confidence, 0.4)

    def test_single_exclamation_mark(self) -> None:
        result = self.detector.detect("Стоп!")
        self.assertEqual(result.exclamation_count, 1)
        self.assertIn(result.primary_emotion, ("excited", "neutral"))

    # ------------------------------------------------------------------
    # 5. Вопросительные знаки → questioning
    # ------------------------------------------------------------------

    def test_question_marks_trigger_questioning(self) -> None:
        result = self.detector.detect("Что происходит? Почему не работает?")
        self.assertEqual(result.primary_emotion, "questioning")
        self.assertEqual(result.question_count, 2)
        self.assertGreater(result.confidence, 0.4)

    def test_single_question_mark(self) -> None:
        result = self.detector.detect("Как дела?")
        self.assertEqual(result.question_count, 1)
        self.assertIn(result.primary_emotion, ("questioning", "neutral"))

    # ------------------------------------------------------------------
    # 6. ALL CAPS → frustrated
    # ------------------------------------------------------------------

    def test_all_caps_triggers_frustrated(self) -> None:
        result = self.detector.detect("ЭТО НЕВОЗМОЖНО ИСПРАВИТЬ")
        self.assertEqual(result.primary_emotion, "frustrated")
        self.assertGreater(result.caps_ratio, 0.5)

    def test_mixed_case_not_frustrated(self) -> None:
        result = self.detector.detect("Обычный текст без капса")
        self.assertNotEqual(result.primary_emotion, "frustrated")

    # ------------------------------------------------------------------
    # 7. Пустой / пробельный ввод
    # ------------------------------------------------------------------

    def test_empty_string_returns_neutral(self) -> None:
        result = self.detector.detect("")
        self.assertEqual(result.primary_emotion, "neutral")
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.indicators, [])

    def test_whitespace_only_returns_neutral(self) -> None:
        result = self.detector.detect("   \t\n  ")
        self.assertEqual(result.primary_emotion, "neutral")

    # ------------------------------------------------------------------
    # 8. Испанский язык
    # ------------------------------------------------------------------

    def test_spanish_positive_words(self) -> None:
        result = self.detector.detect("Excelente trabajo, fantástico", language="es")
        self.assertIn(result.primary_emotion, ("positive", "excited"))
        self.assertGreater(result.confidence, 0.0)

    def test_spanish_negative_words(self) -> None:
        result = self.detector.detect("Terrible, malo y horrible", language="es")
        self.assertEqual(result.primary_emotion, "negative")
        self.assertGreater(result.confidence, 0.3)

    # ------------------------------------------------------------------
    # 9. caps_ratio вычисление
    # ------------------------------------------------------------------

    def test_caps_ratio_all_upper(self) -> None:
        result = self.detector.detect("HELLO WORLD")
        self.assertAlmostEqual(result.caps_ratio, 1.0, places=1)

    def test_caps_ratio_all_lower(self) -> None:
        result = self.detector.detect("hello world")
        self.assertAlmostEqual(result.caps_ratio, 0.0, places=1)

    def test_caps_ratio_mixed(self) -> None:
        result = self.detector.detect("Hello World")
        self.assertGreater(result.caps_ratio, 0.0)
        self.assertLess(result.caps_ratio, 1.0)

    # ------------------------------------------------------------------
    # 10. Комбинированные сигналы: восклицание + позитив → excited
    # ------------------------------------------------------------------

    def test_exclamation_plus_positive_yields_excited(self) -> None:
        result = self.detector.detect("Отлично! Здорово! Круто!")
        self.assertEqual(result.primary_emotion, "excited")
        self.assertGreater(result.exclamation_count, 0)

    # ------------------------------------------------------------------
    # 11. indicators содержат найденные слова
    # ------------------------------------------------------------------

    def test_indicators_contain_triggered_words(self) -> None:
        result = self.detector.detect("Ужасно плохо, всё провалилось")
        self.assertTrue(
            any("ужасно" in ind or "плохо" in ind or "провал" in ind for ind in result.indicators),
            f"Expected негативные слова в indicators, got: {result.indicators}",
        )

    def test_indicators_contain_exclamation_marker(self) -> None:
        result = self.detector.detect("Вперёд!")
        self.assertTrue(
            any("exclamation" in ind for ind in result.indicators),
            f"Expected exclamation_marks в indicators, got: {result.indicators}",
        )

    # ------------------------------------------------------------------
    # 12. EmotionResult dataclass структура
    # ------------------------------------------------------------------

    def test_result_dataclass_fields(self) -> None:
        result = self.detector.detect("тест")
        self.assertIsInstance(result, EmotionResult)
        self.assertIsInstance(result.primary_emotion, str)
        self.assertIsInstance(result.confidence, float)
        self.assertIsInstance(result.indicators, list)
        self.assertIsInstance(result.exclamation_count, int)
        self.assertIsInstance(result.question_count, int)
        self.assertIsInstance(result.caps_ratio, float)

    def test_result_emotion_is_valid_value(self) -> None:
        valid_emotions = {"neutral", "positive", "negative", "excited", "frustrated", "questioning"}
        for text in ["всё хорошо!", "ужасно?", "СТОП", "нейтральный текст", ""]:
            result = self.detector.detect(text)
            self.assertIn(
                result.primary_emotion,
                valid_emotions,
                f"Unexpected emotion {result.primary_emotion!r} for text {text!r}",
            )

    # ------------------------------------------------------------------
    # 13. language fallback: неизвестный язык → English
    # ------------------------------------------------------------------

    def test_unknown_language_falls_back_gracefully(self) -> None:
        result = self.detector.detect("good excellent", language="jp")
        # Не должно упасть, возвращает валидный результат
        self.assertIsInstance(result.primary_emotion, str)

    # ------------------------------------------------------------------
    # 14. Несколько вопросов увеличивают уверенность
    # ------------------------------------------------------------------

    def test_multiple_questions_increase_confidence(self) -> None:
        single = self.detector.detect("Как дела?")
        multiple = self.detector.detect("Как дела? Где ты? Когда придёшь?")
        self.assertGreaterEqual(multiple.confidence, single.confidence)


class TestEmotionDetectorIPC(unittest.TestCase):
    """Тесты IPC-метода detect_emotion через BackendService."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from backend.state_store import StateStore
        from backend.service import BackendService

        self.store = StateStore(data_dir=Path(self._tmp.name))
        self.service = BackendService(store=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_detect_emotion_ipc_basic(self) -> None:
        resp = self.service.handle_request({
            "id": "1",
            "method": "detect_emotion",
            "params": {"text": "Отлично, всё работает!", "language": "ru"},
        })
        self.assertTrue(resp.get("ok"), f"IPC failed: {resp}")
        result = resp["result"]
        self.assertIn("primary_emotion", result)
        self.assertIn("confidence", result)
        self.assertIn("indicators", result)
        self.assertIn("exclamation_count", result)
        self.assertIn("question_count", result)
        self.assertIn("caps_ratio", result)

    def test_detect_emotion_ipc_empty_text(self) -> None:
        resp = self.service.handle_request({
            "id": "2",
            "method": "detect_emotion",
            "params": {"text": "", "language": "ru"},
        })
        self.assertTrue(resp.get("ok"), f"IPC failed: {resp}")
        self.assertEqual(resp["result"]["primary_emotion"], "neutral")

    def test_detect_emotion_ipc_negative(self) -> None:
        resp = self.service.handle_request({
            "id": "3",
            "method": "detect_emotion",
            "params": {"text": "Всё ужасно, плохо и невозможно"},
        })
        self.assertTrue(resp.get("ok"), f"IPC failed: {resp}")
        self.assertEqual(resp["result"]["primary_emotion"], "negative")

    def test_detect_emotion_ipc_default_language(self) -> None:
        """Если language не передан — используется 'ru' по умолчанию."""
        resp = self.service.handle_request({
            "id": "4",
            "method": "detect_emotion",
            "params": {"text": "Здорово!"},
        })
        self.assertTrue(resp.get("ok"), f"IPC failed: {resp}")
        self.assertIn(resp["result"]["primary_emotion"], ("excited", "positive"))


if __name__ == "__main__":
    unittest.main()
