# -*- coding: utf-8 -*-
"""Pin the dispatch error contract: EXPECTED conditions vs GENUINE crashes.

The IPC dispatch (BackendService.handle_request) classifies handler exceptions:
  - ValueError / RuntimeError → these are the codebase's validation / not-found
    idioms ("Параметр id обязателен", "Элемент не найден: ...") → mapped to a
    semantic `invalid_request` code and logged at WARNING. They are normal user
    outcomes (a stale-id click, a missing param), NOT backend failures, so they
    must NOT surface as `internal_error` + ERROR/Sentry noise.
  - Any other exception (AttributeError/KeyError/TypeError/IndexError/...) → a
    GENUINE bug → `internal_error`, logged loudly (ERROR + Sentry). E.g. the
    HistoryItem-vs-dict crash raised AttributeError and must stay loud.

This test injects synthetic handlers into the live dispatch table to pin both
sides, plus an end-to-end check through a real handler's not-found path.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import BackendService
from backend.state_store import StateStore
from backend.ipc_errors import IpcOperationalError


def _make_service() -> BackendService:
    tmp = Path(tempfile.mkdtemp())
    store = StateStore(data_dir=tmp / "data")
    return BackendService(store=store)


class DispatchErrorContractTest(unittest.TestCase):

    def setUp(self) -> None:
        # This is a dispatch-table contract test — it has no business exercising
        # a real STT engine. AudioEngine.__init__ unconditionally spawns a
        # background "GigaAM-warmup" thread whenever the ambient
        # core.config.settings.STT_GIGAAM_ENABLED singleton is True (e.g. a dev
        # machine's real settings.json has GigaAM enabled for production
        # dictation). That thread races tearDown()'s service.close(): close()
        # can run before the thread has cached an adapter to close, after which
        # the thread spawns a real gigaam_worker.py subprocess with no owner
        # left to terminate it — an orphaned worker (see CLAUDE.md "GigaAM v3"
        # / STTRouter._spawn_lock notes). Disable it for the ambient singleton,
        # matching how sibling engine tests isolate STT_GIGAAM_ENABLED.
        gigaam_patcher = patch("core.config.settings.STT_GIGAAM_ENABLED", False)
        gigaam_patcher.start()
        self.addCleanup(gigaam_patcher.stop)
        self.service = _make_service()

    def tearDown(self) -> None:
        self.service.close()

    def _dispatch(self, method: str, params: dict) -> dict:
        return self.service.handle_request({"id": "t", "method": method, "params": params})

    def _inject(self, name: str, fn) -> None:
        """Register a synthetic handler into the live dispatch table."""
        self.service._dispatch_table[name] = fn

    # ----- EXPECTED conditions → invalid_request (WARNING) -----

    def test_value_error_maps_to_invalid_request(self) -> None:
        self._inject("_synthetic_ve", lambda params: (_ for _ in ()).throw(ValueError("bad value")))
        resp = self._dispatch("_synthetic_ve", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "invalid_request")
        self.assertIn("bad value", resp["error"]["message"])

    def test_runtime_error_maps_to_invalid_request(self) -> None:
        def _h(params):
            raise RuntimeError("Параметр id обязателен")
        self._inject("_synthetic_re", _h)
        resp = self._dispatch("_synthetic_re", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "invalid_request")

    def test_invalid_request_logged_at_warning_not_error(self) -> None:
        def _h(params):
            raise RuntimeError("Элемент не найден: zzz")
        self._inject("_synthetic_nf", _h)
        with self.assertLogs("KrabEar.Backend.Service", level="DEBUG") as cm:
            self._dispatch("_synthetic_nf", {})
        joined = "\n".join(cm.output)
        # Must be WARNING (not an ERROR / exception traceback line).
        self.assertIn("WARNING", joined)
        self.assertNotIn("ERROR:KrabEar.Backend.Service", joined)

    # ----- GENUINE bugs → internal_error (loud) -----

    def test_attribute_error_stays_internal_error(self) -> None:
        # The exact class of the topic_timeline crash ('X' object has no attribute).
        def _h(params):
            return None.text  # AttributeError
        self._inject("_synthetic_ae", _h)
        with self.assertLogs("KrabEar.Backend.Service", level="DEBUG") as cm:
            resp = self._dispatch("_synthetic_ae", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "internal_error")
        self.assertIn("ERROR", "\n".join(cm.output))

    def test_key_error_stays_internal_error(self) -> None:
        def _h(params):
            return {}["missing"]  # KeyError
        self._inject("_synthetic_ke", _h)
        resp = self._dispatch("_synthetic_ke", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "internal_error")

    # ----- IpcOperationalError → internal_error (loud, not downgraded) -----

    def test_ipc_operational_error_stays_internal_error(self) -> None:
        """IpcOperationalError must NOT be downgraded to invalid_request.

        The dispatch must catch IpcOperationalError BEFORE the generic
        (ValueError, RuntimeError) branch, since IpcOperationalError is a
        RuntimeError subclass and would otherwise be silenced as invalid_request.
        """
        def _h(params):
            raise IpcOperationalError("Gateway down")
        self._inject("_synthetic_op_err", _h)
        with self.assertLogs("KrabEar.Backend.Service", level="DEBUG") as cm:
            resp = self._dispatch("_synthetic_op_err", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "internal_error")
        self.assertIn("Gateway down", resp["error"]["message"])
        # Must be logged at ERROR (not WARNING) — stays loud for Sentry.
        self.assertIn("ERROR", "\n".join(cm.output))

    # ----- End-to-end through a real handler's not-found path -----

    def test_real_handler_not_found_is_invalid_request(self) -> None:
        resp = self._dispatch("repaste_item", {"history_id": "does-not-exist"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
