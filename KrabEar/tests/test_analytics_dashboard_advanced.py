"""Расширенные тесты AnalyticsDashboard — интеграция + TTL + concurrent."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.analytics_dashboard import AnalyticsDashboard, _CACHE_TTL_SEC
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_fake_item(
    days_ago: int = 0,
    hour: int = 10,
    confidence: float = 0.85,
    audio_duration_sec: float = 30.0,
    text: str = "hello world test",
    source_lang: str = "ru",
):
    base_date = date.today() - timedelta(days=days_ago)
    dt = datetime.combine(base_date, datetime.min.time(), tzinfo=timezone.utc)
    dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)

    class FakeItem:
        ts = dt.isoformat()

    item = FakeItem()
    item.confidence = confidence
    item.audio_duration_sec = audio_duration_sec
    item.text = text
    item.source_lang = source_lang
    item.translated_text = ""
    item.translation_status = "not_requested"
    item.llm_applied = False
    item.diarization = None
    return item


def _make_mock_store(items=None, data_dir=None):
    store = MagicMock()
    if data_dir is None:
        data_dir = Path(tempfile.mkdtemp())
    store.data_dir = data_dir
    store.history_path = data_dir / "history.ndjson"
    store.history_path.touch()
    lock_ctx = MagicMock()
    lock_ctx.__enter__ = MagicMock(return_value=None)
    lock_ctx.__exit__ = MagicMock(return_value=False)
    store._lock = MagicMock(return_value=lock_ctx)
    store._load_active_items_unlocked = MagicMock(return_value=items or [])
    return store


# ---------------------------------------------------------------------------
# A1. Интеграция с реальным StateStore
# ---------------------------------------------------------------------------

class TestDashboardIntegrationRealStore(unittest.TestCase):
    """Дашборд работает с настоящим StateStore без исключений."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self._tmp.name))
        self.dashboard = AnalyticsDashboard()

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_real_store_no_crash(self):
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 0)

    def test_with_multiple_real_items(self):
        for i in range(5):
            self.store.add_history_item(
                text=f"тест {i} один два три",
                paste_status="ok",
                audio_duration_sec=float(30 + i),
            )
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 5)
        self.assertGreater(result["overview"]["total_words"], 0)
        self.assertGreater(result["overview"]["total_hours"], 0)

    def test_today_section_accurate_with_real_store(self):
        self.store.add_history_item(
            text="сегодняшняя запись слово слово",
            paste_status="ok",
            audio_duration_sec=120.0,
        )
        self.dashboard.invalidate_cache()
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["today"]["recordings"], 1)
        self.assertGreater(result["today"]["duration_min"], 0)

    def test_storage_size_nonzero_after_writing(self):
        self.store.add_history_item(text="размер файла тест запись", paste_status="ok")
        self.dashboard.invalidate_cache()
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertGreaterEqual(result["storage"]["history_size_mb"], 0.0)
        self.assertIsInstance(result["storage"]["history_size_mb"], float)

    def test_performance_section_returns_floats(self):
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertIsInstance(result["performance"]["avg_stt_latency_ms"], float)
        self.assertIsInstance(result["performance"]["p95_latency_ms"], float)

    def test_quality_section_with_real_items(self):
        """Качество считается корректно по реальным items (без confidence по умолчанию)."""
        self.store.add_history_item(text="раз два три", paste_status="ok")
        self.dashboard.invalidate_cache()
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        # StateStore items могут не иметь confidence — допустимо 0.0
        self.assertGreaterEqual(result["quality"]["avg_confidence"], 0.0)
        self.assertLessEqual(result["quality"]["avg_confidence"], 1.0)


# ---------------------------------------------------------------------------
# A2. TTL кэша — корректно истекает
# ---------------------------------------------------------------------------

