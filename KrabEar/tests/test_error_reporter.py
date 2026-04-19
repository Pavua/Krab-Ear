"""Тесты ErrorReporter — сервиса сбора ошибок бэкенда Krab Ear."""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.error_reporter import ErrorReporter, ErrorRecord, VALID_CATEGORIES  # noqa: E402


class ErrorReporterBasicTestCase(unittest.TestCase):
    """Базовые тесты report_error и get_recent_errors."""

    def setUp(self) -> None:
        self.reporter = ErrorReporter()

    # 1 — report_error возвращает ErrorRecord с корректными полями
    def test_report_returns_error_record(self) -> None:
        rec = self.reporter.report_error("stt", "TimeoutError", "STT timed out")
        self.assertIsInstance(rec, ErrorRecord)
        self.assertEqual(rec.component, "stt")
        self.assertEqual(rec.error_type, "TimeoutError")
        self.assertEqual(rec.message, "STT timed out")
        self.assertFalse(rec.resolved)
        self.assertIsInstance(rec.context, dict)

    # 2 — неизвестная категория нормализуется в "other"
    def test_unknown_component_normalized_to_other(self) -> None:
        rec = self.reporter.report_error("GPU", "RuntimeError", "boom")
        self.assertEqual(rec.component, "other")

    # 3 — все VALID_CATEGORIES принимаются без изменения
    def test_all_valid_categories_accepted(self) -> None:
        for cat in VALID_CATEGORIES:
            rec = self.reporter.report_error(cat, "E", "msg")
            if cat != "other":
                self.assertEqual(rec.component, cat, f"category {cat!r} was not preserved")

    # 4 — get_recent_errors возвращает последние limit записей (новейшие первыми)
    def test_get_recent_errors_order_and_limit(self) -> None:
        for i in range(5):
            self.reporter.report_error("ipc", "E", f"msg {i}")
        errors = self.reporter.get_recent_errors(limit=3)
        self.assertEqual(len(errors), 3)
        # Последнее добавленное — первое в списке
        self.assertEqual(errors[0].message, "msg 4")

    # 5 — context сохраняется корректно
    def test_context_stored(self) -> None:
        ctx = {"file": "audio.wav", "line": 42}
        rec = self.reporter.report_error("audio", "IOError", "file not found", context=ctx)
        self.assertEqual(rec.context["file"], "audio.wav")
        self.assertEqual(rec.context["line"], 42)

    # 6 — context=None превращается в пустой dict
    def test_context_none_becomes_empty_dict(self) -> None:
        rec = self.reporter.report_error("storage", "OSError", "disk full", context=None)
        self.assertEqual(rec.context, {})


class ErrorReporterRingBufferTestCase(unittest.TestCase):
    """Тесты кольцевого буфера (max_size)."""

    # 7 — буфер не превышает max_size
    def test_ring_buffer_max_size(self) -> None:
        reporter = ErrorReporter(max_size=10)
        for i in range(25):
            reporter.report_error("llm", "E", f"msg {i}")
        errors = reporter.get_recent_errors(limit=100)
        self.assertEqual(len(errors), 10)
        # Первый — самый новый (msg 24)
        self.assertEqual(errors[0].message, "msg 24")

    # 8 — clear очищает буфер
    def test_clear_empties_buffer(self) -> None:
        reporter = ErrorReporter()
        reporter.report_error("stt", "E", "some error")
        reporter.clear()
        self.assertEqual(reporter.get_recent_errors(), [])


class ErrorReporterResolveTestCase(unittest.TestCase):
    """Тесты resolve_error."""

    def setUp(self) -> None:
        self.reporter = ErrorReporter()

    # 9 — resolve_error помечает ошибку как resolved
    def test_resolve_error_marks_resolved(self) -> None:
        self.reporter.report_error("stt", "TimeoutError", "msg1")
        self.reporter.report_error("stt", "TimeoutError", "msg2")
        errors = self.reporter.get_recent_errors(limit=2)
        # Последняя добавленная — первая в списке (msg2)
        self.assertEqual(errors[0].message, "msg2")
        self.assertFalse(errors[0].resolved)
        # resolve_error работает с индексом в исходном буфере (от старого к новому)
        # индекс 1 — это вторая добавленная ошибка (msg2)
        success = self.reporter.resolve_error(1)
        self.assertTrue(success)
        errors_after = self.reporter.get_recent_errors(limit=2)
        self.assertTrue(errors_after[0].resolved)

    # 10 — resolve_error возвращает False для неверного индекса
    def test_resolve_error_invalid_index(self) -> None:
        self.reporter.report_error("stt", "E", "msg")
        success = self.reporter.resolve_error(999)
        self.assertFalse(success)

    # 11 — resolve_error на пустом буфере возвращает False
    def test_resolve_error_empty_buffer(self) -> None:
        success = self.reporter.resolve_error(0)
        self.assertFalse(success)

    # 12 — ErrorRecord.to_dict() работает корректно
    def test_error_record_to_dict(self) -> None:
        rec = self.reporter.report_error("audio", "OSError", "no device", context={"device_id": 1})
        d = rec.to_dict()
        self.assertEqual(d["component"], "audio")
        self.assertEqual(d["error_type"], "OSError")
        self.assertEqual(d["message"], "no device")
        self.assertEqual(d["context"]["device_id"], 1)
        self.assertFalse(d["resolved"])


