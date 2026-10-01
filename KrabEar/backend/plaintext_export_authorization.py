"""A5.3 карточка A — authorizer разрешения plaintext-вывода: типизация policy snapshot.

Slice 1 содержит ТОЛЬКО типы и машинные причины. Reader живёт в
``backend.state_store`` (``StateStore._read_plaintext_policy_snapshot_unlocked``),
поэтому этот модуль НЕ импортирует ``state_store`` — иначе циклический импорт.
Grants/receipts/epoch/issue/validate — slice 2.

Контракт: спека ``2026-09-24-a5-history-at-rest-design.md`` §7.1–7.3 и
``A53_STRONG_MODEL_HANDOFF.md`` п.3, 4, 9, 13, 14.

Инварианты, которые типы ОБЯЗАНЫ защищать:
  * ``ON`` означает только явно валидный ``true``; bool ошибки НЕ кодирует
    состояние (всё, что не ``True``/``False`` из файла, — ``UNKNOWN``).
  * Отсутствие ``settings.json`` НЕ «свежий профиль OFF» (п.4): authorizer
    никогда не инициализирует настройки сам.
  * ``reason`` — машинная константа, НЕ текст для UI и НЕ ``repr`` payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Internal revision, которую центральный settings commit генерирует заново на
#: КАЖДОЙ поддержанной записи settings (§7.3). Это durable-версия против
#: ON→OFF→ON между процессами, НЕ secret/capability: персистентность допустима.
POLICY_REVISION_KEY = "_plaintext_export_policy_revision"

#: Канонический формат internal revision = ``uuid4().hex``: ровно 32 символа
#: в lowercase hex. Единственный формат, который пишет центральный commit, и
#: единственный, который принимает reader. «Любая непустая строка» — слишком
#: широко: произвольная строка прошла бы как валидная ревизия, и проверка
#: «ревизия ли изменилась» теряла бы смысл.
POLICY_REVISION_HEX_LEN = 32
_POLICY_REVISION_HEX_ALPHABET = frozenset("0123456789abcdef")


def is_valid_policy_revision(value: object) -> bool:
    """True только для строки канонического вида ``uuid4().hex``.

    Fail-closed: не-строка, неверная длина, uppercase, пробелы, любой не-hex
    символ → False (и, следовательно, ``INVALID_REVISION``). Legacy-валидный
    формат (тот, что пишет commit) НЕ отвергается.
    """
    if type(value) is not str or len(value) != POLICY_REVISION_HEX_LEN:
        return False
    return _POLICY_REVISION_HEX_ALPHABET.issuperset(value)


#: Жёсткий кап на чтение settings.json при снятии policy snapshot (§7.3).
#: Превышение → UNKNOWN, файл целиком НЕ читается.
MAX_POLICY_BYTES = 16 * 1024 * 1024

# Машинные причины UNKNOWN. Значения — короткие snake_case константы: они
# уходят в IPC/reason-коды и в логи, поэтому НИКОГДА не содержат пути,
# содержимого файла, секретов или repr исключения.
REASON_UNKNOWN_MISSING_SETTINGS = "unknown_missing_settings"
REASON_UNKNOWN_UNREADABLE = "unknown_unreadable"
REASON_UNKNOWN_NOT_OBJECT = "unknown_not_object"
REASON_UNKNOWN_DUPLICATE_KEYS = "unknown_duplicate_keys"
REASON_UNKNOWN_MISSING_KEY = "unknown_missing_key"
REASON_UNKNOWN_NON_BOOL = "unknown_non_bool"
REASON_UNKNOWN_MISSING_REVISION = "unknown_missing_revision"
REASON_UNKNOWN_INVALID_REVISION = "unknown_invalid_revision"
REASON_UNKNOWN_NON_REGULAR = "unknown_non_regular"
REASON_UNKNOWN_OVERSIZE = "unknown_oversize"
REASON_UNKNOWN_UNSTABLE = "unknown_unstable"
REASON_UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


class PolicyState(str, Enum):
    """Типизированное состояние policy snapshot.

    ``str``-миксин — чтобы значение сериализовалось в IPC без ручного ``.value``.
    """

    KNOWN_OFF = "KNOWN_OFF"
    KNOWN_ON = "KNOWN_ON"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PolicyFingerprint:
    """Полный отпечаток снимка (§7.3).

    ``profile_identity`` — путь профиля (``str(data_dir)``), НЕ хеш секрета.
    ``content_sha256`` — SHA256 ровно тех bytes, из которых разобраны флаги и
    ревизия; SHA не логируется вместе с содержимым.
    """

    profile_identity: str
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int
    content_sha256: str
    internal_revision: str


@dataclass(frozen=True)
class PolicySnapshot:
    """Результат чтения policy snapshot.

    ``privacy_mode_enabled`` — раздельно от ``state``: privacy true запрещает
    вывод даже при ``KNOWN_OFF`` (контракт п.18), поэтому это НЕ часть ``state``.
    ``fingerprint`` заполнен только для KNOWN_*; для UNKNOWN он ``None``.
    """

    state: PolicyState
    privacy_mode_enabled: bool | None
    internal_revision: str | None
    fingerprint: PolicyFingerprint | None
    reason: str | None


def unknown_snapshot(reason: str) -> PolicySnapshot:
    """Единственный конструктор UNKNOWN-снимка (fail-closed, без payload)."""
    return PolicySnapshot(
        state=PolicyState.UNKNOWN,
        privacy_mode_enabled=None,
        internal_revision=None,
        fingerprint=None,
        reason=reason,
    )
