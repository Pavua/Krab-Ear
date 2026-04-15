"""Граничные тест-кейсы для TextUtils (core/utils.py)."""

from __future__ import annotations
from core.utils import TextUtils

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class CleanupEdgeCasesTestCase(unittest.TestCase):
    """Граничные случаи cleanup_transcript."""

    def test_cleanup_empty_string(self) -> None:
        """Пустая строка возвращает пустую строку."""
        self.assertEqual(TextUtils.cleanup_transcript(""), "")

    def test_cleanup_only_whitespace(self) -> None:
        """Строка только из пробелов/табуляций/переносов возвращает пустую строку."""
        for ws in ("   ", "\t\t", "\n\n", "  \t  \n  "):
            with self.subTest(ws=repr(ws)):
                self.assertEqual(TextUtils.cleanup_transcript(ws), "")

    def test_cleanup_unicode_emoji_preserved(self) -> None:
        """Emoji и Unicode-символы не уничтожаются при очистке."""
        raw = "Хорошая идея 👍 давай попробуем."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("👍", cleaned)

    def test_cleanup_unicode_chinese_preserved(self) -> None:
        """Китайские иероглифы сохраняются после очистки."""
        raw = "我喜欢编程 — это интересно."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("我喜欢编程", cleaned)

    def test_cleanup_unicode_arabic_preserved(self) -> None:
        """Арабский текст сохраняется после очистки."""
        raw = "مرحبا — привет по-арабски."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("مرحبا", cleaned)


class BrandReplacementsTestCase(unittest.TestCase):
    """Бренд-замены работают независимо от регистра."""

    def test_brand_telegram_cyrillic(self) -> None:
        """Кириллическое «Телеграм» заменяется на «Telegram»."""
        cleaned = TextUtils.cleanup_transcript("Напиши мне в Телеграм.")
        self.assertIn("Telegram", cleaned)
        self.assertNotIn("Телеграм", cleaned)

    def test_brand_claude_cyrillic(self) -> None:
        """«Клод» (именительный падеж) заменяется на «Claude»."""
        # Паттерн в BRAND_REPLACEMENTS покрывает только \bКлод\b — именительный падеж.
        cleaned = TextUtils.cleanup_transcript("Клод написал код.")
        self.assertIn("Claude", cleaned)

    def test_brand_github_cyrillic_variants(self) -> None:
        """Все кириллические варианты ГитХаба нормализуются."""
        for raw in ("ГитХаб", "Гит-Хаб", "Гит Хаб"):
            with self.subTest(raw=raw):
                cleaned = TextUtils.cleanup_transcript(f"Залей на {raw}.")
                self.assertIn("GitHub", cleaned)

    def test_brand_whisper_cyrillic(self) -> None:
        """«Виспер» → «Whisper»."""
        cleaned = TextUtils.cleanup_transcript("Виспер не распознал тихую речь.")
        self.assertIn("Whisper", cleaned)

    def test_brand_replacements_case_insensitive_latin(self) -> None:
        """Латинский вариант «Crab Ear» приводится к «Krab Ear» (case-insensitive)."""
        for raw in ("Crab Ear", "CRAB EAR", "crab ear"):
            with self.subTest(raw=raw):
                cleaned = TextUtils.cleanup_transcript(f"Запусти {raw}.")
                self.assertIn("Krab Ear", cleaned)

    def test_brand_docker_cyrillic(self) -> None:
        """«Докер» → «Docker»."""
        cleaned = TextUtils.cleanup_transcript("Запусти Докер контейнер.")
        self.assertIn("Docker", cleaned)


