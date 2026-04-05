"""Тесты постобработки транскрибации для удаления хвостовых артефактов."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine


class EngineCleanupTestCase(unittest.TestCase):
    """Проверяет, что cleanup убирает типичные хвостовые повторы."""

    def test_removes_repeated_last_sentence(self) -> None:
        raw = "Сегодня отличный день для работы. Сегодня отличный день для работы."
        cleaned = AudioEngine._cleanup_transcript(raw)
        self.assertEqual(cleaned, "Сегодня отличный день для работы")

    def test_removes_short_doubled_tail(self) -> None:
        raw = "Запиши, пожалуйста, эту задачу в список сделай это сделай это"
        cleaned = AudioEngine._cleanup_transcript(raw)
        self.assertEqual(cleaned, "Запиши, пожалуйста, эту задачу в список")

    def test_keeps_normal_text(self) -> None:
        raw = "Нужно отправить отчёт до шести вечера, а потом позвонить Анне."
        cleaned = AudioEngine._cleanup_transcript(raw)
        self.assertEqual(cleaned, "Нужно отправить отчёт до шести вечера, а потом позвонить Анне.")

    def test_strict_removes_non_adjacent_tail_repeat(self) -> None:
        raw = "План такой: утром спорт. Днем работа. утром спорт."
        cleaned = AudioEngine._cleanup_transcript(raw, cleanup_profile="strict")
        self.assertEqual(cleaned, "План такой: утром спорт. Днем работа")

    def test_soft_keeps_non_adjacent_tail_repeat(self) -> None:
        raw = "План такой: утром спорт. Днем работа. утром спорт."
        cleaned = AudioEngine._cleanup_transcript(raw, cleanup_profile="soft")
        self.assertEqual(cleaned, "План такой: утром спорт. Днем работа. утром спорт.")

    def test_soft_removes_tripled_tail(self) -> None:
        raw = "Добавь в заметки важный пункт сделай это сделай это сделай это"
        cleaned = AudioEngine._cleanup_transcript(raw, cleanup_profile="soft")
        self.assertEqual(cleaned, "Добавь в заметки важный пункт сделай это")

    def test_strict_removes_known_hallucination_tail(self) -> None:
        raw = "Сегодня обсудили три важных вопроса и договорились о сроках. Спасибо за внимание."
        cleaned = AudioEngine._cleanup_transcript(raw, cleanup_profile="strict")
        self.assertEqual(cleaned, "Сегодня обсудили три важных вопроса и договорились о сроках")

    def test_soft_removes_continuation_hallucination_tail(self) -> None:
        raw = "Сейчас закончу мысль и отправлю итог. Продолжение следует..."
        cleaned = AudioEngine._cleanup_transcript(raw, cleanup_profile="soft")
        self.assertEqual(cleaned, "Сейчас закончу мысль и отправлю итог")

    def test_soft_drops_pure_continuation_hallucination(self) -> None:
        raw = "Продолжение следует..."
        cleaned = AudioEngine._cleanup_transcript(raw, cleanup_profile="soft")
        self.assertEqual(cleaned, "")


if __name__ == "__main__":
    unittest.main()
