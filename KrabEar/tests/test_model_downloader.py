"""Тесты ModelDownloader — фоновой загрузки STT-моделей с прогрессом.

Все тесты используют mock snapshot_download — реальные 1.5 GB модели не скачиваются.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# sys.path setup (must precede backend imports)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from backend.model_downloader import ModelDownloader  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal EventBus stub (non-blocking, records events)
# ---------------------------------------------------------------------------

class _StubEventBus:
    """Thread-safe stub that captures emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.events.append((event_type, payload))

    def events_by_type(self, etype: str) -> list[dict[str, Any]]:
        with self._lock:
            return [p for et, p in self.events if et == etype]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_MODEL = "mlx-community/whisper-large-v3-turbo"
_FAKE_PATH = "/tmp/fake_hf_cache/models--mlx-community--whisper-large-v3-turbo"


def _make_downloader(bus: "_StubEventBus | None" = None) -> ModelDownloader:
    return ModelDownloader(event_bus=bus)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestModelDownloaderAlreadyCached(unittest.TestCase):
    """When the model is already cached, start_download returns immediately."""

    def test_already_cached_returns_status(self) -> None:
        bus = _StubEventBus()
        dl = _make_downloader(bus)
        with patch.object(dl, "_is_cached", return_value=True):
            result = dl.start_download(_FAKE_MODEL)
        self.assertEqual(result, "already_cached")

    def test_already_cached_does_not_call_snapshot_download(self) -> None:
        dl = _make_downloader()
        with patch.object(dl, "_is_cached", return_value=True), \
                patch("backend.model_downloader.ModelDownloader._download_worker") as mock_worker:
            dl.start_download(_FAKE_MODEL)
        mock_worker.assert_not_called()

    def test_already_cached_state_is_done(self) -> None:
        dl = _make_downloader()
        # NOTE: get_status() must run INSIDE the _is_cached patch — it calls
        # _is_cached() to compute the "cached" field. If left outside the patch
        # it hits the REAL HF cache lookup, which returns True/False depending on
        # whether the runner happens to have whisper-large-v3-turbo cached →
        # environment-dependent flake (green on ubuntu where no cache exists,
        # red on a macOS runner without that model cached, and vice-versa).
        with patch.object(dl, "_is_cached", return_value=True), \
                patch.object(dl, "_model_cache_path", return_value=Path(_FAKE_PATH)):
            dl.start_download(_FAKE_MODEL)
            status = dl.get_status(_FAKE_MODEL)
        self.assertEqual(status["status"], "done")
        self.assertTrue(status["cached"])
        self.assertFalse(status["downloading"])


class TestModelDownloaderStartsThread(unittest.TestCase):
    """start_download launches a background thread when model is missing."""

    def test_starts_and_emits_done(self) -> None:
        bus = _StubEventBus()
        dl = _make_downloader(bus)
        # _is_cached returns False initially, True after "download"
        call_count = [0]

        def fake_is_cached(model_id: str) -> bool:
            call_count[0] += 1
            # First call (pre-thread): not cached; subsequent (in thread): cached after lock
            return call_count[0] > 1

        with patch.object(dl, "_is_cached", side_effect=fake_is_cached), \
                patch("backend.model_downloader.snapshot_download",
                      return_value=_FAKE_PATH, create=True), \
                patch("huggingface_hub.snapshot_download", return_value=_FAKE_PATH,
                      create=True):
            status = dl.start_download(_FAKE_MODEL)
        self.assertEqual(status, "started")

    def test_done_event_emitted_after_download(self) -> None:
        bus = _StubEventBus()
        dl = _make_downloader(bus)

        done_event = threading.Event()

        def fake_snapshot_download(**kwargs: Any) -> str:
            return _FAKE_PATH

        with patch.object(dl, "_is_cached", side_effect=[False, False, True]), \
                patch("huggingface_hub.snapshot_download",
                      side_effect=fake_snapshot_download, create=True):
            dl.start_download(_FAKE_MODEL)

        # Wait for background thread to finish (max 5s)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            done_events = bus.events_by_type("model_download.progress")
            if any(e.get("status") == "done" for e in done_events):
                done_event.set()
                break
            time.sleep(0.05)

        self.assertTrue(done_event.is_set(), "Expected 'done' progress event within 5s")
        done_payloads = [
            e for e in bus.events_by_type("model_download.progress")
            if e.get("status") == "done"
        ]
        self.assertGreater(len(done_payloads), 0)
        self.assertEqual(done_payloads[0]["model_id"], _FAKE_MODEL)
        self.assertAlmostEqual(done_payloads[0]["pct"], 100.0)

    def test_initial_progress_event_emitted(self) -> None:
        """A 'downloading' event is emitted at the start before snapshot_download runs."""
        bus = _StubEventBus()
        dl = _make_downloader(bus)

        download_started = threading.Event()
        download_proceed = threading.Event()

        def fake_snapshot_download(**kwargs: Any) -> str:
            download_started.set()
            download_proceed.wait(timeout=2.0)
            return _FAKE_PATH

        with patch.object(dl, "_is_cached", side_effect=[False, False, True]), \
                patch("huggingface_hub.snapshot_download",
                      side_effect=fake_snapshot_download, create=True):
            dl.start_download(_FAKE_MODEL)
            download_started.wait(timeout=3.0)
            download_proceed.set()

        # Wait for thread to complete
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any(e.get("status") == "done"
                   for e in bus.events_by_type("model_download.progress")):
                break
            time.sleep(0.05)

        all_events = bus.events_by_type("model_download.progress")
        statuses = [e["status"] for e in all_events]
        self.assertIn("downloading", statuses)


