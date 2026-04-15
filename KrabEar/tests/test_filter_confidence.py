"""Тесты handle_filter_by_confidence в HistoryService."""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _add_item(store: StateStore, text: str, confidence: float | None) -> str:
    """Добавляет запись истории с заданным confidence и возвращает её id."""
    from backend.models import HistoryItem as HI
    payload: dict = {"text": text, "paste_status": "failed"}
    if confidence is not None:
        payload["confidence"] = confidence
    item = HI.create(**payload)
    with store._lock():
        store._append_ndjson(store.history_path, item.to_dict())
    return item.id


class FilterByConfidenceTestCase(unittest.TestCase):
    """Покрывает handle_filter_by_confidence в HistoryService."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    # ------------------------------------------------------------------
    # 1. Базовая фильтрация по min_confidence
    # ------------------------------------------------------------------

    def test_filter_min_confidence_basic(self) -> None:
        """Возвращает только записи с confidence >= min_confidence."""
        _add_item(self.store, "низкая", 0.3)
        _add_item(self.store, "средняя", 0.6)
        _add_item(self.store, "высокая", 0.9)

        result = self.svc.handle_filter_by_confidence({"min_confidence": 0.5})

        self.assertEqual(result["count"], 2)
        texts = {it["text"] for it in result["items"]}
        self.assertIn("средняя", texts)
        self.assertIn("высокая", texts)
        self.assertNotIn("низкая", texts)

    # ------------------------------------------------------------------
    # 2. Диапазон min + max
    # ------------------------------------------------------------------

    def test_filter_confidence_range(self) -> None:
        """Диапазон [min, max] включает только подходящие записи."""
        _add_item(self.store, "очень низкая", 0.1)
        _add_item(self.store, "средняя", 0.5)
        _add_item(self.store, "высокая", 0.95)

        result = self.svc.handle_filter_by_confidence(
            {"min_confidence": 0.4, "max_confidence": 0.8}
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["text"], "средняя")

    # ------------------------------------------------------------------
    # 3. Записи без confidence не включаются
    # ------------------------------------------------------------------

    def test_items_without_confidence_excluded(self) -> None:
        """Записи с confidence=None не попадают в результат."""
        _add_item(self.store, "без уверенности", None)
        _add_item(self.store, "с уверенностью", 0.8)

        result = self.svc.handle_filter_by_confidence({"min_confidence": 0.0})

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["text"], "с уверенностью")

    # ------------------------------------------------------------------
    # 4. avg_confidence считается корректно
    # ------------------------------------------------------------------

    def test_avg_confidence_correct(self) -> None:
        """avg_confidence равен среднему значению confidence подходящих записей."""
        _add_item(self.store, "A", 0.6)
        _add_item(self.store, "B", 0.8)
        _add_item(self.store, "ниже порога", 0.2)

        result = self.svc.handle_filter_by_confidence({"min_confidence": 0.5})

        self.assertEqual(result["count"], 2)
        self.assertAlmostEqual(result["avg_confidence"], 0.7, places=3)

    # ------------------------------------------------------------------
    # 5. Пустой результат → avg_confidence = 0.0
    # ------------------------------------------------------------------

    def test_empty_result_avg_confidence_zero(self) -> None:
        """При пустом результате avg_confidence = 0.0."""
        _add_item(self.store, "низкая", 0.2)

        result = self.svc.handle_filter_by_confidence({"min_confidence": 0.9})

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["avg_confidence"], 0.0)

    # ------------------------------------------------------------------
    # 6. Ошибка при отсутствии min_confidence
    # ------------------------------------------------------------------

    def test_missing_min_confidence_raises(self) -> None:
        """Если min_confidence не передан — RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_filter_by_confidence({})

    # ------------------------------------------------------------------
    # 7. Ошибка при max_confidence < min_confidence
    # ------------------------------------------------------------------

    def test_max_less_than_min_raises(self) -> None:
        """max_confidence < min_confidence вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_filter_by_confidence(
                {"min_confidence": 0.8, "max_confidence": 0.3}
            )

    # ------------------------------------------------------------------
    # 8. min_confidence = 0.0 включает все записи с confidence
    # ------------------------------------------------------------------

    def test_min_confidence_zero_includes_all(self) -> None:
        """min_confidence=0.0 без max включает все записи, у которых задан confidence."""
        _add_item(self.store, "нулевая", 0.0)
        _add_item(self.store, "полная", 1.0)
        _add_item(self.store, "нет данных", None)

        result = self.svc.handle_filter_by_confidence({"min_confidence": 0.0})

        self.assertEqual(result["count"], 2)


if __name__ == "__main__":
    unittest.main()
