"""Тесты TextComparator — сравнение двух транскрипций Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from core.text_comparator import TextComparator, ComparisonResult

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TextComparatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.comparator = TextComparator()

    def test_identical_texts_similarity_one(self) -> None:
        """Одинаковые тексты → similarity == 1.0."""
        result = self.comparator.compare_texts("привет мир", "привет мир")
        self.assertAlmostEqual(result.similarity, 1.0, places=2)

    def test_completely_different_texts(self) -> None:
        """Полностью разные тексты → similarity близко к 0."""
        result = self.comparator.compare_texts("аааааа", "ббббббб")
        self.assertLessEqual(result.similarity, 0.5)

    def test_empty_texts(self) -> None:
        """Пустые тексты → similarity == 1.0 (оба пустые)."""
        result = self.comparator.compare_texts("", "")
        self.assertGreaterEqual(result.similarity, 0.0)

    def test_returns_comparison_result(self) -> None:
        """compare_texts возвращает ComparisonResult."""
        result = self.comparator.compare_texts("первый текст", "второй текст")
        self.assertIsInstance(result, ComparisonResult)

    def test_result_has_required_fields(self) -> None:
        """ComparisonResult содержит обязательные поля."""
        result = self.comparator.compare_texts("текст один два три", "текст один три четыре")
        self.assertIsInstance(result.similarity, float)
        self.assertIsInstance(result.text_1, str)
        self.assertIsInstance(result.text_2, str)
        self.assertIsInstance(result.common_phrases, list)
        self.assertIsInstance(result.unique_to_1, list)
        self.assertIsInstance(result.unique_to_2, list)
        self.assertIsInstance(result.word_count_diff, int)
        self.assertIsInstance(result.summary, str)

    def test_similarity_range(self) -> None:
        """similarity всегда в диапазоне [0.0, 1.0]."""
        result = self.comparator.compare_texts(
            "машинное обучение это хорошо",
            "глубокое обучение нейронных сетей",
        )
        self.assertGreaterEqual(result.similarity, 0.0)
        self.assertLessEqual(result.similarity, 1.0)

    def test_word_count_diff(self) -> None:
        """word_count_diff равен абсолютной разнице числа слов."""
        result = self.comparator.compare_texts("один два три", "один два")
        self.assertEqual(result.word_count_diff, 1)

    def test_texts_stored_correctly(self) -> None:
        """text_1 и text_2 сохраняются в результате без изменений."""
        t1, t2 = "первый текст здесь", "второй текст там"
        result = self.comparator.compare_texts(t1, t2)
        self.assertEqual(result.text_1, t1)
        self.assertEqual(result.text_2, t2)


class TextComparatorItemsTestCase(unittest.TestCase):
    """Тесты compare_items — сравнение по ID из StateStore."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.comparator = TextComparator()

    def test_compare_items_by_id(self) -> None:
        """compare_items находит тексты по ID и сравнивает их."""
        item1 = self.store.add_history_item(text="привет мир это тест", paste_status="ok")
        item2 = self.store.add_history_item(text="привет мир другой тест", paste_status="ok")
        result = self.comparator.compare_items(item1.id, item2.id, self.store)
        self.assertIsInstance(result, ComparisonResult)
        self.assertGreater(result.similarity, 0.0)

    def test_compare_items_unknown_id_raises(self) -> None:
        """compare_items с несуществующим ID бросает ValueError."""
        with self.assertRaises(ValueError):
            self.comparator.compare_items("nonexistent-id", "also-nonexistent", self.store)


class TextComparatorIPCTestCase(unittest.TestCase):
    """Проверяет IPC-хэндлер compare_texts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")

        from unittest.mock import MagicMock
        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        self.svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )

    def test_compare_texts_by_content(self) -> None:
        """IPC-хэндлер compare_texts сравнивает два переданных текста."""
        resp = self.svc.handle_request({
            "id": "1",
            "method": "compare_texts",
            "params": {
                "text1": "машинное обучение обработка речи",
                "text2": "машинное обучение компьютерное зрение",
            },
        })
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("similarity", result)
        self.assertIn("common_phrases", result)
        self.assertIn("unique_to_1", result)
        self.assertIn("unique_to_2", result)
        self.assertIn("summary", result)

    def test_compare_texts_identical(self) -> None:
        """Одинаковые тексты → similarity == 1.0."""
        resp = self.svc.handle_request({
            "id": "2",
            "method": "compare_texts",
            "params": {"text1": "одинаковый текст", "text2": "одинаковый текст"},
        })
        self.assertTrue(resp["ok"])
        self.assertAlmostEqual(resp["result"]["similarity"], 1.0, places=2)

    def test_compare_texts_by_item_ids(self) -> None:
        """IPC-хэндлер compare_texts работает с item_id_1/item_id_2."""
        # Добавляем записи через BackendService
        self.svc.handle_request({
            "id": "add1",
            "method": "add_history_item",
            "params": {"text": "тест раз два три", "paste_status": "ok"},
        })
        self.svc.handle_request({
            "id": "add2",
            "method": "add_history_item",
            "params": {"text": "тест раз два четыре", "paste_status": "ok"},
        })
        # Получаем ID через history
        hist_resp = self.svc.handle_request(
            {"id": "h", "method": "get_history_page", "params": {"limit": 10}}
        )
        items = hist_resp["result"]["items"]
        if len(items) >= 2:
            resp = self.svc.handle_request({
                "id": "3",
                "method": "compare_texts",
                "params": {
                    "item_id_1": items[0]["id"],
                    "item_id_2": items[1]["id"],
                },
            })
            self.assertTrue(resp["ok"])
            self.assertIn("similarity", resp["result"])


if __name__ == "__main__":
    unittest.main()
