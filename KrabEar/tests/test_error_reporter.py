"""Тесты ErrorReporter — сервиса сбора ошибок бэкенда Krab Ear."""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.error_reporter import (  # noqa: E402
    ErrorReporter,
    ErrorRecord,
    VALID_CATEGORIES,
    _MAX_MESSAGE_LEN,
    _MAX_CONTEXT_BYTES,
)


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


class ErrorReporterRingBufferEvictionTestCase(unittest.TestCase):
    """Кольцевой буфер: при переполнении самые старые записи вытесняются."""

    def test_oldest_records_evicted_when_full(self) -> None:
        reporter = ErrorReporter(max_size=3)
        reporter.report_error("stt", "E", "oldest")
        reporter.report_error("stt", "E", "middle")
        reporter.report_error("stt", "E", "newest")
        # Буфер полон — добавляем ещё одну запись
        reporter.report_error("stt", "E", "overflow")
        errors = reporter.get_recent_errors(limit=10)
        messages = [e.message for e in errors]
        # "oldest" должна быть вытеснена
        self.assertNotIn("oldest", messages)
        # остальные три присутствуют
        self.assertIn("overflow", messages)
        self.assertIn("newest", messages)
        self.assertIn("middle", messages)

    def test_buffer_size_never_exceeds_max_size(self) -> None:
        reporter = ErrorReporter(max_size=5)
        for i in range(20):
            reporter.report_error("llm", "E", f"msg {i}")
        stats = reporter.get_error_stats()
        self.assertEqual(stats["total"], 5)


class ErrorReporterSummaryOrderTestCase(unittest.TestCase):
    """get_error_stats: по_type / по_component содержат корректные счётчики."""

    def test_by_type_counts_most_frequent(self) -> None:
        reporter = ErrorReporter()
        for _ in range(5):
            reporter.report_error("stt", "TimeoutError", "t")
        for _ in range(2):
            reporter.report_error("llm", "ValueError", "v")
        reporter.report_error("ipc", "IOError", "i")
        stats = reporter.get_error_stats()
        # TimeoutError должен быть самым частым
        by_type = stats["by_type"]
        self.assertEqual(by_type["TimeoutError"], 5)
        self.assertEqual(by_type["ValueError"], 2)
        self.assertEqual(by_type["IOError"], 1)
        most_frequent = max(by_type, key=lambda k: by_type[k])
        self.assertEqual(most_frequent, "TimeoutError")

    def test_by_component_aggregates_across_types(self) -> None:
        reporter = ErrorReporter()
        reporter.report_error("stt", "TypeA", "a")
        reporter.report_error("stt", "TypeB", "b")
        reporter.report_error("stt", "TypeC", "c")
        reporter.report_error("llm", "TypeA", "d")
        stats = reporter.get_error_stats()
        self.assertEqual(stats["by_component"]["stt"], 3)
        self.assertEqual(stats["by_component"]["llm"], 1)


