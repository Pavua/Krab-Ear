"""Дополнительные тесты AnalyticsDashboard — покрытие вспомогательных функций
и граничных случаев, не охваченных в test_analytics_dashboard.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.analytics_dashboard import (
    AnalyticsDashboard,
    _build_storage_info,
    _calc_streak,
    _calc_trend,
    _parse_ts,
)
from tests.test_helpers import make_test_item as _make_item  # noqa: E402


def _make_store(items: list) -> MagicMock:
    store = MagicMock()
    store.data_dir = Path(tempfile.mkdtemp())
    store.history_path = store.data_dir / "history.ndjson"
    store.history_path.touch()
    lock_ctx = MagicMock()
    lock_ctx.__enter__ = MagicMock(return_value=None)
    lock_ctx.__exit__ = MagicMock(return_value=False)
    store._lock = MagicMock(return_value=lock_ctx)
    store._load_active_items_unlocked = MagicMock(return_value=items)
    return store


# ---------------------------------------------------------------------------
# Тесты _parse_ts — дополнительные форматы
# ---------------------------------------------------------------------------

class TestParseTsEdgeCases(unittest.TestCase):
    """Дополнительные edge-cases для _parse_ts."""

    def test_z_suffix_iso_string(self) -> None:
        """ISO-строка с Z парсится как UTC."""
        dt = _parse_ts("2025-06-15T08:30:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.month, 6)

    def test_aware_datetime_returned_as_is(self) -> None:
        """Aware datetime возвращается без изменений."""
        aware = datetime(2025, 4, 1, 12, 0, tzinfo=timezone.utc)
        result = _parse_ts(aware)
        self.assertEqual(result, aware)

    def test_integer_epoch(self) -> None:
        """Целочисленный timestamp интерпретируется как epoch."""
        epoch = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
        result = _parse_ts(epoch)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2025)

    def test_non_parseable_type_returns_none(self) -> None:
        """Несовместимый тип (list) → None."""
        self.assertIsNone(_parse_ts([1, 2, 3]))

    def test_empty_string_returns_none(self) -> None:
        """Пустая строка → None."""
        self.assertIsNone(_parse_ts(""))


# ---------------------------------------------------------------------------
# Тесты _calc_trend — граничные случаи
# ---------------------------------------------------------------------------

class TestCalcTrendEdgeCases(unittest.TestCase):
    """Граничные случаи для функции _calc_trend."""

    def test_two_equal_points_stable(self) -> None:
        """Два одинаковых значения → stable (наклон = 0)."""
        pts = [{"date": "2025-01-01", "val": 0.5}, {"date": "2025-01-02", "val": 0.5}]
        self.assertEqual(_calc_trend(pts), "stable")

    def test_two_rising_points_improving(self) -> None:
        """Два значения с ростом → improving."""
        pts = [{"date": "2025-01-01", "val": 0.1}, {"date": "2025-01-02", "val": 0.9}]
        self.assertEqual(_calc_trend(pts), "improving")

    def test_two_falling_points_declining(self) -> None:
        """Два значения с падением → declining."""
        pts = [{"date": "2025-01-01", "val": 0.9}, {"date": "2025-01-02", "val": 0.1}]
        self.assertEqual(_calc_trend(pts), "declining")

    def test_all_zeros_stable(self) -> None:
        """Все нули → stable."""
        pts = [{"date": f"2025-01-{i + 1:02d}", "val": 0.0} for i in range(5)]
        self.assertEqual(_calc_trend(pts), "stable")

    def test_near_zero_slope_stable(self) -> None:
        """Очень малое изменение (≤ 0.001) → stable."""
        pts = [
            {"date": "2025-01-01", "val": 0.500000},
            {"date": "2025-01-02", "val": 0.500001},
        ]
        self.assertEqual(_calc_trend(pts), "stable")


# ---------------------------------------------------------------------------
# Тесты _calc_streak — дополнительные случаи
# ---------------------------------------------------------------------------

class TestCalcStreakEdgeCases(unittest.TestCase):
    """Дополнительные тесты _calc_streak."""

    def test_yesterday_only_streak_zero(self) -> None:
        """Только вчерашние записи (сегодня пусто) → streak = 0."""
        items = [_make_item(days_ago=1)]
        self.assertEqual(_calc_streak(items), 0)

    def test_long_consecutive_streak(self) -> None:
        """7 дней подряд (включая сегодня) → streak = 7."""
        items = [_make_item(days_ago=i) for i in range(7)]
        self.assertEqual(_calc_streak(items), 7)

    def test_multiple_items_same_day_count_once(self) -> None:
        """Несколько записей в один день считаются как 1 день в streak."""
        items = [_make_item(days_ago=0) for _ in range(5)]
        self.assertEqual(_calc_streak(items), 1)

    def test_items_with_no_ts_ignored(self) -> None:
        """Элементы без ts не должны вызывать исключение."""

        class ItemNoTs:
            ts = None

        items = [ItemNoTs()]
        # Не должно бросать
        streak = _calc_streak(items)
        self.assertEqual(streak, 0)


# ---------------------------------------------------------------------------
# Тесты _build_storage_info
# ---------------------------------------------------------------------------

class TestBuildStorageInfo(unittest.TestCase):
    """Тесты вспомогательной функции _build_storage_info."""

    def test_returns_expected_keys(self) -> None:
        """Функция возвращает dict с тремя ожидаемыми ключами."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MagicMock()
            store.data_dir = Path(tmpdir)
            history_path = Path(tmpdir) / "history.ndjson"
            history_path.write_text("line1\nline2\n", encoding="utf-8")
            store.history_path = history_path

            result = _build_storage_info(store)

        self.assertIn("history_size_mb", result)
        self.assertIn("backups_count", result)
        self.assertIn("cache_size_mb", result)

    def test_history_size_nonzero_when_file_exists(self) -> None:
        """Реальный файл истории → history_size_mb > 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MagicMock()
            store.data_dir = Path(tmpdir)
            history_path = Path(tmpdir) / "history.ndjson"
            history_path.write_text("a" * 1024, encoding="utf-8")
            store.history_path = history_path

            result = _build_storage_info(store)

        self.assertGreater(result["history_size_mb"], 0.0)

    def test_history_size_zero_when_file_missing(self) -> None:
        """Отсутствующий файл истории → history_size_mb = 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MagicMock()
            store.data_dir = Path(tmpdir)
            store.history_path = Path(tmpdir) / "nonexistent.ndjson"

            result = _build_storage_info(store)

        self.assertEqual(result["history_size_mb"], 0.0)

    def test_backups_counted(self) -> None:
        """ndjson-файлы в backups/ учитываются в backups_count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MagicMock()
            store.data_dir = Path(tmpdir)
            store.history_path = Path(tmpdir) / "history.ndjson"
            store.history_path.touch()
            backups_dir = Path(tmpdir) / "backups"
            backups_dir.mkdir()
            for i in range(3):
                (backups_dir / f"backup_{i}.ndjson").write_text("{}", encoding="utf-8")

            result = _build_storage_info(store)

        self.assertEqual(result["backups_count"], 3)

    def test_error_store_returns_zeros(self) -> None:
        """Если store ломается — возвращаются нулевые значения."""
        broken_store = MagicMock()
        # data_dir задан как некорректный тип, history_path тоже ломаный
        broken_store.data_dir = object()  # не Path → Path(object()) упадёт
        broken_store.history_path = object()  # не Path → .exists() упадёт

        result = _build_storage_info(broken_store)
        self.assertEqual(result["history_size_mb"], 0.0)
        self.assertEqual(result["backups_count"], 0)
        self.assertEqual(result["cache_size_mb"], 0.0)


# ---------------------------------------------------------------------------
# Тесты AnalyticsDashboard — дополнительные сценарии
# ---------------------------------------------------------------------------

class TestAnalyticsDashboardAdditional(unittest.TestCase):
    """Дополнительные тесты AnalyticsDashboard."""

    def setUp(self) -> None:
        self.dashboard = AnalyticsDashboard()

    def test_no_confidence_items_handled_gracefully(self) -> None:
        """Записи без confidence → avg_confidence=0 без исключений."""
        item = _make_item(confidence=None)
        store = _make_store([item])
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["quality"]["avg_confidence"], 0.0)

    def test_zero_duration_items_no_division_by_zero(self) -> None:
        """Записи с audio_duration_sec=0 не вызывают деление на ноль."""
        items = [_make_item(audio_duration_sec=0.0) for _ in range(3)]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["overview"]["total_hours"], 0.0)

    def test_items_with_no_source_lang_not_counted(self) -> None:
        """Записи без source_lang не попадают в distribution."""
        item = _make_item(source_lang="")
        store = _make_store([item])
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["languages"]["distribution"], {})

    def test_cache_multiple_days_independent(self) -> None:
        """Кэши для days=7, days=14, days=30 независимы."""
        store = _make_store([_make_item()])
        self.dashboard.get_full_dashboard(store, days=7)
        self.dashboard.get_full_dashboard(store, days=14)
        self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(store._load_active_items_unlocked.call_count, 3)

    def test_store_exception_returns_empty_dashboard(self) -> None:
        """Если store._lock() бросает исключение → дашборд с нулями, не краш."""
        store = MagicMock()
        store.data_dir = Path(tempfile.mkdtemp())
        store.history_path = store.data_dir / "history.ndjson"
        store.history_path.touch()
        broken_ctx = MagicMock()
        broken_ctx.__enter__ = MagicMock(side_effect=RuntimeError("сломан"))
        broken_ctx.__exit__ = MagicMock(return_value=False)
        store._lock = MagicMock(return_value=broken_ctx)

        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 0)

    def test_days_clamped_to_minimum_1(self) -> None:
        """days=0 или отрицательный → days нормализуется до 1."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=0)
        self.assertIn("overview", result)

    def test_streak_with_no_items_returns_zero(self) -> None:
        """Пустая история → streak_days=0."""
        store = _make_store([])
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(result["engagement"]["streak_days"], 0)

    def test_all_translations_counted(self) -> None:
        """100% переведённых записей → translation_rate=1.0."""
        items = [
            _make_item(translated_text="hola", translation_status="ok"),
            _make_item(translated_text="mundo", translation_status="ok"),
        ]
        store = _make_store(items)
        result = self.dashboard.get_full_dashboard(store, days=30)
        self.assertAlmostEqual(result["languages"]["translation_rate"], 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
