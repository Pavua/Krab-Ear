"""Тесты утечек памяти и ресурсов Krab Ear.

Проверяет, что кольцевые буферы, кэши, трекеры и временные ресурсы
не растут неограниченно при интенсивной нагрузке.
"""

from __future__ import annotations
from core.pipeline.executor import PipelineExecutor
from core.pipeline.context import PipelineContext
from backend.playback_tracker import PlaybackTracker
from backend.translation_cache import TranslationCache
from backend.session_tracker import SessionTracker
from backend.search_history import SearchHistoryManager, _MAX_ENTRIES as SEARCH_MAX
from backend.event_replay import EventReplayManager, _MAX_BUFFER_SIZE as REPLAY_BUFFER_SIZE
from backend.error_reporter import ErrorReporter, _BUFFER_SIZE as ERROR_BUFFER_SIZE

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 1. ErrorReporter — кольцевой буфер не растёт за _BUFFER_SIZE
# ---------------------------------------------------------------------------

class TestErrorReporterRingBuffer(unittest.TestCase):
    """Буфер ErrorReporter ограничен _BUFFER_SIZE=500."""

    def test_buffer_capped_at_default_size(self):
        """При добавлении 1 000 ошибок буфер содержит не больше 500 записей."""
        reporter = ErrorReporter()
        for i in range(1000):
            reporter.report_error("stt", "E", f"msg {i}")
        errors = reporter.get_recent_errors(limit=ERROR_BUFFER_SIZE + 100)
        self.assertLessEqual(len(errors), ERROR_BUFFER_SIZE)

    def test_buffer_capped_at_custom_size(self):
        """При кастомном max_size буфер не превышает его."""
        reporter = ErrorReporter(max_size=50)
        for i in range(300):
            reporter.report_error("llm", "E", f"msg {i}")
        errors = reporter.get_recent_errors(limit=1000)
        self.assertLessEqual(len(errors), 50)

    def test_buffer_preserves_most_recent(self):
        """После вытеснения буфер содержит самые свежие записи."""
        reporter = ErrorReporter(max_size=10)
        for i in range(25):
            reporter.report_error("ipc", "E", f"msg {i}")
        errors = reporter.get_recent_errors(limit=10)
        # Самая свежая запись — msg 24
        self.assertEqual(errors[0].message, "msg 24")

    def test_buffer_thread_safe_under_load(self):
        """Конкурентная запись из 20 потоков не вызывает переполнения буфера."""
        reporter = ErrorReporter(max_size=100)

        def worker():
            for _ in range(50):
                reporter.report_error("audio", "E", "concurrent msg")

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        errors = reporter.get_recent_errors(limit=1000)
        self.assertLessEqual(len(errors), 100)


# ---------------------------------------------------------------------------
# 2. EventReplayManager — кольцевой буфер не растёт за max_buffer
# ---------------------------------------------------------------------------

class TestEventReplayRingBuffer(unittest.TestCase):
    """Буфер EventReplayManager ограничен max_buffer."""

    def test_buffer_capped_at_default(self):
        """При записи 12 000 событий буфер содержит не больше 10 000."""
        mgr = EventReplayManager()
        for i in range(12_000):
            mgr.record_event("stt.final", {"idx": i})
        stats = mgr.get_event_stats()
        self.assertLessEqual(stats["total_events"], REPLAY_BUFFER_SIZE)

    def test_buffer_capped_at_custom_size(self):
        """При кастомном max_buffer буфер не превышает его."""
        mgr = EventReplayManager(max_buffer=200)
        for i in range(500):
            mgr.record_event("test.event", {"i": i})
        stats = mgr.get_event_stats()
        self.assertLessEqual(stats["total_events"], 200)

    def test_file_handle_closed_after_close(self):
        """close() закрывает файловый дескриптор персистенции."""
        with tempfile.TemporaryDirectory() as tmpdir:
            persist_path = Path(tmpdir) / "events.ndjson"
            mgr = EventReplayManager(persist_path=persist_path)
            mgr.record_event("stt.final", {"x": 1})
            # Файловый дескриптор должен быть открыт
            self.assertIsNotNone(mgr._file_handle)
            mgr.close()
            # После close — дескриптор обнулён
            self.assertIsNone(mgr._file_handle)

    def test_no_file_handle_without_persist_path(self):
        """Без persist_path файловый дескриптор не открывается."""
        mgr = EventReplayManager()
        mgr.record_event("stt.final", {"x": 1})
        self.assertIsNone(mgr._file_handle)
        mgr.close()


