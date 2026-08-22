"""Атомарное владение backend Unix-socket endpoint'ом.

Спека: docs/superpowers/specs/2026-08-22-socket-ownership-design.md.

Единый владелец трёх вещей, которыми раньше порознь занимались
``IPCServer`` и ``StartupDiagnostics``:

1. **Canonical path** — одна формула нормализации пути сокета, чтобы
   parent-symlink алиасы попадали в один lock-domain, а конечный symlink
   оставался различим (fail-closed ``OCCUPIED``).
2. **Read-only probe** — классификация состояния pathname
   (``MISSING`` / ``LISTENING`` / ``STALE`` / ``OCCUPIED``) live-connect'ом;
   неоднозначность (timeout, EACCES, EAGAIN, не-сокет) — всегда ``OCCUPIED``,
   никогда не ``STALE``: удалять можно только доказанно мёртвый сокет.
3. **Sidecar flock claim** — ``<canonical>.lock`` под ``flock(LOCK_EX|LOCK_NB)``,
   захватывается в ``main()`` ДО тяжёлых side effects и держится до конца
   ``_shutdown_backend``; смерть процесса снимает flock средствами ядра.

🔴 Sidecar НИКОГДА не удаляется (в т.ч. purge): unlink залоченного файла
позволил бы двум процессам держать flock на РАЗНЫХ inode под одним pathname.
"""

from __future__ import annotations

import errno
import os
import socket
import stat as stat_mod
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_PROBE_CONNECT_TIMEOUT_SEC = 0.5


class SocketPathStatus(str, Enum):
    MISSING = "missing"
    LISTENING = "listening"
    STALE = "stale"
    OCCUPIED = "occupied"


class SocketOwnershipState(str, Enum):
    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    LISTENING = "listening"


class SocketOwnershipError(RuntimeError):
    """Базовый класс ошибок владения socket endpoint'ом."""


class SocketAlreadyOwnedError(SocketOwnershipError):
    """Endpoint уже принадлежит другому живому процессу (flock или live listener)."""


class UnsafeSocketPathError(SocketOwnershipError):
    """Путь/lock-файл небезопасен — startup обязан отказаться, ничего не удаляя."""


@dataclass(frozen=True)
class SocketIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class SocketPathProbe:
    status: SocketPathStatus
    identity: SocketIdentity | None
    error: str | None = None


@dataclass(frozen=True)
class SocketOwnershipSnapshot:
    socket_path: Path
    state: SocketOwnershipState
    bound_identity: SocketIdentity | None


def canonical_socket_path(path: Path | str) -> Path:
    """Нормализует путь сокета: parent — resolve(strict=False), имя — как есть.

    Parent-алиасы (symlink на каталог, относительные пути) схлопываются в один
    lock-domain; конечный symlink НЕ разрешается — probe увидит его lstat'ом
    и классифицирует ``OCCUPIED``.
    """
    p = Path(path).expanduser()
    parent = p.parent if str(p.parent) not in ("", ".") else Path.cwd()
    resolved_parent = Path(os.path.abspath(parent)).resolve(strict=False)
    return resolved_parent / p.name


def default_socket_path(data_dir: Path | str) -> Path:
    """Канонический endpoint backend'а для данного data dir (чистая функция)."""
    return Path(data_dir) / "krabear.sock"


def _identity_from_stat(st: os.stat_result) -> SocketIdentity:
    return SocketIdentity(device=st.st_dev, inode=st.st_ino)


