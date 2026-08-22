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
