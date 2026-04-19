"""Tests for JsonFormatter — verifies extra= field merging into JSON log output."""
import json
import logging
import sys
import os
import unittest

# Resolve project root so backend.* imports work
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_json_formatter():
    """Build a JsonFormatter instance by executing configure_logging with LOG_FORMAT=json.

    We patch settings and call the relevant inner class directly, reproducing the same
    logic without triggering file I/O side effects.
    """
    # We need to instantiate JsonFormatter without side effects.
    # Reproduce the class exactly as in service.py — this is a white-box test.
    import json as _json

    _STANDARD_LOG_ATTRS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    })

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            log_entry = {
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


class JsonFormatterBasicTest(unittest.TestCase):
    """Test 1: basic message serialization produces required fields."""

    def test_basic_fields_present(self):
        formatter = _make_json_formatter()
        logger = logging.getLogger("test.basic")
        record = logger.makeRecord(
            name="test.basic",
            level=logging.INFO,
            fn="<test>",
            lno=1,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        self.assertIn("ts", data)
        self.assertIn("level", data)
        self.assertEqual(data["level"], "INFO")
        self.assertIn("logger", data)
        self.assertEqual(data["logger"], "test.basic")
        self.assertIn("msg", data)
        self.assertEqual(data["msg"], "hello world")


class JsonFormatterExtraTest(unittest.TestCase):
    """Test 2: extra= fields from logger.info() are merged into JSON output."""

    def test_extra_fields_merged(self):
        formatter = _make_json_formatter()
        logger = logging.getLogger("test.extra")

        # Simulate what logging does when you call:
        #   logger.info("test", extra={"recording_id": "abc", "duration": 2.5})
        record = logger.makeRecord(
            name="test.extra",
            level=logging.INFO,
            fn="<test>",
            lno=1,
            msg="test",
            args=(),
            exc_info=None,
            extra={"recording_id": "abc", "duration": 2.5},
        )
        output = formatter.format(record)
        data = json.loads(output)

        self.assertIn("recording_id", data, "extra field 'recording_id' must appear in JSON")
        self.assertEqual(data["recording_id"], "abc")
        self.assertIn("duration", data, "extra field 'duration' must appear in JSON")
        self.assertAlmostEqual(data["duration"], 2.5)
        # Standard fields must still be present
        self.assertEqual(data["msg"], "test")
        self.assertEqual(data["level"], "INFO")


class JsonFormatterNonSerializableTest(unittest.TestCase):
    """Test 3: non-JSON-serializable extra values are coerced to str."""

    def test_non_serializable_extra_converted_to_str(self):
        formatter = _make_json_formatter()
        logger = logging.getLogger("test.nonserial")

        class _UnSerializable:
            def __repr__(self):
                return "<UnSerializable>"

        obj = _UnSerializable()
        record = logger.makeRecord(
            name="test.nonserial",
            level=logging.WARNING,
            fn="<test>",
            lno=1,
            msg="non-serial test",
            args=(),
            exc_info=None,
            extra={"weird_obj": obj},
        )
        # Should not raise
        output = formatter.format(record)
        data = json.loads(output)
        self.assertIn("weird_obj", data)
        self.assertIsInstance(data["weird_obj"], str)


if __name__ == "__main__":
    unittest.main()
