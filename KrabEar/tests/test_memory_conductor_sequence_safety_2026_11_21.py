"""KE05C-22 / KE05C-08 / KE05C-09: безопасность evict-цикла и секвенса записи MemoryConductor.

* исключение из ``model_loaded_fn`` выходило из verify — исход не учитывался (должен быть UNKNOWN);
* rewriter грузился после неподтверждённой выгрузки brain, т.е. поверх невыгруженных 19 ГБ;
* «ровно один воркер» не выполнялось: каждый ``on_recording_start`` запускал новый поток.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.memory_conductor import MemoryConductor  # noqa: E402

_TMP = tempfile.TemporaryDirectory(prefix="ear-conductor-seq-")


def _mk(model_loaded=False) -> MemoryConductor:
    settings = {
        "memory_conductor_enabled": True,
        "memory_conductor_enforce": True,
        "llm_brain_model": "brain-model",
        "llm_model": "rewriter-model",
        "llm_base_url": "http://localhost:1234/v1",
        "memory_evict_cooldown_sec": 600.0,
    }
    svc = MagicMock()
    svc.cached_settings.return_value = settings
    return MemoryConductor(
        settings_service=svc,
        ledger=MagicMock(),
        is_recording=lambda: False,
        is_meeting_active=lambda: False,
        pressure_fn=lambda: 0,
        host_stats_fn=lambda: None,
        gigaam_close_if_idle=MagicMock(return_value=True),
        gigaam_idle_sec_fn=lambda: 0.0,
        last_stt_activity_ts_fn=lambda: time.monotonic(),
        tick_sec=0.05,
        unload_model_fn=MagicMock(),
        load_model_fn=MagicMock(),
        model_loaded_fn=MagicMock(return_value=model_loaded),
        models_share_loaded_instance_fn=MagicMock(return_value=False),
        lease_holder_fn=lambda: None,
        verify_timeout_sec=0.2,
        verify_poll_sec=0.02,
        shadow_marker_path=Path(_TMP.name) / "shadow.json",
    )


class VerifyExceptionIsUnknown(unittest.TestCase):
    def test_model_loaded_fn_raising_counts_unknown_and_burns_no_cooldown(self):
        c = _mk()
        c.model_loaded_fn.side_effect = RuntimeError("catalog unreachable")
        c._evict_model("rewriter", "rewriter-model")  # не должен бросить
        counters = c._counters["rewriter"]
        self.assertEqual(counters["unknown"], 1)
        self.assertEqual(counters["succeeded"], 0)
        self.assertNotIn("rewriter", c._cooldown_until)


class SequenceLoadsOnlyAfterVerifiedUnload(unittest.TestCase):
    def _run(self, c):
        c.on_recording_start()
        c.wait_workers(3.0)

    def test_still_loaded_does_not_load_rewriter(self):
        c = _mk(model_loaded=True)
        self._run(c)
        c.unload_model_fn.assert_called_once()
        c.load_model_fn.assert_not_called()

    def test_unknown_verify_does_not_load_rewriter(self):
        c = _mk()
        c.model_loaded_fn.return_value = None
        self._run(c)
        c.load_model_fn.assert_not_called()

    def test_verify_exception_does_not_load_rewriter(self):
        c = _mk()
        c.model_loaded_fn.side_effect = RuntimeError("boom")
        self._run(c)
        c.load_model_fn.assert_not_called()

    def test_verified_unload_loads_rewriter(self):
        c = _mk(model_loaded=False)
        self._run(c)
        c.load_model_fn.assert_called_once()


class ExactlyOneSequenceWorker(unittest.TestCase):
    def test_second_start_during_running_sequence_is_skipped(self):
        c = _mk()
        entered = threading.Event()
        gate = threading.Event()

        def slow_unload(*a, **k):
            entered.set()
            gate.wait(5)

        c.unload_model_fn.side_effect = slow_unload
        c.on_recording_start()
        self.assertTrue(entered.wait(3))
        c.on_recording_start()
        c.on_recording_start()
        gate.set()
        c.wait_workers(3.0)
        self.assertEqual(c.unload_model_fn.call_count, 1, "параллельные unload/load")

    def test_sequence_can_run_again_after_previous_finished(self):
        c = _mk()
        c.on_recording_start()
        c.wait_workers(3.0)
        c.on_recording_start()
        c.wait_workers(3.0)
        self.assertEqual(c.unload_model_fn.call_count, 2)


if __name__ == "__main__":
    unittest.main()
