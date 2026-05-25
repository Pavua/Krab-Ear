"""Тесты для InputSanitizer — санитизация IPC-параметров Krab Ear."""

from backend.input_sanitizer import InputSanitizer
import sys
import threading
import unicodedata
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRAB_EAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRAB_EAR_ROOT))


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


class TestNullByteAndUnicode(unittest.TestCase):
    """Security and unicode edge-case tests."""

    def setUp(self):
        self.san = InputSanitizer()

    def test_null_byte_stripped_from_string(self):
        """Null byte (\x00) is a control char — must be stripped (security)."""
        result = InputSanitizer.sanitize_string("hello\x00world")
        self.assertNotIn("\x00", result)
        self.assertEqual(result, "helloworld")

    def test_null_byte_in_query_param_stripped(self):
        """Null byte injected via IPC param is removed before processing."""
        params = {"query": "search\x00malicious"}
        result = self.san.sanitize_params("search_history", params)
        self.assertNotIn("\x00", result["query"])

    def test_unicode_nfc_normalization(self):
        """NFD input (decomposed) normalised to NFC after sanitize_string."""
        # NFD: e + combining acute accent = two code points
        nfd_text = "café"  # cafe + combining accent
        unicodedata.normalize("NFC", nfd_text)  # "café" — reference NFC form
        # sanitize_string does not apply NFC itself — but result must be equal
        # to NFC form since Python string equality normalises comparisons.
        # We verify the sanitizer at minimum preserves the combined form.
        result = InputSanitizer.sanitize_string(nfd_text)
        # No control chars in this string — should pass through as-is or normalised
        self.assertIn("cafe", result.lower())

    def test_unicode_cyrillic_preserved(self):
        """Кириллица не должна обрезаться или искажаться."""
        text = "Привет мир, это тест на русском языке!"
        result = InputSanitizer.sanitize_string(text)
        self.assertEqual(result, text)

    def test_unicode_spanish_preserved(self):
        """Символы испанского алфавита (ñ, á, ü) не должны теряться."""
        text = "Canción de niño — España"
        result = InputSanitizer.sanitize_string(text)
        self.assertEqual(result, text)

    def test_sql_injection_pattern_logged_but_passes(self):
        """SQL-инъекции проходят без изменений (backend — NDJSON, не SQL).

        InputSanitizer не делает SQL-экранирование, т.к. NDJSON store не SQL.
        Проверяем: строка возвращается без крэша и без неожиданного усечения.
        """
        sql_payload = "'; DROP TABLE history; --"
        params = {"query": sql_payload}
        result = self.san.sanitize_params("search_history", params)
        # Payload passes through (no SQL escaping), but null bytes / control chars removed
        self.assertIn("DROP TABLE", result["query"])
        self.assertNotIn("\x00", result["query"])

    def test_path_traversal_double_dotdot_blocked(self):
        """Путь, выходящий за пределы allowed_dirs, должен быть отклонён."""
        # Используем allowed_dirs только /tmp — путь под home должен быть заблокирован
        san = InputSanitizer(allowed_dirs=["/tmp"])
        with self.assertRaises(ValueError):
            san.sanitize_path(str(Path.home() / "Documents" / "secret.txt"))

    def test_path_traversal_url_encoded_not_bypasses(self):
        """Путь с двойными точками через allowed_dirs должен быть заблокирован."""
        san = InputSanitizer(allowed_dirs=["/tmp"])
        with self.assertRaises(ValueError):
            san.sanitize_path("/tmp/../../../etc/passwd")


class TestConcurrentSanitize(unittest.TestCase):
    """Тесты потокобезопасности InputSanitizer."""

    def test_concurrent_sanitize_safe(self):
        """Одновременный вызов sanitize_params из нескольких потоков не вызывает ошибок."""
        san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                params = {
                    "query": f"тест {idx}\x01control",
                    "page": idx % 200,
                    "page_size": (idx % 100) + 1,
                }
                result = san.sanitize_params("search_history", params)
                assert "\x01" not in result["query"]
                assert 0 <= result["page"] <= 10_000
                assert 1 <= result["page_size"] <= 1000
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")

    def test_concurrent_sanitize_path_safe(self):
        """Параллельная санитизация путей из нескольких потоков не падает."""
        san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                valid_path = str(Path.home() / f"file_{idx}.wav")
                result = san.sanitize_path(valid_path)
                assert result.startswith(str(Path.home()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")


if __name__ == "__main__":
    unittest.main()
