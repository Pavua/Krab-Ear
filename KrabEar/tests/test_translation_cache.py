"""Тесты для TranslationCache — персистентный LRU-кэш переводов."""

from backend.translation_cache import TranslationCache, _make_key
import json
import os
import sys
import tempfile
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestMakeKey(unittest.TestCase):
    """Тесты функции генерации ключей."""

    def test_same_params_same_key(self):
        k1 = _make_key("hello", "en", "ru", "hf_marian")
        k2 = _make_key("hello", "en", "ru", "hf_marian")
        self.assertEqual(k1, k2)

    def test_different_text_different_key(self):
        k1 = _make_key("hello", "en", "ru", "hf_marian")
        k2 = _make_key("world", "en", "ru", "hf_marian")
        self.assertNotEqual(k1, k2)

    def test_different_engine_different_key(self):
        k1 = _make_key("hello", "en", "ru", "hf_marian")
        k2 = _make_key("hello", "en", "ru", "deepl")
        self.assertNotEqual(k1, k2)

    def test_key_is_hex_string(self):
        k = _make_key("text", "ru", "es", "hf_marian")
        self.assertIsInstance(k, str)
        # SHA-256 hex = 64 chars
        self.assertEqual(len(k), 64)


class TestTranslationCacheBasic(unittest.TestCase):
    """Базовые операции get/put."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.cache = TranslationCache(data_dir=self._tmpdir)

    def test_get_miss_returns_none(self):
        result = self.cache.get("missing text", "en", "ru", "hf_marian")
        self.assertIsNone(result)

    def test_put_then_get_returns_value(self):
        self.cache.put("hello", "en", "ru", "hf_marian", "привет")
        result = self.cache.get("hello", "en", "ru", "hf_marian")
        self.assertEqual(result, "привет")

    def test_different_engine_is_separate_entry(self):
        self.cache.put("hello", "en", "ru", "engine_a", "привет_a")
        self.cache.put("hello", "en", "ru", "engine_b", "привет_b")
        self.assertEqual(self.cache.get("hello", "en", "ru", "engine_a"), "привет_a")
        self.assertEqual(self.cache.get("hello", "en", "ru", "engine_b"), "привет_b")

    def test_put_overwrites_existing_entry(self):
        self.cache.put("hello", "en", "ru", "hf_marian", "версия_1")
        self.cache.put("hello", "en", "ru", "hf_marian", "версия_2")
        self.assertEqual(self.cache.get("hello", "en", "ru", "hf_marian"), "версия_2")


class TestTranslationCacheStats(unittest.TestCase):
    """Тесты статистики кэша."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.cache = TranslationCache(data_dir=self._tmpdir)

    def test_initial_stats_are_zero(self):
        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(stats["hit_rate"], 0.0)

    def test_stats_after_miss(self):
        self.cache.get("text", "en", "ru", "hf_marian")
        stats = self.cache.get_stats()
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hits"], 0)

    def test_stats_after_hit(self):
        self.cache.put("text", "en", "ru", "hf_marian", "перевод")
        self.cache.get("text", "en", "ru", "hf_marian")
        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["entries"], 1)

    def test_hit_rate_calculation(self):
        self.cache.put("text", "en", "ru", "hf_marian", "перевод")
        self.cache.get("text", "en", "ru", "hf_marian")   # hit
        self.cache.get("other", "en", "ru", "hf_marian")  # miss
        stats = self.cache.get_stats()
        self.assertAlmostEqual(stats["hit_rate"], 0.5, places=4)


