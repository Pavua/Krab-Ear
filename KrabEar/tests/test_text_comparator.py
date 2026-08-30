"""Тесты TextComparator — сравнение двух транскрипций Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from core.text_comparator import (
    MAX_COMPARE_WORDS,
    MAX_PHRASE_SIZE,
    TextComparator,
    ComparisonResult,
)

from pathlib import Path
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.timing_budgets import REDOS_BUDGET_SEC  # noqa: E402


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


class TextComparatorDoSGuardTestCase(unittest.TestCase):
    """Тесты защиты от DoS (wave1765) — O(n²) → линейная сложность."""

    def setUp(self) -> None:
        self.comparator = TextComparator()

    # ------------------------------------------------------------------
    # Производительность — fail-before, pass-after
    # ------------------------------------------------------------------

    def test_long_text_completes_quickly(self) -> None:
        """50 000 слов завершаются менее чем за 0.5 с (DoS guard активен).

        До фикса: O(n²) фраз для n=50000 → миллиарды итераций, ОЗУ не хватает.
        После фикса: входной текст усекается до MAX_COMPARE_WORDS,
        n-граммы ограничены MAX_PHRASE_SIZE → линейное время.
        """
        # Генерируем два «документа» по 50 000 слов
        long_text_1 = " ".join(f"слово{i}" for i in range(50_000))
        long_text_2 = " ".join(f"слово{i}" for i in range(25_000, 75_000))

        t0 = time.perf_counter()
        result = self.comparator.compare_texts(long_text_1, long_text_2)
        elapsed = time.perf_counter() - t0

        self.assertIsInstance(result, ComparisonResult)
        # Порог из общего модуля: защита ловит КВАДРАТИЧНЫЙ взрыв (секунды и
        # минуты на 50k слов), а не отличает 0.4с от 0.6с. Прежние 0.5с падали
        # на загруженном раннере при честных 1.0-1.2с (30.08.2026, два прогона).
        self.assertLess(
            elapsed,
            REDOS_BUDGET_SEC,
            f"compare_texts на 50k-словных текстах занял {elapsed:.3f}s "
            f"(лимит {REDOS_BUDGET_SEC}s)",
        )

    def test_long_text_phrase_count_bounded(self) -> None:
        """Число извлечённых фраз ограничено — не превышает MAX_COMPARE_WORDS × MAX_PHRASE_SIZE."""
        # Используем 10 000 уникальных слов — существенно больше MAX_COMPARE_WORDS
        long_text = " ".join(f"w{i}" for i in range(10_000))
        phrases = self.comparator._extract_phrases(long_text.lower().split()[:MAX_COMPARE_WORDS])

        # После усечения до MAX_COMPARE_WORDS слов с n-граммами до MAX_PHRASE_SIZE
        # теоретический максимум: (MAX_COMPARE_WORDS - MIN + 1) * (MAX_PHRASE_SIZE - MIN + 1)
        # на практике значительно меньше; важна конечность
        max_possible = MAX_COMPARE_WORDS * MAX_PHRASE_SIZE
        self.assertLessEqual(
            len(phrases),
            max_possible,
            f"Число фраз {len(phrases)} превышает ожидаемый лимит {max_possible}",
        )

    def test_input_truncated_to_max_compare_words(self) -> None:
        """_safe_split усекает входной текст до MAX_COMPARE_WORDS слов."""
        oversized = " ".join(f"x{i}" for i in range(MAX_COMPARE_WORDS + 500))
        words = TextComparator._safe_split(oversized)
        self.assertEqual(len(words), MAX_COMPARE_WORDS)

    def test_short_input_not_truncated(self) -> None:
        """_safe_split не изменяет короткий текст."""
        text = "раз два три четыре пять"
        words = TextComparator._safe_split(text)
        self.assertEqual(words, text.lower().split())

    def test_phrase_size_capped_at_max_phrase_size(self) -> None:
        """_extract_phrases генерирует n-граммы не длиннее MAX_PHRASE_SIZE слов."""
        # 200 слов — достаточно для проверки максимального размера n-граммы
        words = [f"w{i}" for i in range(200)]
        phrases = self.comparator._extract_phrases(words)
        for phrase in phrases:
            word_count = len(phrase.split())
            self.assertLessEqual(
                word_count,
                MAX_PHRASE_SIZE,
                f"Фраза из {word_count} слов превышает MAX_PHRASE_SIZE={MAX_PHRASE_SIZE}: {phrase!r}",
            )

    # ------------------------------------------------------------------
    # Корректность для коротких текстов — не должна измениться
    # ------------------------------------------------------------------

    def test_normal_shared_phrases_detected(self) -> None:
        """Общие фразы из 3+ слов обнаруживаются в коротких текстах."""
        t1 = "машинное обучение это хорошо сегодня"
        t2 = "машинное обучение это плохо завтра"
        result = self.comparator.compare_texts(t1, t2)

        # «машинное обучение это» — трёхсловная общая фраза
        self.assertIn("машинное обучение это", result.common_phrases)

    def test_normal_unique_phrases_detected(self) -> None:
        """Уникальные фразы корректно распределяются между unique_to_1 и unique_to_2."""
        t1 = "раз два три четыре"
        t2 = "раз два три пять"
        result = self.comparator.compare_texts(t1, t2)

        # «два три четыре» есть только в t1
        self.assertIn("два три четыре", result.unique_to_1)
        # «два три пять» есть только в t2
        self.assertIn("два три пять", result.unique_to_2)

    def test_similarity_preserved_for_identical_short_text(self) -> None:
        """similarity == 1.0 для двух одинаковых коротких текстов."""
        text = "привет мир это тест"
        result = self.comparator.compare_texts(text, text)
        self.assertAlmostEqual(result.similarity, 1.0, places=2)

    def test_word_count_diff_preserved(self) -> None:
        """word_count_diff корректен после введения _safe_split."""
        result = self.comparator.compare_texts("один два три", "один два")
        self.assertEqual(result.word_count_diff, 1)


if __name__ == "__main__":
    unittest.main()
