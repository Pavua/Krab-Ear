"""M2: EventBridge не работает, когда REST поднят внутри процесса."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.event_bridge import EventBridge  # noqa: E402


class EventBridgeOfflineWhenInProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _bridge(self, *, bridge_on: bool, in_process: bool) -> EventBridge:
        cfg = SimpleNamespace(
            EVENT_BRIDGE_ENABLED=bridge_on,
            REST_IN_PROCESS_ENABLED=in_process,
            REST_SERVER_PORT=5005,
        )
        return EventBridge(settings=cfg, data_dir=self.data_dir)

    def test_disabled_when_rest_is_in_process(self):
        b = self._bridge(bridge_on=True, in_process=True)
        self.assertEqual(b.get_diagnostics()["state"], "disabled")

    def test_in_process_bridge_ignores_events(self):
        b = self._bridge(bridge_on=True, in_process=True)
        b.on_event("krab_error", {"code": "test.code"})
        self.assertEqual(b.get_diagnostics()["queue_depth"], 0)

    def test_still_enabled_in_two_process_mode(self):
        b = self._bridge(bridge_on=True, in_process=False)
        self.assertEqual(b.get_diagnostics()["state"], "unknown")   # «ещё не пробовал»
        b.on_event("krab_error", {"code": "test.code"})
        self.assertEqual(b.get_diagnostics()["queue_depth"], 1)


if __name__ == "__main__":
    unittest.main()
