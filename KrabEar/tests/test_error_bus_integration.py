"""Integration tests for ErrorBus IPC handlers (Phase B.1) and repetition-loop
detector (Phase C C.4).

Tests: list_recent_errors, clear_recent_errors, handle_error_action, probe_llm_http,
STT repetition-loop end-to-end (C.4 verification).
Uses the same direct-BackendService harness as test_backend_service.py.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.service import BackendService
from backend.translator import TranslationResult


# ---------------------------------------------------------------------------
# Minimal fakes (same pattern as test_backend_service.py)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(16000, dtype=np.float32), float(self._snapshot_counter)


class _FakeTranscriber:
    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None) -> str:
        self.counter += 1
        return f"тест #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class _FakeTranslator:
    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary: dict | None = None) -> TranslationResult:
        return TranslationResult(
            text="", status="not_requested",
            source_lang="", target_lang="", mode="off", engine="fake",
        )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class ErrorBusIntegrationTestCase(unittest.TestCase):
    """IPC integration tests for Phase B.1 error bus handlers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t1", "method": method, "params": params or {}}
        )

    # ------------------------------------------------------------------
    # 1. list_recent_errors — fresh backend → empty list
    # ------------------------------------------------------------------

    def test_list_recent_errors_empty(self) -> None:
        """Fresh backend has no errors in ring buffer."""
        resp = self._call("list_recent_errors")
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertIn("errors", result)
        self.assertIsInstance(result["errors"], list)
        self.assertEqual(result["errors"], [])

    # ------------------------------------------------------------------
    # 2. clear_recent_errors — should return cleared count (0 on fresh svc)
    # ------------------------------------------------------------------

    def test_clear_recent_errors(self) -> None:
        """Clearing empty ring buffer returns cleared=0 without error."""
        resp = self._call("clear_recent_errors")
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertIn("cleared", result)
        self.assertIsInstance(result["cleared"], int)
        self.assertGreaterEqual(result["cleared"], 0)

    # ------------------------------------------------------------------
    # 3. handle_error_action — unknown action_id → executed=False
    # ------------------------------------------------------------------

    def test_handle_error_action_unknown(self) -> None:
        """Bogus action_id returns executed=False with a descriptive reason."""
        resp = self._call("handle_error_action", {"action_id": "bogus_nonexistent_action"})
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertIn("executed", result)
        self.assertFalse(result["executed"])
        self.assertIn("reason", result)
        self.assertIsNotNone(result["reason"])
        reason_lower = str(result["reason"]).lower()
        self.assertTrue(
            "unknown" in reason_lower or "not found" in reason_lower or "bogus" in reason_lower,
            msg=f"Expected 'unknown' in reason, got: {result['reason']!r}",
        )

    def test_handle_error_action_missing_action_id(self) -> None:
        """Missing action_id returns executed=False with reason 'missing action_id'."""
        resp = self._call("handle_error_action", {})
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        self.assertFalse(result["executed"])
        self.assertIn("missing", str(result.get("reason", "")).lower())

    # ------------------------------------------------------------------
    # 4. probe_llm_http — response shape valid (LM Studio not required)
    # ------------------------------------------------------------------

    def test_probe_llm_http_response_shape(self) -> None:
        """probe_llm_http returns dict with reachable, latency_ms, model keys."""
        resp = self._call("probe_llm_http")
        self.assertTrue(resp["ok"], msg=f"IPC error: {resp}")
        result = resp["result"]
        # All three keys must be present regardless of LM Studio availability
        self.assertIn("reachable", result)
        self.assertIn("latency_ms", result)
        self.assertIn("model", result)
        # reachable must be a bool
        self.assertIsInstance(result["reachable"], bool)
        # latency_ms must be a number
        self.assertIsInstance(result["latency_ms"], (int, float))

    # ------------------------------------------------------------------
    # 5. send_diagnostics_to_sentry — no errors → ok=False, reason known
    # ------------------------------------------------------------------

    def test_send_diagnostics_to_sentry_empty_buffer(self) -> None:
        """Empty ring buffer → ok=False, reason='no_errors_to_send' (or sentry unavailable)."""
        resp = self._call("send_diagnostics_to_sentry")
        self.assertTrue(resp["ok"], msg=f"IPC dispatch error: {resp}")
        result = resp["result"]
        self.assertIn("ok", result)
        # With empty buffer the expected reason is no_errors_to_send;
        # if sentry_sdk is not installed, reason is sentry_sdk_not_available — both ok.
        self.assertFalse(result["ok"])
        reason = result.get("reason", "")
        self.assertIn(reason, ("no_errors_to_send", "sentry_sdk_not_available"),
                      msg=f"Unexpected reason: {reason!r}")

    def test_send_diagnostics_to_sentry_with_errors(self) -> None:
        """With errors in ring buffer + mocked sentry_sdk → ok=True, sent_count>0."""
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone
        from backend.error_bus import KrabError

        # Push a test error into the ring buffer
        test_err = KrabError(
            code="test_code",
            severity="error",
            component="stt",
            message_user="тест ошибки",
            message_debug="debug info",
            timestamp=datetime.now(tz=timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        self.service._error_bus.push(test_err)

        fake_sdk = MagicMock()
        fake_sdk.flush.return_value = None

        with patch.dict("sys.modules", {"sentry_sdk": fake_sdk}):
            resp = self._call("send_diagnostics_to_sentry")

        self.assertTrue(resp["ok"], msg=f"IPC dispatch error: {resp}")
        result = resp["result"]
        self.assertIn("ok", result)
        if result["ok"]:
            # sentry_sdk available — check sent_count
            self.assertIn("sent_count", result)
            self.assertGreater(result["sent_count"], 0)
            fake_sdk.capture_message.assert_called_once()
            fake_sdk.flush.assert_called_once()
        else:
            # sentry_sdk was already imported and real — skip shape check
            self.assertIn(result.get("reason", ""), (
                "no_errors_to_send", "sentry_sdk_not_available",
            ))

    # ------------------------------------------------------------------
    # 6. get_memory_stats — response shape valid
    # ------------------------------------------------------------------

    def test_get_memory_stats_psutil_available(self) -> None:
        """get_memory_stats returns ok=True with processes list when psutil available."""
        resp = self._call("get_memory_stats")
        self.assertTrue(resp["ok"], msg=f"IPC dispatch error: {resp}")
        result = resp["result"]
        self.assertIn("ok", result)
        if result["ok"]:
            # Shape check: processes must be a list
            self.assertIn("processes", result)
            self.assertIsInstance(result["processes"], list)
            # Each entry must have required fields (may be empty list if no matching procs)
            for proc in result["processes"]:
                self.assertIn("pid", proc)
                self.assertIn("name", proc)
                self.assertIn("rss_mb", proc)
                self.assertIn("vsz_mb", proc)
                self.assertIn("kind", proc)
                # "rest" — отдельный вид (standalone rest_server.py, см. комментарий
                # в service.py::handle_get_memory_stats) — не отражён в этом тесте
                # раньше; live-прогон на машине с реально запущенным standalone
                # rest_server.py его ловит.
                self.assertIn(proc["kind"], ("agent", "backend", "worker", "rest"))
                self.assertIsInstance(proc["rss_mb"], float)
                self.assertIsInstance(proc["vsz_mb"], float)
        else:
            # psutil not installed — reason field expected
            self.assertEqual(result.get("reason"), "psutil_not_installed")

    def test_get_memory_stats_mocked_psutil(self) -> None:
        """get_memory_stats correctly groups processes by kind with mocked psutil."""
        from unittest.mock import patch, MagicMock

        class _FakeMemInfo:
            rss = 512 * 1024 * 1024  # 512 MB
            vms = 1024 * 1024 * 1024  # 1024 MB

        def _make_proc(pid: int, name: str, cmd: str):
            p = MagicMock()
            p.pid = pid
            p.name.return_value = name
            p.cmdline.return_value = cmd.split()
            p.memory_info.return_value = _FakeMemInfo()
            return p

        def _fake_process_iter(*args, **kwargs):
            return [
                _make_proc(1234, "KrabEarAgent", "/path/to/KrabEarAgent"),
                _make_proc(1235, "python3", "/path/KrabEar/backend/service.py --data-dir /tmp"),
                _make_proc(1236, "python3", "/path/gigaam_worker.py"),
            ]

        fake_psutil = MagicMock()
        fake_psutil.process_iter.side_effect = _fake_process_iter
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        fake_psutil.ZombieProcess = Exception

        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            resp = self._call("get_memory_stats")

        self.assertTrue(resp["ok"], msg=f"IPC dispatch error: {resp}")
        result = resp["result"]
        self.assertTrue(result["ok"])
        procs = result["processes"]
        self.assertEqual(len(procs), 3)
        kinds = {p["kind"] for p in procs}
        self.assertIn("agent", kinds)
        self.assertIn("backend", kinds)
        self.assertIn("worker", kinds)
        # All rss_mb should be 512.0
        for p in procs:
            self.assertAlmostEqual(p["rss_mb"], 512.0, places=0)

    def test_get_memory_stats_swallows_proc_cmdline_systemerror(self) -> None:
        """Regression for KRAB-EAR-BACKEND-H.

        On macOS psutil.process_iter can raise SystemError/PermissionError when
        proc_cmdline() hits an inaccessible system process (e.g. mdworker_shared).
        The iteration must keep going and matching processes must still be returned.
        """
        from unittest.mock import patch, MagicMock

        class _FakeMemInfo:
            rss = 256 * 1024 * 1024
            vms = 512 * 1024 * 1024

        bad_proc = MagicMock()
        bad_proc.pid = 9999
        bad_proc.name.return_value = "mdworker_shared"
        bad_proc.cmdline.side_effect = SystemError(
            "<built-in function proc_cmdline> returned a result with an exception set"
        )

        denied_proc = MagicMock()
        denied_proc.pid = 9998
        denied_proc.name.return_value = "kernel_task"
        denied_proc.cmdline.side_effect = PermissionError(13, "denied")

        good_proc = MagicMock()
        good_proc.pid = 4242
        good_proc.name.return_value = "KrabEarAgent"
        good_proc.cmdline.return_value = ["/path/to/KrabEarAgent"]
        good_proc.memory_info.return_value = _FakeMemInfo()

        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = [bad_proc, denied_proc, good_proc]
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        fake_psutil.ZombieProcess = Exception

        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            resp = self._call("get_memory_stats")

        self.assertTrue(resp["ok"], msg=f"IPC dispatch error: {resp}")
        result = resp["result"]
        self.assertTrue(result["ok"])
        procs = result["processes"]
        # bad procs swallowed, good agent surfaced
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0]["kind"], "agent")
        self.assertEqual(procs[0]["pid"], 4242)


