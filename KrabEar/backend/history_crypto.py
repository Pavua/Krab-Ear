"""Шифрование строк истории с помощью AES-256-GCM (AESGCM).

Каждая строка NDJSON шифруется независимо: nonce (12 байт) || ciphertext
кодируется в base64 и предваряется константой ``SENTINEL``.

Это позволяет безопасно смешивать в одном файле открытый и зашифрованный текст:
``is_encrypted()`` отличает одно от другого, и StateStore читает оба формата.

Не шифруем: settings.json (там хранится флаг включения шифрования — chicken-and-egg).
"""

from __future__ import annotations

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("KrabEar.Backend.HistoryCrypto")

SENTINEL = "ENC1:"
_NONCE_BYTES = 12


class HistoryCrypto:
    """AES-256-GCM шифровальщик/дешифровальщик строк истории."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"Ключ должен быть 32 байта, получено {len(key)}")
        self._aesgcm = AESGCM(key)

    def encrypt_line(self, plaintext: str) -> str:
        """Шифрует строку и возвращает ``SENTINEL + base64(nonce + ciphertext)``."""
        nonce = os.urandom(_NONCE_BYTES)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        token = base64.b64encode(nonce + ct).decode("ascii")
        return SENTINEL + token

    def decrypt_line(self, token: str) -> str:
        """Дешифрует строку.  Поднимает исключение при ошибке аутентификации или порче данных."""
        if not token.startswith(SENTINEL):
            raise ValueError("Строка не является зашифрованной (отсутствует SENTINEL)")
        b64_part = token[len(SENTINEL):]
        raw = base64.b64decode(b64_part)
        if len(raw) < _NONCE_BYTES:
            raise ValueError("Зашифрованные данные слишком коротки")
        nonce = raw[:_NONCE_BYTES]
        ct = raw[_NONCE_BYTES:]
        plaintext_bytes = self._aesgcm.decrypt(nonce, ct, None)
        return plaintext_bytes.decode("utf-8")

    @staticmethod
    def is_encrypted(line: str) -> bool:
        """Возвращает True, если строка начинается с ``SENTINEL``."""
        return line.startswith(SENTINEL)


def build_history_crypto() -> HistoryCrypto | None:
    """Создаёт ``HistoryCrypto`` используя ключ из macOS Keychain.

    Возвращает ``None``, если Keychain недоступен (Linux CI / нет ключа).
    Логирует предупреждение, но не поднимает исключение — StateStore
    обрабатывает None как «шифрование недоступно».
    """
    try:
        from backend.crypto_keystore import get_or_create_history_key
        key = get_or_create_history_key()
        return HistoryCrypto(key)
    except Exception as exc:
        logger.warning(
            "history_crypto: не удалось инициализировать шифрование: %s — "
            "история будет храниться в открытом виде",
            exc,
        )
        return None
