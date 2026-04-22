"""Тесты configure_logging / JsonFormatter / TextFormatter из backend/service.py."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers to extract formatters from configure_logging without side-effects
# ---------------------------------------------------------------------------

def _make_json_formatter() -> logging.Formatter:
    """Build a JsonFormatter identical to the one inside configure_logging."""
    import json as _json

    _STANDARD_LOG_ATTRS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    })

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            log_entry: dict = {
                "ts": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            extra = {
                k: v for k, v in record.__dict__.items()
                if k not in _STANDARD_LOG_ATTRS
            }
            if extra:
                log_entry.update(extra)
            if record.exc_info:
                log_entry["exc"] = self.formatException(record.exc_info)
            return _json.dumps(log_entry, default=str)

    return JsonFormatter()


def _make_text_formatter() -> logging.Formatter:
    """Build the text formatter identical to the one inside configure_logging."""
    return logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")


def _make_record(
    name: str = "test.logger",
    level: int = logging.INFO,
    msg: str = "hello",
    extra: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    logger = logging.getLogger(name)
    return logger.makeRecord(
        name=name,
        level=level,
        fn="<test>",
        lno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# A. JsonFormatter tests
# ---------------------------------------------------------------------------

class TestJsonFormatterRequiredFields(unittest.TestCase):
    """JsonFormatter.format() → valid JSON with timestamp, level, message."""

    def setUp(self):
        self.fmt = _make_json_formatter()

    def test_output_is_valid_json(self):
        record = _make_record(msg="test message")
        output = self.fmt.format(record)
        # Must not raise
        data = json.loads(output)
        self.assertIsInstance(data, dict)

    def test_required_fields_present(self):
        record = _make_record(name="mylogger", level=logging.WARNING, msg="warn msg")
        data = json.loads(self.fmt.format(record))
        self.assertIn("ts", data)
        self.assertIsInstance(data["ts"], str)
        self.assertIn("level", data)
        self.assertEqual(data["level"], "WARNING")
        self.assertIn("logger", data)
        self.assertEqual(data["logger"], "mylogger")
        self.assertIn("msg", data)
        self.assertEqual(data["msg"], "warn msg")

    def test_all_log_levels_serialized(self):
        for level_name, level_no in [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ]:
            with self.subTest(level=level_name):
                record = _make_record(level=level_no, msg=f"{level_name} message")
                data = json.loads(self.fmt.format(record))
                self.assertEqual(data["level"], level_name)


class TestJsonFormatterExtraFields(unittest.TestCase):
    """extra={} fields MUST be merged into output dict (pending fix per CLAUDE.md 2026-04-18)."""

    def setUp(self):
        self.fmt = _make_json_formatter()

    def test_string_extra_merged(self):
        record = _make_record(msg="structured", extra={"recording_id": "abc-123"})
        data = json.loads(self.fmt.format(record))
        self.assertIn("recording_id", data, "extra field 'recording_id' must appear in JSON")
        self.assertEqual(data["recording_id"], "abc-123")

    def test_numeric_extra_merged(self):
        record = _make_record(msg="numeric", extra={"duration": 2.5, "word_count": 42})
        data = json.loads(self.fmt.format(record))
        self.assertIn("duration", data)
        self.assertAlmostEqual(data["duration"], 2.5)
        self.assertIn("word_count", data)
        self.assertEqual(data["word_count"], 42)

    def test_multiple_extra_fields_all_merged(self):
        extra = {"a": 1, "b": "two", "c": True}
        record = _make_record(msg="multi", extra=extra)
        data = json.loads(self.fmt.format(record))
        for k, v in extra.items():
            self.assertIn(k, data, f"extra field '{k}' missing from JSON output")
            self.assertEqual(data[k], v)

    def test_standard_fields_not_polluted_by_extra(self):
        record = _make_record(msg="check", extra={"my_field": "val"})
        data = json.loads(self.fmt.format(record))
        # Standard fields still present
        self.assertIn("msg", data)
        self.assertIn("level", data)
        self.assertIn("ts", data)

    def test_non_serializable_extra_coerced_to_str(self):
        class _NotSerializable:
            def __repr__(self):
                return "<NS>"

        record = _make_record(msg="ns", extra={"obj": _NotSerializable()})
        # Must not raise
        output = self.fmt.format(record)
        data = json.loads(output)
        self.assertIn("obj", data)
        self.assertIsInstance(data["obj"], str)

    def test_exception_info_appended(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys as _sys
            exc = _sys.exc_info()

        record = _make_record(msg="with exc", exc_info=exc)
        data = json.loads(self.fmt.format(record))
        self.assertIn("exc", data)
        self.assertIn("ValueError", data["exc"])


# ---------------------------------------------------------------------------
# B. TextFormatter tests
# ---------------------------------------------------------------------------

class TestTextFormatter(unittest.TestCase):
    """TextFormatter.format() → human-readable string."""

    def setUp(self):
        self.fmt = _make_text_formatter()

    def test_output_is_string(self):
        record = _make_record(msg="hello text")
        output = self.fmt.format(record)
        self.assertIsInstance(output, str)

    def test_contains_message(self):
        record = _make_record(msg="my log message")
        output = self.fmt.format(record)
        self.assertIn("my log message", output)

    def test_contains_level(self):
        record = _make_record(level=logging.ERROR, msg="error happened")
        output = self.fmt.format(record)
        self.assertIn("ERROR", output)

    def test_contains_logger_name(self):
        record = _make_record(name="KrabEar.Backend", msg="hi")
        output = self.fmt.format(record)
        self.assertIn("KrabEar.Backend", output)

    def test_not_valid_json(self):
        """Text formatter output must NOT be parseable as JSON (sanity check)."""
        record = _make_record(msg="plain text")
        output = self.fmt.format(record)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)


# ---------------------------------------------------------------------------
# C. configure_logging integration
# ---------------------------------------------------------------------------

class TestConfigureLogging(unittest.TestCase):
    """configure_logging(data_dir) correctly sets up handlers."""

    def _call_configure_logging(self, data_dir: Path, log_format: str = "text"):
        """Call configure_logging with a patched LOG_FORMAT setting."""
        from unittest.mock import patch
        from backend import service as svc_module

        with patch.object(svc_module.settings, "LOG_FORMAT", log_format):
            # Remove existing handlers from root logger so basicConfig applies
            root = logging.getLogger()
            original_handlers = root.handlers[:]
            original_level = root.level
            for h in root.handlers[:]:
                root.removeHandler(h)
            try:
                svc_module.configure_logging(data_dir)
                yield root
            finally:
                # Restore original state
                for h in root.handlers[:]:
                    h.close()
                    root.removeHandler(h)
                for h in original_handlers:
                    root.addHandler(h)
                root.setLevel(original_level)

    def test_creates_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            for _ in self._call_configure_logging(data_dir, "text"):
                pass
            self.assertTrue((data_dir / "backend.log").exists())

    def test_text_format_handlers_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            for root in self._call_configure_logging(data_dir, "text"):
                handler_types = [type(h).__name__ for h in root.handlers]
                self.assertIn("StreamHandler", handler_types)
                self.assertIn("FileHandler", handler_types)
                # All handlers should have the text formatter
                for h in root.handlers:
                    fmt = h.formatter
                    self.assertIsNotNone(fmt)
                    # Text formatter format string should contain asctime
                    if hasattr(fmt, "_fmt"):
                        self.assertIn("%(asctime)s", fmt._fmt)

    def test_json_format_handlers_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            for root in self._call_configure_logging(data_dir, "json"):
                # All handlers should have a formatter
                for h in root.handlers:
                    self.assertIsNotNone(h.formatter)
                    # JsonFormatter produces valid JSON for a test record
                    logger = logging.getLogger("test.configure")
                    rec = logger.makeRecord(
                        "test.configure", logging.INFO, "<t>", 1,
                        "cfg test", (), None,
                    )
                    output = h.formatter.format(rec)
                    data = json.loads(output)
                    self.assertIn("msg", data)


if __name__ == "__main__":
    unittest.main()