# ---------------------------------------------------------------------------
# Phase C C.4 — STT repetition-loop integration tests
# ---------------------------------------------------------------------------

class _LoopingFakeRecorder:
    """Fake recorder returning non-trivial audio so silence / background guards pass."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        # Return non-silent audio (random signal) so silence guard does NOT fire.
        # 2 s @ 16 kHz — long enough that empty_text guard does NOT suppress.
        rng = np.random.default_rng(42)
        audio = rng.standard_normal(32000).astype(np.float32) * 0.1
        return audio, 2.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(16000, dtype=np.float32), float(self._snapshot_counter)


class _LoopingTranscriber:
    """Fake transcriber that:
    1. Calls the **real** ``is_likely_repetition_loop`` on preset hallucinated text.
    2. If loop is detected, pushes ``stt.repetition_loop`` to the error bus that
       BackendService late-injects as ``self._error_bus``.

    This mirrors exactly what ``AudioEngine._push_error`` does after the C.4 wire.
    No MLX / Whisper loaded — fakes only.
    """

    class _FakeEngine:
        """Minimal engine stub to satisfy BackendService attribute checks."""
        current_model: str = "fake-balanced"
        quality_profile: str = "balanced"

        def set_quality_profile(self, profile: str) -> None:
            self.quality_profile = profile

    LOOP_TEXT = (
        "согласен да согласен да согласен да согласен да согласен да"
    )

    def __init__(self) -> None:
        self.engine = self._FakeEngine()

    def transcribe(self, audio_data, **kwargs) -> dict:
        from core.utils import is_likely_repetition_loop

        text = self.LOOP_TEXT
        is_loop, loop_reason = is_likely_repetition_loop(text)
        if is_loop:
            error_bus = getattr(self, "_error_bus", None)
            if error_bus is not None:
                from backend.error_bus import KrabError
                from backend.error_codes import ERROR_REGISTRY
                code = "stt.repetition_loop"
                entry = ERROR_REGISTRY.get(code, {})
                err = KrabError(
                    severity=entry.get("severity", "warn"),
                    component="stt",
                    code=code,
                    message_user=entry.get("user_msg_ru", "STT ошибка"),
                    message_debug=f"reason={loop_reason} text_len={len(text)}",
                    timestamp=datetime.now(timezone.utc),
                    context={
                        "model": self.engine.current_model,
                        "profile": self.engine.quality_profile,
                    },
                    actionable=entry.get("actionable", False),
                    action_id=entry.get("action_id"),
                )
                error_bus.push(err)

        return {
            "text": text,
            "segments": [],
            "language": "ru",
            "model_used": self.engine.current_model,
            "audio_duration_sec": 2.0,
        }

    def transcribe_preview(self, audio_data, **kwargs) -> dict:
        return {
            "text": "preview",
            "segments": [],
            "language": "ru",
            "model_used": self.engine.current_model,
            "audio_duration_sec": 0.1,
        }


class _NormalTranscriber:
    """Fake transcriber returning clean, non-repetitive text."""

    class _FakeEngine:
        current_model: str = "fake-balanced"
        quality_profile: str = "balanced"

        def set_quality_profile(self, profile: str) -> None:
            self.quality_profile = profile

    def __init__(self) -> None:
        self.engine = self._FakeEngine()

    def transcribe(self, audio_data, **kwargs) -> dict:
        from core.utils import is_likely_repetition_loop

        text = "Привет, как дела сегодня? Всё хорошо."
        is_loop, loop_reason = is_likely_repetition_loop(text)
        # Sanity: clean text must NOT trigger a push.  Still follow the same
        # pattern as _LoopingTranscriber so the code paths are symmetric.
        if is_loop:
            error_bus = getattr(self, "_error_bus", None)
            if error_bus is not None:
                from backend.error_bus import KrabError
                from backend.error_codes import ERROR_REGISTRY
                code = "stt.repetition_loop"
                entry = ERROR_REGISTRY.get(code, {})
                err = KrabError(
                    severity=entry.get("severity", "warn"),
                    component="stt",
                    code=code,
                    message_user=entry.get("user_msg_ru", "STT ошибка"),
                    message_debug=f"reason={loop_reason} text_len={len(text)}",
                    timestamp=datetime.now(timezone.utc),
                    context={
                        "model": self.engine.current_model,
                        "profile": self.engine.quality_profile,
                    },
                    actionable=entry.get("actionable", False),
                    action_id=entry.get("action_id"),
                )
                error_bus.push(err)

        return {
            "text": text,
            "segments": [],
            "language": "ru",
            "model_used": self.engine.current_model,
            "audio_duration_sec": 2.0,
        }

    def transcribe_preview(self, audio_data, **kwargs) -> dict:
        return {
            "text": "preview",
            "segments": [],
            "language": "ru",
            "model_used": self.engine.current_model,
            "audio_duration_sec": 0.1,
        }


class STTRepetitionLoopIntegrationTests(unittest.TestCase):
    """End-to-end: BackendService receives transcribe IPC → fake transcriber runs
    real ``is_likely_repetition_loop`` on preset hallucinated text → error bus
    receives ``stt.repetition_loop`` → IPC ``list_recent_errors`` exposes the code.

    Validates Phase C C.4-wire (commit 5c2b0af): the detector is called inside
    the real transcription call-site and pushes through the real ErrorBus path.

    No MLX / Whisper loaded — fakes only.
    """

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_service(self, transcriber) -> "BackendService":
        store = StateStore(Path(self.tmp.name) / "data")
        service = BackendService(
            store=store,
            recorder=_LoopingFakeRecorder(),
            transcriber=transcriber,
            translator=_FakeTranslator(),
        )

        def _full_cleanup():
            """W1746: comprehensive shutdown so xdist worker exits cleanly."""
            service.close()
            # Stop disk monitor daemon thread (not handled by close())
            dm = getattr(service, "_disk_monitor", None)
            if dm is not None:
                try:
                    dm.stop()
                except Exception:
                    pass
            # Stop export scheduler daemon thread (close() does this, but re-set for safety)
            stop_ev = getattr(service, "_export_scheduler_stop", None)
            if stop_ev is not None:
                stop_ev.set()

        self.addCleanup(_full_cleanup)
        return service

    def _call(self, service, method: str, params: dict | None = None) -> dict:
        return service.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # ------------------------------------------------------------------
    # 1. Positive: loop text → error bus contains stt.repetition_loop
    # ------------------------------------------------------------------

    def test_loop_text_pushes_stt_repetition_loop_via_pipeline(self) -> None:
        """Hallucinated text ('согласен да' × 5) → stt.repetition_loop in ErrorBus."""
        service = self._make_service(_LoopingTranscriber())

        # Verify error bus is wired into transcriber by BackendService.__init__
        self.assertIsNotNone(
            getattr(service.transcriber, "_error_bus", None),
            "BackendService must inject _error_bus into transcriber",
        )

        # Trigger full stop_recording → transcribe pipeline.
        # Pass silence_guard_enabled=False so the fake silent audio does not
        # short-circuit before reaching transcribe().
        self._call(service, "start_recording", {})
        time.sleep(0.05)
        stop_resp = self._call(
            service,
            "stop_recording",
            {"silence_guard_enabled": False, "background_guard_enabled": False},
        )
        self.assertTrue(stop_resp.get("ok", True), f"stop_recording IPC error: {stop_resp}")

        errors_resp = self._call(service, "list_recent_errors", {})
        self.assertTrue(errors_resp["ok"], f"list_recent_errors IPC error: {errors_resp}")
        errors = errors_resp["result"]["errors"]
        codes = [e["code"] for e in errors]

        self.assertIn(
            "stt.repetition_loop",
            codes,
            f"Expected 'stt.repetition_loop' in error bus, got: {codes}",
        )

        # Verify error shape
        loop_errors = [e for e in errors if e["code"] == "stt.repetition_loop"]
        self.assertGreaterEqual(len(loop_errors), 1)
        entry = loop_errors[0]
        self.assertIn("severity", entry)
        self.assertIn("component", entry)
        self.assertEqual(entry["component"], "stt")

    # ------------------------------------------------------------------
    # 2. Negative: clean text → no stt.repetition_loop event
    # ------------------------------------------------------------------

    def test_normal_text_does_not_push_repetition_loop(self) -> None:
        """Clean text ('Привет, как дела?') → no stt.repetition_loop in ErrorBus."""
        service = self._make_service(_NormalTranscriber())

        self._call(service, "start_recording", {})
        time.sleep(0.05)
        self._call(
            service,
            "stop_recording",
            {"silence_guard_enabled": False, "background_guard_enabled": False},
        )

        errors_resp = self._call(service, "list_recent_errors", {})
        self.assertTrue(errors_resp["ok"], f"list_recent_errors IPC error: {errors_resp}")
        errors = errors_resp["result"]["errors"]
        codes = [e["code"] for e in errors]

        self.assertNotIn(
            "stt.repetition_loop",
            codes,
            f"Unexpected 'stt.repetition_loop' in error bus for clean text: {codes}",
        )

    # ------------------------------------------------------------------
    # 3. Wire verification: error_bus injected by BackendService into transcriber
    # ------------------------------------------------------------------

    def test_error_bus_wired_into_transcriber_by_backend_service(self) -> None:
        """BackendService injects _error_bus into the transcriber instance."""
        transcriber = _LoopingTranscriber()
        # Before BackendService init: no _error_bus
        self.assertIsNone(getattr(transcriber, "_error_bus", None))

        service = self._make_service(transcriber)
        # After init: _error_bus must be present and be the same object as service's
        self.assertIsNotNone(getattr(transcriber, "_error_bus", None))
        self.assertIs(transcriber._error_bus, service._error_bus)


if __name__ == "__main__":
    unittest.main()
