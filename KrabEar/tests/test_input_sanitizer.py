"""Тесты для InputSanitizer — санитизация IPC-параметров Krab Ear."""

import sys
import os
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRAB_EAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRAB_EAR_ROOT))

from backend.input_sanitizer import InputSanitizer


class TestSanitizeString(unittest.TestCase):
    def setUp(self):
        self.san = InputSanitizer()

    def test_strips_whitespace(self):
        result = InputSanitizer.sanitize_string("  hello  ")
        self.assertEqual(result, "hello")

    def test_removes_control_characters(self):
        result = InputSanitizer.sanitize_string("hello\x00\x01world\x07")
        self.assertEqual(result, "helloworld")

    def test_preserves_tab_newline_cr(self):
        result = InputSanitizer.sanitize_string("line1\nline2\ttab\r")
        self.assertIn("\n", result)
        self.assertIn("\t", result)

    def test_truncates_oversized_string(self):
        big = "A" * 20_000
        result = InputSanitizer.sanitize_string(big, max_length=10_000)
        self.assertEqual(len(result), 10_000)

    def test_xss_attempt_preserved_but_cleaned(self):
        xss = "<script>alert('xss')</script>"
        result = InputSanitizer.sanitize_string(xss)
        # No control chars in XSS — string passes through (HTML escaping is a
        # higher-level concern), but we verify no crash and stripping works.
        self.assertIn("<script>", result)

    def test_non_string_coerced(self):
        result = InputSanitizer.sanitize_string(42)  # type: ignore[arg-type]
        self.assertEqual(result, "42")


class TestSanitizePath(unittest.TestCase):
    def setUp(self):
        self.san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])

    def test_valid_path_under_home(self):
        p = str(Path.home() / "Downloads" / "file.txt")
        result = self.san.sanitize_path(p)
        self.assertTrue(result.startswith(str(Path.home())))

    def test_valid_path_under_tmp(self):
        result = self.san.sanitize_path("/tmp/krabear_test.wav")
        self.assertTrue(result.startswith("/tmp") or result.startswith("/private/tmp"))

    def test_path_traversal_blocked(self):
        bad = "/tmp/../../../etc/passwd"
        with self.assertRaises(ValueError):
            self.san.sanitize_path(bad)

    def test_absolute_outside_allowed_blocked(self):
        with self.assertRaises(ValueError):
            self.san.sanitize_path("/etc/shadow")

    def test_empty_path_raises(self):
        with self.assertRaises(ValueError):
            self.san.sanitize_path("")

    def test_tilde_expansion_valid(self):
        result = self.san.sanitize_path("~/Documents/notes.txt")
        self.assertTrue(result.startswith(str(Path.home())))

    def test_tilde_expansion_traversal_blocked(self):
        # A path under an unexpected root
        with self.assertRaises(ValueError):
            self.san.sanitize_path("/var/db/secret")


class TestSanitizeParams(unittest.TestCase):
    def setUp(self):
        self.san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])

    def test_string_field_cleaned(self):
        params = {"text": "hello\x01world   "}
        result = self.san.sanitize_params("translate_text", params)
        self.assertEqual(result["text"], "helloworld")

    def test_path_field_traversal_raises(self):
        params = {"path": "/tmp/../../../etc/passwd"}
        with self.assertRaises(ValueError):
            self.san.sanitize_params("transcribe_paths", params)

    def test_numeric_field_clamped(self):
        params = {"page": -5}
        result = self.san.sanitize_params("get_history_page", params)
        self.assertEqual(result["page"], 0)

    def test_numeric_field_max_clamped(self):
        params = {"page_size": 999_999}
        result = self.san.sanitize_params("get_history_page", params)
        self.assertEqual(result["page_size"], 1000)

    def test_numeric_field_coerced_from_string(self):
        params = {"page": "3"}
        result = self.san.sanitize_params("get_history_page", params)
        self.assertEqual(result["page"], 3)

    def test_list_field_truncated(self):
        params = {"items": list(range(2000))}
        result = self.san.sanitize_params("some_method", params)
        self.assertEqual(len(result["items"]), 1000)

    def test_nested_dict_sanitized(self):
        params = {"settings": {"key": "val\x00ue"}}
        result = self.san.sanitize_params("set_settings", params)
        self.assertEqual(result["settings"]["key"], "value")

    def test_none_values_passed_through(self):
        params = {"speaker": None}
        result = self.san.sanitize_params("search_by_speaker", params)
        self.assertIsNone(result["speaker"])

    def test_control_char_in_query(self):
        params = {"query": "find me\x1bmalicious"}
        result = self.san.sanitize_params("search_history", params)
        self.assertNotIn("\x1b", result["query"])

    def test_oversized_query_truncated(self):
        params = {"query": "x" * 50_000}
        result = self.san.sanitize_params("search_history", params)
        self.assertLessEqual(len(result["query"]), 10_000)

    def test_float_confidence_clamped(self):
        params = {"confidence_threshold": 1.5}
        result = self.san.sanitize_params("filter_by_confidence", params)
        self.assertEqual(result["confidence_threshold"], 1.0)

    def test_valid_path_preserved(self):
        valid = str(Path.home() / "test.wav")
        params = {"audio_path": valid}
        result = self.san.sanitize_params("transcribe_paths", params)
        self.assertEqual(result["audio_path"], str(Path(valid).resolve()))


if __name__ == "__main__":
    unittest.main()