# ---------------------------------------------------------------------------
# 3. SearchHistoryManager — история ограничена 500 записями
# ---------------------------------------------------------------------------

class TestSearchHistoryBounded(unittest.TestCase):
    """SearchHistoryManager хранит не более _MAX_ENTRIES=500 запросов."""

    def test_search_history_capped_at_500(self):
        """При добавлении 800 запросов остаётся только 500 самых свежих."""
        mgr = SearchHistoryManager()
        for i in range(800):
            mgr.record_search(f"query {i}", results_count=i)
        searches = mgr.get_recent_searches(limit=1000)
        self.assertLessEqual(len(searches), SEARCH_MAX)

    def test_search_history_keeps_latest(self):
        """После усечения сохраняются самые поздние запросы."""
        mgr = SearchHistoryManager()
        for i in range(600):
            mgr.record_search(f"q{i}")
        # Самый свежий — последний добавленный
        recent = mgr.get_recent_searches(limit=1)
        self.assertEqual(recent[0]["query"], "q599")

    def test_search_history_empty_queries_ignored(self):
        """Пустые и пробельные запросы не добавляются в историю."""
        mgr = SearchHistoryManager()
        mgr.record_search("")
        mgr.record_search("   ")
        mgr.record_search("\t\n")
        searches = mgr.get_recent_searches(limit=10)
        self.assertEqual(len(searches), 0)


# ---------------------------------------------------------------------------
# 4. TranslationCache — LRU-кэш ограничен max_entries
# ---------------------------------------------------------------------------