class TestModelDownloaderErrorPath(unittest.TestCase):
    """When snapshot_download raises, status becomes error and event is emitted."""

    def test_error_status_on_exception(self) -> None:
        bus = _StubEventBus()
        dl = _make_downloader(bus)

        def fake_snapshot_download(**kwargs: Any) -> str:
            raise OSError("Network unreachable")

        # Persist mock for the lifetime of the thread — use start()/stop() not context mgr.
        is_cached_mock = patch.object(dl, "_is_cached", return_value=False)
        hf_mock = patch("huggingface_hub.snapshot_download",
                        side_effect=fake_snapshot_download, create=True)
        is_cached_mock.start()
        hf_mock.start()
        try:
            dl.start_download(_FAKE_MODEL)
            # Wait for background thread
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                error_events = [
                    e for e in bus.events_by_type("model_download.progress")
                    if e.get("status") == "error"
                ]
                if error_events:
                    break
                time.sleep(0.05)

            # Read internal state directly (bypasses the get_status _is_cached override)
            internal = dl._get_or_create_state(_FAKE_MODEL).to_dict()
            self.assertEqual(internal["status"], "error")
            self.assertIn("Network unreachable", internal["error_msg"])
            self.assertFalse(internal["status"] == "downloading")
        finally:
            is_cached_mock.stop()
            hf_mock.stop()

    def test_error_does_not_crash_thread(self) -> None:
        """The worker catches exceptions and never re-raises."""
        dl = _make_downloader()

        def bad_download(**kwargs: Any) -> str:
            raise RuntimeError("Catastrophic failure")

        is_cached_mock = patch.object(dl, "_is_cached", return_value=False)
        hf_mock = patch("huggingface_hub.snapshot_download",
                        side_effect=bad_download, create=True)
        is_cached_mock.start()
        hf_mock.start()
        try:
            dl.start_download(_FAKE_MODEL)
            # Give the thread time to fail
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                internal = dl._get_or_create_state(_FAKE_MODEL).to_dict()
                if internal["status"] in ("error", "done"):
                    break
                time.sleep(0.05)

            # Read internal state directly (bypasses get_status _is_cached override)
            internal = dl._get_or_create_state(_FAKE_MODEL).to_dict()
            self.assertEqual(internal["status"], "error",
                             "Worker should catch exception and mark status=error, not re-raise")
        finally:
            is_cached_mock.stop()
            hf_mock.stop()


