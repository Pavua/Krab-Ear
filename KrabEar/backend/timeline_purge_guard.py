"""Межпроцессный барьер публикации timeline и очистки профиля."""
from contextlib import contextmanager
from functools import wraps
import fcntl
import os
import secrets
import stat
import threading
import time
from weakref import WeakValueDictionary

from backend.plaintext_export_authorization import PlaintextExportDenied


class TimelinePurgeGuard:
    _WAIT_SECONDS = 10.0
    _FILE = ".timeline-purge-guard"
    _SIZE = 32

    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.Lock()
        self._operation_local = threading.local()

    @contextmanager
    def _locked(self, *, repair=False):
        deadline = time.monotonic() + self._WAIT_SECONDS
        if not self.lock.acquire(timeout=self._WAIT_SECONDS):
            raise PlaintextExportDenied("timeline_purge_in_progress")
        fd = None
        try:
            path = os.path.join(self.directory, self._FILE)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(path, flags)
                created = False
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PlaintextExportDenied("timeline_purge_in_progress")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PlaintextExportDenied("timeline_purge_in_progress") from None
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise PlaintextExportDenied("timeline_purge_in_progress")
            if created:
                self._write_epoch(fd, secrets.token_bytes(self._SIZE))
            os.lseek(fd, 0, os.SEEK_SET)
            epoch = os.read(fd, self._SIZE + 1)
            if len(epoch) != self._SIZE:
                if not repair:
                    raise PlaintextExportDenied("timeline_purge_in_progress")
                epoch = secrets.token_bytes(self._SIZE)
                self._write_epoch(fd, epoch)
            yield fd, epoch
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                self.lock.release()

    @staticmethod
    def _write_epoch(fd, epoch):
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        if os.write(fd, epoch) != len(epoch):
            raise OSError("short timeline purge epoch write")
        os.fsync(fd)

    _PENDING = ".purge-pending"

    def recovery_pending(self):
        # lstat видит и повреждённый/висячий marker: неизвестное не значит clean.
        try:
            os.lstat(os.path.join(self.directory, self._PENDING))
        except FileNotFoundError:
            return False
        return True

    def _sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _begin_pending(self):
        path = os.path.join(self.directory, self._PENDING)
        flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("purge_recovery_required")
            # Сам факт существования означает intent; содержимое не имеет путей/PII.
            # Даже пустой файл после прерывания создания остаётся pending.
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync_directory()

    def run_purge(self, callback, *, only_pending=False, after_complete=None):
        # Backend wrapper и HistoryService образуют ОДНУ операцию. Только внешний
        # scope снимает intent, после RAM-cleanup и результата всех шагов.
        if getattr(self._operation_local, "active", False):
            return callback()
        with self._locked(repair=True) as (fd, _):
            if only_pending and not self.recovery_pending():
                return None
            self._begin_pending()
            self._write_epoch(fd, secrets.token_bytes(self._SIZE))
            self._operation_local.active = True
            try:
                result = callback()
                if (isinstance(result, dict) and result.get("complete") is True
                        and result.get("ok") is not False and not result.get("errors")):
                    self._sync_directory()
                    os.unlink(os.path.join(self.directory, self._PENDING))
                    try:
                        self._sync_directory()
                    except BaseException:
                        self._begin_pending()
                        raise
                    # Разморозить backup до отпускания lock: следующий purge
                    # не должен получить запоздалый clear_purged предыдущего.
                    if after_complete is not None:
                        after_complete()
                return result
            finally:
                self._operation_local.active = False

    def _deny_pending(self):
        if self.recovery_pending():
            raise PlaintextExportDenied("purge_recovery_required")

    @contextmanager
    def snapshot(self):
        with self._locked(repair=True) as (_, epoch):
            self._deny_pending()
            yield epoch

    @contextmanager
    def publish(self, generation):
        with self._locked() as (_, epoch):
            self._deny_pending()
            if epoch != generation:
                raise PlaintextExportDenied("timeline_purge_in_progress")
            yield

    @contextmanager
    def purge(self):
        with self._locked(repair=True) as (fd, _):
            self._write_epoch(fd, secrets.token_bytes(self._SIZE))
            yield


_registry_lock = threading.Lock()
_registry = WeakValueDictionary()


def timeline_guard(store):
    """Все StateStore одного пути делят локальный lock и файловый barrier."""
    with _registry_lock:
        guard = getattr(store, "_timeline_purge_guard", None)
        if isinstance(guard, TimelinePurgeGuard):
            return guard
        key = os.path.realpath(os.fspath(store.data_dir))
        guard = _registry.get(key)
        if guard is None:
            guard = TimelinePurgeGuard(key)
            _registry[key] = guard
        store._timeline_purge_guard = guard
        return guard


def guard_timeline_purge(method):
    @wraps(method)
    def wrapped(self, params):
        confirm = params.get("confirm")
        if confirm is not True and confirm != "PURGE_ALL":
            return method(self, params)
        return timeline_guard(self.store).run_purge(lambda: method(self, params))
    return wrapped
