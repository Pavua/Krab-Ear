"""Regression tests — G3-Krab-Ear-memory-03 (R02, R03, R04).

R02 (P1): _sanitize_threshold — порог ниже DEFAULT_DEDUP_THRESHOLD (incl 0.0)
  возвращает DEFAULT_DEDUP_THRESHOLD, не 0.0. Порог 0.0 допускает автоматический
  skip различных текстов → потеря данных.
  Применяется в check_duplicate, run_deduplication и handle_run_deduplication.

R03 (P1): check_duplicate с невалидным timestamp → kept (fail-open window off):
  нельзя подтвердить 60-секундное окно — не инициировать дедупликацию без ts.

R04 (P1): handle_run_deduplication — отчёт о дубликатах НЕ мутирует
  semantic_searcher (remove_item не вызывается). Удаление индекса — явная
  отдельная операция.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auto_deduplication import (  # noqa: E402
    AutoDeduplicator,
    DEFAULT_DEDUP_THRESHOLD,
    _sanitize_threshold,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_store(items: list[dict]) -> MagicMock:
    store = MagicMock()
    store.get_history_page.return_value = (items, None)
    return store


class R02SanitizeThresholdTestCase(unittest.TestCase):
    def test_below_zero_returns_default(self):
        self.assertEqual(_sanitize_threshold(-1), DEFAULT_DEDUP_THRESHOLD)

    def test_zero_returns_default(self):
        # 0.0 допускал skip разных текстов — возвращаем default.
        self.assertEqual(_sanitize_threshold(0.0), DEFAULT_DEDUP_THRESHOLD)

    def test_non_finite_returns_default(self):
        self.assertEqual(_sanitize_threshold(float("nan")), DEFAULT_DEDUP_THRESHOLD)
        self.assertEqual(_sanitize_threshold(float("inf")), DEFAULT_DEDUP_THRESHOLD)

    def test_valid_passthrough(self):
        self.assertEqual(_sanitize_threshold(0.95), 0.95)
        self.assertEqual(_sanitize_threshold(1.0), 1.0)


class R02CheckDuplicateRejectsZeroThresholdTestCase(unittest.TestCase):
    def test_zero_threshold_order_swap_not_duplicate(self):
        now = _now()
        existing = [{
            "id": "orig-1",
            "text": "man bit dog",
            "ts": _iso(now - timedelta(seconds=5)),
        }]
        store = _make_store(existing)
        dedup = AutoDeduplicator()
        result = dedup.check_duplicate(
            text="dog bit man",
            timestamp=_iso(now),
            store=store,
            threshold=0.0,
        )
        self.assertFalse(result.is_duplicate)
        self.assertEqual(result.action_taken, "kept")


class R03InvalidTimestampNotRemovedTestCase(unittest.TestCase):
    def test_invalid_timestamp_returns_kept(self):
        store = _make_store([])
        dedup = AutoDeduplicator()
        result = dedup.check_duplicate(
            text="некий текст",
            timestamp="bad-ts",
            store=store,
            threshold=DEFAULT_DEDUP_THRESHOLD,
        )
        self.assertFalse(result.is_duplicate)
        self.assertEqual(result.action_taken, "kept")


class R04RunDedupReportDoesNotMutateSemanticIndexTestCase(unittest.TestCase):
    def test_report_does_not_call_remove_item(self):
        now = _now()
        items = [
            {"id": "a", "text": "текст один", "ts": _iso(now - timedelta(seconds=1))},
            {"id": "b", "text": "текст один", "ts": _iso(now - timedelta(seconds=2))},
        ]
        store = _make_store(items)
        searcher = MagicMock()
        dedup = AutoDeduplicator()
        result = dedup.handle_run_deduplication({"_store": store, "_semantic_searcher": searcher})
        searcher.remove_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
