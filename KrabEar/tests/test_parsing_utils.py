"""Unit tests for core.parsing_utils."""
from __future__ import annotations

import logging
import sys
import os
import threading
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


class TestParsingUtilsSpecNames(unittest.TestCase):
    """Wave 115 — spec-named tests for explicit requirement coverage."""

    def test_valid_json_parses_normal(self):
        """Standard dict with mixed value types parses without error."""
        data = '{"name": "Краб", "version": 2, "active": true, "score": 3.14}'
        result = safe_json_loads(data)
        self.assertEqual(result["name"], "Краб")
        self.assertEqual(result["version"], 2)
        self.assertIs(result["active"], True)
        self.assertAlmostEqual(result["score"], 3.14)

    def test_invalid_json_returns_default(self):
        """Malformed JSON returns the supplied default without raising."""
        self.assertIsNone(safe_json_loads("{{invalid}}"))
        self.assertEqual(safe_json_loads("[unclosed", default=[]), [])
        self.assertEqual(safe_json_loads("not json at all", default=42), 42)

    def test_empty_string_returns_default(self):
        """Empty string is treated as 'no data' and returns default."""
        self.assertIsNone(safe_json_loads(""))
        self.assertEqual(safe_json_loads("", default="fallback"), "fallback")
        self.assertEqual(safe_json_loads(b"", default=0), 0)

    def test_unicode_json(self):
        """Unicode content (Cyrillic, CJK, emoji) round-trips correctly."""
        payload = '{"ru": "привет мир", "es": "hola mundo", "emoji": "\\ud83e\\udd80"}'
        result = safe_json_loads(payload)
        self.assertEqual(result["ru"], "привет мир")
        self.assertEqual(result["es"], "hola mundo")
        # Also test dumps preserves unicode
        dumped = safe_json_dumps({"кириллица": "тест", "数字": 123})
        self.assertIn("кириллица", dumped)
        self.assertIn("数字", dumped)

    def test_nested_json(self):
        """Deeply nested structures are parsed correctly."""
        data = '{"a": {"b": {"c": {"d": [1, 2, {"e": "deep"}]}}}}'
        result = safe_json_loads(data)
        self.assertEqual(result["a"]["b"]["c"]["d"][2]["e"], "deep")
        # Also test a nested list of objects
        data2 = '[{"id": 1, "tags": ["x", "y"]}, {"id": 2, "tags": []}]'
        result2 = safe_json_loads(data2)
        self.assertEqual(len(result2), 2)
        self.assertEqual(result2[0]["tags"], ["x", "y"])
        self.assertEqual(result2[1]["tags"], [])

    def test_logs_context_on_error(self):
        """Context string appears in the warning log on parse failure."""
        with self.assertLogs("core.parsing_utils", level=logging.WARNING) as cm:
            safe_json_loads("{broken: json}", context="ipc_method/get_history")
        all_output = "\n".join(cm.output)
        self.assertIn("ipc_method/get_history", all_output)
        self.assertIn("JSON parse failed", all_output)

    def test_concurrent_parse(self):
        """safe_json_loads must be thread-safe under concurrent load."""
        payloads = [
            ('{"idx": %d, "data": "value_%d"}' % (i, i), i)
            for i in range(50)
        ]
        results = {}
        errors = []

        def parse_one(json_str, idx):
            try:
                obj = safe_json_loads(json_str, default=None)
                results[idx] = obj
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=parse_one, args=(js, idx)) for js, idx in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, f"Thread errors: {errors}")
        self.assertEqual(len(results), 50)
        for idx, obj in results.items():
            self.assertIsNotNone(obj)
            self.assertEqual(obj["idx"], idx)


if __name__ == "__main__":
    unittest.main()
