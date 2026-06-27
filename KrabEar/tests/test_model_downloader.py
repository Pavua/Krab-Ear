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
        expected_keys = {
            "model_id", "cached", "downloading", "status",
            "pct", "downloaded", "total", "error_msg", "path",
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


if __name__ == "__main__":
    unittest.main()
