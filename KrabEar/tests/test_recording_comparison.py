"""Тесты RecordingComparison — сравнение нескольких записей Krab Ear."""

from __future__ import annotations
from backend.recording_comparison import (
    ComparisonView,
    RecordingComparison,
    _view_to_dict,
)

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


class RecordingComparisonIdenticalTestCase(unittest.TestCase):
    """Тесты для идентичных и полностью различных записей."""

    def setUp(self) -> None:
        self.svc = RecordingComparison()
        self.store = FakeStore()

    def test_identical_texts_similarity_is_one(self) -> None:
        """Два идентичных текста имеют сходство == 1.0."""
        same_text = "apple orange banana mango grape"
        self.store.add_item("id1", text=same_text)
        self.store.add_item("id2", text=same_text)
        result = self.svc.compare(["id1", "id2"], self.store)
        self.assertAlmostEqual(result.text_similarity_matrix[0][1], 1.0, places=4)

    def test_identical_texts_common_words_equals_all_tokens(self) -> None:
        """Идентичные тексты: common_words содержит все токены."""
        same_text = "apple orange banana"
        self.store.add_item("id1", text=same_text)
        self.store.add_item("id2", text=same_text)
        result = self.svc.compare(["id1", "id2"], self.store)
        # все слова общие (>= 3 букв, не стоп-слова)
        self.assertIn("apple", result.common_words)
        self.assertIn("orange", result.common_words)
        self.assertIn("banana", result.common_words)

    def test_identical_texts_no_unique_words(self) -> None:
        """Идентичные тексты: unique_words_per_item для обоих пустые."""
        same_text = "apple orange banana"
        self.store.add_item("id1", text=same_text)
        self.store.add_item("id2", text=same_text)
        result = self.svc.compare(["id1", "id2"], self.store)
        self.assertEqual(result.unique_words_per_item[0], [])
        self.assertEqual(result.unique_words_per_item[1], [])

    def test_completely_different_texts_similarity_is_zero(self) -> None:
        """Тексты без общих слов имеют сходство == 0.0."""
        self.store.add_item("diff1", text="mountain river valley forest")
        self.store.add_item("diff2", text="algebra geometry calculus matrix")
        result = self.svc.compare(["diff1", "diff2"], self.store)
        self.assertAlmostEqual(result.text_similarity_matrix[0][1], 0.0, places=4)

    def test_completely_different_texts_no_common_words(self) -> None:
        """Тексты без общих слов: common_words пустой."""
        self.store.add_item("diff1", text="mountain river valley forest")
        self.store.add_item("diff2", text="algebra geometry calculus matrix")
        result = self.svc.compare(["diff1", "diff2"], self.store)
        self.assertEqual(result.common_words, [])

    def test_single_item_compare_returns_view(self) -> None:
        """Один элемент в compare() возвращает ComparisonView (диагональ == 1.0)."""
        self.store.add_item("solo", text="единственный текст здесь")
        result = self.svc.compare(["solo"], self.store)
        self.assertIsInstance(result, ComparisonView)
        self.assertEqual(len(result.items), 1)
        self.assertAlmostEqual(result.text_similarity_matrix[0][0], 1.0)

    def test_three_items_pairwise_matrix(self) -> None:
        """Три записи: матрица 3x3, все off-diagonal значения >= 0."""
        self.store.add_item("t1", text="apple orange pineapple")
        self.store.add_item("t2", text="apple banana pineapple")
        self.store.add_item("t3", text="mango grape watermelon")
        result = self.svc.compare(["t1", "t2", "t3"], self.store)
        self.assertEqual(len(result.text_similarity_matrix), 3)
        for i in range(3):
            for j in range(3):
                self.assertGreaterEqual(result.text_similarity_matrix[i][j], 0.0)
                self.assertLessEqual(result.text_similarity_matrix[i][j], 1.0)
        # t1 и t2 имеют общие слова (apple, pineapple) → сходство > t1 с t3
        sim_12 = result.text_similarity_matrix[0][1]
        sim_13 = result.text_similarity_matrix[0][2]
        self.assertGreater(sim_12, sim_13)

    def test_four_items_duration_stats(self) -> None:
        """Четыре записи: duration_comparison корректно считает статистику."""
        durations = [5.0, 10.0, 15.0, 20.0]
        for i, d in enumerate(durations):
            self.store.add_item(f"s{i}", text=f"текст запись {i}", audio_duration_sec=d)
        result = self.svc.compare([f"s{i}" for i in range(4)], self.store)
        self.assertAlmostEqual(result.duration_comparison["min"], 5.0)
        self.assertAlmostEqual(result.duration_comparison["max"], 20.0)
        self.assertAlmostEqual(result.duration_comparison["avg"], 12.5)
        self.assertEqual(result.duration_comparison["count"], 4)

    def test_two_items_confidence_avg(self) -> None:
        """Два элемента: среднее confidence вычисляется корректно."""
        self.store.add_item("conf1", text="первый текст здесь", confidence=0.6)
        self.store.add_item("conf2", text="второй текст здесь", confidence=1.0)
        result = self.svc.compare(["conf1", "conf2"], self.store)
        self.assertAlmostEqual(result.confidence_comparison["avg"], 0.8, places=4)

    def test_mixed_language_distribution(self) -> None:
        """Смешанные языки: language_distribution правильно группирует."""
        self.store.add_item("lang1", text="hello world", source_lang="en")
        self.store.add_item("lang2", text="привет мир", source_lang="ru")
        self.store.add_item("lang3", text="hola mundo", source_lang="es")
        self.store.add_item("lang4", text="hello again", source_lang="en")
        result = self.svc.compare(["lang1", "lang2", "lang3", "lang4"], self.store)
        self.assertEqual(result.language_distribution.get("en"), 2)
        self.assertEqual(result.language_distribution.get("ru"), 1)
        self.assertEqual(result.language_distribution.get("es"), 1)


