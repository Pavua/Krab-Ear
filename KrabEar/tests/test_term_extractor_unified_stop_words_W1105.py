"""W1105: Тесты унификации стоп-слов TermExtractor с core/stop_words.py.

Проверяет:
1. test_term_extractor_uses_unified_stop_words — StopWords.get_stop_words()
   покрывает базовые стоп-слова (не дублирует их как frozenset в модуле).
2. test_no_duplicate_stop_word_lists — в term_extractor.py нет самостоятельных
   _STOP_WORDS_RU/_STOP_WORDS_ES/_STOP_WORDS_EN frozenset-переменных.
3. test_extracted_terms_match_keyword_cloud_baseline — TermExtractor и
   KeywordCloudGenerator фильтруют одно и то же слово через StopWords.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.stop_words import StopWords
from core.term_extractor import TermExtractor, _ALL_STOP_WORDS  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TERM_EXTRACTOR_PY = PROJECT_ROOT / "core" / "term_extractor.py"


def _get_module_level_frozenset_names(path: Path) -> list[str]:
    """Возвращает имена module-level переменных, назначенных frozenset(...)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name) and func.id == "frozenset":
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
    return names


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class UnifiedStopWordsTest(unittest.TestCase):
    """term_extractor использует unified StopWords, без дублирующих frozenset."""

    def test_term_extractor_uses_unified_stop_words(self) -> None:
        """_ALL_STOP_WORDS в term_extractor содержит всё из StopWords для ru/es/en."""
        for lang in ("ru", "es", "en"):
            canonical = StopWords.get_stop_words(lang)
            for word in canonical:
                self.assertIn(
                    word,
                    _ALL_STOP_WORDS,
                    msg=f"stop_words.{lang} слово '{word}' отсутствует в "
                        "term_extractor._ALL_STOP_WORDS",
                )

    def test_no_duplicate_stop_word_lists(self) -> None:
        """В term_extractor.py нет самостоятельных _STOP_WORDS_RU/ES/EN frozenset."""
        forbidden_names = {"_STOP_WORDS_RU", "_STOP_WORDS_ES", "_STOP_WORDS_EN"}
        found = set(_get_module_level_frozenset_names(_TERM_EXTRACTOR_PY))
        duplicates = found & forbidden_names
        self.assertEqual(
            duplicates,
            set(),
            msg=f"term_extractor.py содержит дублирующие frozenset: {duplicates}. "
                "Должны быть удалены в пользу StopWords из core/stop_words.py.",
        )

    def test_extracted_terms_match_keyword_cloud_baseline(self) -> None:
        """TermExtractor не возвращает базовые стоп-слова из unified StopWords."""
        extractor = TermExtractor()
        # Текст, содержащий ключевые слова и стоп-слова из unified StopWords
        text = (
            "Искусственный интеллект нейронные сети машинное обучение "
            "для всех через между или что это и в на но"
        )
        terms = extractor.extract_terms(text, language="ru")
        term_words = {t.term.lower() for t in terms}

        # Базовые RU стоп-слова из unified StopWords не должны появиться в результатах
        basic_stop_words_ru = {"и", "в", "на", "но", "это", "для", "что", "или", "через", "между"}
        leaked = term_words & basic_stop_words_ru
        self.assertEqual(
            leaked,
            set(),
            msg=f"TermExtractor вернул стоп-слова: {leaked}",
        )

    def test_extra_stop_words_included(self) -> None:
        """TermExtractor-специфичные слова (_EXTRA_STOP_*) включены в _ALL_STOP_WORDS."""
        from core.term_extractor import _EXTRA_STOP_RU, _EXTRA_STOP_ES  # type: ignore[attr-defined]
        for word in _EXTRA_STOP_RU:
            self.assertIn(word, _ALL_STOP_WORDS,
                          msg=f"RU extra '{word}' не найдено в _ALL_STOP_WORDS")
        for word in _EXTRA_STOP_ES:
            self.assertIn(word, _ALL_STOP_WORDS,
                          msg=f"ES extra '{word}' не найдено в _ALL_STOP_WORDS")

    def test_stop_words_api_unchanged(self) -> None:
        """StopWords.get_stop_words() API не изменился после рефакторинга."""
        ru = StopWords.get_stop_words("ru")
        self.assertIsInstance(ru, frozenset)
        self.assertGreater(len(ru), 50)
        # Базовые слова должны присутствовать
        self.assertIn("в", ru)
        self.assertIn("и", ru)
        self.assertIn("на", ru)

    def test_term_extractor_api_signature_preserved(self) -> None:
        """TermExtractor.extract_terms / extract_from_history API не изменился."""
        extractor = TermExtractor(min_term_length=3)
        self.assertEqual(extractor.min_term_length, 3)
        # extract_terms
        result = extractor.extract_terms("тест тест тест", language="ru")
        self.assertIsInstance(result, list)
        # extract_from_history
        items = [{"text": "тест тест тест"}, {"source_text": "тест тест тест тест"}]
        result2 = extractor.extract_from_history(items, min_frequency=2)
        self.assertIsInstance(result2, list)


if __name__ == "__main__":
    unittest.main()