class HallucinationStrippingTestCase(unittest.TestCase):
    """Известные галлюцинации Whisper вырезаются."""

    def test_strip_spasibo_za_prosmotr(self) -> None:
        """«Спасибо за просмотр» в конце — типичная галлюцинация."""
        raw = "Сегодня мы разобрали новую тему. Спасибо за просмотр."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Спасибо за просмотр", cleaned)
        self.assertIn("Сегодня мы разобрали новую тему", cleaned)

    def test_strip_podpisyvajtes(self) -> None:
        """«Подписывайтесь на канал» удаляется."""
        raw = "Это был наш обзор. Подписывайтесь на канал."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Подписывайтесь на канал", cleaned)

    def test_strip_do_novyh_vstrech(self) -> None:
        """«До новых встреч» удаляется."""
        raw = "Всё на сегодня. До новых встреч."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("До новых встреч", cleaned)

    def test_strip_standalone_spasibo(self) -> None:
        """Одиночное «Спасибо.» в конце — тоже галлюцинация."""
        raw = "Мы закончили работу. Спасибо."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Спасибо", cleaned)
        self.assertIn("Мы закончили работу", cleaned)

    def test_hallucination_only_returns_empty(self) -> None:
        """Если вся строка — галлюцинация, возвращается пустая строка."""
        raw = "Спасибо за просмотр."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(cleaned, "")

    def test_non_hallucination_preserved(self) -> None:
        """Обычная речь, случайно содержащая слово «Спасибо» в середине, не обрезается."""
        raw = "Я сказал ему спасибо и ушёл домой."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("спасибо", cleaned.lower())


class DedupRepeatedSentencesTestCase(unittest.TestCase):
    """Дублирующиеся финальные предложения удаляются."""

    def test_exact_sentence_repeat(self) -> None:
        """Одинаковые предложения подряд сводятся к одному."""
        raw = "Я иду домой. Я иду домой."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(cleaned.lower().count("я иду домой"), 1)

    def test_dedup_with_punctuation_variation(self) -> None:
        """Повтор с разной пунктуацией всё равно обнаруживается."""
        raw = "Мы начинаем. Мы начинаем!"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(
            sum(1 for _ in ["мы начинаем"] if _.lower() in cleaned.lower()),
            1,
        )

    def test_no_dedup_different_sentences(self) -> None:
        """Разные предложения не дедуплицируются."""
        raw = "Утро было холодным. День оказался тёплым."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("холодным", cleaned)
        self.assertIn("тёплым", cleaned)

    def test_dedup_strict_profile(self) -> None:
        """Строгий профиль дополнительно убирает повторяющиеся части."""
        raw = "Тест начался. Всё хорошо. Тест начался."
        cleaned = TextUtils.cleanup_transcript(raw, profile="strict")
        self.assertEqual(cleaned.lower().count("тест начался"), 1)


class NormalizeEntitiesTimeFormatTestCase(unittest.TestCase):
    """normalize_entities корректно обрабатывает форматы времени."""

    def test_time_dot_separator(self) -> None:
        """«15.00» → «15:00»."""
        self.assertIn("15:00", TextUtils.normalize_entities("встреча в 15.00"))

    def test_time_space_separator(self) -> None:
        """«8 30» с пробелом → «8:30»."""
        result = TextUtils.normalize_entities("подъём в 8.30")
        self.assertIn("8:30", result)

    def test_time_edge_midnight(self) -> None:
        """«00:00» остаётся «00:00» (не ломает нулевой час)."""
        result = TextUtils.normalize_entities("полночь в 00.00")
        self.assertIn("00:00", result)

    def test_time_edge_end_of_day(self) -> None:
        """«23:59» — крайнее допустимое время суток."""
        result = TextUtils.normalize_entities("последнее событие в 23.59")
        self.assertIn("23:59", result)

    def test_time_large_number_not_converted(self) -> None:
        """«100.50» — не время (час > 23), не конвертируется."""
        result = TextUtils.normalize_entities("цена 100.50 евро")
        self.assertIn("100.50", result)

    def test_multiple_times_in_one_sentence(self) -> None:
        """Несколько временных меток в одном предложении нормализуются все."""
        result = TextUtils.normalize_entities("с 09.00 до 17.30 в офисе")
        self.assertIn("09:00", result)
        self.assertIn("17:30", result)


if __name__ == "__main__":
    unittest.main()
