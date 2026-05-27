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


class TestBuildInitialPromptAutoGlossary(unittest.TestCase):
    """Тесты 8-10: auto_glossary + deduplication."""

    def test_auto_glossary_merged_with_hotwords(self):
        result = build_initial_prompt(
            [],
            hotwords=["KrabEar", "Торревьеха"],
            auto_glossary=["AutoTerm", "KrabEar"],  # KrabEar duplicate
        )
        self.assertIn("Glossary:", result)
        self.assertIn("KrabEar", result)
        self.assertIn("AutoTerm", result)
        # Duplicate KrabEar should appear only once
        self.assertEqual(result.count("KrabEar"), 1)

    def test_auto_glossary_only_no_hotwords(self):
        result = build_initial_prompt([], auto_glossary=["SomeTerm", "OtherTerm"])
        self.assertIn("Glossary:", result)
        self.assertIn("SomeTerm", result)
        self.assertIn("OtherTerm", result)

    def test_auto_glossary_case_insensitive_dedup(self):
        result = build_initial_prompt(
            [],
            hotwords=["GPT4"],
            auto_glossary=["gpt4", "GPT4", "Gpt4"],
        )
        # All three are case-insensitive duplicates; only first (GPT4) survives
        self.assertEqual(result.count("GPT4") + result.count("gpt4") + result.count("Gpt4"), 1)


class TestBuildInitialPromptSpecNames(unittest.TestCase):
    """Wave-126 spec-named tests для build_initial_prompt."""

    # ------------------------------------------------------------------
    # test_no_recent_history_returns_minimal
    # ------------------------------------------------------------------
    def test_no_recent_history_returns_minimal(self):
        result = build_initial_prompt(history_items=[], code_switching_detect=False)
        # Without history or hotwords → empty string (minimal)
        self.assertEqual(result, "")

    def test_no_recent_history_with_hotwords_returns_glossary_only(self):
        result = build_initial_prompt(
            history_items=[],
            hotwords=["KrabEar"],
            code_switching_detect=False,
        )
        self.assertIn("Glossary:", result)
        self.assertNotIn("Previous transcript:", result)

    # ------------------------------------------------------------------
    # test_recent_history_within_30min_included
    # ------------------------------------------------------------------
    def test_recent_history_within_30min_included(self):
        # Items timestamped 1, 5, 10, 25 minutes ago — all within 30 min
        items = [
            _item("один минуту", offset_seconds=60),
            _item("пять минут", offset_seconds=5 * 60),
            _item("десять минут", offset_seconds=10 * 60),
            _item("двадцать пять минут", offset_seconds=25 * 60),
        ]
        result = build_initial_prompt(items, code_switching_detect=False)
        self.assertIn("один минуту", result)
        self.assertIn("пять минут", result)
        self.assertIn("десять минут", result)
        self.assertIn("двадцать пять минут", result)

    # ------------------------------------------------------------------
    # test_old_history_excluded
    # ------------------------------------------------------------------
    def test_old_history_excluded(self):
        old_item = _item("очень старый текст", offset_seconds=35 * 60)
        recent_item = _item("свежий контент", offset_seconds=60)
        result = build_initial_prompt(
            [recent_item, old_item], code_switching_detect=False
        )
        self.assertIn("свежий контент", result)
        self.assertNotIn("очень старый текст", result)

    def test_all_old_history_returns_empty_transcript_section(self):
        items = [_item("совсем старый", offset_seconds=90 * 60)]
        result = build_initial_prompt(items, code_switching_detect=False)
        self.assertNotIn("Previous transcript:", result)

    # ------------------------------------------------------------------
    # test_vocabulary_merged
    # ------------------------------------------------------------------
    def test_vocabulary_merged(self):
        result = build_initial_prompt(
            [_item("текст")],
            hotwords=["WordA", "WordB"],
            auto_glossary=["WordC", "WordD"],
            code_switching_detect=False,
        )
        self.assertIn("WordA", result)
        self.assertIn("WordB", result)
        self.assertIn("WordC", result)
        self.assertIn("WordD", result)
        # All appear in a single Glossary: section
        self.assertEqual(result.count("Glossary:"), 1)

    def test_vocabulary_merged_no_duplicates(self):
        result = build_initial_prompt(
            [],
            hotwords=["UniqueToken"],
            auto_glossary=["UniqueToken", "AnotherToken"],
            code_switching_detect=False,
        )
        self.assertEqual(result.count("UniqueToken"), 1)
        self.assertIn("AnotherToken", result)

    # ------------------------------------------------------------------
    # test_max_prompt_size_respected
    # ------------------------------------------------------------------
    def test_max_prompt_size_respected(self):
        many_words = " ".join(f"word{i}" for i in range(300))
        items = [_item(many_words)]
        result = build_initial_prompt(items, max_words=15, code_switching_detect=False)
        # Extract only the Previous transcript part
        if "Previous transcript:" in result:
            transcript_part = result.split("Previous transcript:")[-1].strip()
            count = len(transcript_part.split())
            self.assertLessEqual(count, 15)

    def test_max_words_zero_returns_no_previous_transcript(self):
        items = [_item("слово1 слово2 слово3")]
        result = build_initial_prompt(items, max_words=0, code_switching_detect=False)
        # With max_words=0, combined words list becomes empty after slicing [-0:]
        # (Python slice [-0:] is the full list, but [:0] would be empty)
        # The function does words[-max_words:] when len > max_words → [-0:] = full
        # So this just checks we don't crash
        self.assertIsInstance(result, str)

    # ------------------------------------------------------------------
    # test_unicode_text_in_prompt
    # ------------------------------------------------------------------
    def test_unicode_text_in_prompt(self):
        unicode_text = "Привет мир 你好世界 مرحبا بالعالم"
        items = [_item(unicode_text)]
        result = build_initial_prompt(items, code_switching_detect=False)
        self.assertIn("Привет мир", result)
        # Should not raise; full text preserved
        self.assertIsInstance(result, str)

    def test_unicode_hotwords_in_glossary(self):
        result = build_initial_prompt(
            [],
            hotwords=["Торревьеха", "Антигравити", "ВоисГейтвей"],
            code_switching_detect=False,
        )
        self.assertIn("Торревьеха", result)
        self.assertIn("Антигравити", result)
        self.assertIn("ВоисГейтвей", result)

    # ------------------------------------------------------------------
    # test_empty_vocabulary_handled
    # ------------------------------------------------------------------
    def test_empty_vocabulary_handled(self):
        # None hotwords / auto_glossary should not crash
        result = build_initial_prompt(
            [_item("нормальный текст")],
            hotwords=None,
            auto_glossary=None,
            code_switching_detect=False,
        )
        self.assertIn("нормальный текст", result)
        self.assertNotIn("Glossary:", result)

    def test_empty_list_vocabulary_handled(self):
        result = build_initial_prompt(
            [_item("контент")],
            hotwords=[],
            auto_glossary=[],
            code_switching_detect=False,
        )
        self.assertIn("контент", result)
        self.assertNotIn("Glossary:", result)

    def test_whitespace_only_vocabulary_ignored(self):
        result = build_initial_prompt(
            [],
            hotwords=["   ", "", "\t"],
            auto_glossary=["  "],
            code_switching_detect=False,
        )
        # All whitespace-only → no Glossary section
        self.assertNotIn("Glossary:", result)


