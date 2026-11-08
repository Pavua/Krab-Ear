"""#2196 F1–F3 (handoff 2026-11-08): точечные дефекты replay/stop.

F1: replay в privacy mode оставлял текст ошибок Gateway (тело HTTP-ответа).
F2: исключение stop A снимало маркер «stop идёт» у новой сессии B.
F3: stop A, завершившийся после start B, затирал состояние B своим.
"""
from __future__ import annotations

import json
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

CANARY = "SECRET-CANARY-BODY-7731"


class _Gw(VoiceGatewayClient):
    """Первый summary-POST блокируется на gate и затем бросает/возвращает по режиму."""

    def __init__(self, mode="ok", gate=None):
        self.mode = mode
        self.gate = gate
        self.entered = threading.Event()

    def post(self, voice_gateway_url, api_key, path, payload):
        if "/summary" not in path:
            return {"ok": True, "payload": {}}
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "error_body":
            return {"ok": False, "error": f"HTTP 500: {CANARY}"}
        return {"ok": True, "payload": {"summary": "итог", "tasks": []}}

    def get(self, *a, **k):
        return {"ok": True, "payload": {}}

    def start_session(self, voice_gateway_url, api_key, payload):
        return {"ok": False, "error": "no gateway in test"}  # без _assist_loop потока

    def stop_session(self, *a, **k):
        return {"ok": True}


class _Rec:
    def __init__(self):
        self.is_recording = False
        self.block = None
        self.entered = threading.Event()

    def start(self):
        return False

    def stop(self):
        self.entered.set()
        if self.block is not None:
            self.block.wait(5)
        self.is_recording = False


class F1F3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.store.initialize_startup_plaintext_policy(new_profile=True)

    def _svc(self, gw, rec=None, privacy=None):
        svc = CallAssistService(store=self.store, recorder=rec or _Rec(), transcriber=object(), gateway=gw)
        if privacy is not None:
            svc._settings_get = lambda k, d: privacy["on"] if k == "privacy_mode_enabled" else d
        svc._state.update({"active": True, "status": "running", "session_id": "call_A", "gateway_session_id": "gw_A"})
        return svc

    def test_f1_privacy_replay_has_no_gateway_error_text(self):
        privacy = {"on": False}
        svc = self._svc(_Gw("error_body"), privacy=privacy)
        first = svc.handle_stop({"auto_summary": True})
        self.assertIn(CANARY, json.dumps(first))  # исходный ответ владельцу — как раньше
        privacy["on"] = True
        replay = svc.handle_stop({})
        self.assertTrue(replay.get("replayed"))
        self.assertNotIn(CANARY, json.dumps(replay))

    def test_f2_stop_a_exception_keeps_stop_b_in_progress(self):
        gate = threading.Event()
        gw = _Gw("raise", gate)
        rec = _Rec()
        svc = self._svc(gw, rec)
        errs = []

        def stop_a():
            try:
                svc.handle_stop({"auto_summary": True})
            except RuntimeError as e:
                errs.append(e)

        ta = threading.Thread(target=stop_a)
        ta.start()
        self.assertTrue(gw.entered.wait(5))
        started = svc.handle_start({"auto_summary": False})
        self.assertNotEqual(started.get("error"), "already_active")
        rec.is_recording = True
        rec.block = threading.Event()
        tb = threading.Thread(target=lambda: svc.handle_stop({"auto_summary": False}))
        tb.start()
        self.assertTrue(rec.entered.wait(5))
        gate.set()
        ta.join(5)
        self.assertEqual(len(errs), 1)
        mid = svc.handle_stop({})
        self.assertTrue(mid.get("stop_in_progress"), "исключение A сняло маркер B")
        rec.block.set()
        tb.join(5)

    def test_f3_old_stop_finishing_does_not_overwrite_new_session(self):
        gate = threading.Event()
        gw = _Gw("ok", gate)
        svc = self._svc(gw)
        ta = threading.Thread(target=lambda: svc.handle_stop({"auto_summary": True}))
        ta.start()
        self.assertTrue(gw.entered.wait(5))
        started = svc.handle_start({"auto_summary": False})
        new_sid = started["session_id"]
        gate.set()
        ta.join(5)
        st = svc.state
        self.assertEqual(st.get("session_id"), new_sid, "stop A затёр состояние новой сессии")
        self.assertTrue(st.get("active"))
        stopped = svc.handle_stop({"auto_summary": False})
        again = svc.handle_stop({})
        self.assertEqual(stopped.get("session_id"), new_sid)
        self.assertFalse(again.get("stop_in_progress", False))


if __name__ == "__main__":
    unittest.main()
