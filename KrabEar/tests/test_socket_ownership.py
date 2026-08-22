"""Tests for backend/socket_ownership.py — atomic Unix-socket endpoint ownership.

Спека: docs/superpowers/specs/2026-08-22-socket-ownership-design.md.
Только temp-пути; никакого production data dir / сокета (Operational boundaries).
Импорт модуля — внутри тестов, чтобы отсутствие модуля было обычным FAIL,
а не ошибкой коллекции.
"""

from __future__ import annotations

import errno
import importlib
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _mod():
    return importlib.import_module("backend.socket_ownership")


def _short_tmpdir() -> tempfile.TemporaryDirectory:
    # AF_UNIX ограничен ~104 байтами на macOS — системный TMPDIR короткий,
    # а вот scratchpad-пути сессий — нет. Страхуемся assert'ом в setUp.
    return tempfile.TemporaryDirectory(prefix="sockown_")


class ProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _short_tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.assertLess(len(str(self.base)), 80, "temp-путь слишком длинный для AF_UNIX")

    def test_probe_distinguishes_missing_listening_stale_and_occupied(self):
        ownership = _mod()
        missing = self.base / "missing.sock"
        self.assertIs(
            ownership.probe_unix_socket_path(missing).status,
            ownership.SocketPathStatus.MISSING,
        )

        live_path = self.base / "live.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(live_path))
        listener.listen(1)
        try:
            self.assertIs(
                ownership.probe_unix_socket_path(live_path).status,
                ownership.SocketPathStatus.LISTENING,
            )
        finally:
            listener.close()
        self.assertIs(
            ownership.probe_unix_socket_path(live_path).status,
            ownership.SocketPathStatus.STALE,
        )

        regular = self.base / "regular.sock"
        regular.write_text("keep", encoding="utf-8")
        self.assertIs(
            ownership.probe_unix_socket_path(regular).status,
            ownership.SocketPathStatus.OCCUPIED,
        )
        self.assertEqual(regular.read_text(encoding="utf-8"), "keep")

    def test_probe_reports_identity_for_existing_socket(self):
        ownership = _mod()
        live_path = self.base / "id.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(live_path))
        listener.listen(1)
        self.addCleanup(listener.close)
        st = os.lstat(live_path)
        probe = ownership.probe_unix_socket_path(live_path)
        self.assertIsNotNone(probe.identity)
        self.assertEqual((probe.identity.device, probe.identity.inode), (st.st_dev, st.st_ino))

    def test_final_symlink_is_occupied_and_preserved(self):
        ownership = _mod()
        real = self.base / "real.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(real))
        listener.listen(1)
        self.addCleanup(listener.close)
        link = self.base / "link.sock"
        link.symlink_to(real)
        self.assertIs(
            ownership.probe_unix_socket_path(link).status,
            ownership.SocketPathStatus.OCCUPIED,
        )
        self.assertTrue(link.is_symlink(), "символическая ссылка не должна быть удалена")

    def test_ambiguous_connect_error_is_occupied_not_stale(self):
        ownership = _mod()
        dead = self.base / "dead.sock"
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        holder.bind(str(dead))
        holder.listen(1)
        self.addCleanup(holder.close)

        real_connect = socket.socket.connect

        def flaky_connect(sock_self, addr):
            if isinstance(addr, (str, bytes)) and str(addr).endswith("dead.sock"):
                raise OSError(errno.ETIMEDOUT, "timed out")
            return real_connect(sock_self, addr)

        with patch.object(socket.socket, "connect", flaky_connect):
            probe = ownership.probe_unix_socket_path(dead)
        self.assertIs(probe.status, ownership.SocketPathStatus.OCCUPIED)


class CanonicalPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _short_tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_parent_aliases_collapse_to_one_lock_domain(self):
        ownership = _mod()
        real_dir = self.base / "realdir"
        real_dir.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(real_dir, target_is_directory=True)
        c1 = ownership.canonical_socket_path(real_dir / "krab.sock")
        c2 = ownership.canonical_socket_path(alias / "krab.sock")
        self.assertEqual(c1, c2)

    def test_final_name_is_not_resolved(self):
        ownership = _mod()
        d = self.base / "d"
        d.mkdir()
        target = d / "target.sock"
        target.write_text("x", encoding="utf-8")
        link = d / "krab.sock"
        link.symlink_to(target)
        canonical = ownership.canonical_socket_path(link)
        self.assertEqual(canonical.name, "krab.sock", "конечный symlink не разрешаем")


class ClaimLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _short_tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.sock_path = self.base / "krab.sock"

    def test_acquire_prepare_bind_listen_cleanup_release_states(self):
        ownership = _mod()
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.UNCLAIMED)

        claim.acquire()
        self.addCleanup(claim.release)
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.CLAIMED)

        probe = claim.prepare_for_bind()
        self.assertIs(probe.status, ownership.SocketPathStatus.MISSING)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(self.sock_path))
        claim.record_bound_socket()
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.CLAIMED)
        self.assertIsNotNone(claim.snapshot().bound_identity)

        server.listen(1)
        claim.mark_listening()
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.LISTENING)

        server.close()
        claim.cleanup_bound_socket()
        self.assertFalse(self.sock_path.exists(), "собственный inode должен быть удалён")
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.CLAIMED)

        claim.release()
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.UNCLAIMED)

    def test_release_is_idempotent(self):
        ownership = _mod()
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        claim.release()
        claim.release()
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.UNCLAIMED)

    def test_sidecar_inode_stable_across_release_reacquire(self):
        ownership = _mod()
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        lock_path = Path(str(ownership.canonical_socket_path(self.sock_path)) + ".lock")
        st1 = os.lstat(lock_path)
        claim.release()
        self.assertTrue(lock_path.exists(), "sidecar не удаляется на release")
        claim2 = ownership.SocketOwnershipClaim(self.sock_path)
        claim2.acquire()
        self.addCleanup(claim2.release)
        st2 = os.lstat(lock_path)
        self.assertEqual((st1.st_dev, st1.st_ino), (st2.st_dev, st2.st_ino))

    def test_acquire_creates_missing_parent_directory(self):
        # Ревью-заметка приёмки №2: кастомный --socket-path в несуществующем каталоге.
        ownership = _mod()
        deep = self.base / "sub" / "dir" / "krab.sock"
        claim = ownership.SocketOwnershipClaim(deep)
        claim.acquire()
        self.addCleanup(claim.release)
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.CLAIMED)

    def test_symlinked_sidecar_is_unsafe(self):
        ownership = _mod()
        lock_path = Path(str(ownership.canonical_socket_path(self.sock_path)) + ".lock")
        victim = self.base / "victim"
        victim.write_text("v", encoding="utf-8")
        lock_path.symlink_to(victim)
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        with self.assertRaises(ownership.UnsafeSocketPathError):
            claim.acquire()
        self.assertEqual(victim.read_text(encoding="utf-8"), "v")

    def test_prepare_for_bind_requires_claim(self):
        ownership = _mod()
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        with self.assertRaises(ownership.SocketOwnershipError):
            claim.prepare_for_bind()


class PrepareForBindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _short_tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.sock_path = self.base / "krab.sock"

    def test_live_legacy_listener_survives_and_blocks(self):
        ownership = _mod()
        legacy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        legacy.bind(str(self.sock_path))
        # backlog > 1: probe внутри prepare_for_bind успешно коннектится и
        # закрывается, но неакцептованное соединение остаётся в очереди —
        # с backlog=1 наш контрольный connect ниже получил бы ECONNREFUSED.
        legacy.listen(8)
        self.addCleanup(legacy.close)
        st_before = os.lstat(self.sock_path)

        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        self.addCleanup(claim.release)
        with self.assertRaises(ownership.SocketAlreadyOwnedError):
            claim.prepare_for_bind()

        st_after = os.lstat(self.sock_path)
        self.assertEqual((st_before.st_dev, st_before.st_ino), (st_after.st_dev, st_after.st_ino))
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self.sock_path))
        finally:
            probe.close()

    def test_stale_socket_removed_only_under_claim_with_identity_recheck(self):
        ownership = _mod()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.sock_path))
        stale.listen(1)
        stale.close()
        self.assertTrue(self.sock_path.exists())

        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        self.addCleanup(claim.release)
        probe = claim.prepare_for_bind()
        self.assertIs(probe.status, ownership.SocketPathStatus.STALE)
        self.assertFalse(self.sock_path.exists(), "stale inode должен быть удалён")

    def test_occupied_regular_file_blocks_and_survives(self):
        ownership = _mod()
        self.sock_path.write_text("data", encoding="utf-8")
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        self.addCleanup(claim.release)
        with self.assertRaises(ownership.UnsafeSocketPathError):
            claim.prepare_for_bind()
        self.assertEqual(self.sock_path.read_text(encoding="utf-8"), "data")

    def test_cleanup_skips_replacement_inode(self):
        ownership = _mod()
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        self.addCleanup(claim.release)
        claim.prepare_for_bind()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.sock_path))
        claim.record_bound_socket()
        server.listen(1)
        claim.mark_listening()
        server.close()

        # Подменяем inode: чужой процесс успел пере-bind-ить тот же pathname.
        self.sock_path.unlink()
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(str(self.sock_path))
        self.addCleanup(other.close)

        claim.cleanup_bound_socket()
        self.assertTrue(self.sock_path.exists(), "чужой replacement inode не удаляем")


class CrossProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _short_tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.sock_path = self.base / "krab.sock"
        self.env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))

    def _child(self, code: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", code],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_fresh_subprocess_does_not_get_second_claim(self):
        ownership = _mod()
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        self.addCleanup(claim.release)
        child = self._child(
            "import sys\n"
            "from pathlib import Path\n"
            "from backend.socket_ownership import SocketOwnershipClaim, SocketAlreadyOwnedError\n"
            f"claim = SocketOwnershipClaim(Path({str(self.sock_path)!r}))\n"
            "try:\n"
            "    claim.acquire()\n"
            "except SocketAlreadyOwnedError:\n"
            "    print('CONTENDED'); sys.exit(0)\n"
            "print('ACQUIRED'); sys.exit(3)\n"
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn("CONTENDED", child.stdout)

    def test_reacquire_after_holder_hard_exit(self):
        ownership = _mod()
        child = self._child(
            "import os\n"
            "from pathlib import Path\n"
            "from backend.socket_ownership import SocketOwnershipClaim\n"
            f"claim = SocketOwnershipClaim(Path({str(self.sock_path)!r}))\n"
            "claim.acquire()\n"
            "print('HELD', flush=True)\n"
            "os._exit(0)\n"
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn("HELD", child.stdout)
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        claim.acquire()
        self.addCleanup(claim.release)
        self.assertIs(claim.snapshot().state, ownership.SocketOwnershipState.CLAIMED)


if __name__ == "__main__":
    unittest.main()


class FlockErrnoClassificationTest(unittest.TestCase):
    """Ревью F1: contention — только EWOULDBLOCK/EAGAIN/EACCES; остальное — Unsafe."""

    def setUp(self) -> None:
        self.tmp = _short_tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.sock_path = Path(self.tmp.name) / "krab.sock"

    def _acquire_with_flock_errno(self, err: int):
        import fcntl
        ownership = _mod()
        claim = ownership.SocketOwnershipClaim(self.sock_path)
        with patch.object(fcntl, "flock", side_effect=OSError(err, os.strerror(err))):
            claim.acquire()

    def test_enolck_is_unsafe_not_contended(self):
        ownership = _mod()
        with self.assertRaises(ownership.UnsafeSocketPathError):
            self._acquire_with_flock_errno(errno.ENOLCK)

    def test_eopnotsupp_is_unsafe_not_contended(self):
        ownership = _mod()
        with self.assertRaises(ownership.UnsafeSocketPathError):
            self._acquire_with_flock_errno(errno.EOPNOTSUPP)

    def test_ewouldblock_is_contended(self):
        ownership = _mod()
        with self.assertRaises(ownership.SocketAlreadyOwnedError):
            self._acquire_with_flock_errno(errno.EWOULDBLOCK)
