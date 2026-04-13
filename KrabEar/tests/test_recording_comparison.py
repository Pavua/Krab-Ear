"""Тесты RecordingComparison — сравнение нескольких записей Krab Ear."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_comparison import (
    ComparisonView,
    RecordingComparison,
    _view_to_dict,
)


class FakeHistoryItem:
    """Минимальный фейк HistoryItem для тестов."""

    def __init__(self, **kwargs: Any) -> None:
        self._data = kwargs

    def to_dict(self) -> dict:
        return dict(self._data)


class FakeStore:
    """Минимальный фейк StateStore."""

    def __init__(self) -> None:
        self._items: dict[str, FakeHistoryItem] = {}

    def add_item(
        self,
        item_id: str,
        text: str = "",
        audio_duration_sec: float | None = None,
        confidence: float | None = None,
        source_lang: str = "",
    ) -> FakeHistoryItem:
        item = FakeHistoryItem(
            id=item_id,
            text=text,
            audio_duration_sec=audio_duration_sec,
            confidence=confidence,
            source_lang=source_lang,
        )
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)


class RecordingComparisonBasicTestCase(unittest.TestCase):
    """Базовые тесты RecordingComparison."""

    def setUp(self) -> None:
        self.svc = RecordingComparison()
        self.store = FakeStore()
        self.store.add_item("a1", text="Hello world foo bar", audio_duration_sec=10.0, confidence=0.9, source_lang="en")
        self.store.add_item("a2", text="Hello world baz qux", audio_duration_sec=20.0, confidence=0.8, source_lang="en")
        self.store.add_item("a3", text="Совсем другой текст здесь", audio_duration_sec=5.0, confidence=0.7, source_lang="ru")

    def test_returns_comparison_view(self) -> None:
        """compare() возвращает ComparisonView."""
        result = self.svc.compare(["a1", "a2"], self.store)
        self.assertIsInstance(result, ComparisonView)

    def test_items_list_matches_input_order(self) -> None:
        """items в ComparisonView соответствуют переданным ID в том же порядке."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        self.assertEqual(len(result.items), 3)
        self.assertEqual(result.items[0]["id"], "a1")
        self.assertEqual(result.items[1]["id"], "a2")
        self.assertEqual(result.items[2]["id"], "a3")

    def test_similarity_matrix_shape(self) -> None:
        """text_similarity_matrix имеет размер NxN."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        n = 3
        self.assertEqual(len(result.text_similarity_matrix), n)
        for row in result.text_similarity_matrix:
            self.assertEqual(len(row), n)

    def test_similarity_matrix_diagonal_is_one(self) -> None:
        """Диагональ матрицы сходства == 1.0."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        for i in range(3):
            self.assertAlmostEqual(result.text_similarity_matrix[i][i], 1.0)

    def test_similarity_matrix_is_symmetric(self) -> None:
        """Матрица сходства симметрична."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        n = 3
        for i in range(n):
            for j in range(n):
                self.assertAlmostEqual(
                    result.text_similarity_matrix[i][j],
                    result.text_similarity_matrix[j][i],
                    places=6,
                )

    def test_similar_texts_have_higher_score(self) -> None:
        """Тексты с общими словами получают сходство > 0."""
        result = self.svc.compare(["a1", "a2"], self.store)
        # "Hello world" есть в обоих
        self.assertGreater(result.text_similarity_matrix[0][1], 0.0)

    def test_different_language_texts_low_similarity(self) -> None:
        """Тексты на разных языках без общих слов имеют сходство == 0."""
        result = self.svc.compare(["a1", "a3"], self.store)
        self.assertAlmostEqual(result.text_similarity_matrix[0][1], 0.0)

    def test_duration_comparison_keys(self) -> None:
        """duration_comparison содержит ключи min/max/avg/std/count."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        for key in ("min", "max", "avg", "std", "count"):
            self.assertIn(key, result.duration_comparison)

    def test_duration_comparison_values(self) -> None:
        """duration_comparison корректно вычисляет min/max/avg."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        self.assertAlmostEqual(result.duration_comparison["min"], 5.0)
        self.assertAlmostEqual(result.duration_comparison["max"], 20.0)
        self.assertAlmostEqual(result.duration_comparison["avg"], 35.0 / 3, places=2)

    def test_confidence_comparison_keys(self) -> None:
        """confidence_comparison содержит ключи min/max/avg/std/count."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        for key in ("min", "max", "avg", "std", "count"):
            self.assertIn(key, result.confidence_comparison)

    def test_language_distribution(self) -> None:
        """language_distribution правильно считает языки."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        self.assertEqual(result.language_distribution.get("en"), 2)
        self.assertEqual(result.language_distribution.get("ru"), 1)

    def test_common_words_present_in_all(self) -> None:
        """common_words содержит слова, присутствующие во ВСЕХ записях."""
        self.store.add_item("b1", text="apple orange banana")
        self.store.add_item("b2", text="apple grape banana")
        result = self.svc.compare(["b1", "b2"], self.store)
        # "apple" и "banana" — общие (>= MIN_WORD_LEN и не стоп-слова)
        self.assertIn("apple", result.common_words)
        self.assertIn("banana", result.common_words)
        self.assertNotIn("orange", result.common_words)
        self.assertNotIn("grape", result.common_words)

    def test_unique_words_per_item(self) -> None:
        """unique_words_per_item содержит слова только из данной записи."""
        self.store.add_item("c1", text="apple orange")
        self.store.add_item("c2", text="apple grape")
        result = self.svc.compare(["c1", "c2"], self.store)
        self.assertIn("orange", result.unique_words_per_item[0])
        self.assertNotIn("grape", result.unique_words_per_item[0])
        self.assertIn("grape", result.unique_words_per_item[1])
        self.assertNotIn("orange", result.unique_words_per_item[1])

    def test_unique_words_per_item_count(self) -> None:
        """unique_words_per_item имеет длину N (по числу записей)."""
        result = self.svc.compare(["a1", "a2", "a3"], self.store)
        self.assertEqual(len(result.unique_words_per_item), 3)


class RecordingComparisonEdgeCasesTestCase(unittest.TestCase):
    """Тесты граничных случаев."""

    def setUp(self) -> None:
        self.svc = RecordingComparison()
        self.store = FakeStore()

    def test_empty_item_ids_raises(self) -> None:
        """Пустой item_ids вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.svc.compare([], self.store)

    def test_too_many_items_raises(self) -> None:
        """Более 10 элементов вызывает ValueError."""
        for i in range(11):
            self.store.add_item(f"x{i}", text="текст")
        with self.assertRaises(ValueError):
            self.svc.compare([f"x{i}" for i in range(11)], self.store)

    def test_duplicate_ids_raises(self) -> None:
        """Дублирующиеся ID вызывают ValueError."""
        self.store.add_item("dup", text="текст")
        with self.assertRaises(ValueError):
            self.svc.compare(["dup", "dup"], self.store)

    def test_nonexistent_id_raises(self) -> None:
        """Несуществующий ID вызывает ValueError."""
        self.store.add_item("exists", text="текст")
        with self.assertRaises(ValueError):
            self.svc.compare(["exists", "no_such_id"], self.store)

    def test_items_without_duration_stat_is_empty(self) -> None:
        """Если у всех записей нет audio_duration_sec, count=0."""
        self.store.add_item("n1", text="первый текст")
        self.store.add_item("n2", text="второй текст")
        result = self.svc.compare(["n1", "n2"], self.store)
        self.assertEqual(result.duration_comparison["count"], 0)
        self.assertIsNone(result.duration_comparison["min"])

    def test_items_without_confidence_stat_is_empty(self) -> None:
        """Если у всех записей нет confidence, count=0."""
        self.store.add_item("n1", text="первый текст")
        self.store.add_item("n2", text="второй текст")
        result = self.svc.compare(["n1", "n2"], self.store)
        self.assertEqual(result.confidence_comparison["count"], 0)
        self.assertIsNone(result.confidence_comparison["min"])

    def test_empty_texts_produce_zero_similarity(self) -> None:
        """Пустые тексты → сходство == 0 (кроме диагонали)."""
        self.store.add_item("e1", text="")
        self.store.add_item("e2", text="")
        result = self.svc.compare(["e1", "e2"], self.store)
        self.assertAlmostEqual(result.text_similarity_matrix[0][1], 0.0)

    def test_no_common_language_distribution_empty(self) -> None:
        """Если ни у одной записи нет source_lang — language_distribution пустой."""
        self.store.add_item("l1", text="текст")
        self.store.add_item("l2", text="текст")
        result = self.svc.compare(["l1", "l2"], self.store)
        self.assertEqual(result.language_distribution, {})

    def test_max_items_exactly_10(self) -> None:
        """Ровно 10 элементов проходят без ошибки."""
        for i in range(10):
            self.store.add_item(f"m{i}", text=f"запись номер {i}")
        result = self.svc.compare([f"m{i}" for i in range(10)], self.store)
        self.assertEqual(len(result.items), 10)
        self.assertEqual(len(result.text_similarity_matrix), 10)


