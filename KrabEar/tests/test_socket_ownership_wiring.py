"""Wiring-тесты socket-ownership: фабрика и (Task 4) порядок main().

Не конструирует production-ресурсы: BackendService/StateStore подменяются.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class BuildServiceForwardingTest(unittest.TestCase):
    def test_build_service_forwards_socket_path_and_snapshot_getter(self):
        import backend.service as service_mod

        tmp = tempfile.mkdtemp(prefix="wire_")
        fake_store = MagicMock()
        fake_store.load_settings.return_value = {}
        getter = lambda: None  # noqa: E731
        sock = Path(tmp) / "krab.sock"
        with patch.object(service_mod, "StateStore", return_value=fake_store), \
             patch.object(service_mod, "BackendService") as svc_cls:
            service_mod.build_service(
                Path(tmp),
                socket_path=sock,
                socket_ownership_snapshot_getter=getter,
            )
        kwargs = svc_cls.call_args.kwargs
        self.assertEqual(kwargs.get("socket_path"), sock)
        self.assertIs(kwargs.get("socket_ownership_snapshot_getter"), getter)

    def test_build_service_without_new_kwargs_still_works(self):
        import backend.service as service_mod

        tmp = tempfile.mkdtemp(prefix="wire_")
        fake_store = MagicMock()
        fake_store.load_settings.return_value = {}
        with patch.object(service_mod, "StateStore", return_value=fake_store), \
             patch.object(service_mod, "BackendService") as svc_cls:
            service_mod.build_service(Path(tmp))
        self.assertTrue(svc_cls.called)


if __name__ == "__main__":
    unittest.main()


class MainLifecycleOrderTest(unittest.TestCase):
    """Source-контракт: main() захватывает claim ДО тяжёлых side effects."""

    @staticmethod
    def _main_src() -> str:
        import inspect

        import backend.service as service_mod

        return inspect.getsource(service_mod.main)

    def test_claim_acquired_before_store_sentry_and_service(self):
        src = self._main_src()
        acq = src.index("ownership.acquire()")
        self.assertLess(acq, src.index("_early_store = StateStore"))
        self.assertLess(acq, src.index("init_sentry("))
        self.assertLess(acq, src.index("build_service("))

    def test_release_after_shutdown_backend(self):
        src = self._main_src()
        self.assertLess(src.index("_shutdown_backend("), src.rindex("ownership.release()"))

    def test_server_gets_production_claim_and_diag_gets_snapshot(self):
        src = self._main_src()
        self.assertIn("ownership=ownership", src)
        self.assertIn("socket_ownership_snapshot_getter=ownership.snapshot", src)

    def test_contention_exits_tempfail_without_service(self):
        src = self._main_src()
        self.assertIn("SocketAlreadyOwnedError", src)
        self.assertIn("os.EX_TEMPFAIL", src)
        self.assertIn("os.EX_CANTCREAT", src)


class MainContenderBehaviorTest(unittest.TestCase):
    """Ревью F2/F6: живой contender-путь main() — EX_TEMPFAIL и освобождённый claim."""

    def test_main_exits_tempfail_and_releases_claim_on_live_listener(self):
        import os
        import socket as socket_mod

        import backend.service as service_mod
        from backend.socket_ownership import SocketOwnershipClaim

        tmp = tempfile.TemporaryDirectory(prefix="mainconf_")
        self.addCleanup(tmp.cleanup)
        sock_path = Path(tmp.name) / "krabear.sock"
        listener = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        listener.bind(str(sock_path))
        listener.listen(8)
        self.addCleanup(listener.close)

        argv = ["service.py", "--data-dir", tmp.name]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                service_mod.main()
        self.assertEqual(ctx.exception.code, os.EX_TEMPFAIL)
        # Claim освобождён (F2): свежий захват в ЭТОМ ЖЕ процессе проходит.
        claim = SocketOwnershipClaim(sock_path)
        claim.acquire()
        claim.release()
        # Живой listener не тронут.
        self.assertTrue(sock_path.exists())