class TestModelDownloaderLock(unittest.TestCase):
    """Only one download at a time; concurrent requests get in_progress."""

    def test_concurrent_download_returns_in_progress(self) -> None:
        dl = _make_downloader()

        hold_lock = threading.Event()
        release_lock = threading.Event()

        def slow_snapshot_download(**kwargs: Any) -> str:
            hold_lock.set()
            release_lock.wait(timeout=3.0)
            return _FAKE_PATH

        # Patch _is_cached: always False so the first call enters the worker.
        with patch.object(dl, "_is_cached", return_value=False), \
                patch("huggingface_hub.snapshot_download",
                      side_effect=slow_snapshot_download, create=True):
            # Start first download
            result1 = dl.start_download(_FAKE_MODEL)
            # Wait until worker thread has acquired dl_lock
            hold_lock.wait(timeout=3.0)
            # Second call on same model: state is already "downloading"
            result2 = dl.start_download(_FAKE_MODEL)
            # Release the first worker
            release_lock.set()

        self.assertEqual(result1, "started")
        self.assertEqual(result2, "in_progress")


class TestModelDownloaderGetStatus(unittest.TestCase):
    """get_status returns correctly shaped dict."""

    def test_idle_status_shape(self) -> None:
        dl = _make_downloader()
        with patch.object(dl, "_is_cached", return_value=False):
            status = dl.get_status(_FAKE_MODEL)
        # F2-LOW wave2: 'path' must NOT be in the response (no absolute FS path exposed).
        expected_keys = {
            "model_id", "cached", "downloading", "status",
            "pct", "downloaded", "total", "error_msg",
        }
        self.assertEqual(set(status.keys()), expected_keys)
        self.assertFalse(status["cached"])
        self.assertFalse(status["downloading"])
        self.assertEqual(status["status"], "idle")

    def test_cached_model_reflects_done(self) -> None:
        dl = _make_downloader()
        with patch.object(dl, "_is_cached", return_value=True):
            status = dl.get_status(_FAKE_MODEL)
        self.assertTrue(status["cached"])
        self.assertEqual(status["status"], "done")
        self.assertAlmostEqual(status["pct"], 100.0)

    def test_model_id_preserved(self) -> None:
        dl = _make_downloader()
        with patch.object(dl, "_is_cached", return_value=False):
            status = dl.get_status(_FAKE_MODEL)
        self.assertEqual(status["model_id"], _FAKE_MODEL)


class TestModelDownloaderProgressThrottle(unittest.TestCase):
    """Progress events are throttled to avoid EventBus flooding."""

    def test_rapid_updates_are_throttled(self) -> None:
        bus = _StubEventBus()
        dl = _make_downloader(bus)
        # Fire 100 tiny updates (0.01% each) in rapid succession.
        for i in range(100):
            dl._on_progress(_FAKE_MODEL, i * 1024, 100 * 1024, i * 0.01)
        events = bus.events_by_type("model_download.progress")
        # Should be far fewer than 100 due to throttling.
        self.assertLess(len(events), 50,
                        "Throttling should reduce rapid updates to much fewer events")

    def test_large_pct_jump_always_emits(self) -> None:
        """A 10% jump should emit regardless of time elapsed."""
        bus = _StubEventBus()
        dl = _make_downloader(bus)
        # First emit at 0%
        dl._on_progress(_FAKE_MODEL, 0, 100, 0.0)
        initial_count = len(bus.events_by_type("model_download.progress"))
        # Jump to 10% — exceeds _PROGRESS_MIN_PCT_DELTA
        dl._on_progress(_FAKE_MODEL, 10, 100, 10.0)
        new_count = len(bus.events_by_type("model_download.progress"))
        self.assertGreater(new_count, initial_count)


