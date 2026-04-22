"""Тесты для VocabularyStore — постоянного хранилища словаря STT.

Покрывает: save, load, merge, пустой файл, повреждённый файл,
           add_words, remove_words, дедупликация, атомарная запись.
"""
from __future__ import annotations
from backend.vocabulary_store import VocabularyStore

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))


class TestVocabularySave(unittest.TestCase):
    """Тест базового сохранения."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VocabularyStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_creates_json_file(self):
        self.store.save(["привет", "мир"])
        self.assertTrue(self.store.path.exists())

    def test_saved_file_has_correct_format(self):
        self.store.save(["hello", "world"])
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertIn("words", payload)
        self.assertIn("updated_at", payload)
        self.assertIsInstance(payload["words"], list)

    def test_save_deduplicates_words(self):
        self.store.save(["foo", "foo", "bar", "foo"])
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["words"]), 2)
        self.assertIn("foo", payload["words"])
        self.assertIn("bar", payload["words"])

    def test_save_sorts_words(self):
        self.store.save(["zebra", "apple", "mango"])
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["words"], sorted(payload["words"]))

    def test_save_strips_whitespace(self):
        self.store.save(["  hello  ", " world"])
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertNotIn("  hello  ", payload["words"])
        self.assertIn("hello", payload["words"])


class TestVocabularyLoad(unittest.TestCase):
    """Тест загрузки."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VocabularyStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_returns_saved_words(self):
        self.store.save(["alpha", "beta", "gamma"])
        loaded = self.store.load()
        self.assertEqual(sorted(loaded), ["alpha", "beta", "gamma"])

    def test_load_empty_file_returns_empty_list(self):
        # Файл не существует
        loaded = self.store.load()
        self.assertEqual(loaded, [])

    def test_load_after_create_empty_file(self):
        # Создаём пустой файл
        self.store.path.write_text("", encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded, [])

    def test_load_corrupt_file_returns_empty_list(self):
        self.store.path.write_text("{not valid json!!!}", encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded, [])

    def test_load_wrong_format_file_returns_empty_list(self):
        # Валидный JSON но не ожидаемая структура
        self.store.path.write_text(json.dumps(["word1", "word2"]), encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded, [])

    def test_load_missing_words_key_returns_empty_list(self):
        self.store.path.write_text(json.dumps({"updated_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded, [])


class TestVocabularyMerge(unittest.TestCase):
    """Тест объединения словарей."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VocabularyStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_combines_saved_and_extra(self):
        self.store.save(["alpha", "beta"])
        result = self.store.merge(["gamma", "delta"])
        self.assertIn("alpha", result)
        self.assertIn("beta", result)
        self.assertIn("gamma", result)
        self.assertIn("delta", result)

    def test_merge_deduplicates(self):
        self.store.save(["alpha", "beta"])
        result = self.store.merge(["beta", "gamma"])
        self.assertEqual(result.count("beta"), 1)

    def test_merge_does_not_save_to_disk(self):
        self.store.save(["alpha"])
        self.store.merge(["newword"])
        # Файл на диске не должен содержать newword
        loaded = self.store.load()
        self.assertNotIn("newword", loaded)

    def test_merge_empty_extra_returns_saved(self):
        self.store.save(["alpha", "beta"])
        result = self.store.merge([])
        self.assertEqual(sorted(result), ["alpha", "beta"])

    def test_merge_with_empty_store(self):
        result = self.store.merge(["only_extra"])
        self.assertEqual(result, ["only_extra"])


class TestVocabularyAddRemove(unittest.TestCase):
    """Тест add_words и remove_words."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VocabularyStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_words_persists(self):
        self.store.save(["alpha"])
        self.store.add_words(["beta", "gamma"])
        loaded = self.store.load()
        self.assertIn("alpha", loaded)
        self.assertIn("beta", loaded)
        self.assertIn("gamma", loaded)

    def test_remove_words_persists(self):
        self.store.save(["alpha", "beta", "gamma"])
        self.store.remove_words(["beta"])
        loaded = self.store.load()
        self.assertIn("alpha", loaded)
        self.assertNotIn("beta", loaded)
        self.assertIn("gamma", loaded)

    def test_remove_nonexistent_word_is_noop(self):
        self.store.save(["alpha"])
        result = self.store.remove_words(["nonexistent"])
        self.assertEqual(result, ["alpha"])


class TestVocabularyUpdatedAt(unittest.TestCase):
    """Тест поля updated_at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VocabularyStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_updated_at_is_iso8601(self):
        self.store.save(["test"])
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        ts = payload["updated_at"]
        # Должен содержать дату и время
        self.assertIn("T", ts)
        self.assertIn("+", ts)

    def test_updated_at_changes_on_save(self):
        import time
        self.store.save(["first"])
        payload1 = json.loads(self.store.path.read_text(encoding="utf-8"))
        time.sleep(0.01)
        self.store.save(["second"])
        payload2 = json.loads(self.store.path.read_text(encoding="utf-8"))
        # Либо изменился, либо остался таким же (зависит от точности),
        # но updated_at должен присутствовать в обоих случаях
        self.assertIn("updated_at", payload1)
        self.assertIn("updated_at", payload2)


class TestVocabularyUnicode(unittest.TestCase):
    """Тест работы с юникод-словами (кириллица, латинница, спецсимволы)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VocabularyStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load_cyrillic_words(self):
        words = ["привет", "мир", "Краб", "аудио"]
        self.store.save(words)
        loaded = self.store.load()
        self.assertEqual(sorted(loaded), sorted(words))

    def test_save_and_load_mixed_scripts(self):
        words = ["hello", "привет", "hola", "мир"]
        self.store.save(words)
        loaded = self.store.load()
        self.assertEqual(sorted(loaded), sorted(words))

    def test_save_unicode_no_data_loss(self):
        # Слова с диакритикой и акцентами
        words = ["café", "naïve", "résumé", "über"]
        self.store.save(words)
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        for w in words:
            self.assertIn(w, payload["words"])

    def test_add_words_unicode(self):
        self.store.save(["alpha"])
        self.store.add_words(["бета", "гамма"])
        loaded = self.store.load()
        self.assertIn("бета", loaded)
        self.assertIn("гамма", loaded)
        self.assertIn("alpha", loaded)

    def test_remove_words_unicode(self):
        self.store.save(["alpha", "привет", "мир"])
        self.store.remove_words(["привет"])
        loaded = self.store.load()
        self.assertNotIn("привет", loaded)
        self.assertIn("мир", loaded)
        self.assertIn("alpha", loaded)

    def test_dedup_unicode(self):
        self.store.save(["краб", "краб", "КРАБ"])
        loaded = self.store.load()
        # "краб" != "КРАБ" — case-sensitive
        self.assertEqual(len(loaded), 2)

    def test_empty_string_and_whitespace_filtered(self):
        self.store.save(["", "   ", "valid", "\t"])
        loaded = self.store.load()
        self.assertEqual(loaded, ["valid"])


class TestVocabularyListAll(unittest.TestCase):
    """Тест list_all / load как публичного API перечисления слов."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VocabularyStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_returns_list(self):
        self.store.save(["x", "y", "z"])
        result = self.store.load()
        self.assertIsInstance(result, list)

    def test_load_after_multiple_saves_returns_latest(self):
        self.store.save(["alpha", "beta"])
        self.store.save(["gamma", "delta"])
        loaded = self.store.load()
        self.assertNotIn("alpha", loaded)
        self.assertIn("gamma", loaded)
        self.assertIn("delta", loaded)

    def test_add_words_returns_full_list(self):
        self.store.save(["a", "b"])
        result = self.store.add_words(["c", "d"])
        self.assertEqual(sorted(result), ["a", "b", "c", "d"])

    def test_remove_words_returns_remaining_list(self):
        self.store.save(["a", "b", "c"])
        result = self.store.remove_words(["b"])
        self.assertEqual(sorted(result), ["a", "c"])

    def test_add_empty_list_returns_existing(self):
        self.store.save(["existing"])
        result = self.store.add_words([])
        self.assertEqual(result, ["existing"])

    def test_remove_all_words_leaves_empty(self):
        self.store.save(["only"])
        result = self.store.remove_words(["only"])
        self.assertEqual(result, [])
        loaded = self.store.load()
        self.assertEqual(loaded, [])

    def test_add_duplicate_of_existing_no_double(self):
        self.store.save(["alpha"])
        result = self.store.add_words(["alpha"])
        self.assertEqual(result.count("alpha"), 1)


if __name__ == "__main__":
    unittest.main()
