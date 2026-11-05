"""Synthetic scoped claims; не production admission и не право на execution.

Schema v4 не открывает и не мигрирует старые схемы. Нет transport/auth, executor, terminal,
recovery или drain. Все файлы должны быть тестовыми в доверенном namespace.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from core import maintenance_foundation as foundation
from core.maintenance_reservation_journal import JournalError, JournalConflict, RecoveryRequired, _name


_CONTROL_SQL = (
    "CREATE TABLE control (version INTEGER NOT NULL, installation_id TEXT NOT NULL, "
    "journal_id TEXT NOT NULL, controller_epoch TEXT NOT NULL, "
    "control_revision INTEGER NOT NULL, admission TEXT NOT NULL)"
)
_GRANTS_SQL = (
    "CREATE TABLE scoped_grants (grant_id TEXT PRIMARY KEY, principal TEXT NOT NULL, "
    "incarnation TEXT NOT NULL, executor TEXT NOT NULL, kind TEXT NOT NULL, claim_id TEXT UNIQUE, "
    "revoked INTEGER NOT NULL CHECK(revoked IN (0, 1)))"
)
_LEDGER_SQL = (
    "CREATE TABLE command_ledger (sequence INTEGER PRIMARY KEY, principal TEXT NOT NULL, "
    "incarnation TEXT NOT NULL, request_key TEXT NOT NULL, operation TEXT NOT NULL, "
    "payload TEXT NOT NULL, outcome TEXT NOT NULL, grant_id TEXT, claim_id TEXT, "
    "result_revision INTEGER NOT NULL, UNIQUE(principal, incarnation, request_key), "
    "CHECK((operation = 'reserve' AND outcome = 'RESERVED' AND grant_id IS NOT NULL AND claim_id IS NULL) OR "
    "(operation = 'reserve' AND outcome = 'DENIED_CLOSED' AND grant_id IS NULL AND claim_id IS NULL) OR "
    "(operation = 'begin' AND outcome IN ('CLOSED', 'ALREADY_CLOSED') AND grant_id IS NULL AND claim_id IS NULL) OR "
    "(operation = 'claim' AND outcome = 'CLAIMED' AND grant_id IS NOT NULL AND claim_id IS NOT NULL) OR "
    "(operation = 'revoke' AND outcome IN ('REVOKED', 'CLAIMED_UNKNOWN') AND grant_id IS NOT NULL AND claim_id IS NULL) OR "
    "(operation = 'seal' AND outcome = 'SEALED' AND grant_id IS NULL AND claim_id IS NULL)))"
)
_MAX_RECORDS = 1024


class _SyntheticPermit:
    """Локальный тестовый объект, не production capability."""

    __slots__ = ("_scope", "_lock", "_used", "_creator_pid")

    def __init__(self, journal_id, grant_id, claim_id, executor, kind):
        self._scope = (journal_id, grant_id, claim_id, executor, kind)
        self._lock = threading.Lock()
        self._used = False
        self._creator_pid = os.getpid()

    @property
    def journal_id(self):
        return self._scope[0]

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce_ex__(self, protocol):
        raise TypeError("Synthetic permit нельзя сериализовать")

    def consume(self, journal_id, grant_id, claim_id, executor, kind):
        if os.getpid() != self._creator_pid:
            return False
        with self._lock:
            if self._used or (journal_id, grant_id, claim_id, executor, kind) != self._scope:
                return False
            self._used = True
            return True


def _response(outcome, grant_id, claim_id, revision, *, permit=None):
    return {"outcome": outcome, "grant_id": grant_id, "claim_id": claim_id,
            "control_revision": revision, "fresh": permit is not None, "permit": permit,
            "production_execution_authority": False, "drain_authority": False}


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as exc:
        raise JournalError("Некорректный synthetic UUID") from exc
    return value


def _validate(connection, installation, *, allow_test_open):
    if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
        raise JournalError("Целостность не подтверждена")
    objects = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name LIMIT 8"
    ).fetchall()
    expected = [
        ("index", "sqlite_autoindex_command_ledger_1", None),
        ("index", "sqlite_autoindex_scoped_grants_1", None),
        ("index", "sqlite_autoindex_scoped_grants_2", None),
        ("table", "command_ledger", _LEDGER_SQL),
        ("table", "control", _CONTROL_SQL),
        ("table", "scoped_grants", _GRANTS_SQL),
    ]
    if objects != expected:
        raise JournalError("Неизвестная schema v4")
    rows = connection.execute(
        "SELECT version, installation_id, journal_id, controller_epoch, control_revision, admission "
        "FROM control LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise JournalError("Неизвестный control")
    version, found, journal_id, epoch, revision, admission = rows[0]
    if (version != 4 or found != installation or not isinstance(revision, int) or revision < 0
            or admission not in {"CLOSED", "TEST_OPEN", "SEALED"}):
        raise JournalError("Неизвестное состояние")
    _uuid(journal_id)
    _name(epoch)
    if admission == "TEST_OPEN" and not allow_test_open:
        raise RecoveryRequired("TEST_OPEN требует восстановления")
    # До materialization проверяем count и предел отдельных значений SQLite.
    if any(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > _MAX_RECORDS
           for table in ("command_ledger", "scoped_grants")):
        raise JournalError("Лимит журнала превышен")
    grant_rows = connection.execute(
        "SELECT grant_id, principal, incarnation, executor, kind, claim_id, revoked "
        "FROM scoped_grants ORDER BY grant_id LIMIT ?", (_MAX_RECORDS + 1,),
    ).fetchall()
    grants = {}
    for grant_id, principal, incarnation, executor, kind, claim_id, revoked in grant_rows:
        _uuid(grant_id)
        for value in (principal, incarnation, executor):
            _name(value)
        if not isinstance(kind, str) or kind not in {"stt", "tts"}:
            raise JournalError("Неизвестный scope grant")
        if claim_id is not None:
            _uuid(claim_id)
        if revoked not in (0, 1) or (revoked and claim_id is not None):
            raise JournalError("Неизвестный отзыв grant")
        grants[grant_id] = (principal, incarnation, executor, kind, claim_id, revoked)
    entries = connection.execute(
        "SELECT principal, incarnation, request_key, operation, payload, outcome, "
        "grant_id, claim_id, result_revision FROM command_ledger ORDER BY sequence LIMIT ?",
        (_MAX_RECORDS + 1,),
    ).fetchall()
    reserved, claimed, revoked = set(), set(), set()
    history_admission = None
    history_revision = 0
    history_has_begin = False
    for principal, incarnation, key, operation, encoded, outcome, grant_id, claim_id, result_revision in entries:
        for value in (principal, incarnation, key):
            _name(value)
        if (not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 2048
                or not isinstance(result_revision, int) or not 0 <= result_revision <= revision):
            raise JournalError("Неизвестная запись")
        try:
            payload = json.loads(encoded)
        except (ValueError, RecursionError) as exc:
            raise JournalError("Неизвестные параметры") from exc
        if (not isinstance(payload, dict) or json.dumps(payload, sort_keys=True, separators=(",", ":")) != encoded
                or payload.get("operation") != operation or payload.get("controller_epoch") != epoch):
            raise JournalError("Несовпадающий payload")
        if operation == "reserve":
            if (set(payload) != {"operation", "controller_epoch", "executor", "kind"}
                    or not isinstance(payload.get("kind"), str) or payload["kind"] not in {"stt", "tts"}):
                raise JournalError("Неизвестный reserve")
            _name(payload["executor"])
            if outcome == "RESERVED":
                if history_admission in {"CLOSED", "SEALED"}:
                    raise JournalError("Reserve после закрытия")
                history_admission = "TEST_OPEN"
                _uuid(grant_id)
                grant = grants.get(grant_id)
                if (grant is None or grant_id in reserved or claim_id is not None
                        or grant[:4] != (principal, incarnation, payload["executor"], payload["kind"])):
                    raise JournalError("Несовпадающий grant")
                reserved.add(grant_id)
            elif outcome == "DENIED_CLOSED" and grant_id is None and claim_id is None:
                if history_admission == "TEST_OPEN":
                    raise JournalError("Отказ при открытом допуске")
                if history_admission != "SEALED":
                    history_admission = "CLOSED"
            else:
                raise JournalError("Неизвестный исход reserve")
        elif operation == "begin":
            expected_revision = payload.get("expected_revision")
            if (history_admission == "SEALED"
                    or set(payload) != {"operation", "controller_epoch", "expected_revision"}
                    or type(expected_revision) is not int or expected_revision < 0
                    or outcome not in {"CLOSED", "ALREADY_CLOSED"}
                    or result_revision != expected_revision + (outcome == "CLOSED")
                    or grant_id is not None or claim_id is not None):
                raise JournalError("Неизвестный begin")
            if expected_revision != history_revision:
                raise JournalError("Revision begin не соответствует истории")
            if outcome == "CLOSED":
                if history_admission == "CLOSED":
                    raise JournalError("Повторное закрытие без reopen")
                history_revision += 1
            elif history_admission == "TEST_OPEN":
                raise JournalError("ALREADY_CLOSED при открытом допуске")
            history_admission = "CLOSED"
            history_has_begin = True
        elif operation == "claim":
            if set(payload) != {"operation", "controller_epoch", "executor", "grant_id"}:
                raise JournalError("Неизвестный claim")
            _name(payload["executor"])
            _uuid(grant_id)
            _uuid(claim_id)
            grant = grants.get(grant_id)
            if (outcome != "CLAIMED" or grant_id not in reserved or grant_id in claimed or grant_id in revoked
                    or payload["grant_id"] != grant_id or grant is None
                    or grant[:3] != (principal, incarnation, payload["executor"]) or grant[4] != claim_id):
                raise JournalError("Несовпадающий claim")
            claimed.add(grant_id)
        elif operation == "revoke":
            if (set(payload) != {"operation", "controller_epoch", "grant_id"}
                    or payload["grant_id"] != grant_id or grant_id not in reserved
                    or history_admission != "CLOSED" or claim_id is not None):
                raise JournalError("Неизвестный revoke")
            grant = grants[grant_id]
            if grant[:2] != (principal, incarnation):
                raise JournalError("Несовпадающий владелец grant")
            if outcome == "REVOKED" and grant_id not in claimed and grant_id not in revoked:
                revoked.add(grant_id)
            elif outcome != "CLAIMED_UNKNOWN" or grant_id not in claimed:
                raise JournalError("Некорректный исход revoke")
        elif operation == "seal":
            if (set(payload) != {"operation", "controller_epoch", "expected_revision"}
                    or type(payload["expected_revision"]) is not int
                    or payload["expected_revision"] != history_revision
                    or history_admission != "CLOSED" or not history_has_begin or outcome != "SEALED"
                    or grant_id is not None or claim_id is not None):
                raise JournalError("Неизвестный seal")
            revoked.update(reserved - claimed)
            history_revision += 1
            history_admission = "SEALED"
        else:
            raise JournalError("Неизвестная операция")
        if result_revision != history_revision:
            raise JournalError("Revision записи не соответствует истории")
    if (reserved != set(grants) or claimed != {key for key, value in grants.items() if value[4] is not None}
            or revoked != {key for key, value in grants.items() if value[5] == 1}):
        raise JournalError("Потеряна связь grant и ledger")
    if (revision != history_revision or (history_admission is not None and admission != history_admission)
            or (admission == "SEALED" and history_admission != "SEALED")):
        raise JournalError("Control не соответствует истории")
    return journal_id, epoch, revision, admission


class ClaimJournal(foundation.MaintenanceStore):
    """Single-threaded synthetic actor; только локальный учебный permit."""

    def __init__(self, connection, installation, directory_fd, lock_fd, *, active):
        super().__init__(connection, installation, directory_fd, lock_fd)
        self._active = active
        self._poisoned = False
        self._creator_pid = os.getpid()

    def _check_process(self):
        if os.getpid() != self._creator_pid:
            raise JournalError("Унаследованный после fork journal не используется")

    def close(self):
        self._check_process()
        super().close()

    def status(self):
        self._check_process()
        try:
            _, _, revision, admission = _validate(
                self._connection, self._installation_id, allow_test_open=self._active,
            )
            count, claimed = self._connection.execute(
                "SELECT COUNT(*), COUNT(claim_id) FROM scoped_grants"
            ).fetchone()
            return {"admission": admission, "control_revision": revision, "outstanding_count": count,
                    "unknown": self._poisoned or claimed > 0 or not self._active,
                    "inspection_only": not self._active, "production_execution_authority": False,
                    "drain_authority": False}
        except BaseException as exc:
            self._poisoned = True
            if isinstance(exc, (sqlite3.Error, OSError, JournalError)):
                raise JournalError("Журнал недоступен") from exc
            raise

    def reserve_leaf(self, principal, incarnation, key, controller_epoch, executor, kind):
        if not isinstance(kind, str) or kind not in {"stt", "tts"}:
            raise JournalError("Неизвестный synthetic kind")
        return self._command(principal, incarnation, key, {
            "operation": "reserve", "controller_epoch": _name(controller_epoch),
            "executor": _name(executor), "kind": kind,
        })

    def begin_maintenance(self, principal, incarnation, key, controller_epoch, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise JournalError("Некорректная revision")
        return self._command(principal, incarnation, key, {
            "operation": "begin", "controller_epoch": _name(controller_epoch),
            "expected_revision": expected_revision,
        })

    def claim_leaf(self, principal, incarnation, key, controller_epoch, executor, grant_id):
        return self._command(principal, incarnation, key, {
            "operation": "claim", "controller_epoch": _name(controller_epoch),
            "executor": _name(executor), "grant_id": _uuid(grant_id),
        })

    def revoke_unclaimed(self, principal, incarnation, key, controller_epoch, grant_id):
        return self._command(principal, incarnation, key, {
            "operation": "revoke", "controller_epoch": _name(controller_epoch), "grant_id": _uuid(grant_id),
        })

    def seal(self, principal, incarnation, key, controller_epoch, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise JournalError("Некорректная revision")
        return self._command(principal, incarnation, key, {
            "operation": "seal", "controller_epoch": _name(controller_epoch),
            "expected_revision": expected_revision,
        })

    def _command(self, principal, incarnation, key, payload):
        self._check_process()
        principal, incarnation, key = map(_name, (principal, incarnation, key))
        if not self._active or self._poisoned:
            raise JournalError("Только inspection или UNKNOWN")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2048:
            raise JournalError("Слишком большие параметры")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            journal_id, epoch, revision, admission = _validate(
                connection, self._installation_id, allow_test_open=True,
            )
            prior = connection.execute(
                "SELECT payload, outcome, grant_id, claim_id, result_revision FROM command_ledger "
                "WHERE principal=? AND incarnation=? AND request_key=?",
                (principal, incarnation, key),
            ).fetchone()
            if prior is not None:
                if prior[0] != encoded:
                    raise JournalConflict("Ключ занят другими параметрами")
                connection.rollback()
                return _response(prior[1], prior[2], prior[3], prior[4])
            if payload["controller_epoch"] != epoch:
                raise JournalConflict("Несовпадающий epoch")
            if connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] >= _MAX_RECORDS:
                raise JournalError("Журнал заполнен")
            operation, grant_id, claim_id, claim_kind = payload["operation"], None, None, None
            if operation == "reserve":
                outcome = "DENIED_CLOSED"
                if admission == "TEST_OPEN":
                    outcome, grant_id = "RESERVED", str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO scoped_grants VALUES (?, ?, ?, ?, ?, NULL, 0)",
                        (grant_id, principal, incarnation, payload["executor"], payload["kind"]),
                    )
            elif operation == "begin":
                if payload["expected_revision"] != revision:
                    raise JournalConflict("Несовпадающая revision")
                if admission == "SEALED":
                    raise JournalConflict("Admission запечатан")
                outcome = "ALREADY_CLOSED"
                if admission == "TEST_OPEN":
                    revision += 1
                    connection.execute("UPDATE control SET admission='CLOSED', control_revision=?", (revision,))
                    outcome = "CLOSED"
            elif operation == "revoke":
                grant_id = payload["grant_id"]
                grant = connection.execute(
                    "SELECT principal, incarnation, claim_id, revoked FROM scoped_grants WHERE grant_id=?",
                    (grant_id,),
                ).fetchone()
                if admission != "CLOSED" or grant is None or grant[:2] != (principal, incarnation) or grant[3]:
                    raise JournalConflict("Отзыв недоступен")
                outcome = "CLAIMED_UNKNOWN" if grant[2] is not None else "REVOKED"
                if outcome == "REVOKED":
                    connection.execute("UPDATE scoped_grants SET revoked=1 WHERE grant_id=?", (grant_id,))
            elif operation == "seal":
                has_begin = connection.execute(
                    "SELECT 1 FROM command_ledger WHERE operation='begin' LIMIT 1"
                ).fetchone() is not None
                if admission != "CLOSED" or payload["expected_revision"] != revision or not has_begin:
                    raise JournalConflict("Seal недоступен")
                connection.execute("UPDATE scoped_grants SET revoked=1 WHERE claim_id IS NULL")
                revision += 1
                connection.execute("UPDATE control SET admission='SEALED', control_revision=?", (revision,))
                outcome = "SEALED"
            else:
                grant_id = payload["grant_id"]
                grant = connection.execute(
                    "SELECT principal, incarnation, executor, kind, claim_id, revoked FROM scoped_grants WHERE grant_id=?",
                    (grant_id,),
                ).fetchone()
                if (grant is None or grant[:3] != (principal, incarnation, payload["executor"])
                        or grant[4] is not None or grant[5]):
                    raise JournalConflict("Grant отсутствует, занят или принадлежит другому scope")
                claim_kind = grant[3]
                outcome, claim_id = "CLAIMED", str(uuid.uuid4())
                connection.execute("UPDATE scoped_grants SET claim_id=? WHERE grant_id=?", (claim_id, grant_id))
            connection.execute(
                "INSERT INTO command_ledger "
                "(principal, incarnation, request_key, operation, payload, outcome, grant_id, claim_id, result_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (principal, incarnation, key, operation, encoded, outcome, grant_id, claim_id, revision),
            )
            connection.commit()
            permit = _SyntheticPermit(journal_id, grant_id, claim_id, payload["executor"], claim_kind) if claim_id else None
            return _response(outcome, grant_id, claim_id, revision, permit=permit)
        except JournalConflict:
            self._poisoned = True
            try:
                connection.rollback()
            except BaseException as exc:
                if isinstance(exc, (sqlite3.Error, OSError)):
                    raise JournalError("Откат конфликта не подтверждён") from exc
                raise
            self._poisoned = False
            raise
        except BaseException as exc:
            self._poisoned = True
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            if isinstance(exc, (sqlite3.Error, OSError, JournalError)):
                raise JournalError("Результат транзакции неизвестен") from exc
            raise


def _open(path, installation, epoch, *, create):
    path, installation = Path(path), _name(installation)
    connections = []
    directory_fd = lock_fd = None
    try:
        path, directory_fd, lock_fd = foundation._lock_namespace(path, create=create)
        if create:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        connection = foundation._connect_existing(path, read_only=not create)
        connections.append(connection)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
        if connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) > 8192:
            raise JournalError("Предел SQLite не подтверждён")
        if create:
            foundation._configure(connection)
            connection.execute(_CONTROL_SQL)
            connection.execute(_GRANTS_SQL)
            connection.execute(_LEDGER_SQL)
            connection.execute("INSERT INTO control VALUES (?, ?, ?, ?, ?, ?)",
                               (4, installation, str(uuid.uuid4()), _name(epoch), 0, "CLOSED"))
            connection.commit()
            os.fsync(directory_fd)
        _validate(connection, installation, allow_test_open=False)
        return ClaimJournal(connection, installation, directory_fd, lock_fd, active=create)
    except BaseException as exc:
        foundation._abort_open(connections, directory_fd, lock_fd)
        if isinstance(exc, RecoveryRequired):
            raise
        if isinstance(exc, (sqlite3.Error, OSError, foundation.MaintenanceStoreError, JournalError)):
            raise JournalError("Не удалось открыть synthetic claim journal") from exc
        raise


def create_claim_journal(path, installation_id, controller_epoch):
    return _open(path, installation_id, _name(controller_epoch), create=True)


def open_claim_journal(path, installation_id):
    return _open(path, installation_id, None, create=False)
