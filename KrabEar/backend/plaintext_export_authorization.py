"""A5.3 карточка A — authorizer разрешения plaintext-вывода.

Slice 1: типы и машинные причины policy snapshot (ниже). Reader живёт в
``backend.state_store`` (``StateStore._read_plaintext_policy_snapshot_unlocked``),
поэтому этот модуль НЕ импортирует ``state_store`` — иначе циклический импорт.

Slice 2 (этот код): ``PlaintextExportAuthorizer`` — только Python API, БЕЗ IPC
(IPC-методы и namespace — slice 4). Выдаёт session capability и одноразовые
operation receipt'ы, хранит high-water operation_seq, отзывает grants при смене
policy/UNKNOWN/privacy.

Контракт: спека ``2026-09-24-a5-history-at-rest-design.md`` §7.1–7.4, §7.6 и
``A53_STRONG_MODEL_HANDOFF.md`` п.1, 2, 4, 5, 5a, 5b, 5c, 12, 15, 16, 18, 19.

Инварианты, которые типы ОБЯЗАНЫ защищать:
  * ``ON`` означает только явно валидный ``true``; bool ошибки НЕ кодирует
    состояние (всё, что не ``True``/``False`` из файла, — ``UNKNOWN``).
  * Отсутствие ``settings.json`` НЕ «свежий профиль OFF» (п.4): authorizer
    никогда не инициализирует настройки сам.
  * ``reason`` — машинная константа, НЕ текст для UI и НЕ ``repr`` payload.

Инварианты authorizer (slice 2):
  * Порядок захвата ЖЁСТКО: сначала свежий snapshot (внутри ``read_snapshot``
    уже под ``StateStore._lock``), затем authorizer lock. НИКОГДА наоборот.
    Колбэки под authorizer lock НЕ вызываются: ``read_snapshot`` вызывается
    ДО взятия authorizer lock (контракт п.12).
  * Grants/receipts — ТОЛЬКО RAM. Никакой сериализации в settings/UserDefaults/
    Keychain/logs. ``__repr__`` не содержит capability/receipt/session ID.
  * Любой UNKNOWN-снимок немедленно чистит grants/receipts/high-water,
    увеличивает generation и запрещает grant И validation (п.5). Восстановление
    файла НЕ возвращает старое согласие.
  * Любой fingerprint mismatch отзывает прежние grants ДО validation (п.15),
    даже побайтовая synthetic-замена мимо commit (ino/ctime сменились, п.5b).
  * Privacy true всегда deny (``privacy_mode_active``), даже при KNOWN_OFF (п.18).
  * KNOWN_OFF: grant НЕ выдаётся (не нужен); validation без capability допустима
    только при подтверждённом OFF (§7.6). Operation tracking (epoch+generation+
    high-water) ведётся и на OFF-пути.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

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


# ── Slice 2: стабильные строковые причины отказов authorizer (§7.6) ──
#
# Ни один отказ не возвращает успешный path/file (здесь вообще нет path/file —
# только capability/receipt токены; на deny токен всегда None).

#: Подтверждение (sheet/grant) требуется, но не предъявлено: ON без capability
#: или issue при KNOWN_OFF (там grant не выдаётся — путь capabilityless validation).
REASON_PLAINTEXT_CONFIRMATION_REQUIRED = "plaintext_confirmation_required"

#: Предъявленный session-контекст протух: неизвестная/чужоя capability, чужой
#: epoch (replay epoch запрещён), протухшая generation, повторный/меньший
#: operation_seq, отозванная сессия, fingerprint mismatch, malformed-поля.
REASON_PLAINTEXT_SESSION_EXPIRED = "plaintext_session_expired"

#: Policy snapshot UNKNOWN: grant и validation запрещены, RAM очищена (п.5).
REASON_PLAINTEXT_POLICY_UNAVAILABLE = "plaintext_policy_unavailable"

#: Privacy true: всегда deny, даже при KNOWN_OFF и валидном grant (п.18).
REASON_PRIVACY_MODE_ACTIVE = "privacy_mode_active"


@dataclass(frozen=True)
class GrantResult:
    """Итог ``issue_grant``. На отказе ``capability`` всегда None."""

    ok: bool
    capability: Optional[str]
    epoch: bytes
    policy_generation: int
    reason: Optional[str]

    def __repr__(self) -> str:  # noqa: D105 — redacted по контракту §7.7
        redacted = "<redacted>" if self.capability is not None else None
        return (
            "GrantResult(ok=%r, capability=%r, "
            "policy_generation=%r, reason=%r)"
            % (self.ok, redacted, self.policy_generation, self.reason)
        )


@dataclass(frozen=True)
class ValidateResult:
    """Итог ``validate``. На отказе ``receipt`` всегда None."""

    ok: bool
    receipt: Optional[str]
    policy_generation: int
    reason: Optional[str]

    def __repr__(self) -> str:  # noqa: D105 — redacted по контракту §7.7
        redacted = "<redacted>" if self.receipt is not None else None
        return (
            "ValidateResult(ok=%r, receipt=%r, "
            "policy_generation=%r, reason=%r)"
            % (self.ok, redacted, self.policy_generation, self.reason)
        )


@dataclass
class _GrantRecord:
    """RAM-запись выданного grant (никогда не сериализуется)."""

    app_session_id: str
    capability: str
    generation: int
    fingerprint: PolicyFingerprint

    def __repr__(self) -> str:  # noqa: D105 — без session/capability (§7.7)
        return "_GrantRecord(generation=%r)" % (self.generation,)


@dataclass
class _ReceiptRecord:
    """RAM-запись выданного receipt (никогда не сериализуется)."""

    app_session_id: str
    generation: int
    operation_seq: int
    sink_kind: str

    def __repr__(self) -> str:  # noqa: D105 — без session (§7.7)
        return "_ReceiptRecord(generation=%r, operation_seq=%r, sink_kind=%r)" % (
            self.generation,
            self.operation_seq,
            self.sink_kind,
        )


class PlaintextExportAuthorizer:
    """Session authorizer plaintext-вывода: capability + одноразовые receipt'ы.

    Только Python API (IPC — slice 4). Привязка grant: (profile/service,
    app_session_id, epoch, policy_generation, snapshot fingerprint) — НЕ
    глобальный boolean (§7.4).

    :param read_snapshot: callable без аргументов, возвращающий PolicySnapshot
        (для manager без store — callback его store; для BackendService —
        ``store.read_plaintext_policy_snapshot``). Может нести bounded
        timeout/nowait (``functools.partial``) — тогда contention даёт UNKNOWN,
        а не hang (п.12). Вызывается ДО взятия authorizer lock, НИКОГДА под ним.
    :param profile_identity: привязка профиля (``str(store.data_dir)``).
    :param epoch: 32 bytes; None — сгенерировать ``secrets.token_bytes(32)``.
        Новый BackendService/process обязан получить новый epoch (п.1).

    Честная trust boundary (п.4, §7.6): это обычный Python API — same-UID caller
    технически МОЖЕТ вызвать ``issue_grant`` напрямую; backend НЕ отличит
    headless self-grant от Swift sheet. Защита — от accidental export
    (trusted-client consent protocol), НЕ запрет программных self-grant.
    """

    EPOCH_LEN = 32

    def __init__(
        self,
        *,
        read_snapshot: Callable[[], PolicySnapshot],
        profile_identity: str,
        epoch: Optional[bytes] = None,
    ) -> None:
        if not callable(read_snapshot):
            raise TypeError("read_snapshot обязан быть callable без аргументов")
        if type(profile_identity) is not str or not profile_identity:
            raise ValueError("profile_identity обязан быть непустой строкой")
        if epoch is None:
            resolved = secrets.token_bytes(self.EPOCH_LEN)
        else:
            # Строго только bytes длиной 32: bytearray/list/str отвергаются,
            # нулевой epoch (bytes(32)) — тоже отказ (невалидный secret).
            if type(epoch) is not bytes:
                raise ValueError("epoch обязан быть 32 bytes")
            if len(epoch) != self.EPOCH_LEN:
                raise ValueError("epoch обязан быть ровно 32 bytes")
            if epoch == b"\x00" * self.EPOCH_LEN:
                raise ValueError("epoch обязан быть ненулевым")
            resolved = epoch
        self._read_snapshot = read_snapshot
        self._profile_identity = profile_identity
        self._epoch = resolved
        self._mu = threading.Lock()
        self._generation = 0
        self._remembered: Optional[PolicyFingerprint] = None
        self._grants: dict[str, _GrantRecord] = {}
        self._receipts: dict[str, _ReceiptRecord] = {}
        self._consumed: set[str] = set()
        self._high_water: dict[str, int] = {}

    def __repr__(self) -> str:  # noqa: D105 — только счётчики, без секретов
        with self._mu:
            grants = len(self._grants)
            receipts = len(self._receipts)
            generation = self._generation
        return (
            "PlaintextExportAuthorizer(profile_identity=%r, "
            "policy_generation=%r, grants=%d, receipts=%d)"
            % (self._profile_identity, generation, grants, receipts)
        )

    # ── read-only свойства (без секретов) ──

    @property
    def epoch(self) -> bytes:
        """Backend epoch процесса (32 bytes, immutable — копия не нужна)."""
        return self._epoch

    @property
    def profile_identity(self) -> str:
        """Привязка профиля (путь, НЕ секрет)."""
        return self._profile_identity

    @property
    def policy_generation(self) -> int:
        """Локальная policy generation (bump на UNKNOWN/mismatch/privacy)."""
        with self._mu:
            return self._generation

    @property
    def active_grant_count(self) -> int:
        """Число живых grants (только счётчик — сами capability не отдаются)."""
        with self._mu:
            return len(self._grants)

    @property
    def active_receipt_count(self) -> int:
        """Число непотреблённых receipt'ов (только счётчик)."""
        with self._mu:
            return len(self._receipts)

    # ── issue / validate / revoke / consume ──

    def issue_grant(self, app_session_id: str) -> GrantResult:
        """Выдать session capability при KNOWN_ON + privacy false.

        UNKNOWN → отказ ``plaintext_policy_unavailable`` + очистка всего RAM
        (п.5). Privacy true → ``privacy_mode_active`` (п.18). KNOWN_OFF → отказ
        ``plaintext_confirmation_required`` (grant не нужен; путь —
        capabilityless ``validate``).

        Порядок сознательно snapshot-first (зеркально ``validate``): свежий
        snapshot и revoke (UNKNOWN/privacy/mismatch/profile) идут ДО проверки
        ``app_session_id``. Невалидная сессия тоже даёт отказ, но ТОЛЬКО ПОСЛЕ
        revoke — иначе спам ``issue_grant("")`` в UNKNOWN/privacy-окне оставлял
        бы старое согласие живым (FIX 1, оба ревью).
        """
        snapshot = self._fresh_snapshot()
        with self._mu:
            if snapshot.state is PolicyState.UNKNOWN:
                self._revoke_all_locked()
                return self._denied_grant(REASON_PLAINTEXT_POLICY_UNAVAILABLE)
            if snapshot.fingerprint is None:
                # Чужой read_snapshot-колбэк вернул KNOWN с None-fingerprint:
                # fail-closed как UNKNOWN, а не молча KNOWN (FIX 4).
                self._revoke_all_locked()
                return self._denied_grant(REASON_PLAINTEXT_POLICY_UNAVAILABLE)
            if self._profile_mismatch_locked(snapshot.fingerprint):
                # Mis-wired read_snapshot (чужой профиль): revoke, без grant.
                self._revoke_all_locked()
                return self._denied_grant(REASON_PLAINTEXT_SESSION_EXPIRED)
            if snapshot.privacy_mode_enabled:
                self._revoke_all_locked()
                self._remembered = snapshot.fingerprint
                return self._denied_grant(REASON_PRIVACY_MODE_ACTIVE)
            if (
                self._remembered is not None
                and snapshot.fingerprint != self._remembered
            ):
                # Policy сменилась между вызовами: прежние grants мертвы (п.15).
                # Для issue — revoke, но НОВЫЙ grant разрешён (свежее согласие);
                # невалидная сессия после revoke всё равно получит отказ ниже.
                self._revoke_all_locked()
            self._remembered = snapshot.fingerprint
            if not self._valid_session_id(app_session_id):
                return self._denied_grant(REASON_PLAINTEXT_SESSION_EXPIRED)
            if snapshot.state is PolicyState.KNOWN_OFF:
                return self._denied_grant(
                    REASON_PLAINTEXT_CONFIRMATION_REQUIRED
                )
            if snapshot.state is not PolicyState.KNOWN_ON:
                # Защита от будущего 4-го члена enum: fail-closed (п.3).
                self._revoke_all_locked()
                return self._denied_grant(REASON_PLAINTEXT_POLICY_UNAVAILABLE)
            capability = secrets.token_urlsafe(32)
            while capability in self._grants:
                capability = secrets.token_urlsafe(32)
            self._grants[capability] = _GrantRecord(
                app_session_id=app_session_id,
                capability=capability,
                generation=self._generation,
                fingerprint=snapshot.fingerprint,
            )
            return GrantResult(
                ok=True,
                capability=capability,
                epoch=self._epoch,
                policy_generation=self._generation,
                reason=None,
            )

    def validate(
        self,
        app_session_id: str,
        epoch: bytes,
        capability: Optional[str],
        expected_policy_generation: int,
        operation_seq: int,
        sink_kind: str,
    ) -> ValidateResult:
        """Проверить операцию и выдать одноразовый receipt для одной записи.

        Проверки по порядку (§7.6 + п.5/15/18):
          (а) свежий snapshot ПЕРВЫМ: UNKNOWN → revoke всего +
          ``plaintext_policy_unavailable``; privacy true → revoke +
          ``privacy_mode_active``; fingerprint mismatch → revoke прежних
          grants ДО validation + ``plaintext_session_expired``.
          Порядок сознательно snapshot-first (а не capability-first): только так
          п.5 «UNKNOWN немедленно очищает grants» держится безусловно, а не
          лишь для валидных capability.
          (б) capability известна и принадлежит этой сессии (ON требует
          capability; отсутствие при ON — ``plaintext_confirmation_required``;
          при подтверждённом OFF отсутствие допустимо, присутствие —
          ``plaintext_session_expired``);
          (в) epoch совпадает (replay epoch запрещён);
          (г) ``expected_policy_generation`` == текущей generation;
          (д) ``operation_seq`` — int ≥ 1 и строго больше high-water сессии;
          (е) успех: bump high-water + одноразовый receipt, bound к
          (session, epoch, generation, seq, sink_kind).
        """
        snapshot = self._fresh_snapshot()
        with self._mu:
            denied = self._denied_validate
            if snapshot.state is PolicyState.UNKNOWN:
                self._revoke_all_locked()
                return denied(REASON_PLAINTEXT_POLICY_UNAVAILABLE)
            if snapshot.fingerprint is None:
                # Чужой read_snapshot-колбэк: KNOWN с None-fingerprint → UNKNOWN.
                self._revoke_all_locked()
                return denied(REASON_PLAINTEXT_POLICY_UNAVAILABLE)
            if self._profile_mismatch_locked(snapshot.fingerprint):
                self._revoke_all_locked()
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            if snapshot.privacy_mode_enabled:
                self._revoke_all_locked()
                self._remembered = snapshot.fingerprint
                return denied(REASON_PRIVACY_MODE_ACTIVE)
            if (
                self._remembered is not None
                and snapshot.fingerprint != self._remembered
            ):
                self._revoke_all_locked()
                self._remembered = snapshot.fingerprint
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            self._remembered = snapshot.fingerprint
            if not self._valid_session_id(app_session_id):
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            if not isinstance(epoch, bytes) or epoch != self._epoch:
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            if (
                type(expected_policy_generation) is not int
                or expected_policy_generation != self._generation
            ):
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            if snapshot.state is PolicyState.KNOWN_ON:
                if capability is None:
                    return denied(REASON_PLAINTEXT_CONFIRMATION_REQUIRED)
                if type(capability) is not str:
                    return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
                grant = self._grants.get(capability)
                if grant is None or grant.app_session_id != app_session_id:
                    return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            elif snapshot.state is PolicyState.KNOWN_OFF:
                if capability is not None:
                    return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            else:
                self._revoke_all_locked()
                return denied(REASON_PLAINTEXT_POLICY_UNAVAILABLE)
            if type(operation_seq) is not int or operation_seq < 1:
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            if type(sink_kind) is not str or not sink_kind:
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            high = self._high_water.get(app_session_id, 0)
            if operation_seq <= high:
                return denied(REASON_PLAINTEXT_SESSION_EXPIRED)
            self._high_water[app_session_id] = operation_seq
            receipt = secrets.token_urlsafe(32)
            while receipt in self._receipts or receipt in self._consumed:
                receipt = secrets.token_urlsafe(32)
            self._receipts[receipt] = _ReceiptRecord(
                app_session_id=app_session_id,
                generation=self._generation,
                operation_seq=operation_seq,
                sink_kind=sink_kind,
            )
            return ValidateResult(
                ok=True,
                receipt=receipt,
                policy_generation=self._generation,
                reason=None,
            )

    def revoke(self, app_session_id: str, epoch: bytes, capability: str) -> bool:
        """Идемпотентный revoke ЭТОЙ сессии; чужие не трогает (п.19, §7.6).

        Неизвестная capability (а также чужой session/epoch и malformed-поля) —
        успех-идемпотент ``True``, НЕ ошибка. Single-session revoke НЕ bump'ает
        generation (иначе пострадали бы чужие сессии).
        """
        with self._mu:
            grant = None
            if type(capability) is str:
                grant = self._grants.get(capability)
            if (
                grant is not None
                and grant.app_session_id == app_session_id
                and isinstance(epoch, bytes)
                and epoch == self._epoch
            ):
                doomed_caps = [
                    cap
                    for cap, record in self._grants.items()
                    if record.app_session_id == app_session_id
                ]
                for cap in doomed_caps:
                    del self._grants[cap]
                doomed_receipts = [
                    token
                    for token, record in self._receipts.items()
                    if record.app_session_id == app_session_id
                ]
                for token in doomed_receipts:
                    del self._receipts[token]
                self._high_water.pop(app_session_id, None)
            return True

    def consume_receipt(self, receipt: str) -> bool:
        """Потребить одноразовый receipt: первый раз True, повтор/forge — False.

        Consume — часть авторизации, НЕ отдельная проверка: делает свежий
        snapshot ДО lock (тот же порядок, что issue/validate, п.12) и
        fail-closed False при UNKNOWN/privacy/mismatch/generation-уходе
        (+ revoke-all). Без этого validate ON → receipt R → политика в
        UNKNOWN/privacy/mismatch → consume(R) давал True, и будущий
        sink-писатель, доверяющий True, писал бы вопреки deny (FIX 2).
        """
        snapshot = self._fresh_snapshot()
        with self._mu:
            if snapshot.state is PolicyState.UNKNOWN:
                self._revoke_all_locked()
                return False
            if snapshot.fingerprint is None:
                self._revoke_all_locked()
                return False
            if self._profile_mismatch_locked(snapshot.fingerprint):
                self._revoke_all_locked()
                return False
            if snapshot.privacy_mode_enabled:
                self._revoke_all_locked()
                self._remembered = snapshot.fingerprint
                return False
            if (
                self._remembered is not None
                and snapshot.fingerprint != self._remembered
            ):
                self._revoke_all_locked()
                self._remembered = snapshot.fingerprint
                return False
            self._remembered = snapshot.fingerprint
            if type(receipt) is not str:
                return False
            if receipt in self._consumed:
                return False
            record = self._receipts.get(receipt)
            if record is None:
                return False
            if record.generation != self._generation:
                # Receipt из прошлой generation: fail-closed + очистка.
                self._revoke_all_locked()
                return False
            del self._receipts[receipt]
            self._consumed.add(receipt)
            return True

    # ── внутреннее ──

    def _fresh_snapshot(self) -> PolicySnapshot:
        """Свежий snapshot ВНЕ authorizer lock (порядок п.12).

        Колбэк уже ходит под ``StateStore._lock`` сам; исключение/не-тип
        колбэка — fail-closed UNKNOWN/provider-error, БЕЗ cache fallback.
        """
        try:
            snapshot = self._read_snapshot()
        except Exception:  # noqa: BLE001 — fail-closed, payload наружу не идёт
            return unknown_snapshot(REASON_UNKNOWN_PROVIDER_ERROR)
        if not isinstance(snapshot, PolicySnapshot):
            return unknown_snapshot(REASON_UNKNOWN_PROVIDER_ERROR)
        return snapshot

    def _revoke_all_locked(self) -> None:
        """Очистить весь RAM и bump'нуть generation. Только под ``self._mu``."""
        self._grants.clear()
        self._receipts.clear()
        self._consumed.clear()
        self._high_water.clear()
        self._generation += 1
        self._remembered = None

    @staticmethod
    def _resolved_identity(value: object) -> Optional[str]:
        """Резолв пути профиля для сравнения; None на malformed-значении.

        Wiring передаёт нерезолвленный ``str(data_dir)`` (на macOS /var),
        fingerprint — резолвленный (``/private/var``): сравниваем резолвленные
        обе стороны, иначе валидный путь ложно дал бы mismatch.
        """
        if type(value) is not str or not value:
            return None
        try:
            return str(Path(value).resolve())
        except Exception:  # noqa: BLE001 — fail-closed mismatch
            return None

    def _profile_mismatch_locked(self, fingerprint: PolicyFingerprint) -> bool:
        """True при mis-wired read_snapshot (чужой профиль). Только под lock."""
        expected = self._resolved_identity(self._profile_identity)
        actual = self._resolved_identity(fingerprint.profile_identity)
        if expected is None or actual is None:
            return True
        return expected != actual

    def _denied_grant(self, reason: str) -> GrantResult:
        """Отказ issue: токена нет, но epoch+generation отдаём (не секреты)."""
        return GrantResult(
            ok=False,
            capability=None,
            epoch=self._epoch,
            policy_generation=self._generation,
            reason=reason,
        )

    def _denied_validate(self, reason: str) -> ValidateResult:
        """Отказ validate: receipt нет. Только под ``self._mu``."""
        return ValidateResult(
            ok=False,
            receipt=None,
            policy_generation=self._generation,
            reason=reason,
        )

    @staticmethod
    def _valid_session_id(value: object) -> bool:
        """app_session_id — непустая строка (формат UUID проверяет slice 4/IPC)."""
        return type(value) is str and len(value) > 0
