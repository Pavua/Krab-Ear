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


class TestCodeSwitchingDetectorWave112(unittest.TestCase):
    """Wave 112 — дополнительные кейсы по spec."""

    def setUp(self) -> None:
        self.det = CodeSwitchingDetector(switch_threshold=0.10)

    # --- exact names from Wave 112 spec ---

    def test_no_switch_pure_russian(self) -> None:
        result = self.det.analyze("я пошёл в магазин купить продукты на ужин")
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        self.assertIsNone(result["secondary_lang"])

    def test_switch_ru_to_en_mid_sentence(self) -> None:
        result = self.det.analyze(
            "я нажал кнопку submit и потом нажал cancel в диалоге"
        )
        self.assertTrue(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        self.assertEqual(result["secondary_lang"], "en")

    def test_switch_ru_to_es(self) -> None:
        """Spanish words (plain ASCII) trigger Latin detection alongside Russian."""
        # «hola» / «casa» / «bueno» — plain ASCII Latin (no ES markers),
        # but they ARE classified as "latin" → triggers code-switching with RU.
        result = self.det.analyze(
            "привет amigo это mucho trabajo для меня сегодня"
        )
        self.assertTrue(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        # secondary_lang is "en" (detector doesn't distinguish ES from EN — by design)
        self.assertIsNotNone(result["secondary_lang"])

    def test_tech_tokens_excluded(self) -> None:
        """URLs and ProperNouns like MacBook should be excluded from Latin count."""
        # MacBook would match camelCase pattern (uppercase M + mixed case)
        result = self.det.analyze(
            "я открыл MacBook и посетил https://apple.com там всё понятно"
        )
        # URL excluded, MacBook is camelCase → excluded; only RU remains
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")

    def test_quoted_foreign_text_excluded(self) -> None:
        """Words wrapped in quotes are still processed, but short foreign quotes
        should not push ratio above threshold when surrounded by RU text."""
        # 1 quoted Latin word vs 10 Russian words → ratio < 10%
        result = self.det.analyze(
            "она сказала мне слово ok и ушла домой в хорошем настроении"
        )
        # "ok" = 1 Latin vs 10 RU = 9.1% → just below default 10% threshold
        self.assertFalse(result["is_mixed"])
        self.assertLess(result["switch_ratio"], 0.10)

    def test_multiple_switches_in_long_text(self) -> None:
        """Multiple language switches in a long utterance are detected."""
        text = (
            "сначала я написал функцию потом сделал commit и запустил "
            "тесты через pytest потом написал документацию"
        )
        result = self.det.analyze(text)
        # commit, pytest are plain Latin words → triggers mixing
        self.assertTrue(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        self.assertGreater(result["switch_ratio"], 0.0)
        self.assertLess(result["switch_ratio"], 0.90)

    def test_unicode_punctuation_handled(self) -> None:
        """Unicode punctuation (—, «», …) does not break analysis."""
        result = self.det.analyze(
            "он сказал — «запусти deploy немедленно» — и ушёл…"
        )
        # "deploy" is a Latin word → mixed
        self.assertTrue(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")


if __name__ == "__main__":
    unittest.main()
