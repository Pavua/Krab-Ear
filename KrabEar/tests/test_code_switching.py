"""Тесты для CodeSwitchingDetector и хука build_initial_prompt.

Покрывает:
- Чистый русский текст -> is_mixed=False
- Чистый английский текст -> is_mixed=False
- Смешанный RU+EN 30% -> is_mixed=True
- Смешанный RU+EN 5% (ниже порога) -> is_mixed=False
- camelCase / snake_case -> не считаются латинскими словами
- Кириллица + латинский URL -> URL исключён из подсчёта
- Пустой текст -> is_mixed=False, primary_lang=unknown
- build_initial_prompt: hint добавляется если last item mixed
- build_initial_prompt: hint НЕ добавляется если last item pure RU
- build_initial_prompt: hint НЕ добавляется при code_switching_detect=False
- switch_ratio корректен для 50% смешения
- primary_lang определяется правильно при EN-доминантном тексте
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.code_switching_detector import CodeSwitchingDetector
from core.transcript_context import build_initial_prompt, _CODE_SWITCHING_HINT


class TestCodeSwitchingDetectorPureTexts(unittest.TestCase):
    """Тесты на однородные тексты."""

    def setUp(self) -> None:
        self.det = CodeSwitchingDetector()

    def test_pure_russian(self) -> None:
        """Чисто русский текст не является code-switching."""
        result = self.det.analyze(
            "я пошёл в магазин купить хлеб и молоко сегодня вечером"
        )
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        self.assertIsNone(result["secondary_lang"])
        self.assertEqual(result["switch_ratio"], 0.0)

    def test_pure_english(self) -> None:
        """Чисто английский текст не является code-switching."""
        result = self.det.analyze(
            "I went to the store to buy bread and milk this evening"
        )
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "en")
        self.assertIsNone(result["secondary_lang"])
        self.assertEqual(result["switch_ratio"], 0.0)

    def test_empty_text(self) -> None:
        """Пустой текст -> unknown, не mixed."""
        result = self.det.analyze("")
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "unknown")
        self.assertIsNone(result["secondary_lang"])

    def test_whitespace_only(self) -> None:
        """Текст из пробелов -> unknown, не mixed."""
        result = self.det.analyze("   \t\n  ")
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "unknown")


class TestCodeSwitchingDetectorMixedTexts(unittest.TestCase):
    """Тесты на смешанные тексты."""

    def setUp(self) -> None:
        self.det = CodeSwitchingDetector(switch_threshold=0.10)

    def test_ru_en_mix_30_percent(self) -> None:
        """RU+EN смешение ~30% -> is_mixed=True."""
        # Примерно 7 русских слов + 3 английских = 30% EN
        result = self.det.analyze(
            "я запушил коммит в main репозиторий сделал pull request готово"
        )
        self.assertTrue(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        self.assertEqual(result["secondary_lang"], "en")
        self.assertGreater(result["switch_ratio"], 0.0)

    def test_ru_en_mix_5_percent_below_threshold(self) -> None:
        """RU+EN смешение 5% (ниже порога 10%) -> is_mixed=False."""
        # 19 русских слов + 1 английское = ~5% EN
        result = self.det.analyze(
            "сегодня я пошёл на работу и сделал очень много важных дел "
            "потом выпил чай test"
        )
        self.assertFalse(result["is_mixed"])
        # switch_ratio должен быть < 0.10
        self.assertLess(result["switch_ratio"], 0.10)

    def test_switch_ratio_accuracy(self) -> None:
        """switch_ratio отражает долю вторичного языка."""
        # 4 RU + 4 EN = 50% ratio (4 cyrillic + 4 latin)
        result = self.det.analyze(
            "привет мир здравствуй друг git push commit merge"
        )
        self.assertTrue(result["is_mixed"])
        # switch_ratio = 4/8 = 0.5
        self.assertAlmostEqual(result["switch_ratio"], 0.5, delta=0.05)

    def test_primary_lang_en_dominant(self) -> None:
        """При EN-доминантном тексте с небольшим RU -- primary_lang='en'."""
        # 7 EN + 2 RU = ~22% RU -> primary_lang = en
        result = self.det.analyze(
            "I just pushed the commit and merged the branch привет мир"
        )
        self.assertEqual(result["primary_lang"], "en")
        self.assertEqual(result["secondary_lang"], "ru")
        self.assertTrue(result["is_mixed"])


class TestCodeSwitchingDetectorSpecialTokens(unittest.TestCase):
    """Тесты на технические токены."""

    def setUp(self) -> None:
        self.det = CodeSwitchingDetector(switch_threshold=0.10)

    def test_camel_case_preserved(self) -> None:
        """camelCase-идентификаторы не считаются латинскими словами."""
        # "getUserName" и "myVariable" -- camelCase, не должны детектироваться как EN
        result = self.det.analyze(
            "функция getUserName возвращает myVariable из базы данных"
        )
        # Без учёта camelCase -> только RU слова -> is_mixed=False
        self.assertFalse(result["is_mixed"])

    def test_snake_case_preserved(self) -> None:
        """snake_case-идентификаторы не считаются латинскими словами."""
        result = self.det.analyze(
            "вызов функции get_user_name возвращает my_variable правильно"
        )
        self.assertFalse(result["is_mixed"])

    def test_cyrillic_with_url_excluded(self) -> None:
        """URL не должен считаться латинским словом."""
        result = self.det.analyze(
            "посмотри документацию на https://docs.python.org/3/ там всё написано"
        )
        # URL исключён -> только RU слова
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")


class TestBuildInitialPromptCodeSwitching(unittest.TestCase):
    """Тесты хука code-switching в build_initial_prompt."""

    def _make_item(self, text: str, ts: str | None = None) -> dict:
        if ts is None:
            import datetime
            ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        return {"text": text, "ts": ts}

    def test_hint_added_when_last_item_mixed(self) -> None:
        """Hint добавляется в prompt когда последний item mixed."""
        items = [
            self._make_item("я запушил коммит в main репозиторий делаю pull request"),
        ]
        prompt = build_initial_prompt(
            items,
            code_switching_detect=True,
            code_switching_threshold=0.10,
        )
        self.assertIn(_CODE_SWITCHING_HINT, prompt)

    def test_hint_not_added_when_last_item_pure_ru(self) -> None:
        """Hint НЕ добавляется когда последний item чисто русский."""
        items = [
            self._make_item("сегодня хорошая погода и я пошёл на прогулку в парк"),
        ]
        prompt = build_initial_prompt(
            items,
            code_switching_detect=True,
            code_switching_threshold=0.10,
        )
        self.assertNotIn(_CODE_SWITCHING_HINT, prompt)

    def test_hint_not_added_when_detect_disabled(self) -> None:
        """Hint НЕ добавляется при code_switching_detect=False."""
        items = [
            self._make_item("я запушил коммит в main репозиторий делаю pull request"),
        ]
        prompt = build_initial_prompt(
            items,
            code_switching_detect=False,
            code_switching_threshold=0.10,
        )
        self.assertNotIn(_CODE_SWITCHING_HINT, prompt)

    def test_hint_not_added_when_empty_history(self) -> None:
        """Hint НЕ добавляется при пустой истории."""
        prompt = build_initial_prompt(
            [],
            code_switching_detect=True,
        )
        self.assertNotIn(_CODE_SWITCHING_HINT, prompt)

    def test_prompt_still_contains_context_when_hint_added(self) -> None:
        """Базовый контекст (Previous transcript) присутствует вместе с hint."""
        items = [
            self._make_item("я запушил коммит в main репозиторий делаю pull request"),
        ]
        prompt = build_initial_prompt(
            items,
            code_switching_detect=True,
            code_switching_threshold=0.10,
        )
        self.assertIn("Previous transcript:", prompt)
        self.assertIn(_CODE_SWITCHING_HINT, prompt)


if __name__ == "__main__":
    unittest.main()
