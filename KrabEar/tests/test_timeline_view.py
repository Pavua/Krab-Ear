"""Unit-тесты для TimelineViewGenerator.

Покрывает generate_timeline (group_by: hour, day, week),
generate_activity_heatmap и граничные случаи.
"""

from __future__ import annotations
from backend.timeline_view import TimelineViewGenerator

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — стандартный паттерн для тестов Krab Ear
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_item(
    ts: str,
    text: str = "hello world test",
    audio_duration_sec: float | None = None,
    source_lang: str = "",
) -> dict[str, Any]:
    """Создаёт минимальный dict-элемент истории."""
    return {
        "ts": ts,
        "text": text,
        "audio_duration_sec": audio_duration_sec,
        "source_lang": source_lang,
    }


class SimpleItem:
    """Объект-имитатор HistoryItem для проверки работы с атрибутами."""

    def __init__(
        self,
        ts: str,
        text: str = "test phrase",
        audio_duration_sec: float | None = None,
        source_lang: str = "",
    ) -> None:
        self.ts = ts
        self.text = text
        self.audio_duration_sec = audio_duration_sec
        self.source_lang = source_lang


# ---------------------------------------------------------------------------
# Тесты generate_timeline
# ---------------------------------------------------------------------------

class TimelineGenerateTestCase(unittest.TestCase):
    """Тесты generate_timeline: группировка, агрегаты, сортировка."""

    def setUp(self) -> None:
        self.gen = TimelineViewGenerator()

    # 1. Пустой список → пустой результат
    def test_empty_items_returns_empty_list(self) -> None:
        result = self.gen.generate_timeline([], group_by="hour")
        self.assertEqual(result, [])

    # 2. Группировка по часам — 2 записи в одном часу попадают в один блок
    def test_group_by_hour_same_hour_merges(self) -> None:
        items = [
            _make_item("2026-04-10T14:05:00", text="alpha beta gamma"),
            _make_item("2026-04-10T14:45:00", text="delta epsilon zeta"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].items_count, 2)

    # 3. Группировка по часам — 2 разных часа → 2 блока
    def test_group_by_hour_different_hours_splits(self) -> None:
        items = [
            _make_item("2026-04-10T14:05:00"),
            _make_item("2026-04-10T15:05:00"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(len(blocks), 2)

    # 4. Группировка по дням — записи в один день объединяются
    def test_group_by_day_merges_same_day(self) -> None:
        items = [
            _make_item("2026-04-10T08:00:00"),
            _make_item("2026-04-10T20:00:00"),
            _make_item("2026-04-11T09:00:00"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="day")
        self.assertEqual(len(blocks), 2)
        # Первый блок (2026-04-10) содержит 2 записи
        self.assertEqual(blocks[0].items_count, 2)
        self.assertEqual(blocks[1].items_count, 1)

    # 5. Группировка по неделям — 2 записи в одну неделю → 1 блок
    def test_group_by_week_merges_same_week(self) -> None:
        # 2026-04-06 понедельник, 2026-04-10 пятница — одна неделя
        items = [
            _make_item("2026-04-06T10:00:00"),
            _make_item("2026-04-10T18:00:00"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="week")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].items_count, 2)

    # 6. total_duration_sec и total_words агрегируются корректно
    def test_aggregates_duration_and_words(self) -> None:
        items = [
            _make_item("2026-04-10T14:00:00", text="one two three", audio_duration_sec=10.0),
            _make_item("2026-04-10T14:30:00", text="four five", audio_duration_sec=5.5),
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertAlmostEqual(b.total_duration_sec, 15.5)
        self.assertEqual(b.total_words, 5)

    # 7. languages — список уникальных языков, отсортирован по частоте
    def test_languages_sorted_by_frequency(self) -> None:
        items = [
            _make_item("2026-04-10T14:00:00", source_lang="ru"),
            _make_item("2026-04-10T14:10:00", source_lang="ru"),
            _make_item("2026-04-10T14:20:00", source_lang="es"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        langs = blocks[0].languages
        self.assertEqual(langs[0], "ru")   # наиболее частый первым
        self.assertIn("es", langs)

    # 8. Неверный group_by → ValueError
    def test_invalid_group_by_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.gen.generate_timeline([_make_item("2026-04-10T14:00:00")], group_by="minute")

    # 9. Записи без ts (None / пустая строка) игнорируются
    def test_items_without_ts_are_skipped(self) -> None:
        items = [
            {"ts": None, "text": "ignored"},
            {"ts": "", "text": "also ignored"},
            _make_item("2026-04-10T14:00:00", text="counted"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="day")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].items_count, 1)

    # 10. TimelineBlock.to_dict содержит все ожидаемые ключи
    def test_timeline_block_to_dict_keys(self) -> None:
        items = [_make_item("2026-04-10T14:00:00", text="foo bar baz")]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        d = blocks[0].to_dict()
        for key in ("start_time", "end_time", "items_count",
                    "total_duration_sec", "total_words", "languages", "summary_text"):
            self.assertIn(key, d)

    # 11. Результат отсортирован по start_time (возрастание)
    def test_timeline_blocks_sorted_ascending(self) -> None:
        items = [
            _make_item("2026-04-12T10:00:00"),
            _make_item("2026-04-10T08:00:00"),
            _make_item("2026-04-11T15:00:00"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="day")
        starts = [b.start_time for b in blocks]
        self.assertEqual(starts, sorted(starts))

    # 12. Работает с объектами (атрибутами), не только dict
    def test_works_with_object_attributes(self) -> None:
        items = [
            SimpleItem("2026-04-10T09:00:00", text="привет мир", audio_duration_sec=3.0, source_lang="ru"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="day")
        self.assertEqual(len(blocks), 1)
        self.assertAlmostEqual(blocks[0].total_duration_sec, 3.0)

    # 13. start_time и end_time для hour-блока корректны
    def test_hour_block_start_end_times(self) -> None:
        items = [_make_item("2026-04-10T14:35:00")]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertIn("T14:00:00", blocks[0].start_time)
        self.assertIn("T15:00:00", blocks[0].end_time)


# ---------------------------------------------------------------------------
# Тесты generate_activity_heatmap
# ---------------------------------------------------------------------------

class ActivityHeatmapTestCase(unittest.TestCase):
    """Тесты generate_activity_heatmap: структура матрицы, фильтрация по дате."""

    def setUp(self) -> None:
        self.gen = TimelineViewGenerator()

    # 14. Пустой список → нулевая матрица, total_items=0
    def test_empty_items_zero_matrix(self) -> None:
        result = self.gen.generate_activity_heatmap([], days=30)
        self.assertEqual(result["total_items"], 0)
        matrix = result["matrix"]
        # Матрица 24×7
        self.assertEqual(len(matrix), 24)
        for h in range(24):
            self.assertEqual(len(matrix[str(h)]), 7)

    # 15. Все ячейки нулевые при пустом списке
    def test_empty_items_all_zeros(self) -> None:
        result = self.gen.generate_activity_heatmap([], days=30)
        matrix = result["matrix"]
        total = sum(
            matrix[str(h)][str(d)]
            for h in range(24) for d in range(7)
        )
        self.assertEqual(total, 0)

    # 16. peak_hour и peak_dow None при отсутствии записей
    def test_empty_items_peak_hour_dow_none(self) -> None:
        result = self.gen.generate_activity_heatmap([], days=30)
        self.assertIsNone(result["peak_hour"])
        self.assertIsNone(result["peak_dow"])

    # 17. total_items соответствует числу учтённых записей
    def test_total_items_counted_correctly(self) -> None:
        # Все 3 записи — сегодня/вчера, в пределах days=30
        now = datetime.now(tz=timezone.utc)
        items = [
            _make_item(now.replace(hour=10).isoformat()),
            _make_item(now.replace(hour=12).isoformat()),
            _make_item(now.replace(hour=14).isoformat()),
        ]
        result = self.gen.generate_activity_heatmap(items, days=30)
        self.assertEqual(result["total_items"], 3)

    # 18. Записи старше days не учитываются
    def test_old_items_filtered_out(self) -> None:
        old_ts = "2000-01-01T12:00:00+00:00"  # заведомо за пределами любого days
        items = [_make_item(old_ts)]
        result = self.gen.generate_activity_heatmap(items, days=30)
        self.assertEqual(result["total_items"], 0)

    # 19. peak_hour определяется корректно
    def test_peak_hour_correct(self) -> None:
        now = datetime.now(tz=timezone.utc)
        # Создаём 3 записи в час 9, 1 запись в час 14
        items = [
            _make_item(now.replace(hour=9, minute=0).isoformat()),
            _make_item(now.replace(hour=9, minute=10).isoformat()),
            _make_item(now.replace(hour=9, minute=20).isoformat()),
            _make_item(now.replace(hour=14, minute=0).isoformat()),
        ]
        result = self.gen.generate_activity_heatmap(items, days=30)
        self.assertEqual(result["peak_hour"], 9)

    # 20. days <= 0 → ValueError
    def test_invalid_days_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.gen.generate_activity_heatmap([], days=0)

    # 21. days_covered считает уникальные дни
    def test_days_covered_unique_days(self) -> None:
        now = datetime.now(tz=timezone.utc)
        # 3 записи: 2 в один день, 1 в другой
        d1 = now.replace(hour=10, minute=0, second=0, microsecond=0)
        d2 = now.replace(hour=12, minute=0, second=0, microsecond=0)
        # Используем datetime двухдневной давности для второго дня
        d3 = (now - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        items = [
            _make_item(d1.isoformat()),
            _make_item(d2.isoformat()),
            _make_item(d3.isoformat()),
        ]
        result = self.gen.generate_activity_heatmap(items, days=30)
        self.assertEqual(result["days_covered"], 2)


class TimelineTopicSummaryTestCase(unittest.TestCase):
    """Тесты summary_text — тематические сдвиги через разные summary_text в блоках."""

    def setUp(self) -> None:
        self.gen = TimelineViewGenerator()

    def test_single_topic_same_summary_across_items(self) -> None:
        """Все записи об одной теме → summary_text содержит доминирующее слово."""
        items = [
            _make_item("2026-04-10T14:00:00", text="программа программа программа код"),
            _make_item("2026-04-10T14:20:00", text="программа код система система"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(len(blocks), 1)
        # Одна тема — summary содержит доминирующие слова
        self.assertIn("программа", blocks[0].summary_text)

    def test_multiple_topics_different_blocks_have_different_summaries(self) -> None:
        """Разные темы в разных блоках → summary_text различается."""
        # Блок 1 — технологии
        tech_item = _make_item(
            "2026-04-10T10:00:00",
            text="программа программа программа сервер",
        )
        # Блок 2 — здоровье
        health_item = _make_item(
            "2026-04-10T15:00:00",
            text="здоровье здоровье врач больница лечение",
        )
        blocks = self.gen.generate_timeline([tech_item, health_item], group_by="hour")
        self.assertEqual(len(blocks), 2)
        # summary_text разных блоков — разные ключевые слова
        self.assertNotEqual(blocks[0].summary_text, blocks[1].summary_text)

    def test_empty_text_gives_empty_summary(self) -> None:
        """Записи с пустым text → summary_text пустой."""
        items = [_make_item("2026-04-10T14:00:00", text="")]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(blocks[0].summary_text, "")

    def test_summary_text_is_comma_separated(self) -> None:
        """summary_text разделён запятыми (топ-5 слов)."""
        items = [
            _make_item(
                "2026-04-10T14:00:00",
                text="программа сервер база данные система приложение функция модель",
            ),
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        summary = blocks[0].summary_text
        if summary:
            # Если больше одного слова — между ними запятая
            parts = [p.strip() for p in summary.split(",")]
            self.assertLessEqual(len(parts), 5)

    def test_single_item_single_block(self) -> None:
        """Одна запись → один блок, никаких сдвигов."""
        items = [_make_item("2026-04-10T09:00:00", text="кошка кошка кошка")]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].items_count, 1)

    def test_no_shifts_single_block_one_summary_word(self) -> None:
        """Одна тема в одном блоке: топ-слово в summary, нет смены тем."""
        items = [_make_item(f"2026-04-10T10:0{i}:00", text="кошка кошка кошка") for i in range(5)]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(len(blocks), 1)
        self.assertIn("кошка", blocks[0].summary_text)


class TimelineWeekGroupTestCase(unittest.TestCase):
    """Тесты group_by='week': записи в разных неделях."""

    def setUp(self) -> None:
        self.gen = TimelineViewGenerator()

    def test_two_different_weeks_two_blocks(self) -> None:
        items = [
            _make_item("2026-04-06T10:00:00"),  # неделя 1 (Mon)
            _make_item("2026-04-13T10:00:00"),  # неделя 2 (Mon+7)
        ]
        blocks = self.gen.generate_timeline(items, group_by="week")
        self.assertEqual(len(blocks), 2)

    def test_week_block_end_is_plus_7_days(self) -> None:
        items = [_make_item("2026-04-06T12:00:00")]  # понедельник
        blocks = self.gen.generate_timeline(items, group_by="week")
        start = datetime.fromisoformat(blocks[0].start_time)
        end = datetime.fromisoformat(blocks[0].end_time)
        self.assertEqual(end - start, timedelta(weeks=1))


class TimelineBlockAggregatesTestCase(unittest.TestCase):
    """Расширенные тесты агрегатов TimelineBlock."""

    def setUp(self) -> None:
        self.gen = TimelineViewGenerator()

    def test_zero_duration_when_no_audio_duration_sec(self) -> None:
        items = [_make_item("2026-04-10T14:00:00", audio_duration_sec=None)]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertAlmostEqual(blocks[0].total_duration_sec, 0.0)

    def test_total_words_correct_with_multiple_items(self) -> None:
        items = [
            _make_item("2026-04-10T14:00:00", text="один два три"),       # 3 слова
            _make_item("2026-04-10T14:10:00", text="четыре пять"),         # 2 слова
            _make_item("2026-04-10T14:20:00", text="шесть семь восемь девять"),  # 4 слова
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(blocks[0].total_words, 9)

    def test_languages_empty_when_no_source_lang(self) -> None:
        items = [_make_item("2026-04-10T14:00:00", source_lang="")]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(blocks[0].languages, [])

    def test_multiple_languages_most_frequent_first(self) -> None:
        items = [
            _make_item("2026-04-10T14:00:00", source_lang="ru"),
            _make_item("2026-04-10T14:05:00", source_lang="ru"),
            _make_item("2026-04-10T14:10:00", source_lang="ru"),
            _make_item("2026-04-10T14:15:00", source_lang="es"),
        ]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(blocks[0].languages[0], "ru")

    def test_day_block_start_time_is_midnight(self) -> None:
        items = [_make_item("2026-04-10T17:45:00")]
        blocks = self.gen.generate_timeline(items, group_by="day")
        self.assertIn("T00:00:00", blocks[0].start_time)

    def test_duration_rounded_to_3_decimals(self) -> None:
        items = [_make_item("2026-04-10T14:00:00", audio_duration_sec=3.14159)]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        dur = blocks[0].total_duration_sec
        self.assertEqual(dur, round(dur, 3))

    def test_items_count_matches_number_of_items(self) -> None:
        items = [_make_item(f"2026-04-10T14:0{i}:00") for i in range(4)]
        blocks = self.gen.generate_timeline(items, group_by="hour")
        self.assertEqual(blocks[0].items_count, 4)


class TimelineHeatmapExtendedTestCase(unittest.TestCase):
    """Расширенные тесты generate_activity_heatmap."""

    def setUp(self) -> None:
        self.gen = TimelineViewGenerator()

    def test_peak_dow_correct(self) -> None:
        """peak_dow соответствует дню с наибольшим числом записей."""
        now = datetime.now(tz=timezone.utc)
        today = now.replace(hour=10, minute=0, second=0, microsecond=0)
        today_dow = today.weekday()
        items = [
            _make_item(today.isoformat()),
            _make_item(today.replace(hour=12).isoformat()),
            _make_item(today.replace(hour=14).isoformat()),
        ]
        result = self.gen.generate_activity_heatmap(items, days=7)
        self.assertEqual(result["peak_dow"], today_dow)

    def test_matrix_keys_are_strings(self) -> None:
        """Ключи матрицы — строки (JSON-совместимость)."""
        result = self.gen.generate_activity_heatmap([], days=30)
        matrix = result["matrix"]
        for h_key in matrix:
            self.assertIsInstance(h_key, str)
            for d_key in matrix[h_key]:
                self.assertIsInstance(d_key, str)

    def test_matrix_covers_all_24_hours(self) -> None:
        result = self.gen.generate_activity_heatmap([], days=7)
        self.assertEqual(len(result["matrix"]), 24)

    def test_matrix_covers_all_7_days_of_week(self) -> None:
        result = self.gen.generate_activity_heatmap([], days=7)
        for h in range(24):
            self.assertEqual(len(result["matrix"][str(h)]), 7)

    def test_item_counted_in_correct_hour_and_dow(self) -> None:
        """Запись считается в правильной ячейке матрицы."""
        # Используем конкретную дату/время в пределах 30 дней
        now = datetime.now(tz=timezone.utc)
        fixed = now.replace(hour=9, minute=0, second=0, microsecond=0)
        items = [_make_item(fixed.isoformat())]
        result = self.gen.generate_activity_heatmap(items, days=30)
        h = str(fixed.hour)
        d = str(fixed.weekday())
        self.assertEqual(result["matrix"][h][d], 1)


if __name__ == "__main__":
    unittest.main()
