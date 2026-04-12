"""Тесты handle_word_frequency_analysis — частотный анализ слов по истории."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.history_service import HistoryService


class WordFrequencyAnalysisTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    # 1. Пустая история
    def test_empty_history_returns_zeros(self) -> None:
        result = self.svc.handle_word_frequency_analysis({})
        self.assertEqual(result["total_words"], 0)
        self.assertEqual(result["unique_words"], 0)
        self.assertEqual(result["vocabulary_richness"], 0.0)
        self.assertEqual(result["top_words"], [])
        self.assertEqual(result["bigrams"], [])
        self.assertEqual(result["by_language"], {})

    # 2. Базовые поля присутствуют
    def test_result_has_required_keys(self) -> None:
        self.store.add_history_item(text="привет мир хорошо", paste_status="ok")
        result = self.svc.handle_word_frequency_analysis({})
        for key in ("top_words", "total_words", "unique_words", "vocabulary_richness", "bigrams", "by_language"):
            self.assertIn(key, result, f"missing key: {key}")

    # 3. Стоп-слова отфильтровываются
    def test_stop_words_are_filtered(self) -> None:
        # "и", "в", "на" — стоп-слова
        self.store.add_history_item(text="кот и собака в доме на улице", paste_status="ok")
        result = self.svc.handle_word_frequency_analysis({})
        words = [entry["word"] for entry in result["top_words"]]
        for sw in ("и", "в", "на"):
            self.assertNotIn(sw, words, f"stop word '{sw}' should be filtered")

    # 4. Самое частое слово на первом месте
    def test_most_frequent_word_first(self) -> None:
        texts = ["работа работа работа", "работа проект проект", "проект"]
        for t in texts:
            self.store.add_history_item(text=t, paste_status="ok")
        result = self.svc.handle_word_frequency_analysis({})
        top = result["top_words"]
        self.assertTrue(len(top) >= 2)
        # "работа" встречается 4 раза, "проект" — 3
        self.assertEqual(top[0]["word"], "работа")
        self.assertEqual(top[0]["count"], 4)
        self.assertGreater(top[0]["percentage"], 0)

    # 5. vocabulary_richness = unique/total
    def test_vocabulary_richness_ratio(self) -> None:
        # "яблоко яблоко груша" — 3 токена, 2 уникальных -> 2/3
        self.store.add_history_item(text="яблоко яблоко груша", paste_status="ok")
        result = self.svc.handle_word_frequency_analysis({})
        total = result["total_words"]
        unique = result["unique_words"]
        expected = round(unique / total, 4) if total else 0.0
        self.assertAlmostEqual(result["vocabulary_richness"], expected, places=4)

    # 6. Биграммы формируются корректно
    def test_bigrams_generated(self) -> None:
        self.store.add_history_item(text="быстрый лис прыгает быстрый лис прыгает", paste_status="ok")
        result = self.svc.handle_word_frequency_analysis({})
        bigram_phrases = [b["phrase"] for b in result["bigrams"]]
        self.assertTrue(len(bigram_phrases) > 0)
        # биграмма "быстрый лис" должна присутствовать дважды → первая в топе
        self.assertIn("быстрый лис", bigram_phrases)

    # 7. Фильтрация по языку
    def test_language_filter(self) -> None:
        self.store.add_history_item(text="кот пёс лис", paste_status="ok", source_lang="ru")
        self.store.add_history_item(text="gato perro zorro", paste_status="ok", source_lang="es")
        result_ru = self.svc.handle_word_frequency_analysis({"language": "ru"})
        result_es = self.svc.handle_word_frequency_analysis({"language": "es"})
        ru_words = [e["word"] for e in result_ru["top_words"]]
        es_words = [e["word"] for e in result_es["top_words"]]
        # ru-слова не должны попадать в es-результат
        for w in ("кот", "пёс", "лис"):
            self.assertNotIn(w, es_words)
        for w in ("gato", "perro", "zorro"):
            self.assertNotIn(w, ru_words)

    # 8. by_language разбивает результаты по языкам
    def test_by_language_structure(self) -> None:
        self.store.add_history_item(text="кот пёс", paste_status="ok", source_lang="ru")
        self.store.add_history_item(text="gato perro", paste_status="ok", source_lang="es")
        result = self.svc.handle_word_frequency_analysis({})
        by_lang = result["by_language"]
        self.assertIn("ru", by_lang)
        self.assertIn("es", by_lang)
        self.assertIn("top_words", by_lang["ru"])
        self.assertIn("top_words", by_lang["es"])

    # 9. top_words ограничен 50 элементами
    def test_top_words_capped_at_50(self) -> None:
        # добавляем 60 уникальных слов
        words = " ".join(f"слово{i}" for i in range(60))
        self.store.add_history_item(text=words, paste_status="ok")
        result = self.svc.handle_word_frequency_analysis({})
        self.assertLessEqual(len(result["top_words"]), 50)

    # 10. percentage суммируется ~100% (допуск на округление)
    def test_percentages_sum_to_100(self) -> None:
        self.store.add_history_item(text="красный синий зелёный красный синий красный", paste_status="ok")
        result = self.svc.handle_word_frequency_analysis({})
        total_pct = sum(e["percentage"] for e in result["top_words"])
        self.assertAlmostEqual(total_pct, 100.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