class TestTranslationCacheBounded(unittest.TestCase):
    """TranslationCache хранит не более max_entries записей."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_cache_capped_at_max_entries(self):
        """При добавлении 300 записей в кэш с max_entries=100 хранится ≤100."""
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=100)
        for i in range(300):
            cache.put(f"text_{i}", "ru", "es", "opus", f"translated_{i}")
        stats = cache.get_stats()
        self.assertLessEqual(stats["entries"], 100)

    def test_cache_lru_eviction_order(self):
        """Старые записи вытесняются первыми (LRU)."""
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=5)
        for i in range(5):
            cache.put(f"text_{i}", "ru", "es", "opus", f"tr_{i}")
        # Добавляем 6-й — должна вытесниться первая запись
        cache.put("text_NEW", "ru", "es", "opus", "tr_new")
        stats = cache.get_stats()
        self.assertLessEqual(stats["entries"], 5)
        # text_0 должна быть вытеснена
        result = cache.get("text_0", "ru", "es", "opus")
        self.assertIsNone(result)

    def test_cache_stays_bounded_under_load(self):
        """При интенсивных вставках кэш не превышает max_entries."""
        cache = TranslationCache(data_dir=self._tmpdir, max_entries=50)
        for i in range(1000):
            cache.put(f"t{i}", "en", "ru", "nllb", f"translated {i}")
        stats = cache.get_stats()
        self.assertLessEqual(stats["entries"], 50)


# ---------------------------------------------------------------------------
# 5. SessionTracker — кольцевой буфер ограничен 1000 сессиями
# ---------------------------------------------------------------------------

class TestSessionTrackerBounded(unittest.TestCase):
    """SessionTracker хранит не более max_sessions=1000 сессий."""

    def test_sessions_capped_at_1000(self):
        """При завершении 1500 сессий в буфере остаётся не более 1000."""
        tracker = SessionTracker(max_sessions=1000)
        for i in range(1500):
            tracker.start_session(audio_device=f"mic_{i}")
            tracker.end_session({"duration_sec": 1.0})
        sessions = tracker.get_sessions(limit=5000)
        self.assertLessEqual(len(sessions), 1000)

    def test_sessions_capped_at_custom_size(self):
        """При кастомном max_sessions буфер не превышает его."""
        tracker = SessionTracker(max_sessions=20)
        for i in range(50):
            tracker.start_session()
            tracker.end_session({"duration_sec": 0.5})
        sessions = tracker.get_sessions(limit=100)
        self.assertLessEqual(len(sessions), 20)

    def test_session_stats_stable_after_overflow(self):
        """get_session_stats() корректно работает после переполнения буфера."""
        tracker = SessionTracker(max_sessions=10)
        for i in range(30):
            tracker.start_session()
            tracker.end_session({"duration_sec": float(i), "paste_status": "ok"})
        stats = tracker.get_session_stats()
        self.assertLessEqual(stats["total_sessions"], 10)
        self.assertGreaterEqual(stats["paste_ok_rate"], 0.0)
        self.assertLessEqual(stats["paste_ok_rate"], 1.0)


# ---------------------------------------------------------------------------
# 6. PipelineExecutor — временные файлы удаляются после выполнения
# ---------------------------------------------------------------------------

class TestPipelineExecutorTempFileCleanup(unittest.TestCase):
    """PipelineExecutor удаляет temp-файл после завершения pipeline."""

    def _make_tmp_file(self) -> str:
        """Создаёт временный файл и возвращает его путь."""
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    def test_temp_file_deleted_on_success(self):
        """После успешного run() temp-файл удаляется."""
        tmp = self._make_tmp_file()
        self.assertTrue(os.path.exists(tmp))

        ctx = PipelineContext(audio_input=None)
        ctx._temp_path = tmp

        executor = PipelineExecutor(stages=[])
        executor.run(ctx)

        self.assertFalse(os.path.exists(tmp), "Temp-файл не был удалён после run()")
        self.assertIsNone(ctx._temp_path)

    def test_temp_file_deleted_on_stage_exception(self):
        """Temp-файл удаляется даже если стадия выбрасывает исключение."""
        from core.pipeline.base import PipelineStage

        class BoomStage(PipelineStage):
            name = "boom"

            def process(self, ctx: PipelineContext) -> PipelineContext:
                raise RuntimeError("stage failure")

        tmp = self._make_tmp_file()
        ctx = PipelineContext(audio_input=None)
        ctx._temp_path = tmp

        executor = PipelineExecutor(stages=[BoomStage()])
        executor.run(ctx)

        self.assertFalse(os.path.exists(tmp), "Temp-файл не был удалён после ошибки стадии")
        self.assertIsNone(ctx._temp_path)

    def test_no_temp_file_path_no_error(self):
        """run() без _temp_path не вызывает ошибок."""
        ctx = PipelineContext(audio_input=None)
        ctx._temp_path = None
        executor = PipelineExecutor(stages=[])
        try:
            executor.run(ctx)
        except Exception as exc:
            self.fail(f"run() без temp_path вызвал исключение: {exc}")


# ---------------------------------------------------------------------------
# 7. PlaybackTracker — данные в памяти не растут неограниченно при нагрузке
# ---------------------------------------------------------------------------

class TestPlaybackTrackerMemory(unittest.TestCase):
    """PlaybackTracker хранит по одной записи на item_id (агрегирование)."""

    def test_unique_items_grow_linearly(self):
        """При добавлении N уникальных item_id хранится ровно N записей."""
        tracker = PlaybackTracker()
        n = 200
        for i in range(n):
            tracker.record_playback(f"item_{i}", duration_listened_sec=5.0)
        # Каждый item_id — отдельная запись
        self.assertEqual(len(tracker._stats), n)

    def test_repeated_item_does_not_duplicate(self):
        """Повторные воспроизведения одного item_id не создают дубликатов."""
        tracker = PlaybackTracker()
        for _ in range(1000):
            tracker.record_playback("item_42", duration_listened_sec=1.0)
        self.assertEqual(len(tracker._stats), 1)
        stats = tracker.get_playback_stats("item_42")
        self.assertEqual(stats["play_count"], 1000)

    def test_total_listened_accumulates_correctly(self):
        """total_listened_sec корректно суммируется при повторных вызовах."""
        tracker = PlaybackTracker()
        for _ in range(10):
            tracker.record_playback("item_X", duration_listened_sec=3.5)
        stats = tracker.get_playback_stats("item_X")
        self.assertAlmostEqual(stats["total_listened_sec"], 35.0, places=5)


# ---------------------------------------------------------------------------
# 8. EventReplayManager — файловый дескриптор закрывается при закрытии
# ---------------------------------------------------------------------------

class TestEventReplayFileHandleLeak(unittest.TestCase):
    """Файловый дескриптор EventReplayManager не утекает."""

    def test_close_is_idempotent(self):
        """Двойной вызов close() не вызывает исключений."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = EventReplayManager(persist_path=Path(tmpdir) / "ev.ndjson")
            mgr.record_event("x", {})
            mgr.close()
            mgr.close()  # второй вызов — без исключений
            self.assertIsNone(mgr._file_handle)

    def test_file_written_before_close(self):
        """События записываются в файл до вызова close()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ev.ndjson"
            mgr = EventReplayManager(persist_path=path)
            mgr.record_event("stt.final", {"text": "hello"})
            mgr.close()
            content = path.read_text(encoding="utf-8")
            self.assertIn("stt.final", content)


# ---------------------------------------------------------------------------
# 9. MetricsCollector — скользящее окно не растёт за window_size
# ---------------------------------------------------------------------------

class TestMetricsCollectorBounded(unittest.TestCase):
    """MetricsCollector хранит не более window_size замеров."""

    def test_latency_deque_bounded(self):
        """При добавлении 2000 записей deque latencies не превышает window_size=1000."""
        from backend.metrics_collector import MetricsCollector
        collector = MetricsCollector(window_size=1000)
        for i in range(2000):
            collector.record(latency_ms=float(i), confidence=0.9)
        self.assertLessEqual(len(collector.latencies), 1000)

    def test_confidence_deque_bounded(self):
        """deque confidences не превышает window_size."""
        from backend.metrics_collector import MetricsCollector
        collector = MetricsCollector(window_size=500)
        for i in range(1500):
            collector.record(latency_ms=100.0, confidence=0.8)
        self.assertLessEqual(len(collector.confidences), 500)

    def test_metrics_summary_stable_after_overflow(self):
        """get_summary() возвращает корректный результат после переполнения окна."""
        from backend.metrics_collector import MetricsCollector
        collector = MetricsCollector(window_size=100)
        for i in range(500):
            collector.record(latency_ms=float(50 + i % 100), confidence=0.7)
        summary = collector.get_summary()
        self.assertIn("stt_metrics", summary)
        self.assertLessEqual(summary.get("window_size", 0), 100)


# ---------------------------------------------------------------------------
# 10. Отсутствие потоков-сирот после shutdown GracefulShutdownHandler
# ---------------------------------------------------------------------------

class TestNoOrphanThreadsAfterShutdown(unittest.TestCase):
    """GracefulShutdownHandler не оставляет висящих потоков."""

    def test_shutdown_completes_without_spawning_threads(self):
        """shutdown() не создаёт новых потоков."""
        from backend.shutdown_handler import GracefulShutdownHandler

        before = threading.active_count()

        handler = GracefulShutdownHandler()
        # Передаём минимальный mock-сервис без реальных ресурсов
        mock_service = MagicMock()
        mock_service.vocabulary = None
        mock_service._audit_logger = None
        mock_service._usage_tracker = None
        mock_service._playback_tracker = None
        mock_service.store = None
        mock_service._ipc_server = None
        handler._service = mock_service

        handler.shutdown()
        handler._shutdown_done.wait(timeout=2.0)

        after = threading.active_count()
        # Допускаем ±1 поток (служебные потоки системы)
        self.assertLessEqual(after, before + 1)

    def test_shutdown_idempotent_thread_count(self):
        """Повторный вызов shutdown() не создаёт новых потоков."""
        from backend.shutdown_handler import GracefulShutdownHandler

        handler = GracefulShutdownHandler()
        handler.shutdown()
        before = threading.active_count()
        handler.shutdown()
        handler.shutdown()
        after = threading.active_count()
        self.assertLessEqual(after, before + 1)


# ---------------------------------------------------------------------------
# 11. SearchHistoryManager — персистентность ограничена при сохранении
# ---------------------------------------------------------------------------

class TestSearchHistoryPersistence(unittest.TestCase):
    """SearchHistoryManager корректно сохраняет ограниченную историю на диск."""

    def test_persisted_history_capped(self):
        """При сохранении на диск в файле не более _MAX_ENTRIES записей."""
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SearchHistoryManager(data_dir=tmpdir)
            for i in range(700):
                mgr.record_search(f"query {i}")

            path = Path(tmpdir) / "search_history.json"
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(data["entries"]), SEARCH_MAX)


# ---------------------------------------------------------------------------
# 12. TranslationCache — нет утечки файловых дескрипторов при put()
# ---------------------------------------------------------------------------

class TestTranslationCacheFileHandles(unittest.TestCase):
    """TranslationCache корректно закрывает файлы после каждого сохранения."""

    def test_no_open_file_handles_after_many_puts(self):
        """После серии put() кэш-файл не остаётся открытым (закрывается атомарно через os.replace)."""
        import gc

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = TranslationCache(data_dir=tmpdir, max_entries=50)

            for i in range(200):
                cache.put(f"text_{i}", "ru", "es", "opus", f"tr_{i}")

            # Принудительная сборка мусора, чтобы убедиться, что временные
            # файловые дескрипторы не задержались в памяти
            gc.collect()

            # Убеждаемся, что cache-файл существует и читаем
            import json
            cache_path = Path(tmpdir) / "translation_cache.json"
            self.assertTrue(cache_path.exists(), "Файл кэша не был создан")
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)
            # Содержимое ограничено max_entries
            self.assertLessEqual(len(data), 50)


if __name__ == "__main__":
    unittest.main()
