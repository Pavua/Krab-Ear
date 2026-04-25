"""Тесты для core.transcript_context.build_initial_prompt."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from core.transcript_context import build_initial_prompt  # noqa: E402


def _ts(offset_seconds: float = 0.0) -> str:
    """Возвращает ISO-8601 UTC строку для (now - offset_seconds).

    Использует calendar.timegm чтобы строка соответствовала UTC epoch,
    что совпадает с форматом StateStore (UTC naive).
    """
    import datetime
    import calendar  # noqa: F401 — used in _iso_to_epoch, imported here for clarity
    epoch = time.time() - offset_seconds
    dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=epoch)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _item(text: str, offset_seconds: float = 0.0) -> dict:
    return {"text": text, "ts": _ts(offset_seconds)}


class TestBuildInitialPromptEmpty(unittest.TestCase):
    """Тест 1: пустая история → пустая строка."""

    def test_empty_history_returns_empty_string(self):
        result = build_initial_prompt(history_items=[])
        self.assertEqual(result, "")

    def test_none_equivalent_to_empty(self):
        # hotwords=None не должен падать
        result = build_initial_prompt(history_items=[], hotwords=None)
        self.assertEqual(result, "")

    def test_empty_hotwords_and_empty_history(self):
        result = build_initial_prompt(history_items=[], hotwords=[])
        self.assertEqual(result, "")


class TestBuildInitialPromptContext(unittest.TestCase):
    """Тесты 2-4: использование истории."""

    def test_last_5_items_included(self):
        items = [_item(f"слово{i}") for i in range(5)]
        result = build_initial_prompt(items)
        for i in range(5):
            self.assertIn(f"слово{i}", result)

    def test_context_in_previous_transcript_section(self):
        items = [_item("привет мир")]
        result = build_initial_prompt(items)
        self.assertIn("Previous transcript:", result)
        self.assertIn("привет мир", result)

    def test_truncation_at_max_words(self):
        # 500 слов в одном элементе, max_words=10 → только 10 слов в контексте
        many_words = " ".join(f"word{i}" for i in range(500))
        items = [_item(many_words)]
        result = build_initial_prompt(items, max_words=10)
        # Извлекаем часть после "Previous transcript:"
        context_part = result.split("Previous transcript:")[-1].strip()
        words_in_result = context_part.split()
        self.assertLessEqual(len(words_in_result), 10)

    def test_old_items_skipped(self):
        # Элемент старше 31 минуты должен быть исключён
        old_item = _item("старый текст", offset_seconds=31 * 60)
        fresh_item = _item("свежий текст", offset_seconds=5)
        # StateStore возвращает newest-first, поэтому [fresh, old]
        result = build_initial_prompt([fresh_item, old_item])
        self.assertIn("свежий текст", result)
        self.assertNotIn("старый текст", result)

    def test_history_limit_respected(self):
        # Передаём 20 элементов, history_limit=5 → используем только 5
        items = [_item(f"item{i}") for i in range(20)]
        result = build_initial_prompt(items, history_limit=5)
        # Последние 5 (индексы 0-4 в newest-first списке) должны присутствовать
        for i in range(5):
            self.assertIn(f"item{i}", result)
        # Элемент за пределами limit (индекс 5+) не должен войти
        self.assertNotIn("item5", result)


class TestBuildInitialPromptHotwords(unittest.TestCase):
    """Тесты 5-7: hotwords prefix."""

    def test_hotwords_prefix_correct(self):
        items = [_item("тест")]
        result = build_initial_prompt(items, hotwords=["Krab Ear", "Torrevieja"])
        self.assertTrue(result.startswith("Glossary:"))
        self.assertIn("Krab Ear", result)
        self.assertIn("Torrevieja", result)

    def test_hotwords_without_history(self):
        # Только hotwords, без контекста → только Glossary без Previous transcript
        result = build_initial_prompt([], hotwords=["Дашуля", "Pablito"])
        self.assertIn("Glossary:", result)
        self.assertIn("Дашуля", result)
        self.assertIn("Pablito", result)
        self.assertNotIn("Previous transcript:", result)

    def test_hotwords_and_context_combined(self):
        items = [_item("Краб слушает")]
        result = build_initial_prompt(items, hotwords=["KrabEar"])
        self.assertIn("Glossary:", result)
        self.assertIn("KrabEar", result)
        self.assertIn("Previous transcript:", result)
        self.assertIn("Краб слушает", result)

    def test_empty_hotword_strings_ignored(self):
        # Пустые строки в hotwords не должны попасть в Glossary
        result = build_initial_prompt([], hotwords=["", "  ", "ValidWord"])
        self.assertIn("ValidWord", result)
        self.assertNotIn("  ,", result)

    def test_dict_and_dataclass_items_both_work(self):
        # dataclass-like объект с атрибутами
        class FakeItem:
            def __init__(self, text: str, ts: str) -> None:
                self.text = text
                self.ts = ts

        obj_item = FakeItem("объект история", _ts(10))
        dict_item = _item("словарь история", offset_seconds=5)
        result = build_initial_prompt([dict_item, obj_item])
        self.assertIn("объект история", result)
        self.assertIn("словарь история", result)


if __name__ == "__main__":
    unittest.main()
