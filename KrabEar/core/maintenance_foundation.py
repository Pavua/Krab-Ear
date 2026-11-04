"""Экспериментальная основа журнала maintenance, без authority и wiring.

Здесь намеренно нет открытия допуска, запуска coordinator или утверждения о
завершении работ. Использовать можно только в изолированных source-тестах.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sqlite3
import stat


class MaintenanceStoreError(RuntimeError):
    """Хранилище недоступно либо его целостность не подтверждена."""


_SCHEMA_VERSION = 1
_LOCK_NAME = ".maintenance.lock"
_SCHEMA_SQL = (
    "CREATE TABLE maintenance_state (version INTEGER NOT NULL, "
    "installation_id TEXT NOT NULL, admission TEXT NOT NULL)"
)

# Неопределённое закрытие сохраняет объект и известные ресурсы. Сам flock
# уже мог освободиться: production fence и recovery API здесь отсутствуют.
_FAILED_OPEN_HOLDS = []


class _FailedOpenHold:
    def __init__(self, connections, directory_fd, lock_fd):
        self.connections = connections
        self.directory_fd = directory_fd
        self.lock_fd = lock_fd
        self.close_uncertain = False

    def retry_close(self):
        while self.connections:
            self.connections[-1].close()
            self.connections.pop()
        error = None
        previous_uncertainty = self.close_uncertain
        self.close_uncertain = True
        for attribute in ("lock_fd", "directory_fd"):
            fd = getattr(self, attribute)
            if fd is not None:
                setattr(self, attribute, None)
                try:
                    os.close(fd)
                except BaseException as exc:
                    self.close_uncertain = True
                    if error is None:
                        error = exc
        if error is not None:
            raise error
        self.close_uncertain = previous_uncertainty
        if self.close_uncertain:
            raise MaintenanceStoreError("Результат закрытия дескриптора неизвестен")


def _abort_open(connections, directory_fd, lock_fd):
    hold = _FailedOpenHold(connections, directory_fd, lock_fd)
    try:
        hold.retry_close()
    except BaseException:
        _FAILED_OPEN_HOLDS.append(hold)


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise MaintenanceStoreError("Недопустимый идентификатор установки")
    return value


def _check_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise MaintenanceStoreError("Ожидался приватный обычный файл журнала")


def _lock_namespace(path: Path, *, create: bool) -> tuple[Path, int, int]:
    """Кооперативный lock приватного каталога; не защита от чужого same-UID кода."""
    if path.name in ("", ".", "..", _LOCK_NAME):
        raise MaintenanceStoreError("Недопустимое имя журнала")
    parent = path.parent.resolve(strict=True)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise MaintenanceStoreError("Ожидался приватный каталог журнала")
        flags = os.O_RDWR | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        lock_fd = os.open(_LOCK_NAME, flags, 0o600, dir_fd=directory_fd)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise MaintenanceStoreError("Ожидался приватный файл блокировки")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return parent / path.name, directory_fd, lock_fd
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)
        raise


def _connect_existing(path: Path, *, read_only: bool) -> sqlite3.Connection:
    _check_file(path)
    mode = "ro" if read_only else "rw"
    return sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True, timeout=2.0)


def _validate(connection: sqlite3.Connection, expected: str) -> None:
    integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
    if integrity != ("ok",):
        raise MaintenanceStoreError("Целостность журнала не подтверждена")
    objects = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    if objects != [("table", "maintenance_state", _SCHEMA_SQL)]:
        raise MaintenanceStoreError("Неизвестная схема журнала")
    rows = connection.execute(
        "SELECT version, installation_id, admission FROM maintenance_state"
    ).fetchall()
    if rows != [(_SCHEMA_VERSION, expected, "closed")]:
        raise MaintenanceStoreError("Неизвестное состояние журнала")


def _configure(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
        raise MaintenanceStoreError("Неподдерживаемый режим журнала")
    connection.execute("PRAGMA synchronous=EXTRA")
    if connection.execute("PRAGMA synchronous").fetchone() != (3,):
        raise MaintenanceStoreError("Настройка устойчивости не подтверждена")


class MaintenanceStore:
    """Только закрытое локальное состояние; не production control owner."""

    def __init__(self, connection: sqlite3.Connection, installation_id: str,
                 directory_fd: int, lock_fd: int):
        self._connection = connection
        self._installation_id = installation_id
        self._directory_fd = directory_fd
        self._lock_fd = lock_fd
        self._close_uncertain = False

    def status(self) -> dict:
        try:
            _validate(self._connection, self._installation_id)
        except (sqlite3.Error, OSError) as exc:
            raise MaintenanceStoreError("Состояние журнала неизвестно") from exc
        return {"installation_id": self._installation_id, "admission": "closed",
                "drain_authority": False}

    def close(self) -> None:
        if self._lock_fd is None:
            if self._close_uncertain:
                raise MaintenanceStoreError("Результат закрытия дескриптора неизвестен")
            return
        self._connection.close()
        error = None
        previous_uncertainty = self._close_uncertain
        self._close_uncertain = True
        for attribute in ("_lock_fd", "_directory_fd"):
            fd = getattr(self, attribute)
            if fd is not None:
                setattr(self, attribute, None)
                try:
                    os.close(fd)
                except BaseException as exc:
                    self._close_uncertain = True
                    if error is None:
                        error = exc
        if error is not None:
            raise error
        self._close_uncertain = previous_uncertainty
        if self._close_uncertain:
            raise MaintenanceStoreError("Результат закрытия дескриптора неизвестен")


def initialize_new(path: Path, installation_id: str) -> MaintenanceStore:
    installation_id = _identity(installation_id)
    path = Path(path)
    connections = []
    directory_fd = lock_fd = None
    try:
        path, directory_fd, lock_fd = _lock_namespace(path, create=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        connection = _connect_existing(path, read_only=False)
        connections.append(connection)
        _configure(connection)
        connection.execute(_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO maintenance_state VALUES (?, ?, ?)",
            (_SCHEMA_VERSION, installation_id, "closed"),
        )
        connection.commit()
        os.fsync(directory_fd)
        _validate(connection, installation_id)
        return MaintenanceStore(connection, installation_id, directory_fd, lock_fd)
    except BaseException as exc:
        _abort_open(connections, directory_fd, lock_fd)
        if isinstance(exc, (OSError, sqlite3.Error, MaintenanceStoreError)):
            raise MaintenanceStoreError("Не удалось создать закрытый журнал") from exc
        raise


def open_existing(path: Path, expected_installation_id: str) -> MaintenanceStore:
    installation_id = _identity(expected_installation_id)
    path = Path(path)
    connections = []
    directory_fd = lock_fd = None
    try:
        path, directory_fd, lock_fd = _lock_namespace(path, create=False)
        probe = _connect_existing(path, read_only=True)
        connections.append(probe)
        _validate(probe, installation_id)
        probe.close()
        connections.pop()
        connection = _connect_existing(path, read_only=False)
        connections.append(connection)
        _validate(connection, installation_id)
        _configure(connection)
        return MaintenanceStore(connection, installation_id, directory_fd, lock_fd)
    except BaseException as exc:
        _abort_open(connections, directory_fd, lock_fd)
        if isinstance(exc, (OSError, sqlite3.Error, MaintenanceStoreError)):
            raise MaintenanceStoreError("Не удалось открыть существующий журнал") from exc
        raise