class TestTranslationCachePersistence(unittest.TestCase):
    """Тесты персистентности на диск."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_persist_and_reload(self):
        cache1 = TranslationCache(data_dir=self._tmpdir)
        cache1.put("hello", "en", "ru", "hf_marian", "привет")
        # Создаём новый экземпляр — должен подгрузить с диска
        cache2 = TranslationCache(data_dir=self._tmpdir)
        result = cache2.get("hello", "en", "ru", "hf_marian")
        self.assertEqual(result, "привет")

    def test_cache_file_created(self):
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("text", "ru", "es", "hf_marian", "texto")
        cache_path = os.path.join(self._tmpdir, "translation_cache.json")
        self.assertTrue(os.path.exists(cache_path))

    def test_cache_file_is_valid_json(self):
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("text", "ru", "es", "hf_marian", "texto")
        cache_path = os.path.join(self._tmpdir, "translation_cache.json")
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIsInstance(data, dict)
        self.assertEqual(len(data), 1)


class TestTranslationCacheEviction(unittest.TestCase):
    """Тесты LRU-вытеснения при превышении лимита."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_evicts_oldest_when_over_limit(self):
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=3)
        cache.put("a", "en", "ru", "e", "А")
        cache.put("b", "en", "ru", "e", "Б")
        cache.put("c", "en", "ru", "e", "В")
        # Добавляем 4-й — должен вытеснить "a" (LRU oldest)
        cache.put("d", "en", "ru", "e", "Г")
        self.assertIsNone(cache.get("a", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("b", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("d", "en", "ru", "e"))

    def test_entries_count_does_not_exceed_limit(self):
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=5)
        for i in range(10):
            cache.put(str(i), "en", "ru", "e", f"перевод_{i}")
        stats = cache.get_stats()
        self.assertLessEqual(stats["entries"], 5)


class TestTranslationCacheClear(unittest.TestCase):
    """Тест очистки кэша."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_clear_resets_all(self):
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("text", "en", "ru", "hf_marian", "перевод")
        cache.get("text", "en", "ru", "hf_marian")
        cache.clear()
        stats = cache.get_stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["entries"], 0)
        self.assertIsNone(cache.get("text", "en", "ru", "hf_marian"))


class TestTranslationCacheConcurrency(unittest.TestCase):
    """Тест потокобезопасности."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_concurrent_put_get(self):
        cache = TranslationCache(data_dir=self._tmpdir)
        errors = []

        def worker(n):
            try:
                key = f"text_{n}"
                cache.put(key, "en", "ru", "hf_marian", f"перевод_{n}")
                result = cache.get(key, "en", "ru", "hf_marian")
                assert result == f"перевод_{n}", f"Ожидалось перевод_{n}, получено {result}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")


