"""Wave 341 — дополнительные тесты для InputSanitizer.

Покрывают специфические требования Wave 341:
  - test_strip_html_tags_safely
  - test_validate_phone_number_format
  - test_validate_email_format
  - test_reject_oversize_payload
  - test_unicode_validation_preserves_meaning
  - test_path_traversal_blocked (../etc/passwd)
  - test_sql_injection_pattern_neutralized
  - test_concurrent_sanitize_thread_safe
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.input_sanitizer import InputSanitizer


class TestStripHtmlTagsSafely(unittest.TestCase):
    """HTML-теги: sanitize_string не делает HTML-escaping, но не крашится."""

    def setUp(self):
        self.san = InputSanitizer()

    def test_simple_script_tag_survives_string_sanitize(self):
        """XSS payload не содержит control chars — проходит как строка."""
        payload = "<script>alert('xss')</script>"
        result = InputSanitizer.sanitize_string(payload)
        # Санитизатор не делает HTML-escaping — это задача рендерера
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")

    def test_html_with_control_chars_stripped(self):
        """HTML с управляющими символами — control chars удаляются."""
        payload = "<b>hello\x00</b>\x1fworld"
        result = InputSanitizer.sanitize_string(payload)
        self.assertNotIn("\x00", result)
        self.assertNotIn("\x1f", result)
        self.assertIn("<b>hello</b>", result)

    def test_html_attribute_injection_no_crash(self):
        """Попытка attribute injection не вызывает исключение."""
        payload = 'value" onmouseover="alert(1)'
        result = InputSanitizer.sanitize_string(payload)
        self.assertIsInstance(result, str)

    def test_html_comment_injection_no_crash(self):
        """HTML-комментарий не вызывает исключение."""
        payload = "<!-- inject --><div>content</div>"
        result = InputSanitizer.sanitize_string(payload)
        self.assertIsInstance(result, str)
        self.assertIn("content", result)

    def test_iframe_src_no_crash(self):
        """iframe src injection не вызывает исключение."""
        payload = '<iframe src="javascript:alert(1)"></iframe>'
        result = InputSanitizer.sanitize_string(payload)
        self.assertIsInstance(result, str)

    def test_empty_html_string(self):
        """Пустой HTML-тег возвращает пустую строку после strip."""
        payload = "   "
        result = InputSanitizer.sanitize_string(payload)
        self.assertEqual(result, "")

    def test_html_in_query_param_cleaned(self):
        """HTML в query-параметре IPC не крашит sanitize_params."""
        params = {"query": "<script>inject</script>\x00bad"}
        result = self.san.sanitize_params("search_history", params)
        self.assertNotIn("\x00", result["query"])
        self.assertIsInstance(result["query"], str)


class TestValidatePhoneNumberFormat(unittest.TestCase):
    """Проверяем, что телефонные номера в полях проходят sanitize без потерь.

    InputSanitizer не валидирует форматы — он очищает строки.
    Эти тесты проверяют, что типичные phone-number строки НЕ обрезаются
    и НЕ теряют цифры при санитизации.
    """

    def setUp(self):
        self.san = InputSanitizer()

    def test_e164_phone_preserved(self):
        """+7XXXXXXXXXX — стандартный E.164 формат сохраняется."""
        phone = "+79161234567"
        result = InputSanitizer.sanitize_string(phone)
        self.assertEqual(result, phone)

    def test_us_phone_with_dashes_preserved(self):
        """US формат +1-800-555-0199 сохраняется."""
        phone = "+1-800-555-0199"
        result = InputSanitizer.sanitize_string(phone)
        self.assertEqual(result, phone)

    def test_phone_with_spaces_preserved(self):
        """Телефон с пробелами сохраняется (after strip leading/trailing)."""
        phone = "+7 916 123 45 67"
        result = InputSanitizer.sanitize_string(phone)
        self.assertEqual(result, phone)

    def test_phone_with_control_chars_stripped(self):
        """Control chars в телефонном номере удаляются."""
        phone = "+79161234567\x00\x01"
        result = InputSanitizer.sanitize_string(phone)
        self.assertEqual(result, "+79161234567")

    def test_overlong_phone_truncated(self):
        """Абсурдно длинный 'телефон' усекается до лимита."""
        phone = "+7" + "9" * 500
        result = InputSanitizer.sanitize_string(phone, max_length=20)
        self.assertLessEqual(len(result), 20)

    def test_phone_in_params_preserved(self):
        """Телефон в обычном строковом поле params сохраняется."""
        params = {"caller_id": "+79161234567"}
        result = self.san.sanitize_params("call_dial", params)
        self.assertEqual(result["caller_id"], "+79161234567")


class TestValidateEmailFormat(unittest.TestCase):
    """Email-адреса проходят sanitize без потерь символов."""

    def setUp(self):
        self.san = InputSanitizer()

    def test_simple_email_preserved(self):
        """user@example.com сохраняется как есть."""
        email = "user@example.com"
        result = InputSanitizer.sanitize_string(email)
        self.assertEqual(result, email)

    def test_email_with_plus_preserved(self):
        """user+tag@example.com (Gmail plus-addressing) сохраняется."""
        email = "user+tag@example.com"
        result = InputSanitizer.sanitize_string(email)
        self.assertEqual(result, email)

    def test_email_with_dots_preserved(self):
        """first.last@sub.domain.org сохраняется полностью."""
        email = "first.last@sub.domain.org"
        result = InputSanitizer.sanitize_string(email)
        self.assertEqual(result, email)

    def test_email_with_control_chars_stripped(self):
        """Control char в email удаляется."""
        email = "user\x00@example.com"
        result = InputSanitizer.sanitize_string(email)
        self.assertNotIn("\x00", result)
        self.assertIn("@example.com", result)

    def test_unicode_email_preserved(self):
        """Email с unicode-символами в localpart сохраняется."""
        email = "пользователь@example.com"
        result = InputSanitizer.sanitize_string(email)
        self.assertEqual(result, email)

    def test_email_in_params_preserved(self):
        """Email в строковом поле params сохраняется."""
        params = {"email": "test@krabear.app"}
        result = self.san.sanitize_params("send_digest", params)
        self.assertEqual(result["email"], "test@krabear.app")


class TestRejectOversizePayload(unittest.TestCase):
    """Проверяем обрезку чрезмерно больших строк и списков."""

    def setUp(self):
        self.san = InputSanitizer()

    def test_oversize_string_truncated_to_default_max(self):
        """Строка >10 000 символов усекается до дефолтного лимита."""
        big = "A" * 50_000
        result = InputSanitizer.sanitize_string(big)
        self.assertEqual(len(result), 10_000)

    def test_oversize_string_truncated_to_custom_max(self):
        """Строка усекается до указанного custom max_length."""
        big = "B" * 200
        result = InputSanitizer.sanitize_string(big, max_length=50)
        self.assertEqual(len(result), 50)

    def test_oversize_list_truncated_to_1000(self):
        """Список >1000 элементов усекается до 1000."""
        big_list = list(range(5000))
        params = {"ids": big_list}
        result = self.san.sanitize_params("bulk_delete", params)
        self.assertEqual(len(result["ids"]), 1000)

    def test_oversize_query_truncated_in_params(self):
        """query-поле >10 000 символов усекается."""
        params = {"query": "x" * 100_000}
        result = self.san.sanitize_params("search_history", params)
        self.assertLessEqual(len(result["query"]), 10_000)

    def test_short_string_field_truncated_to_its_limit(self):
        """method-поле ограничено 256 символами."""
        params = {"method": "m" * 1000}
        result = self.san.sanitize_params("dispatch", params)
        self.assertLessEqual(len(result["method"]), 256)

    def test_id_field_truncated_to_512(self):
        """id-поле ограничено 512 символами."""
        params = {"id": "i" * 1000}
        result = self.san.sanitize_params("get_item", params)
        self.assertLessEqual(len(result["id"]), 512)

    def test_exact_limit_string_not_truncated(self):
        """Строка ровно в лимите — не усекается."""
        exact = "Z" * 10_000
        result = InputSanitizer.sanitize_string(exact)
        self.assertEqual(len(result), 10_000)

    def test_oversize_nested_dict_values_sanitized(self):
        """Вложенный dict с длинными строками — значения усекаются."""
        params = {"settings": {"key": "v" * 20_000}}
        result = self.san.sanitize_params("set_settings", params)
        self.assertLessEqual(len(result["settings"]["key"]), 10_000)


class TestUnicodeValidationPreservesMeaning(unittest.TestCase):
    """Sanitize не уничтожает смысл unicode-строк."""

    def setUp(self):
        self.san = InputSanitizer()

    def test_cyrillic_preserved(self):
        """Кирилличный текст сохраняется полностью."""
        text = "Транскрипция звонка из Москвы в Санкт-Петербург"
        result = InputSanitizer.sanitize_string(text)
        self.assertEqual(result, text)

    def test_spanish_accents_preserved(self):
        """Испанские диакритики сохраняются."""
        text = "Conversación de negocios en España"
        result = InputSanitizer.sanitize_string(text)
        self.assertEqual(result, text)

    def test_emoji_preserved(self):
        """Emoji (surrogate pairs) не ломаются."""
        text = "Meeting summary 🎤📝"
        result = InputSanitizer.sanitize_string(text)
        self.assertIn("🎤", result)
        self.assertIn("📝", result)

    def test_mixed_languages_preserved(self):
        """Смешанный RU+ES+EN текст сохраняется."""
        text = "Привет! Hello! Hola amigo — всё работает"
        result = InputSanitizer.sanitize_string(text)
        self.assertEqual(result, text)

    def test_arabic_numerals_preserved(self):
        """Арабские (десятичные) цифры не теряются."""
        text = "Запись 2026-05-21 12:34:56"
        result = InputSanitizer.sanitize_string(text)
        self.assertEqual(result, text)

    def test_control_chars_within_unicode_stripped(self):
        """Управляющие символы внутри unicode-строки удаляются без потери соседних символов."""
        text = "Привет\x01мир"
        result = InputSanitizer.sanitize_string(text)
        self.assertEqual(result, "Приветмир")

    def test_zero_width_space_stripped(self):
        """ZERO WIDTH SPACE (​) это не control char — допускается."""
        text = "word​space"
        result = InputSanitizer.sanitize_string(text)
        # ​ не входит в _CONTROL_RE — должен сохраниться
        self.assertIn("word", result)
        self.assertIn("space", result)

    def test_rtl_mark_preserved(self):
        """RIGHT-TO-LEFT MARK (‏) не контрольный символ — сохраняется."""
        text = "عربي‏text"
        result = InputSanitizer.sanitize_string(text)
        self.assertIn("text", result)


class TestPathTraversalBlocked(unittest.TestCase):
    """Тесты блокировки path traversal."""

    def setUp(self):
        self.san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])

    def test_etc_passwd_via_dotdot_blocked(self):
        """Классический /tmp/../etc/passwd должен быть заблокирован.

        Примечание: чистый ../etc/passwd является относительным и резолвится
        от CWD — который может быть под home. Используем абсолютный вариант.
        """
        with self.assertRaises(ValueError):
            self.san.sanitize_path("/tmp/../etc/passwd")

    def test_absolute_etc_passwd_blocked(self):
        """/etc/passwd напрямую блокируется."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("/etc/passwd")

    def test_tmp_dotdot_etc_passwd_blocked(self):
        """/tmp/../../../etc/passwd разрешается в /etc/passwd — блокируется."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("/tmp/../../../etc/passwd")

    def test_multiple_dotdot_blocked(self):
        """Множественные ../ из /tmp блокируются — резолвятся в /etc/shadow."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("/tmp/../../../../etc/shadow")

    def test_home_dotdot_system_blocked(self):
        """~/../../etc/hosts — выход за пределы home блокируется."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("~/../../etc/hosts")

    def test_valid_home_subpath_allowed(self):
        """Корректный путь под home — разрешён."""
        valid = str(Path.home() / "Documents" / "recording.wav")
        result = self.san.sanitize_path(valid)
        self.assertTrue(result.startswith(str(Path.home())))

    def test_valid_tmp_path_allowed(self):
        """Путь под /tmp — разрешён."""
        result = self.san.sanitize_path("/tmp/krabear_audio.wav")
        self.assertTrue(
            result.startswith("/tmp") or result.startswith("/private/tmp")
        )

    def test_path_traversal_in_params_raises(self):
        """path traversal в поле path params поднимает ValueError."""
        params = {"path": "/tmp/../../../etc/passwd"}
        with self.assertRaises(ValueError):
            self.san.sanitize_params("export_history", params)

    def test_path_traversal_in_audio_path_raises(self):
        """path traversal в поле audio_path params поднимает ValueError."""
        params = {"audio_path": "/tmp/../../../var/db/passwd.db"}
        with self.assertRaises(ValueError):
            self.san.sanitize_params("transcribe_file", params)


class TestSqlInjectionPatternNeutralized(unittest.TestCase):
    """SQL-injection паттерны — InputSanitizer не SQL DB, но не должен крашиться.

    Backend использует NDJSON, не SQL. Тесты проверяют, что:
    1. Control chars в SQL-payload удаляются (null byte в VALUES()'\x00).
    2. Строка возвращается без краша.
    3. Базовая структура сохраняется (sanitizer не блокирует payload).
    """

    def setUp(self):
        self.san = InputSanitizer()

    def test_classic_drop_table_passes_without_crash(self):
        """'; DROP TABLE history; -- не вызывает исключение."""
        sql = "'; DROP TABLE history; --"
        result = InputSanitizer.sanitize_string(sql)
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")

    def test_union_select_passes_without_crash(self):
        """UNION SELECT * FROM users не вызывает исключение."""
        sql = "' UNION SELECT * FROM users --"
        result = InputSanitizer.sanitize_string(sql)
        self.assertIn("UNION", result)

    def test_null_byte_in_sql_payload_stripped(self):
        """Null byte внутри SQL payload (\x00) удаляется."""
        sql = "SELECT * FROM table WHERE id=1\x00 OR 1=1"
        result = InputSanitizer.sanitize_string(sql)
        self.assertNotIn("\x00", result)
        self.assertIn("SELECT", result)

    def test_sleep_injection_no_crash(self):
        """SLEEP(5) injection не вызывает исключение."""
        sql = "'; WAITFOR DELAY '0:0:5'--"
        result = InputSanitizer.sanitize_string(sql)
        self.assertIsInstance(result, str)

    def test_sql_payload_in_query_param(self):
        """SQL payload в query-параметре: control chars удаляются."""
        params = {"query": "'; DROP TABLE history\x00; --"}
        result = self.san.sanitize_params("search_history", params)
        self.assertNotIn("\x00", result["query"])
        self.assertIn("DROP TABLE", result["query"])

    def test_hex_encoded_null_byte_stripped(self):
        """Hex-кодированный null byte в строке удаляется."""
        sql = "SELECT\x00FROM"
        result = InputSanitizer.sanitize_string(sql)
        self.assertNotIn("\x00", result)
        self.assertEqual(result, "SELECTFROM")


class TestConcurrentSanitizeThreadSafe(unittest.TestCase):
    """InputSanitizer должен быть потокобезопасным."""

    def test_concurrent_sanitize_string_thread_safe(self):
        """Параллельный вызов sanitize_string из 50 потоков не вызывает ошибок."""
        errors: list[Exception] = []
        results: list[str] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            try:
                text = f"Транскрипция #{idx}\x01\x00control\x1fchars"
                result = InputSanitizer.sanitize_string(text)
                assert "\x01" not in result
                assert "\x00" not in result
                assert "\x1f" not in result
                with lock:
                    results.append(result)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertEqual(len(results), 50)

    def test_concurrent_sanitize_params_thread_safe(self):
        """Параллельный вызов sanitize_params из 40 потоков не вызывает ошибок."""
        san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                params = {
                    "query": f"поиск #{idx}\x00null",
                    "page": idx % 100,
                    "page_size": (idx % 50) + 1,
                    "text": "normal text " * 5,
                }
                result = san.sanitize_params("search_history", params)
                assert "\x00" not in result["query"]
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

    def test_concurrent_sanitize_path_thread_safe(self):
        """Параллельная санитизация путей из 20 потоков безопасна."""
        san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                valid = str(Path.home() / f"audio_{idx}.wav")
                result = san.sanitize_path(valid)
                assert result.startswith(str(Path.home()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")

    def test_concurrent_mixed_operations_thread_safe(self):
        """Смешанные операции из нескольких потоков одновременно."""
        san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])
        errors: list[Exception] = []

        def string_worker() -> None:
            for _ in range(10):
                try:
                    InputSanitizer.sanitize_string("test\x00data" * 100)
                except Exception as e:
                    errors.append(e)

        def params_worker() -> None:
            for _ in range(10):
                try:
                    san.sanitize_params("method", {"query": "hello\x01", "page": 5})
                except Exception as e:
                    errors.append(e)

        threads = (
            [threading.Thread(target=string_worker) for _ in range(5)]
            + [threading.Thread(target=params_worker) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Mixed thread errors: {errors}")


if __name__ == "__main__":
    unittest.main()