class ErrorReporterStatsTestCase(unittest.TestCase):
    """Тесты get_error_stats."""

    def setUp(self) -> None:
        self.reporter = ErrorReporter()

    # 13 — get_error_stats содержит нужные ключи при пустом буфере
    def test_stats_empty(self) -> None:
        stats = self.reporter.get_error_stats()
        self.assertEqual(stats["total"], 0)
        self.assertIn("by_component", stats)
        self.assertIn("by_type", stats)
        self.assertIn("by_time_window", stats)

    # 14 — счётчики by_component корректны
    def test_stats_by_component(self) -> None:
        self.reporter.report_error("stt", "TimeoutError", "t1")
        self.reporter.report_error("stt", "ValueError", "t2")
        self.reporter.report_error("llm", "RuntimeError", "t3")
        stats = self.reporter.get_error_stats()
        self.assertEqual(stats["by_component"]["stt"], 2)
        self.assertEqual(stats["by_component"]["llm"], 1)
        self.assertEqual(stats["total"], 3)

    # 15 — счётчики by_type корректны
    def test_stats_by_type(self) -> None:
        self.reporter.report_error("ipc", "ConnectionError", "c1")
        self.reporter.report_error("ipc", "ConnectionError", "c2")
        self.reporter.report_error("audio", "OSError", "a1")
        stats = self.reporter.get_error_stats()
        self.assertEqual(stats["by_type"]["ConnectionError"], 2)
        self.assertEqual(stats["by_type"]["OSError"], 1)

    # 16 — свежие ошибки попадают в last_5m
    def test_stats_time_window_last_5m(self) -> None:
        self.reporter.report_error("translation", "NetworkError", "net fail")
        stats = self.reporter.get_error_stats()
        self.assertGreaterEqual(stats["by_time_window"]["last_5m"], 1)
        self.assertGreaterEqual(stats["by_time_window"]["last_1h"], 1)
        self.assertGreaterEqual(stats["by_time_window"]["last_24h"], 1)


class ErrorReporterIPCTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков handle_get_error_report и handle_get_error_stats."""

    def setUp(self) -> None:
        self.reporter = ErrorReporter()
        for i in range(5):
            self.reporter.report_error("storage", "IOError", f"disk error {i}")

    # 17 — handle_get_error_report возвращает правильный формат
    def test_handle_get_error_report_format(self) -> None:
        result = self.reporter.handle_get_error_report({"limit": 3})
        self.assertIn("errors", result)
        self.assertIn("total_in_buffer", result)
        self.assertEqual(len(result["errors"]), 3)
        rec = result["errors"][0]
        for key in ("timestamp", "component", "error_type", "message", "context", "resolved"):
            self.assertIn(key, rec)

    # 18 — handle_get_error_stats возвращает корректную статистику
    def test_handle_get_error_stats(self) -> None:
        result = self.reporter.handle_get_error_stats({})
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["by_component"]["storage"], 5)
        self.assertIn("last_5m", result["by_time_window"])

    # 19 — limit в handle_get_error_report работает
    def test_handle_get_error_report_limit(self) -> None:
        result = self.reporter.handle_get_error_report({"limit": 2})
        self.assertEqual(len(result["errors"]), 2)

    # 20 — handle_get_error_report без параметров использует limit=50
    def test_handle_get_error_report_default_limit(self) -> None:
        result = self.reporter.handle_get_error_report({})
        # 5 ошибок < 50, должны вернуть все
        self.assertEqual(len(result["errors"]), 5)


class ErrorReporterThreadSafetyTestCase(unittest.TestCase):
    """Тест потокобезопасности."""

    # 21 — одновременная запись из нескольких потоков не приводит к потере/дублированию
    def test_concurrent_report_does_not_crash(self) -> None:
        reporter = ErrorReporter(max_size=200)
        errors = []

        def worker(n: int) -> None:
            for i in range(10):
                rec = reporter.report_error("ipc", "E", f"t{n}-{i}")
                errors.append(rec)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Все 100 report_error вернули ErrorRecord (пусть часть вытеснена из буфера)
        self.assertEqual(len(errors), 100)
        # Буфер не переполнен
        self.assertLessEqual(len(reporter.get_recent_errors(limit=200)), 200)


if __name__ == "__main__":
    unittest.main()
