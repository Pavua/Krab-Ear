"""Тесты для backend/crypto_keystore.py.

Все тесты патчат ``_run_security`` — реальный Keychain никогда не вызывается.
"""
from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _ok_result(stdout: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


def _fail_result(returncode: int = 44, stderr: str = "not found") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = ""
    m.stderr = stderr
    return m


class TestGetOrCreateHistoryKey(unittest.TestCase):
    """get_or_create_history_key: find, create, invalid-length paths."""

    def test_returns_existing_key(self) -> None:
        """Если ключ найден в Keychain — возвращает его без создания нового."""
        fake_key = os.urandom(32)
        b64 = base64.b64encode(fake_key).decode()

        with patch("backend.crypto_keystore._run_security", return_value=_ok_result(b64)) as mock_sec:
            from backend.crypto_keystore import get_or_create_history_key
            result = get_or_create_history_key()

        self.assertEqual(result, fake_key)
        # Только find-generic-password — ни одного add-generic-password
        self.assertEqual(mock_sec.call_count, 1)
        self.assertIn("find-generic-password", mock_sec.call_args[0][0])

    def test_creates_and_stores_key_when_not_found(self) -> None:
        """Если ключ не найден — генерирует новый и сохраняет."""
        find_fail = _fail_result()
        store_ok = _ok_result()

        with patch("backend.crypto_keystore._run_security", side_effect=[find_fail, store_ok]) as mock_sec:
            from backend.crypto_keystore import get_or_create_history_key
            result = get_or_create_history_key()

        self.assertEqual(len(result), 32)
        self.assertEqual(mock_sec.call_count, 2)
        # Второй вызов должен быть add-generic-password
        second_call_args = mock_sec.call_args_list[1][0][0]
        self.assertIn("add-generic-password", second_call_args)

    def test_stored_key_is_base64_of_returned_key(self) -> None:
        """Ключ, переданный в Keychain, совпадает с возвращённым значением."""
        find_fail = _fail_result()
        stored_b64_holder: list[str] = []

        def fake_security(args):
            if args[0] == "find-generic-password":
                return find_fail
            # Извлекаем -w значение из args
            w_idx = args.index("-w")
            stored_b64_holder.append(args[w_idx + 1])
            return _ok_result()

        with patch("backend.crypto_keystore._run_security", side_effect=fake_security):
            from backend.crypto_keystore import get_or_create_history_key
            result = get_or_create_history_key()

        self.assertEqual(len(stored_b64_holder), 1)
        stored_key = base64.b64decode(stored_b64_holder[0])
        self.assertEqual(stored_key, result)

    def test_regenerates_if_stored_key_wrong_length(self) -> None:
        """Если в Keychain хранится ключ неверной длины — генерирует новый."""
        # Возвращаем 16-байтный ключ вместо 32
        short_key = os.urandom(16)
        b64_short = base64.b64encode(short_key).decode()

        find_ok = _ok_result(b64_short)
        store_ok = _ok_result()

        with patch("backend.crypto_keystore._run_security", side_effect=[find_ok, store_ok]) as mock_sec:
            from backend.crypto_keystore import get_or_create_history_key
            result = get_or_create_history_key()

        self.assertEqual(len(result), 32)
        self.assertEqual(mock_sec.call_count, 2)

    def test_keystore_unavailable_when_security_missing(self) -> None:
        """FileNotFoundError от 'security' → KeystoreUnavailable."""
        from backend.crypto_keystore import KeystoreUnavailable

        with patch(
            "backend.crypto_keystore._run_security",
            side_effect=KeystoreUnavailable("no security CLI"),
        ):
            from backend.crypto_keystore import get_or_create_history_key
            with self.assertRaises(KeystoreUnavailable):
                get_or_create_history_key()


class TestDeleteHistoryKey(unittest.TestCase):
    """delete_history_key: успех и игнор not-found."""

    def test_delete_calls_security(self) -> None:
        with patch("backend.crypto_keystore._run_security", return_value=_ok_result()) as mock_sec:
            from backend.crypto_keystore import delete_history_key
            delete_history_key()  # Не бросает исключений

        self.assertEqual(mock_sec.call_count, 1)
        self.assertIn("delete-generic-password", mock_sec.call_args[0][0])

    def test_delete_ignores_not_found(self) -> None:
        """Если ключ не найден — не бросает исключение."""
        not_found = _fail_result(44, "The specified item could not be found")
        with patch("backend.crypto_keystore._run_security", return_value=not_found):
            from backend.crypto_keystore import delete_history_key
            # Не должен бросать исключение
            delete_history_key()

    def test_delete_unavailable_platform(self) -> None:
        """KeystoreUnavailable при отсутствии security CLI."""
        from backend.crypto_keystore import KeystoreUnavailable

        with patch(
            "backend.crypto_keystore._run_security",
            side_effect=KeystoreUnavailable("no CLI"),
        ):
            from backend.crypto_keystore import delete_history_key
            with self.assertRaises(KeystoreUnavailable):
                delete_history_key()


class TestKeystoreUnavailableOnLinux(unittest.TestCase):
    """_run_security поднимает KeystoreUnavailable если 'security' не найдена."""

    def test_file_not_found_wrapped(self) -> None:
        from backend.crypto_keystore import KeystoreUnavailable, _run_security

        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            with self.assertRaises(KeystoreUnavailable):
                _run_security(["find-generic-password", "-s", "X"])


if __name__ == "__main__":
    unittest.main()
