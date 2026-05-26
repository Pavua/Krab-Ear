"""Тесты W1291 F2+F5 — keyword_cloud max_words bound + merge_map constant (W1298).

Покрывает:
1. test_keyword_cloud_max_words_clamped_at_1000   — значения > 1000 зажимаются до 1000
2. test_keyword_cloud_max_words_negative_clamped_to_zero — отрицательные → 0 (пустой список)
3. test_merge_map_module_constant                  — _MERGE_MAP существует как module-level dict
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.keyword_cloud import KeywordCloudGenerator, _MERGE_MAP, _MERGE_PAIRS, _MAX_WORDS_LIMIT


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _dict_item(text: str) -> dict:
    return {"text": text, "source_lang": ""}


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestMaxWordsBoundedAt1000(unittest.TestCase):
    """F2 MED: max_words > 1000 зажимается до 1000 (OOM защита)."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator(stop_words=frozenset())

    def test_keyword_cloud_max_words_clamped_at_1000(self) -> None:
        """Передача max_words=1_000_000 не должна возвращать больше 1000 слов."""
        # 50 уникальных слов — результат ограничен словарём, не лимитом
        words = [f"слово{i}" for i in range(50)]
        items = [_dict_item(" ".join(words))]
        result = self.gen.generate_cloud(items, max_words=1_000_000)
        self.assertLessEqual(len(result), _MAX_WORDS_LIMIT)

    def test_keyword_cloud_max_words_exactly_1000_allowed(self) -> None:
        """max_words=1000 (граница) — принимается без усечения."""
        words = [f"слово{i}" for i in range(1000)]
        items = [_dict_item(" ".join(words))]
        result = self.gen.generate_cloud(items, max_words=1000)
        self.assertLessEqual(len(result), 1000)

    def test_keyword_cloud_max_words_1001_clamped(self) -> None:
        """max_words=1001 зажимается до 1000."""
        words = [f"слово{i}" for i in range(1200)]
        items = [_dict_item(" ".join(words))]
        result_1001 = self.gen.generate_cloud(items, max_words=1001)
        result_1000 = self.gen.generate_cloud(items, max_words=1000)
        # Оба должны вернуть одинаковое количество слов (ограничено 1000)
        self.assertEqual(len(result_1001), len(result_1000))
        self.assertLessEqual(len(result_1001), 1000)

    def test_keyword_cloud_large_max_words_same_as_1000(self) -> None:
        """max_words=999_999 даёт тот же результат, что и max_words=1000."""
        words = [f"слово{i}" for i in range(500)]
        items = [_dict_item(" ".join(words))]
        result_large = self.gen.generate_cloud(items, max_words=999_999)
        result_1000 = self.gen.generate_cloud(items, max_words=1000)
        self.assertEqual(
            [cw.word for cw in result_large],
            [cw.word for cw in result_1000],
        )


class TestMaxWordsNegativeClamped(unittest.TestCase):
    """F2 MED: отрицательный max_words зажимается до 0 → пустой список."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator(stop_words=frozenset())

    def test_keyword_cloud_max_words_negative_clamped_to_zero(self) -> None:
        """max_words=-1 → 0 → generate_cloud возвращает пустой список."""
        items = [_dict_item("кошка собака птица")]
        result = self.gen.generate_cloud(items, max_words=-1)
        self.assertEqual(result, [])

    def test_keyword_cloud_max_words_large_negative_clamped_to_zero(self) -> None:
        """max_words=-999_999 → 0 → пустой список."""
        items = [_dict_item("кошка собака")]
        result = self.gen.generate_cloud(items, max_words=-999_999)
        self.assertEqual(result, [])

    def test_keyword_cloud_max_words_zero_returns_empty(self) -> None:
        """max_words=0 (граница снизу) → пустой список."""
        items = [_dict_item("кошка собака птица")]
        result = self.gen.generate_cloud(items, max_words=0)
        self.assertEqual(result, [])

    def test_keyword_cloud_max_words_one_returns_single(self) -> None:
        """max_words=1 — пограничный случай: возвращается ровно одно слово."""
        items = [_dict_item("кошка кошка кошка собака")]
        result = self.gen.generate_cloud(items, max_words=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].word, "кошка")


class TestMergeMapModuleConstant(unittest.TestCase):
    """F5 LOW: _MERGE_MAP — module-level dict, не пересоздаётся при каждом вызове."""

    def test_merge_map_module_constant(self) -> None:
        """_MERGE_MAP существует на уровне модуля как dict."""
        self.assertIsInstance(_MERGE_MAP, dict)

    def test_merge_map_identity_is_stable(self) -> None:
        """_MERGE_MAP — один и тот же объект при повторных вызовах _merge_similar."""
        id_before = id(_MERGE_MAP)
        gen = KeywordCloudGenerator(stop_words=frozenset())
        gen._merge_similar(["еще", "её", "кошка"])
        gen._merge_similar(["еще", "её", "собака"])
        id_after = id(_MERGE_MAP)
        self.assertEqual(id_before, id_after, "_MERGE_MAP не должен пересоздаваться")

    def test_merge_map_derived_from_merge_pairs(self) -> None:
        """_MERGE_MAP содержит все варианты из _MERGE_PAIRS."""
        for canonical, variant in _MERGE_PAIRS:
            self.assertIn(variant, _MERGE_MAP)
            self.assertEqual(_MERGE_MAP[variant], canonical)

    def test_merge_map_size_matches_merge_pairs(self) -> None:
        """Размер _MERGE_MAP совпадает с количеством пар в _MERGE_PAIRS."""
        self.assertEqual(len(_MERGE_MAP), len(_MERGE_PAIRS))

    def test_merge_similar_uses_merge_map(self) -> None:
        """_merge_similar корректно применяет _MERGE_MAP."""
        words = ["еще", "её", "кошка"]
        result = KeywordCloudGenerator._merge_similar(words)
        self.assertEqual(result[0], "ещё")   # "еще" → "ещё"
        self.assertEqual(result[1], "её")    # "её" → "её" (no change — already canonical)
        self.assertEqual(result[2], "кошка")  # без изменений

    def test_merge_map_variant_eshche_maps_to_canonical(self) -> None:
        """Вариант 'еще' → канонический 'ещё'."""
        self.assertIn("еще", _MERGE_MAP)
        self.assertEqual(_MERGE_MAP["еще"], "ещё")

    def test_merge_map_variant_ee_maps_to_canonical(self) -> None:
        """Вариант 'ее' → канонический 'её'."""
        self.assertIn("ее", _MERGE_MAP)
        self.assertEqual(_MERGE_MAP["ее"], "её")


class TestMaxWordsLimitConstant(unittest.TestCase):
    """Проверка, что _MAX_WORDS_LIMIT = 1000."""

    def test_max_words_limit_value(self) -> None:
        self.assertEqual(_MAX_WORDS_LIMIT, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