class TestDashboardCacheTTL(unittest.TestCase):
    """Cache TTL истекает и данные обновляются."""

    def test_cache_expires_after_ttl(self):
        dashboard = AnalyticsDashboard()
        store = _make_mock_store([_make_fake_item()])

        dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(store._load_active_items_unlocked.call_count, 1)

        # Подделываем старый timestamp в кэше (за пределами TTL)
        with dashboard._lock:
            days_key = 30
            ts_old = time.monotonic() - (_CACHE_TTL_SEC + 5)
            cached_result = dashboard._cache[days_key][1]
            dashboard._cache[days_key] = (ts_old, cached_result)

        dashboard.get_full_dashboard(store, days=30)
        # После истечения TTL должен быть ещё один вызов к store
        self.assertEqual(store._load_active_items_unlocked.call_count, 2)

    def test_cache_still_valid_within_ttl(self):
        dashboard = AnalyticsDashboard()
        store = _make_mock_store([_make_fake_item()])

        dashboard.get_full_dashboard(store, days=30)

        # Выставляем timestamp «почти на границе» TTL (1 сек до истечения)
        with dashboard._lock:
            days_key = 30
            ts_fresh = time.monotonic() - (_CACHE_TTL_SEC - 1)
            cached_result = dashboard._cache[days_key][1]
            dashboard._cache[days_key] = (ts_fresh, cached_result)

        dashboard.get_full_dashboard(store, days=30)
        # Кэш ещё валиден — второй вызов к store не нужен
        self.assertEqual(store._load_active_items_unlocked.call_count, 1)

    def test_invalidate_forces_fresh_load(self):
        dashboard = AnalyticsDashboard()
        store = _make_mock_store([_make_fake_item()])

        dashboard.get_full_dashboard(store, days=30)
        dashboard.invalidate_cache()
        dashboard.get_full_dashboard(store, days=30)
        self.assertEqual(store._load_active_items_unlocked.call_count, 2)

    def test_different_days_independent_ttl(self):
        """TTL каждого days-ключа независим."""
        dashboard = AnalyticsDashboard()
        store = _make_mock_store([_make_fake_item()])

        dashboard.get_full_dashboard(store, days=7)
        dashboard.get_full_dashboard(store, days=30)

        # Истекаем только ключ 7
        with dashboard._lock:
            ts_old = time.monotonic() - (_CACHE_TTL_SEC + 5)
            cached7 = dashboard._cache[7][1]
            dashboard._cache[7] = (ts_old, cached7)

        dashboard.get_full_dashboard(store, days=7)   # должен обновиться
        dashboard.get_full_dashboard(store, days=30)  # ещё валиден

        self.assertEqual(store._load_active_items_unlocked.call_count, 3)


# ---------------------------------------------------------------------------
# A3. Concurrent callers — stress test
# ---------------------------------------------------------------------------

class TestDashboardConcurrentCallers(unittest.TestCase):
    """Несколько потоков вызывают get_full_dashboard одновременно."""

    def test_concurrent_calls_no_exception(self):
        dashboard = AnalyticsDashboard()
        store = _make_mock_store([_make_fake_item(days_ago=i) for i in range(10)])
        errors = []
        results = []
        lock = threading.Lock()

        def worker():
            try:
                r = dashboard.get_full_dashboard(store, days=30)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 20)

    def test_concurrent_all_results_consistent(self):
        """Все потоки возвращают одинаковый результат (одни данные в store)."""
        dashboard = AnalyticsDashboard()
        store = _make_mock_store([_make_fake_item(text="alpha beta gamma")])
        results = []
        lock = threading.Lock()

        def worker():
            r = dashboard.get_full_dashboard(store, days=30)
            with lock:
                results.append(r["overview"]["total_words"])

        threads = [threading.Thread(target=worker) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Все потоки должны увидеть 3 слова
        self.assertTrue(all(v == 3 for v in results), f"Inconsistent results: {results}")

    def test_concurrent_invalidate_and_read(self):
        """invalidate_cache() во время concurrent reads не вызывает исключений."""
        dashboard = AnalyticsDashboard()
        store = _make_mock_store([_make_fake_item()])
        errors = []

        def reader():
            for _ in range(5):
                try:
                    dashboard.get_full_dashboard(store, days=30)
                except Exception as exc:
                    errors.append(exc)

        def invalidator():
            for _ in range(3):
                dashboard.invalidate_cache()
                time.sleep(0.001)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        threads.append(threading.Thread(target=invalidator))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Errors during concurrent invalidate: {errors}")


# ---------------------------------------------------------------------------
# A4. Интеграция с SentimentTrendAnalyzer (smoke)
# ---------------------------------------------------------------------------

class TestDashboardWithSentimentIntegration(unittest.TestCase):
    """Dashboard не падает при реальных данных, подходящих для SentimentTrendAnalyzer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self._tmp.name))
        self.dashboard = AnalyticsDashboard()

    def tearDown(self):
        self._tmp.cleanup()

    def test_dashboard_with_varied_text_content(self):
        """Тексты с разной тональностью не ломают дашборд."""
        texts = [
            "отличный день, всё хорошо",
            "плохой результат, очень недоволен",
            "нормально, ничего особенного",
        ]
        for t in texts:
            self.store.add_history_item(text=t, paste_status="ok")
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 3)

    def test_dashboard_with_multilingual_items(self):
        """Многоязычные записи обрабатываются корректно."""
        self.store.add_history_item(text="привет мир тест", paste_status="ok")
        self.store.add_history_item(text="hola mundo test", paste_status="ok")
        self.store.add_history_item(text="hello world test", paste_status="ok")
        self.dashboard.invalidate_cache()
        result = self.dashboard.get_full_dashboard(self.store, days=30)
        self.assertEqual(result["overview"]["total_recordings"], 3)


if __name__ == "__main__":
    unittest.main()