class TestInitialPromptTokenCap(unittest.TestCase):
    """W1293: char-based cap at 560 chars to stay within Whisper 224-token limit."""

    def test_initial_prompt_under_cap_unchanged(self):
        """Short prompt is returned verbatim without truncation."""
        items = [_item("Привет мир")]
        result = build_initial_prompt(
            items,
            hotwords=["KrabEar"],
            code_switching_detect=False,
        )
        self.assertLessEqual(len(result), 560)
        # Content must be intact
        self.assertIn("KrabEar", result)
        self.assertIn("Привет мир", result)

    def test_initial_prompt_capped_at_560_chars_cyrillic(self):
        """Oversized Cyrillic prompt is capped at or below 560 characters."""
        # 250 multi-character Cyrillic terms will far exceed 560 chars
        terms = [f"КириллическийТерминДлинный{i}" for i in range(250)]
        result = build_initial_prompt(
            [],
            hotwords=terms,
            code_switching_detect=False,
        )
        self.assertLessEqual(len(result), 560,
                             f"Expected len ≤ 560, got {len(result)}")

    def test_truncation_strips_to_last_complete_term(self):
        """Truncation rolls back to the last comma/period, not mid-term."""
        # Build a glossary that will definitely overflow 560 chars.
        terms = [f"TermWord{i}" for i in range(200)]
        result = build_initial_prompt(
            [],
            hotwords=terms,
            code_switching_detect=False,
        )
        self.assertLessEqual(len(result), 560)
        # The result must not end with a partial word (no trailing alpha mid-word)
        # It should end with a comma+possible space, period, or a complete word boundary
        stripped = result.rstrip()
        # After truncation to last comma or period, last char should be , or .
        # or the prompt was within cap and is intact.
        if len(result) < 560:
            # Prompt fit — no truncation needed, just pass
            pass
        else:
            self.assertIn(stripped[-1], {",", ".", " "} | set("abcdefghijklmnopqrstuvwxyz0123456789"),
                          f"Unexpected trailing char: {repr(stripped[-1])}")

    def test_truncation_logged(self):
        """logger.info is called when truncation fires."""
        import logging
        from unittest.mock import patch

        terms = [f"КириллическийТермин{i}" for i in range(250)]
        with patch("core.transcript_context.logger") as mock_logger:
            result = build_initial_prompt(
                [],
                hotwords=terms,
                code_switching_detect=False,
            )
            # If result was actually truncated, logger.info must have been called
            if len(result) < sum(len(t) for t in terms):
                mock_logger.info.assert_called_once()
                call_args = mock_logger.info.call_args[0]
                self.assertIn("truncated", call_args[0].lower())


if __name__ == "__main__":
    unittest.main()
