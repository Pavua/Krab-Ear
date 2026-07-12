"""Dispatch-invariant + privacy + интеграция BackendService для meeting_* (C2a)."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVICE_PY = PROJECT_ROOT / "backend" / "service.py"

_METHODS = {"meeting_start", "meeting_stop", "get_meeting_live_state"}


class MeetingDispatchInvariantTestCase(unittest.TestCase):
    def test_methods_registered_in_dispatch_table(self) -> None:
        src = SERVICE_PY.read_text(encoding="utf-8")
        keys = set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', src))
        missing = _METHODS - keys
        self.assertSetEqual(missing, set(),
                            f"meeting-методы отсутствуют в dispatch: {missing}")

    def test_service_close_stops_meeting_worker(self) -> None:
        src = SERVICE_PY.read_text(encoding="utf-8")
        self.assertIn("_meeting_svc.close()", src,
                      "BackendService.close() обязан звать _meeting_svc.close()")


class MeetingBackendIntegrationTestCase(unittest.TestCase):
    """Полный BackendService с фейками: методы диспатчатся и privacy-гейтятся."""

    def setUp(self) -> None:
        from backend.service import BackendService
        from backend.state_store import StateStore
        from tests.test_backend_service import (
            FakeRecorder, FakeTranscriber, FakeTranslator,
        )
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store, recorder=FakeRecorder(),
            transcriber=FakeTranscriber(), translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()  # 🔴 правило #1782: daemon-треды

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t", "method": method, "params": params or {}})

    def test_live_state_inactive_by_default(self) -> None:
        resp = self._call("get_meeting_live_state")
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["result"]["active"])

    def test_start_stop_roundtrip(self) -> None:
        start = self._call("meeting_start")
        self.assertTrue(start["ok"])
        self.assertTrue(start["result"]["ok"])
        state = self._call("get_meeting_live_state")
        self.assertTrue(state["result"]["active"])
        stop = self._call("meeting_stop")
        self.assertTrue(stop["result"]["ok"])
        state2 = self._call("get_meeting_live_state")
        self.assertFalse(state2["result"]["active"])

    def test_privacy_gates_all_three(self) -> None:
        self._call("set_settings", {"privacy_mode_enabled": True})
        start = self._call("meeting_start")
        self.assertEqual(start["result"].get("skipped"), "privacy_mode")
        state = self._call("get_meeting_live_state")
        self.assertTrue(state["result"].get("privacy_mode_active"))
        stop = self._call("meeting_stop")
        self.assertTrue(stop["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
