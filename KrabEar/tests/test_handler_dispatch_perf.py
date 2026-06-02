"""Micro-benchmark tests for IPC handler dispatch overhead.

Measures the time spent in BackendService.handle_request for read-only
handlers (ping, get_settings, get_recording_state) using a fully-stubbed
service — no STT, no LLM, no network.

Budget: <5 ms per individual dispatch call on any CI runner.

W1769: the dispatch table is now built ONCE in BackendService.__init__
(cached as self._dispatch_table); handle_request performs an O(1) dict lookup —
no per-call rebuild.  These benchmarks catch regressions in method-lookup +
handler execution.

Usage::

    PYTHONPATH=$(pwd)/KrabEar python3 -m unittest \\
        KrabEar.tests.test_handler_dispatch_perf -v

Skipped automatically when SKIP_BENCH=1 or CI=true (to avoid false-fail
on overloaded CI runners).
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
KRAB_EAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRAB_EAR_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402

# ---------------------------------------------------------------------------
# Skip flag — honour SKIP_BENCH and CI env vars
# ---------------------------------------------------------------------------
_SKIP = bool(os.environ.get("SKIP_BENCH")) or os.environ.get("CI") == "true"

# ---------------------------------------------------------------------------
# Minimal fakes (same pattern as test_ipc_dispatch_integration.py)
# ---------------------------------------------------------------------------


class _FakeRecorder:
    is_recording: bool = False
    sample_rate: int = 16_000

    def start(self) -> bool:
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        return None

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(32_000, dtype="float32"), 0.0


class _FakeEngine:
    quality_profile: str = "balanced"
    current_model: str = "fake-model"

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class _FakeTranscriber:
    def __init__(self) -> None:
        self.engine = _FakeEngine()

    def transcribe(self, audio_data: Any, **_kw: Any) -> str:
        return "stub"

    def transcribe_preview(self, audio_data: Any, **_kw: Any) -> str:
        return "preview"


class _FakeTranslator:
    def translate(self, text: str, mode: str, network_mode: str, **_kw: Any) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Helper: time N calls to handle_request
# ---------------------------------------------------------------------------

def _time_calls(service: BackendService, payload: dict[str, Any], n: int) -> float:
    """Return elapsed milliseconds for *n* handle_request calls."""
    t0 = time.perf_counter()
    for _ in range(n):
        service.handle_request(payload)
    return (time.perf_counter() - t0) * 1_000.0


# ---------------------------------------------------------------------------
# Base: spin up one stubbed BackendService per test class
# ---------------------------------------------------------------------------

class _PerfBase(unittest.TestCase):
    """Sets up a stub BackendService with no real I/O dependencies."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        data_dir = pathlib.Path(cls._tmp.name) / "data"
        store = StateStore(data_dir)
        cls.svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


# ---------------------------------------------------------------------------
# Test 1 — ping dispatch: must be <5 ms per call
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP, "SKIP_BENCH set or CI=true")
class TestPingDispatchPerf(_PerfBase):
    """ping is the hottest handler (called every 3 s by HealthMonitor).

    Budget: <5 ms per call.  We measure a single warm call after one
    throwaway call to prime any lazy-init inside handle_request.
    """

    def test_ping_single_call_under_5ms(self) -> None:
        payload = {"id": "w", "method": "ping", "params": {}}
        # warm-up
        self.svc.handle_request(payload)
        # measure
        elapsed_ms = _time_calls(self.svc, payload, 1)
        self.assertLess(
            elapsed_ms,
            5.0,
            f"ping dispatch took {elapsed_ms:.2f} ms — exceeds 5 ms budget",
        )

    def test_ping_100_calls_under_200ms(self) -> None:
        """100 consecutive pings must finish in <200 ms total (~2 ms/call avg)."""
        payload = {"id": "b", "method": "ping", "params": {}}
        # warm-up
        self.svc.handle_request(payload)
        elapsed_ms = _time_calls(self.svc, payload, 100)
        self.assertLess(
            elapsed_ms,
            200.0,
            f"100× ping took {elapsed_ms:.1f} ms — avg {elapsed_ms/100:.2f} ms/call",
        )


# ---------------------------------------------------------------------------
# Test 2 — get_settings dispatch: must be <5 ms per call
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP, "SKIP_BENCH set or CI=true")
class TestGetSettingsDispatchPerf(_PerfBase):
    """get_settings reads from a 5 s TTL cache after the first call.

    Budget: <5 ms per call (cached path must be fast).
    """

    def test_get_settings_single_call_under_5ms(self) -> None:
        payload = {"id": "g", "method": "get_settings", "params": {}}
        # prime the settings TTL cache
        self.svc.handle_request(payload)
        elapsed_ms = _time_calls(self.svc, payload, 1)
        self.assertLess(
            elapsed_ms,
            5.0,
            f"get_settings dispatch took {elapsed_ms:.2f} ms — exceeds 5 ms budget",
        )


# ---------------------------------------------------------------------------
# Test 3 — get_recording_state dispatch: must be <5 ms per call
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP, "SKIP_BENCH set or CI=true")
class TestGetRecordingStateDispatchPerf(_PerfBase):
    """get_recording_state is a simple in-memory state read.

    Budget: <5 ms per call.
    """

    def test_get_recording_state_single_call_under_5ms(self) -> None:
        payload = {"id": "r", "method": "get_recording_state", "params": {}}
        # warm-up
        self.svc.handle_request(payload)
        elapsed_ms = _time_calls(self.svc, payload, 1)
        self.assertLess(
            elapsed_ms,
            5.0,
            f"get_recording_state dispatch took {elapsed_ms:.2f} ms — exceeds 5 ms budget",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
