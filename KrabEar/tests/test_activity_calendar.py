"""Тесты ActivityCalendar — GitHub-style contribution graph данные Krab Ear."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.activity_calendar import (
    ActivityCalendar,
    CalendarData,
    DayActivity,
    _compute_level,
    _compute_thresholds,
    _count_words,
    _parse_ts,
)


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _make_item(days_ago: int, text: str = "hello world", duration_sec: float = 60.0):
    """Создаёт fake-элемент истории в виде dict."""
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    return {
        "ts": ts,
        "text": text,
        "audio_duration_sec": duration_sec,
    }


def _make_item_obj(days_ago: int, text: str = "hello world", duration_sec: float = 60.0):
    """Создаёт fake-элемент истории в виде объекта с атрибутами."""
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    class FakeItem:
        pass

    obj = FakeItem()
    obj.ts = ts
    obj.text = text
    obj.audio_duration_sec = duration_sec
    return obj


# ---------------------------------------------------------------------------
# Тесты вспомогательных функций
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_count_words_empty(self):
        self.assertEqual(_count_words(""), 0)

    def test_count_words_normal(self):
        self.assertEqual(_count_words("hello world foo"), 3)

    def test_count_words_extra_spaces(self):
        self.assertEqual(_count_words("  one   two  "), 2)

    def test_parse_ts_iso(self):
        d = _parse_ts("2024-03-15T12:34:56")
        self.assertEqual(d, date(2024, 3, 15))

    def test_parse_ts_date_only(self):
        d = _parse_ts("2024-03-15")
        self.assertEqual(d, date(2024, 3, 15))

    def test_parse_ts_epoch(self):
        # 2024-01-01 00:00:00 UTC = 1704067200
        d = _parse_ts(1704067200.0)
        self.assertEqual(d.year, 2024)
        self.assertEqual(d.month, 1)
        self.assertEqual(d.day, 1)

    def test_parse_ts_none(self):
        self.assertIsNone(_parse_ts(None))

    def test_parse_ts_invalid(self):
        self.assertIsNone(_parse_ts("not-a-date"))

    def test_compute_thresholds_normal(self):
        t1, t2, t3, t4 = _compute_thresholds(20)
        self.assertEqual(t1, 1)
        self.assertLess(t1, t2)
        self.assertLess(t2, t3)
        self.assertLess(t3, t4)

    def test_compute_level_zero(self):
        self.assertEqual(_compute_level(0, (1, 3, 6, 10)), 0)

    def test_compute_level_max(self):
        self.assertEqual(_compute_level(10, (1, 3, 6, 10)), 4)

    def test_compute_level_mid(self):
        level = _compute_level(3, (1, 3, 6, 10))
        self.assertIn(level, (2, 3))  # >=t2, <t3 → 2


# ---------------------------------------------------------------------------
# Тесты ActivityCalendar.generate_calendar
# ---------------------------------------------------------------------------

class TestGenerateCalendar(unittest.TestCase):

    def setUp(self):
        self.cal = ActivityCalendar()

    def test_empty_items_returns_calendar_data(self):
        """Пустой список → возвращает CalendarData без ошибок."""
        result = self.cal.generate_calendar([], months=1)
        self.assertIsInstance(result, CalendarData)
        self.assertEqual(result.total_active_days, 0)
        self.assertEqual(result.longest_streak, 0)
        self.assertEqual(result.current_streak, 0)

    def test_days_covers_period(self):
        """days содержит все дни в запрошенном периоде."""
        result = self.cal.generate_calendar([], months=1)
        today_key = date.today().isoformat()
        self.assertIn(today_key, result.days)

    def test_single_item_today_active(self):
        """Одна запись сегодня → 1 активный день, уровень >= 1."""
        items = [_make_item(days_ago=0)]
        result = self.cal.generate_calendar(items, months=1)
        today_key = date.today().isoformat()
        self.assertEqual(result.total_active_days, 1)
        da = result.days[today_key]
        self.assertEqual(da.recordings, 1)
        self.assertGreaterEqual(da.level, 1)

    def test_multiple_items_same_day_aggregated(self):
        """Несколько записей одного дня → агрегируются в одном DayActivity."""
        items = [_make_item(0, "one two", 60.0), _make_item(0, "three four five", 120.0)]
        result = self.cal.generate_calendar(items, months=1)
        today_key = date.today().isoformat()
        da = result.days[today_key]
        self.assertEqual(da.recordings, 2)
        self.assertAlmostEqual(da.duration_min, 3.0, places=1)
        self.assertEqual(da.words, 5)

    def test_out_of_range_items_ignored(self):
        """Записи старше периода игнорируются."""
        items = [_make_item(days_ago=400)]
        result = self.cal.generate_calendar(items, months=1)
        self.assertEqual(result.total_active_days, 0)

    def test_object_items_supported(self):
        """Поддержка объектов-атрибутов вместо dict."""
        items = [_make_item_obj(days_ago=0, text="alpha beta gamma")]
        result = self.cal.generate_calendar(items, months=1)
        self.assertEqual(result.total_active_days, 1)
        today_key = date.today().isoformat()
        self.assertEqual(result.days[today_key].words, 3)

    def test_day_activity_level_range(self):
        """level всегда в диапазоне 0–4."""
        items = [_make_item(i % 10, f"word{i}") for i in range(30)]
        result = self.cal.generate_calendar(items, months=1)
        for da in result.days.values():
            self.assertIn(da.level, (0, 1, 2, 3, 4))

    def test_longest_streak_consecutive_days(self):
        """Consecutive дни → longest_streak корректен."""
        items = [_make_item(days_ago=i) for i in range(5)]
        result = self.cal.generate_calendar(items, months=1)
        self.assertGreaterEqual(result.longest_streak, 5)

    def test_longest_streak_broken(self):
        """Пропуск дня → стрик не длиннее 2."""
        items = [_make_item(0), _make_item(2)]
        result = self.cal.generate_calendar(items, months=1)
        self.assertLessEqual(result.longest_streak, 2)

    def test_current_streak_no_activity_today(self):
        """Нет активности ни сегодня ни вчера → current_streak = 0."""
        items = [_make_item(days_ago=5)]
        result = self.cal.generate_calendar(items, months=1)
        self.assertEqual(result.current_streak, 0)

    def test_current_streak_today(self):
        """Активность сегодня и вчера → current_streak >= 2."""
        items = [_make_item(0), _make_item(1)]
        result = self.cal.generate_calendar(items, months=1)
        self.assertGreaterEqual(result.current_streak, 2)

    def test_weeks_grid_structure(self):
        """weeks имеет 7 строк (дней недели)."""
        result = self.cal.generate_calendar([], months=1)
        self.assertEqual(len(result.weeks), 7)

    def test_weeks_columns_consistent(self):
        """Все строки weeks имеют одинаковое число колонок."""
        result = self.cal.generate_calendar([], months=1)
        if result.weeks:
            col_counts = [len(row) for row in result.weeks]
            self.assertEqual(len(set(col_counts)), 1)

    def test_to_dict_serializable(self):
        """to_dict() возвращает сериализуемую структуру без ошибок."""
        items = [_make_item(0), _make_item(1)]
        result = self.cal.generate_calendar(items, months=1)
        d = result.to_dict()
        self.assertIn("days", d)
        self.assertIn("weeks", d)
        self.assertIn("total_active_days", d)
        self.assertIn("longest_streak", d)
        self.assertIn("current_streak", d)

    def test_months_parameter_affects_range(self):
        """months=3 охватывает меньше дней, чем months=12."""
        r3 = self.cal.generate_calendar([], months=3)
        r12 = self.cal.generate_calendar([], months=12)
        self.assertLessEqual(len(r3.days), len(r12.days))

    def test_day_activity_duration_min(self):
        """duration_min рассчитывается корректно из audio_duration_sec."""
        items = [_make_item(0, duration_sec=120.0)]
        result = self.cal.generate_calendar(items, months=1)
        today_key = date.today().isoformat()
        da = result.days[today_key]
        self.assertAlmostEqual(da.duration_min, 2.0, places=2)

    def test_item_with_none_duration(self):
        """Запись без audio_duration_sec не вызывает ошибок."""
        item = {"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "text": "ok"}
        result = self.cal.generate_calendar([item], months=1)
        today_key = date.today().isoformat()
        self.assertEqual(result.days[today_key].recordings, 1)
        self.assertAlmostEqual(result.days[today_key].duration_min, 0.0, places=2)


# ---------------------------------------------------------------------------
# Тесты ActivityCalendar.generate_calendar_svg
# ---------------------------------------------------------------------------

class TestGenerateCalendarSvg(unittest.TestCase):

    def setUp(self):
        self.cal = ActivityCalendar()

    def test_svg_returns_string(self):
        """generate_calendar_svg возвращает строку."""
        svg = self.cal.generate_calendar_svg([], months=1)
        self.assertIsInstance(svg, str)

    def test_svg_contains_svg_tag(self):
        """Результат содержит тег <svg ...>.</svg>"""
        svg = self.cal.generate_calendar_svg([], months=1)
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)

    def test_svg_contains_rects(self):
        """SVG содержит rect-элементы для ячеек."""
        svg = self.cal.generate_calendar_svg([], months=1)
        self.assertIn("<rect", svg)

    def test_svg_cell_size_param(self):
        """cell_size влияет на размер SVG."""
        svg_small = self.cal.generate_calendar_svg([], months=1, cell_size=10)
        svg_large = self.cal.generate_calendar_svg([], months=1, cell_size=20)
        # Больший cell_size → больший width атрибут
        import re
        w_small = re.search(r'width="(\d+)"', svg_small)
        w_large = re.search(r'width="(\d+)"', svg_large)
        if w_small and w_large:
            self.assertLess(int(w_small.group(1)), int(w_large.group(1)))

    def test_svg_with_data_has_colored_cells(self):
        """С данными SVG содержит цветные (не тёмные) ячейки."""
        items = [_make_item(0), _make_item(1)]
        svg = self.cal.generate_calendar_svg(items, months=1)
        # Активные дни используют цвета уровней 1-4
        self.assertTrue(
            any(c in svg for c in ["#0e4429", "#006d32", "#26a641", "#39d353"]),
            "SVG должен содержать хотя бы один цветной прямоугольник активного дня",
        )

    def test_svg_months_param(self):
        """months параметр принимается без ошибок для SVG."""
        svg = self.cal.generate_calendar_svg([], months=3)
        self.assertIn("<svg", svg)


# ---------------------------------------------------------------------------
# Тест IPC-совместимости через BackendService (smoke)
# ---------------------------------------------------------------------------

class TestActivityCalendarIpc(unittest.TestCase):
    """Проверяет что get_activity_calendar зарегистрирован в handle_request."""

    def test_method_registered_in_handlers(self):
        """Метод get_activity_calendar присутствует в handlers dict BackendService."""
        # Проверяем через grep-like поиск в исходнике service.py
        service_path = PROJECT_ROOT / "backend" / "service.py"
        content = service_path.read_text(encoding="utf-8")
        self.assertIn('"get_activity_calendar"', content)
        self.assertIn("_handle_get_activity_calendar", content)

    def test_handler_method_exists_in_activity_calendar(self):
        """ActivityCalendar имеет методы generate_calendar и generate_calendar_svg."""
        cal = ActivityCalendar()
        self.assertTrue(callable(getattr(cal, "generate_calendar", None)))
        self.assertTrue(callable(getattr(cal, "generate_calendar_svg", None)))


if __name__ == "__main__":
    unittest.main()