class RecordingComparisonRequiredNamesTestCase(unittest.TestCase):
    """Тесты с именами, заданными в Wave 139 task spec."""

    def setUp(self) -> None:
        self.svc = RecordingComparison()
        self.store = FakeStore()

    def test_compare_two_recordings(self) -> None:
        """compare() двух записей возвращает ComparisonView с 2x2 матрицей."""
        self.store.add_item("r1", text="hello world test", audio_duration_sec=5.0)
        self.store.add_item("r2", text="hello world check", audio_duration_sec=8.0)
        result = self.svc.compare(["r1", "r2"], self.store)
        self.assertIsInstance(result, ComparisonView)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(len(result.text_similarity_matrix), 2)
        self.assertEqual(len(result.text_similarity_matrix[0]), 2)

    def test_similarity_matrix_n_x_n(self) -> None:
        """Матрица сходства всегда NxN для N записей."""
        for i in range(5):
            self.store.add_item(f"nm{i}", text=f"unique words item number {i} text")
        result = self.svc.compare([f"nm{i}" for i in range(5)], self.store)
        n = 5
        self.assertEqual(len(result.text_similarity_matrix), n)
        for row in result.text_similarity_matrix:
            self.assertEqual(len(row), n)

    def test_shared_words_extracted(self) -> None:
        """common_words содержит слова, общие для всех записей."""
        self.store.add_item("sw1", text="python coding tutorial")
        self.store.add_item("sw2", text="python programming tutorial")
        result = self.svc.compare(["sw1", "sw2"], self.store)
        self.assertIn("python", result.common_words)
        self.assertIn("tutorial", result.common_words)
        self.assertNotIn("coding", result.common_words)
        self.assertNotIn("programming", result.common_words)

    def test_unicode_text_compared(self) -> None:
        """Unicode (кириллица, испанский) корректно обрабатывается."""
        self.store.add_item("uc1", text="Привет добрый мир")
        self.store.add_item("uc2", text="Привет хороший день")
        result = self.svc.compare(["uc1", "uc2"], self.store)
        self.assertIsInstance(result, ComparisonView)
        # «привет» общее слово
        self.assertIn("привет", result.common_words)
        # Матрица симметрична
        self.assertAlmostEqual(
            result.text_similarity_matrix[0][1],
            result.text_similarity_matrix[1][0],
        )

    def test_empty_history_handled(self) -> None:
        """Записи с пустым текстом обрабатываются без исключений."""
        self.store.add_item("eh1", text="")
        self.store.add_item("eh2", text="")
        result = self.svc.compare(["eh1", "eh2"], self.store)
        self.assertIsInstance(result, ComparisonView)
        self.assertEqual(result.text_similarity_matrix[0][1], 0.0)
        self.assertEqual(result.common_words, [])

    def test_concurrent_compare(self) -> None:
        """Параллельный вызов compare() из нескольких потоков безопасен."""
        import threading

        for i in range(4):
            self.store.add_item(f"cc{i}", text=f"concurrent test item data {i}")

        errors: list[Exception] = []
        results: list[ComparisonView] = []
        lock = threading.Lock()

        def run() -> None:
            try:
                r = self.svc.compare(["cc0", "cc1", "cc2", "cc3"], self.store)
                with lock:
                    results.append(r)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], msg=str(errors))
        self.assertEqual(len(results), 6)
        for r in results:
            self.assertEqual(len(r.text_similarity_matrix), 4)


if __name__ == "__main__":
    unittest.main()