class ErrorReporterWave97TestCase(unittest.TestCase):
    """Wave 97 required tests — names match task spec exactly."""

    # test_record_single_error_per_component
    def test_record_single_error_per_component(self) -> None:
        reporter = ErrorReporter()
        for cat in ("stt", "llm", "translation", "ipc", "audio", "storage", "other"):
            reporter.report_error(cat, "SomeError", f"msg from {cat}")
        stats = reporter.get_error_stats()
        # Each known component has exactly one error
        for cat in ("stt", "llm", "translation", "ipc", "audio", "storage", "other"):
            self.assertEqual(stats["by_component"].get(cat, 0), 1, f"component {cat!r} count wrong")
        self.assertEqual(stats["total"], 7)

    # test_buffer_caps_at_max_size
    def test_buffer_caps_at_max_size(self) -> None:
        reporter = ErrorReporter(max_size=5)
        for i in range(12):
            reporter.report_error("stt", "E", f"msg {i}")
        recent = reporter.get_recent_errors(limit=100)
        self.assertEqual(len(recent), 5)
        # Oldest messages (msg 0..6) must be evicted; newest (msg 7..11) must remain
        messages = {e.message for e in recent}
        for evicted in (f"msg {i}" for i in range(7)):
            self.assertNotIn(evicted, messages)

    # test_count_aggregates_correctly_by_type
    def test_count_aggregates_correctly_by_type(self) -> None:
        reporter = ErrorReporter()
        reporter.report_error("stt", "TimeoutError", "t1")
        reporter.report_error("llm", "TimeoutError", "t2")
        reporter.report_error("ipc", "TimeoutError", "t3")
        reporter.report_error("audio", "ConnectionError", "c1")
        reporter.report_error("audio", "ConnectionError", "c2")
        stats = reporter.get_error_stats()
        self.assertEqual(stats["by_type"]["TimeoutError"], 3)
        self.assertEqual(stats["by_type"]["ConnectionError"], 2)

    # test_query_top_errors_by_count
    def test_query_top_errors_by_count(self) -> None:
        reporter = ErrorReporter()
        for _ in range(7):
            reporter.report_error("stt", "TimeoutError", "t")
        for _ in range(3):
            reporter.report_error("llm", "ValueError", "v")
        for _ in range(1):
            reporter.report_error("ipc", "IOError", "i")
        stats = reporter.get_error_stats()
        by_type = stats["by_type"]
        # Find top error type
        top_type = max(by_type, key=lambda k: by_type[k])
        self.assertEqual(top_type, "TimeoutError")
        self.assertEqual(by_type[top_type], 7)
        # by_component ranking
        by_comp = stats["by_component"]
        top_comp = max(by_comp, key=lambda k: by_comp[k])
        self.assertEqual(top_comp, "stt")

    # test_clear_buffer
    def test_clear_buffer(self) -> None:
        reporter = ErrorReporter()
        for i in range(10):
            reporter.report_error("stt", "E", f"msg {i}")
        self.assertEqual(reporter.get_error_stats()["total"], 10)
        reporter.clear()
        self.assertEqual(reporter.get_error_stats()["total"], 0)
        self.assertEqual(reporter.get_recent_errors(), [])
        # Can still add after clear
        reporter.report_error("llm", "E", "after clear")
        self.assertEqual(reporter.get_error_stats()["total"], 1)

    # test_concurrent_record
    def test_concurrent_record(self) -> None:
        reporter = ErrorReporter(max_size=1000)
        records = []
        lock = threading.Lock()

        def worker(component: str) -> None:
            for i in range(20):
                rec = reporter.report_error(component, "E", f"{component}-{i}")
                with lock:
                    records.append(rec)

        threads = [threading.Thread(target=worker, args=(c,)) for c in ("stt", "llm", "ipc", "audio", "storage")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 5 threads × 20 = 100 records returned
        self.assertEqual(len(records), 100)
        # All returned objects are ErrorRecord instances
        self.assertTrue(all(isinstance(r, ErrorRecord) for r in records))
        # Buffer contains at most 100 items (within max_size=1000)
        stats = reporter.get_error_stats()
        self.assertEqual(stats["total"], 100)

    # test_handles_unicode_error_messages
    def test_handles_unicode_error_messages(self) -> None:
        reporter = ErrorReporter()
        unicode_msg = "Ошибка STT: невозможно декодировать аудио — неверный формат"
        emoji_msg = "Error 🎤🔴: microphone disconnected"
        rtl_msg = "خطأ في التعرف على الكلام"
        reporter.report_error("stt", "UnicodeError", unicode_msg)
        reporter.report_error("audio", "DeviceError", emoji_msg)
        reporter.report_error("other", "LanguageError", rtl_msg)
        recent = reporter.get_recent_errors(limit=3)
        messages = [e.message for e in recent]
        self.assertIn(unicode_msg, messages)
        self.assertIn(emoji_msg, messages)
        self.assertIn(rtl_msg, messages)
        # to_dict() round-trips correctly
        d = recent[0].to_dict()
        self.assertIsInstance(d["message"], str)

    # test_record_with_traceback_truncation — updated for W977 F5 truncation fix
    def test_record_with_traceback_truncation(self) -> None:
        """W977 F5: long tracebacks in message are truncated to _MAX_MESSAGE_LEN chars."""
        reporter = ErrorReporter()
        # Simulate a traceback stored in message field (common pattern)
        long_traceback = "Traceback (most recent call last):\n" + ("  File 'x.py', line 1, in <module>\n" * 100) + "RuntimeError: boom"
        self.assertGreater(len(long_traceback), _MAX_MESSAGE_LEN, "pre-condition: traceback must exceed cap")
        rec = reporter.report_error("stt", "RuntimeError", long_traceback, context={"traceback_len": len(long_traceback)})
        self.assertIsInstance(rec.message, str)
        # W977 F5: message must be truncated
        self.assertLessEqual(len(rec.message), _MAX_MESSAGE_LEN + len("... [truncated]"))
        self.assertTrue(rec.message.endswith("... [truncated]"), f"expected truncation suffix, got: {rec.message[-30]!r}")
        # Buffer still works normally after long message
        stats = reporter.get_error_stats()
        self.assertEqual(stats["total"], 1)


class ErrorReporterW987PIITestCase(unittest.TestCase):
    """W977 F2+F3+F5 hardening tests (Wave 987)."""

    # test_report_error_truncates_long_message (F2 + F5)
    def test_report_error_truncates_long_message(self) -> None:
        """Message longer than _MAX_MESSAGE_LEN is truncated with suffix."""
        reporter = ErrorReporter()
        long_msg = "x" * (_MAX_MESSAGE_LEN + 500)
        rec = reporter.report_error("stt", "E", long_msg)
        expected_len = _MAX_MESSAGE_LEN + len("... [truncated]")
        self.assertEqual(len(rec.message), expected_len)
        self.assertTrue(rec.message.endswith("... [truncated]"))
        self.assertEqual(rec.message[:_MAX_MESSAGE_LEN], "x" * _MAX_MESSAGE_LEN)

    # test_report_error_truncates_large_context (F2)
    def test_report_error_truncates_large_context(self) -> None:
        """Context exceeding _MAX_CONTEXT_BYTES JSON is replaced with a tombstone dict."""
        reporter = ErrorReporter()
        # Build a context whose JSON > _MAX_CONTEXT_BYTES
        big_context = {"data": "y" * (_MAX_CONTEXT_BYTES + 1000)}
        serialized_size = len(__import__("json").dumps(big_context, ensure_ascii=False))
        self.assertGreater(serialized_size, _MAX_CONTEXT_BYTES, "pre-condition")
        rec = reporter.report_error("llm", "E", "msg", context=big_context)
        # Tombstone must be returned, not the original data
        self.assertIn("truncated", rec.context)
        self.assertTrue(rec.context["truncated"])
        self.assertIn("original_size_bytes", rec.context)
        self.assertEqual(rec.context["original_size_bytes"], serialized_size)
        self.assertNotIn("data", rec.context)

    # test_report_error_redacts_in_privacy_mode (F2)
    def test_report_error_redacts_in_privacy_mode(self) -> None:
        """privacy_mode_enabled=True: message becomes <redacted> and context is cleared."""
        privacy_settings = {"privacy_mode_enabled": True}
        reporter = ErrorReporter(settings_provider=lambda: privacy_settings)
        rec = reporter.report_error(
            "ipc",
            "ValueError",
            "user said: my name is Pavel",
            context={"transcript": "my name is Pavel", "device": "mic"},
        )
        self.assertEqual(rec.message, "<redacted: privacy_mode>")
        self.assertEqual(rec.context, {})
        # component + error_type still present (non-PII)
        self.assertEqual(rec.component, "ipc")
        self.assertEqual(rec.error_type, "ValueError")

    # test_get_error_report_total_in_buffer_under_lock (F3)
    def test_get_error_report_total_in_buffer_under_lock(self) -> None:
        """handle_get_error_report: total_in_buffer is consistent with returned errors list."""
        reporter = ErrorReporter(max_size=20)
        for i in range(10):
            reporter.report_error("audio", "IOError", f"err {i}")

        result = reporter.handle_get_error_report({"limit": 5})
        errors = result["errors"]
        total = result["total_in_buffer"]

        # total_in_buffer reflects the actual buffer snapshot (10 errors)
        self.assertEqual(total, 10)
        # limit is applied to the returned list
        self.assertEqual(len(errors), 5)
        # total_in_buffer >= len(errors) always (it is the full buffer, not the slice)
        self.assertGreaterEqual(total, len(errors))

        # Verify atomicity: total_in_buffer must equal the true buffer length
        # (no TOCTOU gap — both read in the same lock)
        with reporter._lock:
            real_total = len(reporter._buffer)
        self.assertEqual(total, real_total)


if __name__ == "__main__":
    unittest.main()
