"""Wave-23 — disk-DoS защита TranslationCache.

Покрывает:
  * (a) per-value byte cap — значение >64 KiB не кэшируется;
  * (b) total-bytes ceiling — вытеснение срабатывает по байтам, а не только
    по числу записей;
  * (c) debounced persist — серия быстрых put-ов НЕ делает по одному
    fsync/json.dump на каждый put (коалесцируется), но flush()/close()
    гарантированно сбрасывают остаток на диск;
  * translate_selection БОЛЬШЕ не в ipc_throttle.EXCLUDED_METHODS — write-path
    теперь rate-limited.

Тесты быстрые (<2s), не зависят от mlx_whisper.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.translation_cache import (  # noqa: E402
    TranslationCache,
    _make_key,
    _MAX_VALUE_BYTES,
)


class TestPerValueByteCap(unittest.TestCase):
    """(a) Значения крупнее max_value_bytes не кэшируются."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_oversized_value_not_stored(self):
        cache = TranslationCache(data_dir=self._tmpdir, max_value_bytes=1024)
        big = "x" * 2048  # 2048 байт UTF-8 > 1024 cap
        cache.put("text", "en", "ru", "e", big)
        # Не сохранено → miss
        self.assertIsNone(cache.get("text", "en", "ru", "e"))
        self.assertEqual(cache.get_stats()["entries"], 0)

    def test_oversized_value_not_persisted_to_disk(self):
        cache = TranslationCache(data_dir=self._tmpdir, max_value_bytes=1024)
        cache.put("text", "en", "ru", "e", "x" * 5000)
        cache_path = os.path.join(self._tmpdir, "translation_cache.json")
        # Файл либо не создан, либо пуст — отравленное значение не на диске.
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data, {})

    def test_value_at_cap_is_stored(self):
        cache = TranslationCache(data_dir=self._tmpdir, max_value_bytes=1024)
        exact = "y" * 1024  # ровно cap → допустимо
        cache.put("text", "en", "ru", "e", exact)
        self.assertEqual(cache.get("text", "en", "ru", "e"), exact)

    def test_multibyte_value_counted_in_bytes_not_chars(self):
        """Кириллица = 2 байта/символ; cap считается в байтах."""
        cache = TranslationCache(data_dir=self._tmpdir, max_value_bytes=100)
        # 80 кириллических символов = 160 байт > 100 cap → отклонено
        cache.put("text", "ru", "es", "e", "я" * 80)
        self.assertIsNone(cache.get("text", "ru", "es", "e"))
        # 40 символов = 80 байт < 100 → принято
        cache.put("text2", "ru", "es", "e", "я" * 40)
        self.assertEqual(cache.get("text2", "ru", "es", "e"), "я" * 40)

    def test_default_cap_is_64kib(self):
        self.assertEqual(_MAX_VALUE_BYTES, 64 * 1024)


class TestTotalBytesCeiling(unittest.TestCase):
    """(b) Вытеснение по суммарным байтам, не только по числу записей."""

    def test_total_bytes_eviction_triggers(self):
        # max_entries большой, но total-byte бюджет маленький → eviction по байтам.
        cache = TranslationCache(
            data_dir=tempfile.mkdtemp(),
            max_entries=1000,
            max_value_bytes=10_000,
            max_total_bytes=3000,  # 3 KiB суммарно
        )
        # Каждое значение ~1000 байт; после 4-х суммарно ~4000 > 3000 → eviction.
        for i in range(4):
            cache.put(f"k{i}", "en", "ru", "e", "z" * 1000)
        stats = cache.get_stats()
        # Не более 3 записей умещается в 3000-байтовый бюджет.
        self.assertLessEqual(stats["entries"], 3)
        # Самая старая ("k0") вытеснена.
        self.assertIsNone(cache.get("k0", "en", "ru", "e"))
        # Самая свежая ("k3") сохранена.
        self.assertIsNotNone(cache.get("k3", "en", "ru", "e"))

    def test_total_bytes_decremented_on_overwrite(self):
        """Перезапись ключа не должна накапливать фантомные байты в бюджете."""
        cache = TranslationCache(
            data_dir=tempfile.mkdtemp(),
            max_entries=1000,
            max_value_bytes=10_000,
            max_total_bytes=2500,
        )
        # Перезаписываем один и тот же ключ 10 раз значением ~1000 байт.
        for _ in range(10):
            cache.put("same", "en", "ru", "e", "a" * 1000)
        # Должна остаться ровно 1 запись (overwrite, не накопление).
        self.assertEqual(cache.get_stats()["entries"], 1)
        self.assertEqual(cache.get("same", "en", "ru", "e"), "a" * 1000)

    def test_count_limit_still_enforced_under_byte_budget(self):
        """Лимит по числу записей работает, даже если байтовый бюджет велик."""
        cache = TranslationCache(
            data_dir=tempfile.mkdtemp(),
            max_entries=3,
            max_value_bytes=10_000,
            max_total_bytes=100 * 1024 * 1024,
        )
        for i in range(6):
            cache.put(f"k{i}", "en", "ru", "e", "small")
        self.assertLessEqual(cache.get_stats()["entries"], 3)


