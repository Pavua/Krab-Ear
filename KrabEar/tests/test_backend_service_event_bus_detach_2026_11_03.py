"""ear-full-lifecycle-1029: BackendService.close() detaches EventBus listeners/replay."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import bus as event_bus  # noqa: E402


class BackendServiceEventBusDetachTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        event_bus._event_replay = None
        self._tmpdir.cleanup()

    def _make_service(self):
        from backend.state_store import StateStore
        from _settings_test_helpers import safe_backend_for_settings

        store = StateStore(data_dir=self.data_dir / "profile")
        store.initialize_startup_plaintext_policy(new_profile=True)
        with patch("backend.service.settings.IPC_THROTTLE_ENABLED", False), patch(
            "backend.service.settings.IPC_SIGNING_ENABLED", False
        ):
            return safe_backend_for_settings(self, store)

    def test_close_removes_wired_listeners(self) -> None:
        base = len(event_bus._listeners)
        svc = self._make_service()
        self.assertGreater(len(event_bus._listeners), base)
        self.assertIs(event_bus._event_replay, svc._event_replay)

        svc.close()

        self.assertEqual(len(event_bus._listeners), base)
        self.assertIsNone(event_bus._event_replay)

    def test_close_does_not_clear_foreign_event_replay(self) -> None:
        svc = self._make_service()
        foreign = object()
        event_bus._event_replay = foreign
        svc.close()
        self.assertIs(event_bus._event_replay, foreign)


if __name__ == "__main__":
    unittest.main()