class RecordingComparisonViewToDictTestCase(unittest.TestCase):
    """Тест сериализации ComparisonView в словарь."""

    def setUp(self) -> None:
        self.svc = RecordingComparison()
        self.store = FakeStore()
        self.store.add_item("d1", text="text one here", audio_duration_sec=8.0, confidence=0.85)
        self.store.add_item("d2", text="text two there", audio_duration_sec=12.0, confidence=0.75)

    def test_view_to_dict_has_all_keys(self) -> None:
        """_view_to_dict возвращает все ожидаемые ключи."""
        view = self.svc.compare(["d1", "d2"], self.store)
        d = _view_to_dict(view)
        expected_keys = {
            "items",
            "text_similarity_matrix",
            "duration_comparison",
            "confidence_comparison",
            "language_distribution",
            "common_words",
            "unique_words_per_item",
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_view_to_dict_items_serializable(self) -> None:
        """Данные из _view_to_dict JSON-сериализуемы."""
        import json
        view = self.svc.compare(["d1", "d2"], self.store)
        d = _view_to_dict(view)
        dumped = json.dumps(d)
        self.assertIsInstance(dumped, str)


class RecordingComparisonIPCTestCase(unittest.TestCase):
    """IPC-тест метода compare_recordings через BackendService."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

        from pathlib import Path
        from backend.state_store import StateStore
        from unittest.mock import MagicMock

        store = StateStore(Path(self._tmpdir.name) / "data")
        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        self.svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )

        # Добавляем реальные записи в store
        item1 = store.add_history_item(
            text="Hello world this is a test",
            audio_duration_sec=10.0,
            source_lang="en",
        )
        item2 = store.add_history_item(
            text="Hello world another recording",
            audio_duration_sec=15.0,
            source_lang="en",
        )
        self.id1 = item1.id
        self.id2 = item2.id

    def test_compare_recordings_ok(self) -> None:
        """IPC-метод compare_recordings возвращает ok=True и ожидаемую структуру."""
        resp = self.svc.handle_request({
            "id": "r1",
            "method": "compare_recordings",
            "params": {"item_ids": [self.id1, self.id2]},
        })
        self.assertTrue(resp["ok"], msg=resp)
        result = resp["result"]
        self.assertIn("items", result)
        self.assertIn("text_similarity_matrix", result)
        self.assertIn("duration_comparison", result)
        self.assertIn("confidence_comparison", result)
        self.assertIn("language_distribution", result)
        self.assertIn("common_words", result)
        self.assertIn("unique_words_per_item", result)
        self.assertEqual(len(result["items"]), 2)

    def test_compare_recordings_missing_param(self) -> None:
        """compare_recordings без item_ids возвращает ok=False."""
        resp = self.svc.handle_request({
            "id": "r2",
            "method": "compare_recordings",
            "params": {},
        })
        self.assertFalse(resp["ok"])

    def test_compare_recordings_empty_list(self) -> None:
        """compare_recordings с пустым item_ids возвращает ok=False."""
        resp = self.svc.handle_request({
            "id": "r3",
            "method": "compare_recordings",
            "params": {"item_ids": []},
        })
        self.assertFalse(resp["ok"])

    def test_compare_recordings_nonexistent_id(self) -> None:
        """compare_recordings с несуществующим ID возвращает ok=False."""
        resp = self.svc.handle_request({
            "id": "r4",
            "method": "compare_recordings",
            "params": {"item_ids": [self.id1, "nonexistent-id-xyz"]},
        })
        self.assertFalse(resp["ok"])


if __name__ == "__main__":
    unittest.main()
