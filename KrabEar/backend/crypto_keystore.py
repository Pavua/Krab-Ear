"""Хранилище ключей шифрования на базе macOS Keychain.

Ключи хранятся через CLI-утилиту ``security`` (macOS Keychain).
На платформах без Keychain (Linux CI) — вызывает ``KeystoreUnavailable``.

Единственная точка вызова ``security`` — ``_run_security()`` —
позволяет тестам патчить её и не трогать реальный Keychain.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
from typing import Sequence

logger = logging.getLogger("KrabEar.Backend.CryptoKeystore")

_SERVICE = "KrabEar"
_ACCOUNT = "history-encryption-key"


class KeystoreUnavailable(Exception):
    """``security`` CLI недоступен на этой платформе."""


def _run_security(args: Sequence[str]) -> subprocess.CompletedProcess:
    """Выполняет команду ``security`` и возвращает CompletedProcess.

    Вынесено в отдельную функцию, чтобы тесты могли патчить её
    через ``unittest.mock.patch`` без вызова реального Keychain.
    """
    try:
        return subprocess.run(
            ["security", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise KeystoreUnavailable(
            "Команда 'security' не найдена — Keychain недоступен на этой платформе"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # Заблокированный Keychain (после сна/screen-lock) может показать
        # модальный пароль-диалог и подвесить вызов → IPC-тред зависает и
        # держит connection-слот. Fail-closed: считаем Keychain недоступным.
        raise KeystoreUnavailable(
            "Команда 'security' зависла (>10с) — Keychain заблокирован?"
        ) from exc


def get_or_create_history_key() -> bytes:
    """Возвращает 32-байтный ключ шифрования из Keychain.

    Если ключ отсутствует — генерирует новый ``os.urandom(32)``,
    сохраняет в Keychain и возвращает.

    Raises:
        KeystoreUnavailable: если ``security`` CLI не найден (не macOS).
    """
    # Пробуем найти существующий ключ
    result = _run_security(
        ["find-generic-password", "-s", _SERVICE, "-a", _ACCOUNT, "-w"]
    )
    if result.returncode == 0:
        b64 = result.stdout.strip()
        try:
            key = base64.b64decode(b64)
            if len(key) == 32:
                return key
            logger.warning(
                "crypto_keystore: ключ в Keychain имеет неверную длину %d, перегенерирую",
                len(key),
            )
        except Exception:
            logger.warning("crypto_keystore: ключ в Keychain повреждён, перегенерирую")

    # Генерируем новый ключ и сохраняем
    key = os.urandom(32)
    b64 = base64.b64encode(key).decode()
    store_result = _run_security(
        [
            "add-generic-password",
            "-s", _SERVICE,
            "-a", _ACCOUNT,
            "-w", b64,
            "-U",   # -U: update if exists
        ]
    )
    if store_result.returncode != 0:
        # Fail-closed: НЕ возвращаем неперсистированный ключ. Иначе текущая
        # сессия зашифрует им историю, а при рестарте ключ не найдётся →
        # сгенерируется новый → данные станут НЕДЕШИФРУЕМЫМИ (потеря данных).
        # raise → build_history_crypto вернёт None → шифрование останется ВЫКЛ
        # (открытый текст, но без потери данных).
        raise KeystoreUnavailable(
            "не удалось сохранить ключ в Keychain "
            f"(код {store_result.returncode}): {store_result.stderr.strip()}"
        )
    return key


def delete_history_key() -> None:
    """Удаляет ключ шифрования из Keychain.

    Если ключ не найден — ошибка игнорируется.

    Raises:
        KeystoreUnavailable: если ``security`` CLI не найден (не macOS).
    """
    result = _run_security(
        ["delete-generic-password", "-s", _SERVICE, "-a", _ACCOUNT]
    )
    if result.returncode != 0 and "could not be found" not in result.stderr.lower():
        logger.warning(
            "crypto_keystore: delete-generic-password завершился с кодом %d: %s",
            result.returncode,
            result.stderr.strip(),
        )


def keychain_available() -> bool:
    """Проверяет доступность macOS Keychain без создания ключа.

    Возвращает True, если ``security`` CLI присутствует и платформа — macOS/darwin.
    Не вызывает ``get_or_create_history_key`` (не создаёт ключ как побочный эффект).
    """
    return sys.platform == "darwin" and shutil.which("security") is not None
