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
            if len(epoch) != self._SIZE and not repair:
                raise PlaintextExportDenied("timeline_purge_in_progress")
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

    @contextmanager
    def snapshot(self):
        with self._locked() as (_, epoch):
            yield epoch

    @contextmanager
    def publish(self, generation):
        with self._locked() as (_, epoch):
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
        with timeline_guard(self.store).purge():
            return method(self, params)
    return wrapped