def probe_unix_socket_path(path: Path | str) -> SocketPathProbe:
    """Read-only классификация pathname. Ничего не удаляет и не создаёт."""
    p = Path(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return SocketPathProbe(status=SocketPathStatus.MISSING, identity=None)
    except OSError as exc:
        return SocketPathProbe(
            status=SocketPathStatus.OCCUPIED, identity=None, error=str(exc)
        )

    identity = _identity_from_stat(st)
    if not stat_mod.S_ISSOCK(st.st_mode):
        # Regular file, symlink (lstat!), каталог, fifo — всё fail-closed.
        return SocketPathProbe(
            status=SocketPathStatus.OCCUPIED, identity=identity, error="not a socket"
        )

    probe_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe_sock.settimeout(_PROBE_CONNECT_TIMEOUT_SEC)
        try:
            probe_sock.connect(str(p))
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                return SocketPathProbe(
                    status=SocketPathStatus.STALE, identity=identity
                )
            # timeout (socket.timeout — подкласс OSError без errno), EACCES,
            # EAGAIN, ENOENT-гонка и прочая неоднозначность — fail-closed.
            return SocketPathProbe(
                status=SocketPathStatus.OCCUPIED, identity=identity, error=str(exc)
            )
        return SocketPathProbe(status=SocketPathStatus.LISTENING, identity=identity)
    finally:
        try:
            probe_sock.close()
        except OSError:
            pass


class SocketOwnershipClaim:
    """Sidecar-flock владение одним canonical socket endpoint'ом.

    Жизненный цикл: ``acquire()`` → ``prepare_for_bind()`` → (bind)
    → ``record_bound_socket()`` → (listen) → ``mark_listening()`` →
    … → ``cleanup_bound_socket()`` → ``release()``.
    """

    def __init__(self, socket_path: Path | str) -> None:
        self._socket_path = canonical_socket_path(socket_path)
        self._lock_path = Path(str(self._socket_path) + ".lock")
        self._mutex = threading.Lock()
        self._fd: int | None = None
        self._state = SocketOwnershipState.UNCLAIMED
        self._bound_identity: SocketIdentity | None = None
        # Linux (tmpfs/ext4) охотно переиспользует номера inode: (dev, ino)
        # недостаточно для «это ТОТ ЖЕ файл» — replacement-сокет может получить
        # только что освобождённый номер (поймано ubuntu-CI). mtime_ns
        # выставляется ядром при bind и вместе с (dev, ino) даёт практически
        # неподделываемую identity для пути удаления.
        self._bound_mtime_ns: int | None = None

    # -- свойства -----------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def snapshot(self) -> SocketOwnershipSnapshot:
        with self._mutex:
            return SocketOwnershipSnapshot(
                socket_path=self._socket_path,
                state=self._state,
                bound_identity=self._bound_identity,
            )

    # -- lifecycle ----------------------------------------------------------

    def acquire(self) -> None:
        """Неблокирующе захватить sidecar flock. Contention → SocketAlreadyOwnedError."""
        import fcntl  # POSIX-only, как и AF_UNIX; локальный импорт симметричен state_store

        with self._mutex:
            if self._fd is not None:
                return  # идемпотентно: уже держим

            # Ревью-заметка приёмки №2: кастомный --socket-path может указывать
            # в ещё не созданный каталог — создаём родителя, как это делает
            # default_socket_path в service.py.
            try:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise UnsafeSocketPathError(
                    f"каталог для lock-файла недоступен: {exc}"
                ) from exc

            flags = os.O_CREAT | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            flags |= nofollow
            try:
                fd = os.open(self._lock_path, flags, 0o600)
            except OSError as exc:
                if nofollow and exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise UnsafeSocketPathError(
                        f"lock-файл {self._lock_path} — символическая ссылка"
                    ) from exc
                raise UnsafeSocketPathError(
                    f"не удалось открыть lock-файл {self._lock_path}: {exc}"
                ) from exc

            try:
                st = os.fstat(fd)
                if not stat_mod.S_ISREG(st.st_mode):
                    raise UnsafeSocketPathError(
                        f"lock-файл {self._lock_path} не является regular file"
                    )
                if st.st_uid != os.geteuid():
                    raise UnsafeSocketPathError(
                        f"lock-файл {self._lock_path} принадлежит чужому uid={st.st_uid}"
                    )
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass  # права уже проверены владельцем; fchmod — best effort
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    # Контенция — ТОЛЬКО EWOULDBLOCK/EAGAIN/EACCES. ENOLCK,
                    # EOPNOTSUPP (ФС без flock: SMB/NFS) и прочее — не «занят»,
                    # а неспособность доказать владение: иначе ложный диагноз
                    # уводит в вечный EX_TEMPFAIL-рестарт с неверной строкой лога.
                    if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                        raise SocketAlreadyOwnedError(
                            f"endpoint {self._socket_path} уже занят другим процессом"
                        ) from exc
                    raise UnsafeSocketPathError(
                        f"flock на {self._lock_path} невозможен "
                        f"(errno={exc.errno}): {exc}"
                    ) from exc
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            self._fd = fd
            self._state = SocketOwnershipState.CLAIMED

    def prepare_for_bind(self) -> SocketPathProbe:
        """Под claim'ом расчистить путь для bind. Возвращает probe только для
        безопасных ``MISSING`` и очищенного ``STALE``."""
        with self._mutex:
            if self._fd is None:
                raise SocketOwnershipError(
                    "prepare_for_bind требует захваченного claim (acquire)"
                )

        probe = probe_unix_socket_path(self._socket_path)
        if probe.status is SocketPathStatus.MISSING:
            return probe
        if probe.status is SocketPathStatus.LISTENING:
            # Живой listener — возможно, legacy backend без sidecar flock.
            raise SocketAlreadyOwnedError(
                f"endpoint {self._socket_path} уже слушает другой процесс"
            )
        if probe.status is SocketPathStatus.OCCUPIED:
            raise UnsafeSocketPathError(
                f"путь {self._socket_path} занят не-сокетом или неоднозначен: "
                f"{probe.error}"
            )

        # STALE: удалить можно только ТОТ ЖЕ inode, что мы только что пробовали.
        try:
            st = os.lstat(self._socket_path)
        except FileNotFoundError:
            return SocketPathProbe(status=SocketPathStatus.MISSING, identity=None)
        if not stat_mod.S_ISSOCK(st.st_mode) or probe.identity is None or (
            (st.st_dev, st.st_ino) != (probe.identity.device, probe.identity.inode)
        ):
            raise UnsafeSocketPathError(
                f"inode {self._socket_path} сменился перед stale-cleanup — отказ"
            )
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise UnsafeSocketPathError(
                f"не удалось удалить stale socket {self._socket_path}: {exc}"
            ) from exc
        return probe

    def record_bound_socket(self) -> None:
        """После успешного bind() запомнить identity собственного inode."""
        try:
            st = os.lstat(self._socket_path)
        except OSError as exc:
            raise UnsafeSocketPathError(
                f"bind заявлен, но {self._socket_path} не читается: {exc}"
            ) from exc
        if not stat_mod.S_ISSOCK(st.st_mode):
            raise UnsafeSocketPathError(
                f"bind заявлен, но {self._socket_path} — не сокет"
            )
        with self._mutex:
            if self._fd is None:
                raise SocketOwnershipError("record_bound_socket без claim")
            self._bound_identity = _identity_from_stat(st)
            self._bound_mtime_ns = st.st_mtime_ns

    def mark_listening(self) -> None:
        with self._mutex:
            if self._fd is None or self._bound_identity is None:
                raise SocketOwnershipError(
                    "mark_listening требует claim и записанный bound inode"
                )
            self._state = SocketOwnershipState.LISTENING

    def cleanup_bound_socket(self) -> None:
        """Удалить pathname ТОЛЬКО при совпадении типа и (st_dev, st_ino).

        Идемпотентно; после вызова state возвращается в ``CLAIMED``.
        """
        with self._mutex:
            bound = self._bound_identity
            bound_mtime = self._bound_mtime_ns
            self._bound_identity = None
            self._bound_mtime_ns = None
            if self._state is SocketOwnershipState.LISTENING:
                self._state = SocketOwnershipState.CLAIMED
        if bound is None:
            return
        try:
            st = os.lstat(self._socket_path)
        except OSError:
            return
        if not stat_mod.S_ISSOCK(st.st_mode):
            return
        if (st.st_dev, st.st_ino) != (bound.device, bound.inode):
            return  # replacement inode другого процесса — не трогаем
        if bound_mtime is not None and st.st_mtime_ns != bound_mtime:
            return  # тот же НОМЕР inode, но другой файл (reuse на Linux)
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass

    def release(self) -> None:
        """Идемпотентно отпустить flock и закрыть FD. Sidecar НЕ удаляется."""
        import fcntl

        with self._mutex:
            fd, self._fd = self._fd, None
            self._state = SocketOwnershipState.UNCLAIMED
            self._bound_identity = None
            self._bound_mtime_ns = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
