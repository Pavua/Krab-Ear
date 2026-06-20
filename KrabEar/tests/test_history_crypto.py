"""Тесты для backend/history_crypto.py.

Проверяют: round-trip encrypt→decrypt, is_encrypted, tamper-detect,
SENTINEL-префикс, build_history_crypto с замокированным keystore.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Путь к репозиторию (tests/ → KrabEar/ → repo root)
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.history_crypto import SENTINEL, HistoryCrypto


class TestHistoryCryptoRoundTrip(unittest.TestCase):
    """Базовый round-trip: encrypt → decrypt возвращает исходную строку."""

    def setUp(self) -> None:
        self.key = os.urandom(32)
        self.crypto = HistoryCrypto(self.key)

    def test_round_trip_ascii(self) -> None:
        plaintext = '{"id":"abc","text":"hello world"}'
        token = self.crypto.encrypt_line(plaintext)
        result = self.crypto.decrypt_line(token)
        self.assertEqual(result, plaintext)

    def test_round_trip_cyrillic(self) -> None:
        plaintext = '{"id":"xyz","text":"Привет мир"}'
        token = self.crypto.encrypt_line(plaintext)
        result = self.crypto.decrypt_line(token)
        self.assertEqual(result, plaintext)

    def test_round_trip_empty_string(self) -> None:
        plaintext = ""
        token = self.crypto.encrypt_line(plaintext)
        result = self.crypto.decrypt_line(token)
        self.assertEqual(result, plaintext)

    def test_each_call_produces_unique_token(self) -> None:
        """Каждый вызов encrypt_line генерирует уникальный nonce → разные токены."""
        plaintext = "same text"
        token1 = self.crypto.encrypt_line(plaintext)
        token2 = self.crypto.encrypt_line(plaintext)
        self.assertNotEqual(token1, token2)
        # Но расшифровка обоих даёт одинаковый plaintext
        self.assertEqual(self.crypto.decrypt_line(token1), plaintext)
        self.assertEqual(self.crypto.decrypt_line(token2), plaintext)


class TestHistoryCryptoSentinel(unittest.TestCase):
    """Тесты SENTINEL и is_encrypted."""

    def setUp(self) -> None:
        self.key = os.urandom(32)
        self.crypto = HistoryCrypto(self.key)

    def test_encrypted_line_starts_with_sentinel(self) -> None:
        token = self.crypto.encrypt_line("test")
        self.assertTrue(token.startswith(SENTINEL))

    def test_is_encrypted_true_for_encrypted(self) -> None:
        token = self.crypto.encrypt_line("test")
        self.assertTrue(HistoryCrypto.is_encrypted(token))

    def test_is_encrypted_false_for_plaintext_json(self) -> None:
        self.assertFalse(HistoryCrypto.is_encrypted('{"id":"1","text":"plain"}'))

    def test_is_encrypted_false_for_empty_string(self) -> None:
        self.assertFalse(HistoryCrypto.is_encrypted(""))

    def test_is_encrypted_false_for_partial_sentinel(self) -> None:
        # Строка начинается похоже, но не является полным SENTINEL
        self.assertFalse(HistoryCrypto.is_encrypted("ENC"))
        self.assertFalse(HistoryCrypto.is_encrypted("ENC0:something"))


class TestHistoryCryptoTamper(unittest.TestCase):
    """Тесты обнаружения подделки: перевёрнутый байт → исключение."""

    def setUp(self) -> None:
        self.key = os.urandom(32)
        self.crypto = HistoryCrypto(self.key)

    def test_tamper_raises(self) -> None:
        import base64

        token = self.crypto.encrypt_line("secret data")
        # Декодируем base64-часть, меняем один байт в ciphertext, перекодируем
        b64_part = token[len(SENTINEL):]
        raw = bytearray(base64.b64decode(b64_part))
        # Меняем последний байт (в теле шифротекста, не в nonce)
        raw[-1] ^= 0xFF
        tampered = SENTINEL + base64.b64encode(bytes(raw)).decode("ascii")
        with self.assertRaises(Exception):
            self.crypto.decrypt_line(tampered)

    def test_wrong_key_raises(self) -> None:
        token = self.crypto.encrypt_line("secret data")
        other_crypto = HistoryCrypto(os.urandom(32))
        with self.assertRaises(Exception):
            other_crypto.decrypt_line(token)

    def test_short_token_below_min_payload_rejected(self) -> None:
        """Crypto-audit (2026-06-20): токен с raw < nonce(12)+tag(16)=28 байт
        отвергается явной ValueError (раньше граница была 12 → 13-27 байт
        проходили проверку и падали глубже в InvalidTag, делая ветку мёртвой)."""
        import base64

        # 20 байт raw: больше старой границы (12), меньше валидного минимума (28)
        short = SENTINEL + base64.b64encode(os.urandom(20)).decode("ascii")
        with self.assertRaises(ValueError):
            self.crypto.decrypt_line(short)


class TestHistoryCryptoKeyValidation(unittest.TestCase):
    """Конструктор валидирует длину ключа."""

    def test_short_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            HistoryCrypto(b"short")

    def test_long_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            HistoryCrypto(os.urandom(64))

    def test_correct_key_length_ok(self) -> None:
        # Не должен бросать исключение
        HistoryCrypto(os.urandom(32))


class TestBuildHistoryCrypto(unittest.TestCase):
    """build_history_crypto возвращает None если keystore недоступен."""

    def test_returns_none_when_keystore_unavailable(self) -> None:
        from unittest.mock import patch

        from backend.crypto_keystore import KeystoreUnavailable
        with patch(
            "backend.crypto_keystore._run_security",
            side_effect=KeystoreUnavailable("no keychain on CI"),
        ):
            from backend.history_crypto import build_history_crypto
            result = build_history_crypto()
        self.assertIsNone(result)

    def test_returns_history_crypto_when_key_available(self) -> None:
        from unittest.mock import MagicMock, patch
        import base64

        fake_key = os.urandom(32)
        fake_b64 = base64.b64encode(fake_key).decode()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_b64

        with patch("backend.crypto_keystore._run_security", return_value=mock_result):
            from backend.history_crypto import build_history_crypto
            crypto = build_history_crypto()

        self.assertIsNotNone(crypto)
        self.assertIsInstance(crypto, HistoryCrypto)

    def test_decrypt_with_returned_crypto(self) -> None:
        """Ключ из keystore используется корректно для шифрования/расшифровки."""
        from unittest.mock import MagicMock, patch
        import base64

        fake_key = os.urandom(32)
        fake_b64 = base64.b64encode(fake_key).decode()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_b64

        with patch("backend.crypto_keystore._run_security", return_value=mock_result):
            from backend.history_crypto import build_history_crypto
            crypto = build_history_crypto()

        self.assertIsNotNone(crypto)
        plaintext = '{"id":"test","text":"данные"}'
        token = crypto.encrypt_line(plaintext)
        self.assertEqual(crypto.decrypt_line(token), plaintext)


if __name__ == "__main__":
    unittest.main()
