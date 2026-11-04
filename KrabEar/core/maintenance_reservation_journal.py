"""Изолированный эксперимент журнала; не выдаёт execution/drain authority.

Нет production OPEN, transport, auth, claim или настоящего control owner.
Применим только в тестовом приватном доверенном namespace.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import uuid

from core import maintenance_foundation as foundation


class JournalError(RuntimeError):
    """Журнал недоступен либо результат не подтверждён."""


class JournalConflict(JournalError):
    """Ключ или ожидаемая версия конфликтуют с сохранённым состоянием."""


class RecoveryRequired(JournalError):
    """Предыдущее состояние нельзя автоматически активировать."""


_CONTROL_SQL = (
    "CREATE TABLE control (version INTEGER NOT NULL, installation_id TEXT NOT NULL, "
    "journal_id TEXT NOT NULL, controller_epoch TEXT NOT NULL, "
    "control_revision INTEGER NOT NULL, admission TEXT NOT NULL)"
)
_LEDGER_SQL = (
    "CREATE TABLE command_ledger (sequence INTEGER PRIMARY KEY, principal TEXT NOT NULL, "
    "incarnation TEXT NOT NULL, request_key TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL, "
    "outcome TEXT NOT NULL, grant_id TEXT UNIQUE, result_revision INTEGER NOT NULL, "
    "UNIQUE(principal, incarnation, request_key), "
    "CHECK((operation = 'acquire' AND outcome = 'RESERVED' AND grant_id IS NOT NULL) OR "
    "(operation = 'acquire' AND outcome = 'DENIED_CLOSED' AND grant_id IS NULL) OR "
    "(operation = 'begin' AND outcome IN ('CLOSED', 'ALREADY_CLOSED') AND grant_id IS NULL)))"
)
_MAX_RECORDS = 1024
_ALLOWED_KINDS = frozenset({"stt", "tts"})


def _name(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or not value.isascii()
            or any(not (c.isalnum() or c in "_.:-") for c in value)):
        raise JournalError("Некорректный synthetic идентификатор")
    return value


def _result(outcome: str, grant_id: str | None, revision: int) -> dict:
    return {"outcome": outcome, "grant_id": grant_id, "control_revision": revision,
            "execution_authority": False, "drain_authority": False}


def _validate(connection: sqlite3.Connection, installation: str, *, allow_test_open: bool):
    if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
        raise JournalError("Целостность журнала не подтверждена")
    objects = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name LIMIT 5"
    ).fetchall()
    expected = [
        ("index", "sqlite_autoindex_command_ledger_1", None),
        ("index", "sqlite_autoindex_command_ledger_2", None),
        ("table", "command_ledger", _LEDGER_SQL),
        ("table", "control", _CONTROL_SQL),
    ]
    if objects != expected:
        raise JournalError("Неизвестная схема журнала")
    rows = connection.execute(
        "SELECT version, installation_id, journal_id, controller_epoch, "
        "control_revision, admission FROM control LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise JournalError("Неизвестное состояние журнала")
    version, found, journal_id, epoch, revision, admission = rows[0]
    if (version != 2 or found != installation or not isinstance(revision, int)
            or revision < 0 or admission not in {"CLOSED", "TEST_OPEN"}):
        raise JournalError("Неизвестное состояние журнала")
    _name(journal_id)
    _name(epoch)
    count = connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0]
    if count > _MAX_RECORDS:
        raise JournalError("Неподдерживаемый размер журнала")
    entries = connection.execute(
        "SELECT principal, incarnation, request_key, operation, payload, outcome, grant_id, "
        "result_revision FROM command_ledger ORDER BY sequence LIMIT ?", (_MAX_RECORDS + 1,),
    ).fetchall()
    if len(entries) > _MAX_RECORDS:
        raise JournalError("Неподдерживаемый размер журнала")
    for principal, incarnation, key, stored_operation, encoded, outcome, grant_id, result_revision in entries:
        for value in (principal, incarnation, key):
            _name(value)
        if (not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 2048
                or not isinstance(result_revision, int) or not 0 <= result_revision <= revision):
            raise JournalError("Неизвестная запись журнала")
        try:
            payload = json.loads(encoded)
        except (ValueError, RecursionError) as exc:
            raise JournalError("Неизвестные параметры записи") from exc
        if not isinstance(payload, dict) or json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ) != encoded:
            raise JournalError("Неизвестные параметры записи")
        operation = payload.get("operation")
        if operation != stored_operation:
            raise JournalError("Несовпадающая операция записи")
        _name(payload.get("controller_epoch"))
        if operation == "acquire":
            if (set(payload) != {"operation", "controller_epoch", "kind"}
                    or not isinstance(payload["kind"], str)
                    or payload["kind"] not in _ALLOWED_KINDS
                    or outcome not in {"RESERVED", "DENIED_CLOSED"}):
                raise JournalError("Неизвестное решение acquire")
        elif operation == "begin":
            expected = payload.get("expected_revision")
            if (set(payload) != {"operation", "controller_epoch", "expected_revision"}
                    or type(expected) is not int or expected < 0
                    or outcome not in {"CLOSED", "ALREADY_CLOSED"}
                    or result_revision != expected + (outcome == "CLOSED")):
                raise JournalError("Неизвестное решение begin")
        else:
            raise JournalError("Неизвестная операция")
        if outcome == "RESERVED":
            try:
                if not isinstance(grant_id, str) or str(uuid.UUID(grant_id)) != grant_id:
                    raise ValueError("noncanonical grant")
            except ValueError as exc:
                raise JournalError("Неизвестный grant") from exc
        elif grant_id is not None:
            raise JournalError("Неожиданный grant")
    if admission == "TEST_OPEN" and not allow_test_open:
        raise RecoveryRequired("TEST_OPEN требует отдельного восстановления")
    return epoch, revision, admission


class ReservationJournal(foundation.MaintenanceStore):
    """Single-threaded эксперимент; все capabilities намеренно отключены."""

    def __init__(self, connection, installation, directory_fd, lock_fd, *, active):
        super().__init__(connection, installation, directory_fd, lock_fd)
        self._active = active
        self._poisoned = False

    def status(self) -> dict:
        try:
            _, revision, admission = _validate(
                self._connection, self._installation_id, allow_test_open=self._active,
            )
            count, outstanding = self._connection.execute(
                "SELECT COUNT(*), COUNT(grant_id) FROM command_ledger"
            ).fetchone()
            return {"admission": admission, "control_revision": revision,
                    "ledger_count": count, "outstanding_count": outstanding,
                    "unknown": self._poisoned, "execution_authority": False,
                    "drain_authority": False}
        except BaseException as exc:
            self._poisoned = True
            if isinstance(exc, (sqlite3.Error, OSError, JournalError)):
                raise JournalError("Журнал недоступен") from exc
            raise

    def acquire(self, principal, incarnation, request_key, controller_epoch, kind):
        if not isinstance(kind, str) or kind not in _ALLOWED_KINDS:
            raise JournalError("Неизвестный synthetic kind")
        payload = {"operation": "acquire", "controller_epoch": _name(controller_epoch), "kind": kind}
        return self._command(principal, incarnation, request_key, payload)

    def begin_maintenance(self, principal, incarnation, request_key, controller_epoch,
                          expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise JournalError("Некорректная ревизия")
        payload = {"operation": "begin", "controller_epoch": _name(controller_epoch),
                   "expected_revision": expected_revision}
        return self._command(principal, incarnation, request_key, payload)

    def _command(self, principal, incarnation, request_key, payload):
        principal, incarnation, request_key = map(_name, (principal, incarnation, request_key))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2048:
            raise JournalError("Слишком большие параметры")
        if not self._active or self._poisoned:
            raise JournalError("Журнал доступен только для просмотра или UNKNOWN")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            epoch, revision, admission = _validate(
                connection, self._installation_id, allow_test_open=True,
            )
            prior = connection.execute(
                "SELECT payload, outcome, grant_id, result_revision FROM command_ledger "
                "WHERE principal=? AND incarnation=? AND request_key=?",
                (principal, incarnation, request_key),
            ).fetchone()
            if prior is not None:
                if prior[0] != encoded:
                    raise JournalConflict("Ключ уже использован с другими параметрами")
                connection.rollback()
                return _result(prior[1], prior[2], prior[3])
            if payload["controller_epoch"] != epoch:
                raise JournalConflict("Несовпадающий controller epoch")
            count = connection.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0]
            if count >= _MAX_RECORDS:
                raise JournalError("Лимит журнала достигнут")
            grant_id = None
            if payload["operation"] == "acquire":
                outcome = "DENIED_CLOSED"
                if admission == "TEST_OPEN":
                    outcome, grant_id = "RESERVED", str(uuid.uuid4())
            else:
                if payload["expected_revision"] != revision:
                    raise JournalConflict("Несовпадающая ревизия")
                outcome = "ALREADY_CLOSED"
                if admission == "TEST_OPEN":
                    revision += 1
                    connection.execute(
                        "UPDATE control SET admission='CLOSED', control_revision=?", (revision,),
                    )
                    outcome = "CLOSED"
            connection.execute(
                "INSERT INTO command_ledger "
                "(principal, incarnation, request_key, operation, payload, outcome, grant_id, result_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (principal, incarnation, request_key, payload["operation"], encoded, outcome, grant_id, revision),
            )
            connection.commit()
            return _result(outcome, grant_id, revision)
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
    path = Path(path)
    installation = _name(installation)
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
            connection.execute(_LEDGER_SQL)
            connection.execute("INSERT INTO control VALUES (?, ?, ?, ?, ?, ?)",
                               (2, installation, str(uuid.uuid4()), _name(epoch), 0, "CLOSED"))
            connection.commit()
            os.fsync(directory_fd)
        _validate(connection, installation, allow_test_open=False)
        return ReservationJournal(connection, installation, directory_fd, lock_fd, active=create)
    except BaseException as exc:
        foundation._abort_open(connections, directory_fd, lock_fd)
        if isinstance(exc, RecoveryRequired):
            raise
        if isinstance(exc, (OSError, sqlite3.Error, foundation.MaintenanceStoreError, JournalError)):
            raise JournalError("Не удалось открыть экспериментальный журнал") from exc
        raise


def create_journal(path, installation_id, controller_epoch):
    return _open(path, installation_id, _name(controller_epoch), create=True)


def open_journal(path, installation_id):
    return _open(path, installation_id, None, create=False)
