"""R3 шаг 2 (2026-11-07): повторный stop_call_assist не теряет итог первого.

До фикса: ветка «не активен» затирала финальное состояние (summary,
summary_history_id) на idle — клиент, потерявший ответ первого stop, при
повторе не мог узнать id summary. Повтор во время идущего stop тоже затирал.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _Gateway(VoiceGatewayClient):
    def __init__(self, gate: threading.Event | None = None):
        self.summary_calls = 0
        self.gate = gate
        self.entered = threading.Event()

    def post(self, voice_gateway_url, api_key, path, payload):
        if "/summary" in path:
            self.summary_calls += 1
            self.entered.set()
            if self.gate is not None:
                self.gate.wait(5)
            return {"ok": True, "payload": {"summary": "итог звонка", "tasks": []}}
        return {"ok": True, "payload": {}}

    def get(self, voice_gateway_url, api_key, path):
        return {"ok": True, "payload": {}}

    def stop_session(self, voice_gateway_url, api_key, session_id):
        return {"ok": True}


class _Recorder:
    is_recording = False


class StopReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.store.initialize_startup_plaintext_policy(new_profile=True)

    def _svc(self, gateway):
        svc = CallAssistService(store=self.store, recorder=_Recorder(), transcriber=object(), gateway=gateway)
        svc._state.update({"active": True, "status": "running", "session_id": "call_x", "gateway_session_id": "gw_1"})
        return svc

    def test_retry_after_completed_stop_returns_same_summary_id(self):
        gw = _Gateway()
        svc = self._svc(gw)
        first = svc.handle_stop({"auto_summary": True})
        self.assertTrue(first.get("summary_history_id"))
        second = svc.handle_stop({"auto_summary": True})
        self.assertEqual(second.get("summary_history_id"), first["summary_history_id"])
        self.assertTrue(second.get("replayed"))
        self.assertEqual(gw.summary_calls, 1)
        self.assertEqual(self.store.count_active_items(), 1)

    def test_stop_on_fresh_idle_service_returns_idle(self):
        svc = CallAssistService(store=self.store, recorder=_Recorder(), transcriber=object(), gateway=_Gateway())
        res = svc.handle_stop({})
        self.assertEqual(res["status"], "idle")
        self.assertFalse(res.get("replayed", False))

    def test_retry_during_inflight_stop_reports_in_progress(self):
        gate = threading.Event()
        gw = _Gateway(gate)
        svc = self._svc(gw)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("first", svc.handle_stop({"auto_summary": True})))
        t.start()
        self.assertTrue(gw.entered.wait(5))
        second = svc.handle_stop({"auto_summary": True})
        gate.set()
        t.join(5)
        self.assertTrue(second.get("stop_in_progress"))
        self.assertTrue(out["first"].get("summary_history_id"))
        third = svc.handle_stop({})
        self.assertEqual(third.get("summary_history_id"), out["first"]["summary_history_id"])
        self.assertNotIn("stop_in_progress", out["first"])

    def test_exception_mid_stop_does_not_stick_in_progress(self):
        class _Boom(_Gateway):
            def post(self, *a, **k):
                raise RuntimeError("gateway down")
        svc = self._svc(_Boom())
        with self.assertRaises(RuntimeError):
            svc.handle_stop({"auto_summary": True})
        res = svc.handle_stop({})
        self.assertFalse(res.get("stop_in_progress", False))

    def test_replay_redacts_summary_when_privacy_enabled_later(self):
        privacy = {"on": False}
        svc = self._svc(_Gateway())
        svc._settings_get = lambda k, d: privacy["on"] if k == "privacy_mode_enabled" else d
        first = svc.handle_stop({"auto_summary": True})
        self.assertIn("summary", first)
        privacy["on"] = True
        replay = svc.handle_stop({})
        self.assertTrue(replay.get("replayed"))
        self.assertNotIn("summary", replay)
        self.assertEqual(replay.get("summary_history_id"), first["summary_history_id"])
        self.assertTrue(replay.get("privacy_mode_active"))


if __name__ == "__main__":
    unittest.main()
