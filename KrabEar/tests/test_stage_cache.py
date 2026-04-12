"""Тесты для StageCache — LRU-кэш результатов стадий pipeline.

Покрывает:
- compute_hash: bytes, str, dict, list, numpy array, fallback
- get/put: базовая запись и чтение
- cache miss: несуществующая стадия, несуществующий хэш
- TTL expiration: запись истекает после TTL
- LRU eviction: при превышении max_entries вытесняется oldest
- invalidate: одна стадия и все
- get_stats: hits, misses, hit_rate, entries_per_stage
- thread safety: параллельные get/put не ломают состояние
- PipelineExecutor интеграция: cache hit пропускает выполнение стадии
- PipelineExecutor: cacheable=False стадия не кэшируется
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pipeline.stage_cache import StageCache
from core.pipeline.context import PipelineContext
from core.pipeline.executor import PipelineExecutor


# ---------------------------------------------------------------------------
# Тесты StageCache
# ---------------------------------------------------------------------------

class TestComputeHash(unittest.TestCase):
    """Тесты StageCache.compute_hash."""

    def test_bytes_returns_sha256_hex(self):
        h = StageCache.compute_hash(b"hello world")
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)  # SHA-256 hex digest

    def test_same_bytes_same_hash(self):
        h1 = StageCache.compute_hash(b"audio data")
        h2 = StageCache.compute_hash(b"audio data")
        self.assertEqual(h1, h2)

    def test_different_bytes_different_hash(self):
        h1 = StageCache.compute_hash(b"audio data 1")
        h2 = StageCache.compute_hash(b"audio data 2")
        self.assertNotEqual(h1, h2)

    def test_string_input(self):
        h = StageCache.compute_hash("hello")
        self.assertEqual(len(h), 64)

    def test_dict_input_sorted_keys(self):
        h1 = StageCache.compute_hash({"b": 2, "a": 1})
        h2 = StageCache.compute_hash({"a": 1, "b": 2})
        # Sorted keys — одинаковый хэш независимо от порядка
        self.assertEqual(h1, h2)

    def test_list_input(self):
        h = StageCache.compute_hash([1, 2, 3])
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)

    def test_fallback_to_repr(self):
        # Объект, который не bytes/str/dict/list
        h = StageCache.compute_hash(42)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)

    def test_numpy_array_if_available(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        arr = np.zeros(1000, dtype="float32")
        h = StageCache.compute_hash(arr)
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)

    def test_numpy_same_data_same_hash(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        arr1 = np.array([0.1, 0.2, 0.3], dtype="float32")
        arr2 = np.array([0.1, 0.2, 0.3], dtype="float32")
        self.assertEqual(StageCache.compute_hash(arr1), StageCache.compute_hash(arr2))

    def test_numpy_different_data_different_hash(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        arr1 = np.array([0.1, 0.2, 0.3], dtype="float32")
        arr2 = np.array([0.4, 0.5, 0.6], dtype="float32")
        self.assertNotEqual(StageCache.compute_hash(arr1), StageCache.compute_hash(arr2))


class TestStageCacheBasic(unittest.TestCase):
    """Базовые тесты get/put."""

    def setUp(self):
        self.cache = StageCache(max_entries=10)

    def test_put_and_get_returns_result(self):
        result = {"raw_text": "Привет мир", "confidence": 0.95}
        self.cache.put("stt", "hash_abc", result, ttl_sec=60)
        got = self.cache.get("stt", "hash_abc")
        self.assertIsNotNone(got)
        self.assertEqual(got["raw_text"], "Привет мир")
        self.assertAlmostEqual(got["confidence"], 0.95)

    def test_get_miss_unknown_stage(self):
        result = self.cache.get("nonexistent_stage", "hash_xyz")
        self.assertIsNone(result)

    def test_get_miss_unknown_hash(self):
        self.cache.put("stt", "hash_001", {"raw_text": "test"}, ttl_sec=60)
        result = self.cache.get("stt", "hash_999")
        self.assertIsNone(result)

    def test_get_expired_entry_returns_none(self):
        self.cache.put("stt", "hash_exp", {"raw_text": "будет удалено"}, ttl_sec=0)
        # ttl=0 → немедленно истекает
        time.sleep(0.01)
        result = self.cache.get("stt", "hash_exp")
        self.assertIsNone(result)

    def test_put_overwrites_existing_key(self):
        self.cache.put("stt", "hash_dup", {"raw_text": "старый"}, ttl_sec=60)
        self.cache.put("stt", "hash_dup", {"raw_text": "новый"}, ttl_sec=60)
        got = self.cache.get("stt", "hash_dup")
        self.assertEqual(got["raw_text"], "новый")

    def test_different_stages_independent(self):
        self.cache.put("stt", "hash_x", {"raw_text": "из stt"}, ttl_sec=60)
        self.cache.put("text_cleanup", "hash_x", {"cleaned_text": "из cleanup"}, ttl_sec=60)
        stt_result = self.cache.get("stt", "hash_x")
        cleanup_result = self.cache.get("text_cleanup", "hash_x")
        self.assertEqual(stt_result["raw_text"], "из stt")
        self.assertEqual(cleanup_result["cleaned_text"], "из cleanup")


class TestStageCacheLRU(unittest.TestCase):
    """Тесты LRU eviction."""

    def test_lru_evicts_oldest_when_full(self):
        cache = StageCache(max_entries=3)
        cache.put("stt", "h1", {"v": 1}, ttl_sec=300)
        cache.put("stt", "h2", {"v": 2}, ttl_sec=300)
        cache.put("stt", "h3", {"v": 3}, ttl_sec=300)

        # При добавлении 4-го — h1 (oldest) должен быть вытеснен
        cache.put("stt", "h4", {"v": 4}, ttl_sec=300)

        self.assertIsNone(cache.get("stt", "h1"))
        self.assertIsNotNone(cache.get("stt", "h2"))
        self.assertIsNotNone(cache.get("stt", "h3"))
        self.assertIsNotNone(cache.get("stt", "h4"))

    def test_lru_access_updates_order(self):
        cache = StageCache(max_entries=3)
        cache.put("stt", "h1", {"v": 1}, ttl_sec=300)
        cache.put("stt", "h2", {"v": 2}, ttl_sec=300)
        cache.put("stt", "h3", {"v": 3}, ttl_sec=300)

        # Обращаемся к h1 — он становится recently used
        cache.get("stt", "h1")

        # Добавляем h4 — теперь h2 должен быть вытеснен (h1 уже не oldest)
        cache.put("stt", "h4", {"v": 4}, ttl_sec=300)

        self.assertIsNotNone(cache.get("stt", "h1"))  # h1 выжил
        self.assertIsNone(cache.get("stt", "h2"))     # h2 вытеснен
        self.assertIsNotNone(cache.get("stt", "h3"))
        self.assertIsNotNone(cache.get("stt", "h4"))

    def test_max_entries_enforced(self):
        cache = StageCache(max_entries=5)
        for i in range(10):
            cache.put("stt", f"h{i}", {"v": i}, ttl_sec=300)
        stats = cache.get_stats()
        self.assertLessEqual(stats["entries_per_stage"]["stt"], 5)


class TestStageCacheInvalidate(unittest.TestCase):
    """Тесты invalidate."""

    def setUp(self):
        self.cache = StageCache()

    def test_invalidate_single_stage(self):
        self.cache.put("stt", "h1", {"raw_text": "x"}, ttl_sec=300)
        self.cache.put("text_cleanup", "h1", {"cleaned_text": "y"}, ttl_sec=300)

        self.cache.invalidate("stt")

        self.assertIsNone(self.cache.get("stt", "h1"))
        self.assertIsNotNone(self.cache.get("text_cleanup", "h1"))

    def test_invalidate_all_stages(self):
        self.cache.put("stt", "h1", {"raw_text": "x"}, ttl_sec=300)
        self.cache.put("text_cleanup", "h2", {"cleaned_text": "y"}, ttl_sec=300)
        self.cache.put("llm_rewrite", "h3", {"rewritten_text": "z"}, ttl_sec=300)

        self.cache.invalidate()

        stats = self.cache.get_stats()
        self.assertEqual(stats["total_entries"], 0)

    def test_invalidate_nonexistent_stage_no_error(self):
        # Не должно бросать исключение
        self.cache.invalidate("stage_that_never_existed")


class TestStageCacheStats(unittest.TestCase):
    """Тесты get_stats."""

    def setUp(self):
        self.cache = StageCache()

    def test_initial_stats(self):
        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["hit_rate"], 0.0)
        self.assertEqual(stats["total_entries"], 0)
        self.assertEqual(stats["entries_per_stage"], {})

    def test_stats_track_hits_and_misses(self):
        self.cache.put("stt", "h1", {"raw_text": "ok"}, ttl_sec=300)
        self.cache.get("stt", "h1")   # hit
        self.cache.get("stt", "h1")   # hit
        self.cache.get("stt", "h999") # miss
        self.cache.get("other", "h1") # miss

        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 2)
        self.assertAlmostEqual(stats["hit_rate"], 0.5)

    def test_stats_entries_per_stage(self):
        self.cache.put("stt", "h1", {}, ttl_sec=300)
        self.cache.put("stt", "h2", {}, ttl_sec=300)
        self.cache.put("text_cleanup", "h1", {}, ttl_sec=300)

        stats = self.cache.get_stats()
        self.assertEqual(stats["entries_per_stage"]["stt"], 2)
        self.assertEqual(stats["entries_per_stage"]["text_cleanup"], 1)
        self.assertEqual(stats["total_entries"], 3)

    def test_hit_rate_zero_when_no_lookups(self):
        self.cache.put("stt", "h1", {}, ttl_sec=300)
        stats = self.cache.get_stats()
        self.assertEqual(stats["hit_rate"], 0.0)

    def test_reset_stats(self):
        self.cache.put("stt", "h1", {"raw_text": "test"}, ttl_sec=300)
        self.cache.get("stt", "h1")  # hit
        self.cache.reset_stats()

        stats = self.cache.get_stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        # Записи должны остаться
        self.assertEqual(stats["total_entries"], 1)


class TestStageCacheThreadSafety(unittest.TestCase):
    """Тест thread safety."""

    def test_concurrent_put_get(self):
        cache = StageCache(max_entries=50)
        errors = []

        def worker(thread_id: int):
            try:
                for i in range(20):
                    h = f"hash_{thread_id}_{i}"
                    cache.put("stt", h, {"thread": thread_id, "i": i}, ttl_sec=60)
                    result = cache.get("stt", h)
                    if result is not None:
                        # Если запись не вытеснена, она должна принадлежать этому потоку
                        # (или быть уже перезаписана — оба варианта OK)
                        pass
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        # Кэш жив и не сломан
        stats = cache.get_stats()
        self.assertIsInstance(stats["hits"], int)
        self.assertIsInstance(stats["misses"], int)


# ---------------------------------------------------------------------------
# Тесты интеграции с PipelineExecutor
# ---------------------------------------------------------------------------

def _make_stt_stage(call_counter: list, cacheable: bool = True):
    """Создать stub-стадию STT с счётчиком вызовов."""

    class FakeSTTStage:
        name = "stt"
        cacheable = True  # будет переопределено

        def should_run(self, ctx):
            return True

        def process(self, ctx):
            call_counter.append(1)
            ctx.raw_text = "Привет, это тест"
            ctx.confidence = 0.9
            ctx.model_used = "test-model"
            ctx.language_detected = "ru"
            ctx.segments = []
            return ctx

    stage = FakeSTTStage()
    stage.cacheable = cacheable
    return stage


def _make_noncacheable_stage(call_counter: list):
    """Создать stub-стадию без cacheable=True."""

    class FakeCleanupStage:
        name = "text_cleanup"
        # cacheable НЕ установлен (по умолчанию False через getattr)

        def should_run(self, ctx):
            return True

        def process(self, ctx):
            call_counter.append(1)
            ctx.cleaned_text = ctx.raw_text.strip()
            return ctx

    return FakeCleanupStage()


class TestPipelineExecutorCacheIntegration(unittest.TestCase):
    """Тесты интеграции StageCache с PipelineExecutor."""

    def _make_ctx(self, audio_data=b"fake audio bytes"):
        return PipelineContext(audio_input=audio_data)

    def test_executor_without_cache_works_normally(self):
        """PipelineExecutor без кэша работает как раньше."""
        calls = []
        stage = _make_stt_stage(calls)
        executor = PipelineExecutor([stage])
        ctx = self._make_ctx()
        result = executor.run(ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.raw_text, "Привет, это тест")

    def test_cache_hit_skips_stage_execution(self):
        """При cache hit стадия не выполняется второй раз."""
        calls = []
        stage = _make_stt_stage(calls)
        cache = StageCache()
        executor = PipelineExecutor([stage], cache=cache)

        audio = b"identical audio bytes"

        # Первый вызов — cache miss, стадия выполняется
        ctx1 = self._make_ctx(audio)
        executor.run(ctx1)
        self.assertEqual(len(calls), 1)

        # Второй вызов с тем же аудио — cache hit, стадия НЕ выполняется
        ctx2 = self._make_ctx(audio)
        result2 = executor.run(ctx2)
        self.assertEqual(len(calls), 1)  # всё ещё 1 — стадия не вызывалась

        # Результат применён из кэша
        self.assertEqual(result2.raw_text, "Привет, это тест")
        self.assertAlmostEqual(result2.confidence, 0.9)

    def test_cache_miss_on_different_audio(self):
        """При разном аудио — два cache miss, стадия вызвана дважды."""
        calls = []
        stage = _make_stt_stage(calls)
        cache = StageCache()
        executor = PipelineExecutor([stage], cache=cache)

        executor.run(self._make_ctx(b"audio one"))
        executor.run(self._make_ctx(b"audio two"))

        self.assertEqual(len(calls), 2)

    def test_noncacheable_stage_not_cached(self):
        """Стадия без cacheable=True не кэшируется."""
        calls = []
        stage = _make_noncacheable_stage(calls)
        cache = StageCache()
        executor = PipelineExecutor([stage], cache=cache)

        audio = b"same audio"
        executor.run(self._make_ctx(audio))
        executor.run(self._make_ctx(audio))

        # Стадия вызвана дважды, несмотря на одинаковое аудио
        self.assertEqual(len(calls), 2)

        # Кэш пуст для этой стадии
        stats = cache.get_stats()
        self.assertNotIn("text_cleanup", stats["entries_per_stage"])

    def test_cache_stats_reflect_hits_and_misses(self):
        """Статистика кэша корректна после нескольких запусков."""
        calls = []
        stage = _make_stt_stage(calls)
        cache = StageCache()
        executor = PipelineExecutor([stage], cache=cache)

        audio = b"consistent audio"
        executor.run(self._make_ctx(audio))  # miss
        executor.run(self._make_ctx(audio))  # hit
        executor.run(self._make_ctx(audio))  # hit

        stats = cache.get_stats()
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 1)
        self.assertAlmostEqual(stats["hit_rate"], 2 / 3, places=3)

    def test_cache_invalidate_forces_re_execution(self):
        """После invalidate стадия выполняется снова."""
        calls = []
        stage = _make_stt_stage(calls)
        cache = StageCache()
        executor = PipelineExecutor([stage], cache=cache)

        audio = b"audio to invalidate"
        executor.run(self._make_ctx(audio))  # miss → put
        cache.invalidate("stt")
        executor.run(self._make_ctx(audio))  # miss после invalidate

        self.assertEqual(len(calls), 2)

    def test_stage_error_not_cached(self):
        """Если стадия добавила ошибку — результат не кэшируется."""

        class ErrorSTTStage:
            name = "stt"
            cacheable = True

            def should_run(self, ctx):
                return True

            def process(self, ctx):
                ctx.errors.append("stt: transcription failed")
                return ctx

        cache = StageCache()
        executor = PipelineExecutor([ErrorSTTStage()], cache=cache)

        audio = b"bad audio"
        executor.run(self._make_ctx(audio))

        # Кэш должен быть пуст — ошибки не кэшируются
        stats = cache.get_stats()
        self.assertEqual(stats["total_entries"], 0)


if __name__ == "__main__":
    unittest.main()
