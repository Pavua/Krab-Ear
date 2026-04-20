"""Unit tests for core.parsing_utils."""
from __future__ import annotations

import logging
import sys
import os
import unittest

# Ensure KrabEar package is importable when run standalone.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

from core.parsing_utils import safe_json_dumps, safe_json_loads  # noqa: E402


class TestSafeJsonLoads(unittest.TestCase):
    """Tests for safe_json_loads()."""

    # ── happy path ──────────────────────────────────────────────────────────

    def test_parse_dict(self):
        result = safe_json_loads('{"key": "value", "n": 42}')
        self.assertEqual(result, {"key": "value", "n": 42})

    def test_parse_list(self):
        result = safe_json_loads("[1, 2, 3]")
        self.assertEqual(result, [1, 2, 3])

    def test_parse_bytes(self):
        result = safe_json_loads(b'{"x": true}')
        self.assertEqual(result, {"x": True})

    def test_parse_string_value(self):
        result = safe_json_loads('"hello"')
        self.assertEqual(result, "hello")

    # ── fallback cases ──────────────────────────────────────────────────────

    def test_invalid_json_returns_default_none(self):
        result = safe_json_loads("not-json")
        self.assertIsNone(result)

    def test_invalid_json_returns_custom_default(self):
        result = safe_json_loads("{bad}", default={})
        self.assertEqual(result, {})

    def test_empty_string_returns_default(self):
        result = safe_json_loads("", default="FALLBACK")
        self.assertEqual(result, "FALLBACK")

    def test_empty_bytes_returns_default(self):
        result = safe_json_loads(b"", default=0)
        self.assertEqual(result, 0)

    def test_none_data_returns_default(self):
        # None is falsy — should not raise, just return default.
        result = safe_json_loads(None, default=99)  # type: ignore[arg-type]
        self.assertEqual(result, 99)

    # ── logging ─────────────────────────────────────────────────────────────

    def test_invalid_json_emits_warning(self):
        with self.assertLogs("core.parsing_utils", level=logging.WARNING) as cm:
            safe_json_loads("bad", context="test_endpoint")
        self.assertTrue(
            any("test_endpoint" in line for line in cm.output),
            msg="Warning should include the context label",
        )

    def test_invalid_json_no_context_warning_still_emitted(self):
        with self.assertLogs("core.parsing_utils", level=logging.WARNING) as cm:
            safe_json_loads("{broken}")
        self.assertTrue(any("JSON parse failed" in line for line in cm.output))


class TestSafeJsonDumps(unittest.TestCase):
    """Tests for safe_json_dumps()."""

    def test_dump_dict(self):
        result = safe_json_dumps({"a": 1})
        self.assertEqual(result, '{"a": 1}')

    def test_dump_list(self):
        result = safe_json_dumps([1, 2, 3])
        self.assertEqual(result, "[1, 2, 3]")

    def test_dump_unicode_no_escape(self):
        result = safe_json_dumps({"ru": "привет"})
        self.assertIn("привет", result)

    def test_non_serializable_returns_default(self):
        result = safe_json_dumps(object(), default="{}")
        self.assertEqual(result, "{}")

    def test_non_serializable_custom_default(self):
        result = safe_json_dumps(object(), default="null")
        self.assertEqual(result, "null")

    def test_non_serializable_emits_warning(self):
        with self.assertLogs("core.parsing_utils", level=logging.WARNING) as cm:
            safe_json_dumps(object())
        self.assertTrue(any("JSON serialize failed" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
