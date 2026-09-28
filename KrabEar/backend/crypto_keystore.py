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
# security(1) возвращает errSecItemNotFound (-25300) как 8-битный exit code.
_ITEM_NOT_FOUND_EXIT_CODE = 44


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
            key = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise KeystoreUnavailable("ключ в Keychain повреждён") from exc
        if len(key) != 32:
            raise KeystoreUnavailable("ключ в Keychain имеет неверную длину")
        return key
    if result.returncode != _ITEM_NOT_FOUND_EXIT_CODE:
        raise KeystoreUnavailable(
            f"не удалось прочитать ключ из Keychain (код {result.returncode})"
        )

    # Генерируем новый ключ и сохраняем
    key = os.urandom(32)
    b64 = base64.b64encode(key).decode()
    store_result = _run_security(
        [
            "add-generic-password",
            "-s", _SERVICE,
            "-a", _ACCOUNT,
            "-w", b64,
            # Без -U: конкурентное создание не должно заменить уже записанный ключ.
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


def delete_history_key() -> bool:
    """Удаляет ключ шифрования из Keychain и ПОДТВЕРЖДАЕТ отсутствие.

    Возвращает:
        True  — ключ уничтожен (удалён) или его не было, и отсутствие подтверждено;
        False — уничтожить не удалось, либо результат не удалось подтвердить.

    A5.2c1: вызывающий (privacy-purge) обязан знать исход. Раньше функция
    глотала неудачный exit code в лог и возвращала None, из-за чего отчёт
    purge рапортовал «ключ уничтожен» даже когда живой ключ остался на месте —
    а живой ключ в сочетании с pre-purge бэкапом означает всю историю.
    Fail-closed: неопределённость и отказ оба дают False, а не True.

    L2 (adversarial-ревью) — два риска, оба закрыты здесь:

    1. «Ключа не было» определялось ПОДСТРОКОЙ stderr (``could not be found``).
       На другой локали тот же самый успешный purge стал бы «частичным» — а
       владелец, регулярно получающий ложный ``complete: false``, начинает
       игнорировать сам признак. Решение принимает EXIT CODE, как это уже делает
       соседний :func:`get_or_create_history_key` (``_ITEM_NOT_FOUND_EXIT_CODE``).
    2. ``security delete`` может вернуть 0, а элемент остаться на месте
       (заблокированный Keychain по другому пути, гонка с другим процессом,
       посредник). Поэтому «удалил» ≠ «уничтожен»: после успешного кода
       делается read-only подтверждение отсутствия тем же приёмом, что и
       :func:`history_key_present` (без ``-w``, ключевой материал не читается).
       Неопределённость (``None``) — тоже False: подтвердить не смогли.

    Raises:
        KeystoreUnavailable: если ``security`` CLI не найден (не macOS). Вызывающий
            трактует это как «на этой платформе ключа не существует».
    """
    result = _run_security(
        ["delete-generic-password", "-s", _SERVICE, "-a", _ACCOUNT]
    )
    if result.returncode not in (0, _ITEM_NOT_FOUND_EXIT_CODE):
        logger.warning(
            "crypto_keystore: delete-generic-password завершился с кодом %d: %s",
            result.returncode,
            result.stderr.strip(),
        )
        return False

    present = history_key_present()
    if present is True:
        logger.error(
            "crypto_keystore: delete вернул код 0, но ключ всё ещё в Keychain — "
            " shred не засчитан"
        )
        return False
    if present is None:
        logger.warning(
            "crypto_keystore: не удалось подтвердить отсутствие ключа — shred не засчитан"
        )
        return False
    return True


def keychain_available() -> bool:
    """Проверяет доступность macOS Keychain без создания ключа.

    Возвращает True, если ``security`` CLI присутствует и платформа — macOS/darwin.
    Не вызывает ``get_or_create_history_key`` (не создаёт ключ как побочный эффект).
    """
    return sys.platform == "darwin" and shutil.which("security") is not None


def history_key_present() -> bool | None:
    """Read-only проба «есть ли ключ шифрования истории» (A5.2c1).

    Возвращает:
        True  — ключ есть;
        False — ключа нет;
        None  — определить не удалось (Keychain недоступен/заблокирован).

    Ключевой материал НЕ читается: тот же ``find-generic-password``, но БЕЗ ``-w``,
    поэтому команда не выводит секрет. Проба ничего не создаёт — диагностика не
    должна восстанавливать ключ побочным эффектом (иначе «проверить, есть ли
    ключ» само создало бы ключ и тихо изменило состояние профиля).

    ``None``, а не ``False``: «не смогли определить» и «ключа нет» — разные
    состояния, и подменять одно другим здесь нельзя (fail-closed в сторону
    неопределённости, не в сторону «всё хорошо»).
    """
    if not keychain_available():
        return None
    result = _run_security(
        ["find-generic-password", "-s", _SERVICE, "-a", _ACCOUNT]
    )
    if result.returncode == 0:
        return True
    if result.returncode == _ITEM_NOT_FOUND_EXIT_CODE:
        return False
    # Любой другой код — не «ключа нет», а «не смогли определить».
    logger.warning(
        "crypto_keystore: find-generic-password вернул код %d — наличие ключа неизвестно",
        result.returncode,
    )
    return None