class TestTranslationCacheUnicode(unittest.TestCase):
    """Тесты с юникодом и эмодзи."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.cache = TranslationCache(data_dir=self._tmpdir)

    def test_unicode_text_and_result(self):
        text = "Привет, мир! Как дела?"
        result = "¡Hola, mundo! ¿Cómo estás?"
        self.cache.put(text, "ru", "es", "hf_marian", result)
        retrieved = self.cache.get(text, "ru", "es", "hf_marian")
        self.assertEqual(retrieved, result)

    def test_emoji_in_text(self):
        text = "Hello 👋 world 🌍"
        result = "Привет 👋 мир 🌍"
        self.cache.put(text, "en", "ru", "hf_marian", result)
        retrieved = self.cache.get(text, "en", "ru", "hf_marian")
        self.assertEqual(retrieved, result)

    def test_emoji_in_result(self):
        text = "Good morning"
        result = "🌅 Доброе утро"
        self.cache.put(text, "en", "ru", "hf_marian", result)
        retrieved = self.cache.get(text, "en", "ru", "hf_marian")
        self.assertEqual(retrieved, result)

    def test_cyrillic_and_spanish(self):
        text = "Привет"
        result = "Hola"
        self.cache.put(text, "ru", "es", "hf_marian", result)
        retrieved = self.cache.get(text, "ru", "es", "hf_marian")
        self.assertEqual(retrieved, result)

    def test_mixed_scripts(self):
        text = "Hello привет 你好 مرحبا"
        result = "Greeting in multiple scripts"
        self.cache.put(text, "en", "ru", "hf_marian", result)
        retrieved = self.cache.get(text, "en", "ru", "hf_marian")
        self.assertEqual(retrieved, result)


class TestTranslationCacheLRUBehavior(unittest.TestCase):
    """Тесты LRU-поведения (move_to_end при get и put)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_get_moves_entry_to_end(self):
        """Проверяет, что get() перемещает запись в конец (most recent)."""
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=3)
        cache.put("a", "en", "ru", "e", "А")
        cache.put("b", "en", "ru", "e", "Б")
        cache.put("c", "en", "ru", "e", "В")
        # Обращаемся к "a" — он должен стать самым свежим
        _ = cache.get("a", "en", "ru", "e")
        # Добавляем "d" — должен вытеснить "b" (не "a", т.к. мы к нему обращались)
        cache.put("d", "en", "ru", "e", "Г")
        self.assertIsNotNone(cache.get("a", "en", "ru", "e"))
        self.assertIsNone(cache.get("b", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("c", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("d", "en", "ru", "e"))

    def test_put_on_existing_moves_to_end(self):
        """Проверяет, что put() существующего ключа перемещает его в конец."""
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=3)
        cache.put("a", "en", "ru", "e", "А")
        cache.put("b", "en", "ru", "e", "Б")
        cache.put("c", "en", "ru", "e", "В")
        # Переписываем "a" — он должен стать самым свежим
        cache.put("a", "en", "ru", "e", "А_новое")
        # Добавляем "d" — должен вытеснить "b"
        cache.put("d", "en", "ru", "e", "Г")
        self.assertIsNotNone(cache.get("a", "en", "ru", "e"))
        self.assertIsNone(cache.get("b", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("c", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("d", "en", "ru", "e"))


class TestTranslationCachePersistenceEdgeCases(unittest.TestCase):
    """Тесты персистентности с граничными случаями."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_load_from_corrupted_json_graceful_fail(self):
        """Проверяет, что загрузка из поврежденного JSON не вызывает исключение."""
        cache_path = os.path.join(self._tmpdir, "translation_cache.json")
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write("invalid json {{{")
        # Должна загруженаться без исключения (обработано в _load())
        cache = TranslationCache(data_dir=self._tmpdir)
        self.assertEqual(cache.get_stats()["entries"], 0)

    def test_persist_creates_data_dir_if_not_exists(self):
        """Проверяет, что _persist() создаёт data_dir если его нет."""
        nested_dir = os.path.join(self._tmpdir, "nested", "deep")
        cache = TranslationCache(data_dir=nested_dir)
        cache.put("text", "en", "ru", "hf_marian", "перевод")
        self.assertTrue(os.path.exists(nested_dir))
        self.assertTrue(os.path.exists(os.path.join(nested_dir, "translation_cache.json")))

    def test_reload_respects_max_entries_limit(self):
        """Проверяет, что при загрузке с диска соблюдается max_entries."""
        cache1 = TranslationCache(data_dir=self._tmpdir, max_entries=10)
        for i in range(15):
            cache1.put(str(i), "en", "ru", "e", f"text_{i}")
        # cache1 должна иметь только 10 записей
        stats1 = cache1.get_stats()
        self.assertEqual(stats1["entries"], 10)

        # Создаём новый экземпляр с max_entries=5
        cache2 = TranslationCache(data_dir=self._tmpdir, max_entries=5)
        # Должна загрузить с диска последние 10 записей от cache1
        # но обрезать до своего лимита 5
        stats2 = cache2.get_stats()
        self.assertEqual(stats2["entries"], 5)

    def test_atomic_write_with_tmp_file(self):
        """Проверяет, что персистентность использует атомарную запись через .tmp."""
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("text", "en", "ru", "hf_marian", "перевод")
        cache_path = os.path.join(self._tmpdir, "translation_cache.json")
        # .tmp файл не должен остаться после успешного сохранения
        tmp_path = cache_path + ".tmp"
        self.assertFalse(os.path.exists(tmp_path))
        self.assertTrue(os.path.exists(cache_path))


class TestTranslationCacheEmptyAndBoundary(unittest.TestCase):
    """Тесты пустых кэшей и граничных значений."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_clear_on_empty_cache(self):
        """Проверяет, что clear() на пустом кэше безопасна."""
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.clear()
        stats = cache.get_stats()
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)

    def test_max_entries_one(self):
        """Проверяет поведение при max_entries=1."""
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=1)
        cache.put("a", "en", "ru", "e", "А")
        cache.put("b", "en", "ru", "e", "Б")
        # Только "b" должна остаться
        self.assertIsNone(cache.get("a", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("b", "en", "ru", "e"))

    def test_empty_text_and_translation(self):
        """Проверяет, что пустые строки обрабатываются корректно."""
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("", "en", "ru", "hf_marian", "")
        result = cache.get("", "en", "ru", "hf_marian")
        self.assertEqual(result, "")

    def test_very_long_text(self):
        """Проверяет, что длинные тексты обрабатываются (хэширование)."""
        long_text = "a" * 10000
        long_result = "б" * 10000
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put(long_text, "en", "ru", "hf_marian", long_result)
        result = cache.get(long_text, "en", "ru", "hf_marian")
        self.assertEqual(result, long_result)


class TestTranslationCacheKeyCollision(unittest.TestCase):
    """Тесты коллизий ключей: одинаковый текст, разные параметры."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.cache = TranslationCache(data_dir=self._tmpdir)

    def test_same_text_different_source_lang_is_cache_miss(self):
        """Одинаковый текст с разными source lang — разные записи."""
        self.cache.put("hello", "en", "ru", "hf_marian", "привет_en")
        # Другой source — кэш-промах, None
        result = self.cache.get("hello", "es", "ru", "hf_marian")
        self.assertIsNone(result)

    def test_same_text_different_target_lang_is_cache_miss(self):
        """Одинаковый текст с разными target lang — разные записи."""
        self.cache.put("hello", "en", "ru", "hf_marian", "привет")
        # Другой target — кэш-промах
        result = self.cache.get("hello", "en", "es", "hf_marian")
        self.assertIsNone(result)

    def test_same_text_different_source_and_target_stored_independently(self):
        """Записи с разными source/target хранятся независимо."""
        self.cache.put("text", "en", "ru", "e", "en→ru")
        self.cache.put("text", "ru", "en", "e", "ru→en")
        self.cache.put("text", "en", "es", "e", "en→es")

        self.assertEqual(self.cache.get("text", "en", "ru", "e"), "en→ru")
        self.assertEqual(self.cache.get("text", "ru", "en", "e"), "ru→en")
        self.assertEqual(self.cache.get("text", "en", "es", "e"), "en→es")

    def test_key_uniqueness_all_params_matter(self):
        """Ключ уникален по всем четырём параметрам."""
        from backend.translation_cache import _make_key
        # Все четыре параметра отличаются на единственный символ
        k1 = _make_key("text", "en", "ru", "engine_v1")
        k2 = _make_key("text", "en", "ru", "engine_v2")
        k3 = _make_key("text", "en", "de", "engine_v1")
        k4 = _make_key("text", "fr", "ru", "engine_v1")
        k5 = _make_key("Text", "en", "ru", "engine_v1")  # capital T
        all_keys = [k1, k2, k3, k4, k5]
        self.assertEqual(len(set(all_keys)), 5, "Все ключи должны быть уникальными")

    def test_corrupt_cache_then_normal_operations_work(self):
        """После загрузки повреждённого файла кэш работает нормально."""
        cache_path = os.path.join(self._tmpdir, "translation_cache.json")
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write("{bad json :")
        cache = TranslationCache(data_dir=self._tmpdir)
        # Старые данные не загружены — miss
        self.assertIsNone(cache.get("x", "en", "ru", "e"))
        # Новые записи работают нормально
        cache.put("x", "en", "ru", "e", "translation")
        self.assertEqual(cache.get("x", "en", "ru", "e"), "translation")
        # Персистентность после corrupt-recovery тоже работает
        cache2 = TranslationCache(data_dir=self._tmpdir)
        self.assertEqual(cache2.get("x", "en", "ru", "e"), "translation")

    def test_no_ttl_entries_persist_indefinitely(self):
        """TranslationCache не имеет TTL — записи сохраняются без истечения срока."""
        self.cache.put("hello", "en", "ru", "hf_marian", "привет")
        # Без TTL запись всегда доступна
        for _ in range(3):
            result = self.cache.get("hello", "en", "ru", "hf_marian")
            self.assertEqual(result, "привет")
        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 3)


class TestTranslationCacheWave103(unittest.TestCase):
    """Wave 103 — обязательные test cases по спецификации задачи."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.cache = TranslationCache(data_dir=self._tmpdir)

    def test_set_get_basic(self):
        """put + get возвращает сохранённый перевод."""
        self.cache.put("hello", "en", "ru", "hf_marian", "привет")
        result = self.cache.get("hello", "en", "ru", "hf_marian")
        self.assertEqual(result, "привет")

    def test_cache_miss_returns_none(self):
        """get на несохранённый ключ возвращает None."""
        result = self.cache.get("nonexistent", "en", "ru", "hf_marian")
        self.assertIsNone(result)

    def test_cache_keys_normalized(self):
        """Ключи кэша чувствительны к регистру и пробелам (хэш по точному тексту).

        TranslationCache использует SHA-256 хэш без нормализации — «Hello» и «hello»
        и «hello » — три разных ключа. Тест документирует это поведение явно.
        """
        self.cache.put("Hello", "en", "ru", "hf_marian", "Привет_cap")
        self.cache.put("hello", "en", "ru", "hf_marian", "привет_lower")
        self.cache.put("hello ", "en", "ru", "hf_marian", "привет_trailing")

        # Разные ключи — разные результаты
        self.assertEqual(self.cache.get("Hello", "en", "ru", "hf_marian"), "Привет_cap")
        self.assertEqual(self.cache.get("hello", "en", "ru", "hf_marian"), "привет_lower")
        self.assertEqual(self.cache.get("hello ", "en", "ru", "hf_marian"), "привет_trailing")
        # Несохранённый вариант — miss
        self.assertIsNone(self.cache.get("HELLO", "en", "ru", "hf_marian"))

    def test_eviction_lru(self):
        """LRU вытесняет наименее недавно использованную запись при достижении лимита."""
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=3)
        cache.put("a", "en", "ru", "e", "А")
        cache.put("b", "en", "ru", "e", "Б")
        cache.put("c", "en", "ru", "e", "В")
        # Обращаемся к "a" — он становится MRU
        _ = cache.get("a", "en", "ru", "e")
        # Добавляем "d" → вытесняется "b" (LRU)
        cache.put("d", "en", "ru", "e", "Г")
        self.assertIsNone(cache.get("b", "en", "ru", "e"),
                          "b должна быть вытеснена как LRU")
        self.assertIsNotNone(cache.get("a", "en", "ru", "e"),
                             "a должна остаться (была недавно прочитана)")
        self.assertIsNotNone(cache.get("c", "en", "ru", "e"))
        self.assertIsNotNone(cache.get("d", "en", "ru", "e"))
        stats = cache.get_stats()
        self.assertLessEqual(stats["entries"], 3)

    def test_persist_across_reload(self):
        """Записи кэша сохраняются на диск и доступны после пересоздания экземпляра."""
        self.cache.put("hello", "en", "ru", "hf_marian", "привет")
        self.cache.put("world", "en", "ru", "hf_marian", "мир")
        # Новый экземпляр читает с диска
        cache2 = TranslationCache(data_dir=self._tmpdir)
        self.assertEqual(cache2.get("hello", "en", "ru", "hf_marian"), "привет")
        self.assertEqual(cache2.get("world", "en", "ru", "hf_marian"), "мир")

    def test_unicode_text_in_cache(self):
        """Тексты с кириллицей, CJK и эмодзи корректно сохраняются и извлекаются."""
        text = "Привет 你好 مرحبا 🌍"
        translation = "Hello all 🎉"
        self.cache.put(text, "multi", "en", "hf_marian", translation)
        result = self.cache.get(text, "multi", "en", "hf_marian")
        self.assertEqual(result, translation)

    def test_concurrent_set_thread_safe(self):
        """Параллельные put + get не вызывают ошибок и данные не теряются."""
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                key = f"text_{n}"
                self.cache.put(key, "en", "ru", "hf_marian", f"перевод_{n}")
                result = self.cache.get(key, "en", "ru", "hf_marian")
                assert result == f"перевод_{n}", f"Expected перевод_{n}, got {result}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"Thread errors: {errors}")


class TestTranslationCacheNetworkModeKey(unittest.TestCase):
    """W1318 — network_mode включён в ключ кэша (W1313 F1 HIGH — privacy bypass fix)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_cache_hit_only_when_network_mode_matches(self):
        """put с network_mode='online_opt_in' → get с тем же режимом = hit."""
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("hello", "en", "ru", "hf_marian", "привет", network_mode="online_opt_in")
        result = cache.get("hello", "en", "ru", "hf_marian", network_mode="online_opt_in")
        self.assertEqual(result, "привет")

    def test_offline_strict_request_does_not_hit_online_cached(self):
        """put online_opt_in → get offline_strict = miss (privacy guarantee)."""
        cache = TranslationCache(data_dir=self._tmpdir)
        # Кэшируем результат для online_opt_in запроса
        cache.put("hello", "en", "ru", "hf_marian", "привет_online", network_mode="online_opt_in")
        # offline_strict запрос ДОЛЖЕН получить miss — не online-результат
        result = cache.get("hello", "en", "ru", "hf_marian", network_mode="offline_strict")
        self.assertIsNone(
            result,
            "offline_strict должен получить cache miss, а не online_opt_in результат",
        )

    def test_legacy_cache_keys_treated_as_miss(self):
        """Старые ключи без network_mode (пустая строка) — miss для явных режимов.

        Backward compat: ключ _make_key(..., "") != _make_key(..., "offline_strict").
        Старые записи на диске не «просочатся» в новые режимные запросы.
        """
        cache = TranslationCache(data_dir=self._tmpdir)
        # Записываем с пустым network_mode (имитация legacy put без параметра)
        cache.put("hello", "en", "ru", "hf_marian", "legacy_value", network_mode="")
        # Запрос с явным network_mode должен быть miss
        result_offline = cache.get("hello", "en", "ru", "hf_marian", network_mode="offline_strict")
        result_online = cache.get("hello", "en", "ru", "hf_marian", network_mode="online_opt_in")
        self.assertIsNone(result_offline, "offline_strict не должен найти legacy запись")
        self.assertIsNone(result_online, "online_opt_in не должен найти legacy запись")
        # Пустой режим сам себе hit
        result_empty = cache.get("hello", "en", "ru", "hf_marian", network_mode="")
        self.assertEqual(result_empty, "legacy_value")

    def test_different_network_modes_stored_independently(self):
        """offline_strict и online_opt_in → две независимые записи."""
        cache = TranslationCache(data_dir=self._tmpdir)
        cache.put("hello", "en", "ru", "hf_marian", "offline_result", network_mode="offline_strict")
        cache.put("hello", "en", "ru", "hf_marian", "online_result", network_mode="online_opt_in")
        self.assertEqual(
            cache.get("hello", "en", "ru", "hf_marian", network_mode="offline_strict"),
            "offline_result",
        )
        self.assertEqual(
            cache.get("hello", "en", "ru", "hf_marian", network_mode="online_opt_in"),
            "online_result",
        )

    def test_make_key_network_mode_changes_hash(self):
        """_make_key с разными network_mode даёт разные SHA-256 хэши."""
        from backend.translation_cache import _make_key
        k_offline = _make_key("hello", "en", "ru", "hf_marian", "offline_strict")
        k_online = _make_key("hello", "en", "ru", "hf_marian", "online_opt_in")
        k_default = _make_key("hello", "en", "ru", "hf_marian", "offline_default")
        k_legacy = _make_key("hello", "en", "ru", "hf_marian", "")
        all_keys = [k_offline, k_online, k_default, k_legacy]
        self.assertEqual(
            len(set(all_keys)), 4,
            "Все четыре network_mode варианта должны давать уникальные ключи",
        )

    def test_persist_and_reload_preserves_network_mode_isolation(self):
        """После перезагрузки с диска network_mode изоляция сохраняется."""
        cache1 = TranslationCache(data_dir=self._tmpdir)
        cache1.put("text", "en", "ru", "hf_marian", "offline_cached", network_mode="offline_strict")
        cache1.put("text", "en", "ru", "hf_marian", "online_cached", network_mode="online_opt_in")

        cache2 = TranslationCache(data_dir=self._tmpdir)
        self.assertEqual(
            cache2.get("text", "en", "ru", "hf_marian", network_mode="offline_strict"),
            "offline_cached",
        )
        self.assertEqual(
            cache2.get("text", "en", "ru", "hf_marian", network_mode="online_opt_in"),
            "online_cached",
        )
        # Пустой режим — miss (не смешивается с именованными режимами)
        self.assertIsNone(
            cache2.get("text", "en", "ru", "hf_marian", network_mode=""),
        )


if __name__ == "__main__":
    unittest.main()