class TestDebouncedPersist(unittest.TestCase):
    """(c) Быстрые put-ы коалесцируют дисковую запись."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_default_flush_every_one_writes_each_put(self):
        """flush_every=1 (дефолт) → запись на каждый put (back-compat)."""
        cache = TranslationCache(data_dir=self._tmpdir)  # flush_every=1
        with patch.object(cache, "_persist", wraps=cache._persist) as spy:
            cache.put("a", "en", "ru", "e", "А")
            cache.put("b", "en", "ru", "e", "Б")
            cache.put("c", "en", "ru", "e", "В")
        self.assertEqual(spy.call_count, 3)

    def test_rapid_puts_coalesce_persist_calls(self):
        """flush_every=10 → 9 быстрых put-ов делают 0 дисковых записей."""
        cache = TranslationCache(data_dir=self._tmpdir, flush_every=10)
        with patch.object(cache, "_persist", wraps=cache._persist) as spy:
            for i in range(9):
                cache.put(f"k{i}", "en", "ru", "e", f"v{i}")
            # 9 < flush_every=10 → ни одной дисковой записи (коалесцируется).
            self.assertEqual(
                spy.call_count, 0,
                "9 быстрых put-ов не должны сделать ни одного fsync/json.dump",
            )
            # 10-й put достигает порога → ровно одна запись.
            cache.put("k9", "en", "ru", "e", "v9")
            self.assertEqual(spy.call_count, 1)

    def test_persist_far_fewer_than_puts(self):
        """100 put-ов при flush_every=25 → не более 4 дисковых записей."""
        cache = TranslationCache(data_dir=self._tmpdir, flush_every=25)
        with patch.object(cache, "_persist", wraps=cache._persist) as spy:
            for i in range(100):
                cache.put(f"k{i}", "en", "ru", "e", f"v{i}")
        # 100 / 25 = 4 записи — на порядок меньше 100 синхронных fsync.
        self.assertLessEqual(spy.call_count, 4)
        self.assertLess(spy.call_count, 100)

    def test_flush_writes_pending_coalesced_entries(self):
        """flush() сбрасывает накопленные (некратные flush_every) put-ы на диск."""
        cache = TranslationCache(data_dir=self._tmpdir, flush_every=100)
        # 3 put-а < 100 → ещё не на диске.
        cache.put("a", "en", "ru", "e", "А")
        cache.put("b", "en", "ru", "e", "Б")
        cache.put("c", "en", "ru", "e", "В")
        cache.flush()
        # Новый экземпляр должен прочитать все 3 записи с диска.
        cache2 = TranslationCache(data_dir=self._tmpdir)
        self.assertEqual(cache2.get("a", "en", "ru", "e"), "А")
        self.assertEqual(cache2.get("b", "en", "ru", "e"), "Б")
        self.assertEqual(cache2.get("c", "en", "ru", "e"), "В")

    def test_close_flushes_pending(self):
        """close() — алиас flush(): тоже сбрасывает остаток."""
        cache = TranslationCache(data_dir=self._tmpdir, flush_every=100)
        cache.put("x", "en", "ru", "e", "ИКС")
        cache.close()
        cache2 = TranslationCache(data_dir=self._tmpdir)
        self.assertEqual(cache2.get("x", "en", "ru", "e"), "ИКС")

    def test_flush_is_noop_when_not_dirty(self):
        """Повторный flush без новых put-ов не делает лишних дисковых записей."""
        cache = TranslationCache(data_dir=self._tmpdir, flush_every=100)
        cache.put("x", "en", "ru", "e", "ИКС")
        cache.flush()  # пишет
        with patch.object(cache, "_persist", wraps=cache._persist) as spy:
            cache.flush()  # уже чисто → не пишет
            self.assertEqual(spy.call_count, 0)

    def test_coalesced_entries_in_memory_immediately(self):
        """Коалесцированные записи доступны в памяти сразу (get не ждёт диска)."""
        cache = TranslationCache(data_dir=self._tmpdir, flush_every=100)
        cache.put("a", "en", "ru", "e", "А")
        # На диск не записано, но в памяти есть.
        self.assertEqual(cache.get("a", "en", "ru", "e"), "А")


class TestPoisonedDiskFileEviction(unittest.TestCase):
    """Заранее раздутый файл на диске обрезается при загрузке (byte-eviction)."""

    def test_load_applies_total_byte_eviction(self):
        tmpdir = tempfile.mkdtemp()
        cache_path = os.path.join(tmpdir, "translation_cache.json")
        # Пишем файл с 5 крупными значениями (по ~1000 байт) напрямую.
        poisoned = {}
        for i in range(5):
            key = _make_key(f"k{i}", "en", "ru", "e")
            poisoned[key] = "p" * 1000
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(poisoned, fh, ensure_ascii=False)
        # Загружаем с маленьким total-byte бюджетом → часть вытесняется.
        cache = TranslationCache(
            data_dir=tmpdir,
            max_entries=1000,
            max_value_bytes=10_000,
            max_total_bytes=2500,
        )
        self.assertLessEqual(cache.get_stats()["entries"], 3)


class TestTranslateSelectionThrottled(unittest.TestCase):
    """translate_selection больше не исключён из throttling."""

    def test_translate_selection_not_in_excluded(self):
        from backend.ipc_throttle import EXCLUDED_METHODS
        self.assertNotIn(
            "translate_selection", EXCLUDED_METHODS,
            "translate_selection должен быть throttled (пишет в персистентный кэш)",
        )

    def test_translate_selection_classified_medium(self):
        from backend.ipc_throttle import _classify_method
        self.assertEqual(_classify_method("translate_selection"), "medium")

    def test_translate_selection_is_rate_limited(self):
        """После исчерпания medium-бакета translate_selection отклоняется."""
        from backend.ipc_throttle import IPCThrottle
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 3, "light": 120})
        allowed = sum(
            1 for _ in range(10) if throttle.check_rate("translate_selection")
        )
        # Ровно medium-лимит (3) разрешено, остальное throttled.
        self.assertEqual(allowed, 3)


if __name__ == "__main__":
    unittest.main()
