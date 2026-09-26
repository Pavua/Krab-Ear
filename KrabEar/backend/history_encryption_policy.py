"""A5.2a — единый guard plaintext-копий управляемой истории.

При ``history_encryption_enabled`` (или неопределённой политике: повреждённые
или нечитаемые settings, неверный тип флага, потеря settings рядом с ENC1-
журналом) legacy-операции, создающие plaintext-копию истории, отклоняются ДО
mkdir/touch/copy/append/rewrite/prune.

Политика читается тем же fail-closed механизмом, что
``StateStore._read_encryption_flag_unlocked`` (A5 #2049). Guard НЕ трогает
Keychain: решение принимается только по settings.json и наличию ENC1-строк в
десяти управляемых журналах.

OFF-профиль сохраняет прежнее поведение.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from backend.state_store import (
    history_journal_paths,
    read_history_encryption_flag,
)

logger = logging.getLogger("KrabEar.Backend.HistoryEncryptionPolicy")

OPERATION_UNAVAILABLE_REASON = "history_encryption_operation_unavailable"


class HistoryEncryptionOperationUnavailable(Exception):
    """Plaintext-операция над историей запрещена при encryption ON."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.reason = OPERATION_UNAVAILABLE_REASON
        super().__init__(f"{operation}: {OPERATION_UNAVAILABLE_REASON}")


def data_dir_policy_reader(
    data_dir: Path | str,
    *,
    push_error: Callable[..., None] | None = None,
) -> Callable[[], bool]:
    """Fail-closed reader флага для менеджера без StateStore-ссылки.

    ``push_error`` опционально пробрасывается в ``read_history_encryption_flag``
    (ErrorBus), чтобы отказ политики был виден так же громко, как из StateStore.
    """
    base = Path(data_dir)
    settings_path = base / "settings.json"
    journals = history_journal_paths(base)
    return lambda: read_history_encryption_flag(
        settings_path, journals, push_error=push_error
    )


def store_policy_reader(store: Any) -> Callable[[], bool]:
    """Fail-closed reader флага для менеджера с StateStore.

    Использует существующий ``StateStore._read_encryption_flag_unlocked``.
    Метод обязан быть определён на КЛАССЕ store: проверка через
    ``getattr(type(store), ...)`` не даёт ``MagicMock`` авто-создать атрибут
    (иначе любой mock-store выглядел бы как ON и ломал OFF-контроль).

    Fallback: store без класс-метода, но с ``data_dir`` (будущий proxy/store
    wrapper) НЕ отключает гейты молча — читает ту же policy из
    ``data_dir/settings.json`` тем же fail-closed механизмом.
    """
    class_reader = getattr(type(store), "_read_encryption_flag_unlocked", None)
    if callable(class_reader):
        bound = getattr(store, "_read_encryption_flag_unlocked", None)
        if callable(bound):
            return lambda: bool(bound())
    data_dir = getattr(store, "data_dir", None)
    if isinstance(data_dir, (str, Path)):
        return data_dir_policy_reader(data_dir)
    return lambda: False


def policy_blocks(policy_read: Callable[[], bool] | None) -> bool:
    """Fail-closed: ошибка чтения политики означает «считаем ON» (отказ)."""
    if policy_read is None:
        return False
    try:
        return bool(policy_read())
    except Exception:  # noqa: BLE001
        logger.exception(
            "history_encryption_policy: ошибка чтения политики — считаем ON"
        )
        return True