class _FakeRecorder:
    """Duck-type AudioRecorder for BackendService construction."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        return None

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(32000, dtype=np.float32), 0.0


class _FakeTranscriber:
    vocabulary: list[str] = []
    profile: str = "default"

    def transcribe(self, audio, sample_rate=16000, **kwargs):
        return "fake transcript", 0.9, None

    def warmup(self) -> None:
        pass


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(translated=text, source_lang="ru", target_lang="en")


class TestDispatchTableWiring(unittest.TestCase):
    """Verify download_stt_model and get_stt_model_status are in the dispatch table."""

    def setUp(self) -> None:
        import tempfile
        from backend.state_store import StateStore
        from backend.service import BackendService

        self._tmpdir = tempfile.mkdtemp()
        store = StateStore(Path(self._tmpdir) / "data")
        self._service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        self._service.close()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_download_stt_model_in_dispatch(self) -> None:
        self.assertIn("download_stt_model", self._service._dispatch_table)

    def test_get_stt_model_status_in_dispatch(self) -> None:
        self.assertIn("get_stt_model_status", self._service._dispatch_table)

    def test_download_stt_model_already_cached(self) -> None:
        with patch.object(
            self._service._model_downloader, "_is_cached", return_value=True
        ):
            result = self._service._dispatch_table["download_stt_model"]({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "already_cached")

    def test_get_stt_model_status_shape(self) -> None:
        with patch.object(
            self._service._model_downloader, "_is_cached", return_value=False
        ):
            result = self._service._dispatch_table["get_stt_model_status"]({})
        self.assertTrue(result["ok"])
        self.assertIn("cached", result)
        self.assertIn("downloading", result)
        self.assertIn("pct", result)

    def test_download_stt_model_empty_model_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._service._dispatch_table["download_stt_model"]({"model_id": ""})

    def test_download_stt_model_non_string_model_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._service._dispatch_table["download_stt_model"]({"model_id": 123})

    def test_cancel_stt_model_download_in_dispatch(self) -> None:
        """F1-MED wave2: cancel handler must be wired in dispatch table."""
        self.assertIn("cancel_stt_model_download", self._service._dispatch_table)

    def test_cancel_not_downloading_returns_false(self) -> None:
        """cancel_stt_model_download when idle returns cancelled=False."""
        with patch.object(
            self._service._model_downloader, "_is_cached", return_value=False
        ):
            result = self._service._dispatch_table["cancel_stt_model_download"]({})
        self.assertTrue(result["ok"])
        self.assertFalse(result["cancelled"])

    def test_cancel_while_downloading_returns_true(self) -> None:
        """cancel_stt_model_download while active returns cancelled=True."""
        dl = self._service._model_downloader
        # Force state to 'downloading'
        state = dl._get_or_create_state(_FAKE_MODEL)
        state.update(status="downloading")
        result = self._service._dispatch_table["cancel_stt_model_download"](
            {"model_id": _FAKE_MODEL}
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["cancelled"])

    def test_get_stt_model_status_no_absolute_path(self) -> None:
        """F2-LOW wave2: 'path' field must NOT appear in get_stt_model_status response."""
        with patch.object(
            self._service._model_downloader, "_is_cached", return_value=False
        ):
            result = self._service._dispatch_table["get_stt_model_status"]({})
        self.assertNotIn("path", result,
                         "Absolute FS path must not be exposed via IPC (F2-LOW)")

    def test_download_model_id_too_long_raises(self) -> None:
        """F4-LOW wave2: model_id > MAX_MODEL_ID_LEN must raise ValueError."""
        from backend.model_downloader import MAX_MODEL_ID_LEN
        long_id = "a" * (MAX_MODEL_ID_LEN + 1)
        with self.assertRaises(ValueError):
            self._service._dispatch_table["download_stt_model"]({"model_id": long_id})

    def test_get_status_model_id_too_long_raises(self) -> None:
        """F4-LOW wave2: model_id > MAX_MODEL_ID_LEN must raise ValueError."""
        from backend.model_downloader import MAX_MODEL_ID_LEN
        long_id = "x" * (MAX_MODEL_ID_LEN + 1)
        with self.assertRaises(ValueError):
            self._service._dispatch_table["get_stt_model_status"]({"model_id": long_id})

    def test_cancel_model_id_too_long_raises(self) -> None:
        """F4-LOW wave2: cancel also validates model_id length."""
        from backend.model_downloader import MAX_MODEL_ID_LEN
        long_id = "z" * (MAX_MODEL_ID_LEN + 1)
        with self.assertRaises(ValueError):
            self._service._dispatch_table["cancel_stt_model_download"]({"model_id": long_id})


# ---------------------------------------------------------------------------
# F1-MED: cancel releases _dl_lock so a new download can start after cancel
# ---------------------------------------------------------------------------

class TestCancelReleasesLock(unittest.TestCase):
    """After a cancelled download, a NEW download for any model must be startable."""

    def test_cancel_releases_lock_for_next_download(self) -> None:
        """F1-MED wave2 regression: cancel() must not permanently hold _dl_lock.

        Before fix: _dl_lock was held inside `with self._dl_lock:` — a cancel
        raised inside the tqdm callback would propagate as an unhandled exception
        out of the `with` block, releasing the lock correctly ONLY if Python's
        `with` unwound the lock.  With try/finally the lock is GUARANTEED to
        release even on BaseException subclasses.
        """
        bus = _StubEventBus()
        dl = ModelDownloader(event_bus=bus, stall_timeout_sec=300.0)

        download_entered = threading.Event()
        allow_cancel = threading.Event()

        def fake_snapshot_download(**kwargs: Any) -> str:
            download_entered.set()
            allow_cancel.wait(timeout=3.0)
            # After cancel is signalled, simulate a progress callback that checks it.
            dl._on_progress(_FAKE_MODEL, 0, 0, 0.0)
            return _FAKE_PATH

        is_cached_p = patch.object(dl, "_is_cached", return_value=False)
        hf_p = patch("huggingface_hub.snapshot_download",
                     side_effect=fake_snapshot_download, create=True)
        is_cached_p.start()
        hf_p.start()
        try:
            dl.start_download(_FAKE_MODEL)
            download_entered.wait(timeout=3.0)

            # Signal cancel while download is in progress.
            dl.cancel(_FAKE_MODEL)
            allow_cancel.set()

            # Wait for worker to finish (cancelled state).
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                internal = dl._get_or_create_state(_FAKE_MODEL).to_dict()
                if internal["status"] in ("cancelled", "error"):
                    break
                time.sleep(0.05)

            # Now the lock must be free: try to acquire it with timeout.
            acquired = dl._dl_lock.acquire(timeout=3.0)
            if acquired:
                dl._dl_lock.release()
            self.assertTrue(acquired, "_dl_lock must be released after cancel (F1-MED)")
        finally:
            is_cached_p.stop()
            hf_p.stop()


# ---------------------------------------------------------------------------
# F1-MED: stall watchdog trips error when no byte progress for timeout
# ---------------------------------------------------------------------------

class TestStallWatchdog(unittest.TestCase):
    """Stall watchdog: no byte progress for > stall_timeout_sec → error status."""

    def test_stall_triggers_error_and_releases_lock(self) -> None:
        """F1-MED wave2: simulate stall by freezing byte count past the timeout."""
        bus = _StubEventBus()
        # Very short stall timeout (0.1 s) so the test is fast.
        dl = ModelDownloader(event_bus=bus, stall_timeout_sec=0.1)

        progress_fired = threading.Event()
        allow_stall_check = threading.Event()

        def fake_snapshot_download(**kwargs: Any) -> str:
            # Fire one initial progress call to register the stall start time.
            dl._on_progress(_FAKE_MODEL, 100, 10000, 1.0)
            progress_fired.set()
            allow_stall_check.wait(timeout=3.0)
            # Fire another call with the SAME byte count (stalled) after the timeout.
            dl._on_progress(_FAKE_MODEL, 100, 10000, 1.0)
            # Should never reach here — _on_progress raises _DownloadCancelled.
            return _FAKE_PATH  # pragma: no cover

        is_cached_p = patch.object(dl, "_is_cached", return_value=False)
        hf_p = patch("huggingface_hub.snapshot_download",
                     side_effect=fake_snapshot_download, create=True)
        is_cached_p.start()
        hf_p.start()
        try:
            dl.start_download(_FAKE_MODEL)
            progress_fired.wait(timeout=3.0)
            # Wait slightly longer than stall timeout before the second progress call.
            time.sleep(0.15)
            allow_stall_check.set()

            # Wait for worker to settle.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                internal = dl._get_or_create_state(_FAKE_MODEL).to_dict()
                if internal["status"] in ("error", "cancelled"):
                    break
                time.sleep(0.05)

            internal = dl._get_or_create_state(_FAKE_MODEL).to_dict()
            self.assertIn(internal["status"], ("error", "cancelled"),
                          "Stall must trip error/cancelled (F1-MED)")
            self.assertIn("stalled", internal["error_msg"].lower(),
                          "error_msg must mention 'stalled' (F1-MED)")

            # Lock must be released.
            acquired = dl._dl_lock.acquire(timeout=3.0)
            if acquired:
                dl._dl_lock.release()
            self.assertTrue(acquired, "_dl_lock must be released after stall (F1-MED)")
        finally:
            is_cached_p.stop()
            hf_p.stop()


# ---------------------------------------------------------------------------
# F3-LOW: error / cancel events include error_msg in EventBus payload
# ---------------------------------------------------------------------------

class TestErrorEventIncludesErrorMsg(unittest.TestCase):
    """F3-LOW wave2: EventBus error/cancel event must carry error_msg."""

    def _wait_for_status(self, bus: _StubEventBus, status: str, timeout: float = 5.0) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = [
                e for e in bus.events_by_type("model_download.progress")
                if e.get("status") == status
            ]
            if events:
                return events
            time.sleep(0.05)
        return []

    def test_error_event_has_error_msg(self) -> None:
        bus = _StubEventBus()
        dl = ModelDownloader(event_bus=bus, stall_timeout_sec=300.0)

        def bad_download(**kwargs: Any) -> str:
            raise OSError("Mirror unreachable")

        is_p = patch.object(dl, "_is_cached", return_value=False)
        hf_p = patch("huggingface_hub.snapshot_download",
                     side_effect=bad_download, create=True)
        is_p.start()
        hf_p.start()
        try:
            dl.start_download(_FAKE_MODEL)
            events = self._wait_for_status(bus, "error")
            self.assertTrue(events, "Expected error event")
            self.assertIn("error_msg", events[0],
                          "F3-LOW: error event must include error_msg key")
            self.assertIn("Mirror unreachable", events[0]["error_msg"])
        finally:
            is_p.stop()
            hf_p.stop()

    def test_cancel_event_has_error_msg(self) -> None:
        bus = _StubEventBus()
        dl = ModelDownloader(event_bus=bus, stall_timeout_sec=300.0)

        download_entered = threading.Event()
        allow_cancel = threading.Event()

        def fake_download(**kwargs: Any) -> str:
            download_entered.set()
            allow_cancel.wait(timeout=3.0)
            dl._on_progress(_FAKE_MODEL, 0, 0, 0.0)
            return _FAKE_PATH  # pragma: no cover

        is_p = patch.object(dl, "_is_cached", return_value=False)
        hf_p = patch("huggingface_hub.snapshot_download",
                     side_effect=fake_download, create=True)
        is_p.start()
        hf_p.start()
        try:
            dl.start_download(_FAKE_MODEL)
            download_entered.wait(timeout=3.0)
            dl.cancel(_FAKE_MODEL)
            allow_cancel.set()

            events = self._wait_for_status(bus, "cancelled")
            self.assertTrue(events, "Expected cancelled event")
            self.assertIn("error_msg", events[0],
                          "F3-LOW: cancel event must include error_msg key")
        finally:
            is_p.stop()
            hf_p.stop()


# ---------------------------------------------------------------------------
# F4-LOW: _states cap — unbounded growth prevented
# ---------------------------------------------------------------------------

class TestStatesCap(unittest.TestCase):
    """F4-LOW wave2: _states dict must not grow unboundedly past _MAX_STATES."""

    def test_states_eviction_when_cap_exceeded(self) -> None:
        from backend.model_downloader import _MAX_STATES
        dl = ModelDownloader(stall_timeout_sec=300.0)

        # Fill _states with idle entries up to cap + some.
        for i in range(_MAX_STATES + 10):
            dl._get_or_create_state(f"fake-org/model-{i}")

        # After eviction _states must not exceed cap.
        with dl._states_lock:
            count = len(dl._states)
        self.assertLessEqual(count, _MAX_STATES,
                             f"_states must be capped at {_MAX_STATES} (F4-LOW)")

    def test_downloading_state_not_evicted(self) -> None:
        from backend.model_downloader import _MAX_STATES
        dl = ModelDownloader(stall_timeout_sec=300.0)

        # Mark one model as downloading.
        active = "org/active-model"
        state = dl._get_or_create_state(active)
        state.update(status="downloading")

        # Fill the rest with idle entries past cap.
        for i in range(_MAX_STATES + 5):
            dl._get_or_create_state(f"idle-org/model-{i}")

        with dl._states_lock:
            self.assertIn(active, dl._states,
                          "Active (downloading) state must not be evicted (F4-LOW)")


if __name__ == "__main__":
    unittest.main()
