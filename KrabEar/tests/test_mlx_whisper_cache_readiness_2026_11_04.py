"""Неполный HF snapshot не разрешает пропуск загрузки STT; без сети/ML."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.event_bus import EventBus
from backend.model_downloader import ModelDownloader
from backend.startup_diagnostics import StartupDiagnostics

MODEL = "mlx-community/whisper-large-v3-turbo"
REVISION = "a" * 40


class MlxWhisperCacheReadinessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ear-hf-readiness-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.hub = self.home / "hub"
        self.model = self.hub / ("models--" + MODEL.replace("/", "--"))
        self.snapshot = self.model / "snapshots" / REVISION
        self.snapshot.mkdir(parents=True)
        (self.model / "refs").mkdir()
        (self.model / "refs" / "main").write_text(REVISION)
        (self.snapshot / "config.json").write_text("{}")
        self.env = patch.dict("os.environ", {"HF_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def assert_cached(self, expected):
        # Конструктор запускает LM Studio probe: проверяем только локальный cache-check.
        diag = StartupDiagnostics.__new__(StartupDiagnostics)
        diag._error_bus = None
        with patch("core.config.settings", SimpleNamespace(MODEL_BALANCED=MODEL)):
            result = diag._check_stt_model_cached()
        with self.subTest(consumer="startup"):
            self.assertEqual(result.details["cached"], expected)
            self.assertEqual(result.status, "ok" if expected else "warning")
        with self.subTest(consumer="downloader"):
            self.assertEqual(ModelDownloader()._is_cached(MODEL), expected)

    def test_config_without_weights_is_not_cached(self):
        self.assert_cached(False)

    def test_zero_length_weights_are_not_cached(self):
        (self.snapshot / "weights.npz").touch()
        self.assert_cached(False)

    def test_broken_weight_symlink_is_not_cached(self):
        (self.snapshot / "weights.npz").symlink_to("../../blobs/missing")
        self.assert_cached(False)

    def test_weights_without_config_are_not_cached(self):
        (self.snapshot / "config.json").unlink()
        (self.snapshot / "weights.npz").write_bytes(b"test-weights")
        self.assert_cached(False)

    def test_old_complete_snapshot_cannot_hide_partial_main(self):
        old = self.model / "snapshots" / ("b" * 40)
        old.mkdir()
        (old / "config.json").write_text("{}")
        (old / "weights.npz").write_bytes(b"test-weights")
        self.assert_cached(False)

    def test_missing_main_ref_does_not_select_random_snapshot(self):
        (self.snapshot / "weights.npz").write_bytes(b"test-weights")
        (self.model / "refs" / "main").unlink()
        self.assert_cached(False)

    def test_complete_npz_snapshot_is_cached(self):
        (self.snapshot / "weights.npz").write_bytes(b"test-weights")
        self.assert_cached(True)

    def test_complete_safetensors_snapshot_is_cached(self):
        (self.snapshot / "weights.safetensors").write_bytes(b"test-weights")
        self.assert_cached(True)

    def test_hf_blob_symlink_with_existing_weights_is_cached(self):
        blob = self.model / "blobs" / "weight-blob"
        blob.parent.mkdir()
        blob.write_bytes(b"test-weights")
        (self.snapshot / "weights.npz").symlink_to("../../blobs/weight-blob")
        self.assert_cached(True)

    def test_empty_preferred_safetensors_cannot_be_masked_by_npz(self):
        # MLX Whisper выбирает существующий safetensors раньше npz.
        (self.snapshot / "weights.safetensors").touch()
        (self.snapshot / "weights.npz").write_bytes(b"test-weights")
        self.assert_cached(False)

    def test_incomplete_cache_does_not_skip_user_download(self):
        downloader = ModelDownloader()
        started = threading.Event()
        workers = []

        def local_worker(_):
            workers.append(threading.current_thread())
            started.set()

        # Настоящий Thread, но вместо сетевой загрузки — локальный Event.
        with patch.object(downloader, "_download_worker", side_effect=local_worker):
            try:
                status = downloader.start_download(MODEL)
                self.assertEqual(status, "started")
                self.assertTrue(started.wait(timeout=2.0))
            finally:
                for worker in workers:
                    worker.join(timeout=2.0)
                    self.assertFalse(worker.is_alive())

    def test_download_returning_incomplete_snapshot_does_not_emit_done(self):
        bus = EventBus()
        queue = bus.subscribe()
        downloader = ModelDownloader(event_bus=bus)
        # До загрузки кэша нет; fake remote возвращает физический config-only snapshot.
        (self.model / "refs" / "main").unlink()
        try:
            with self.assertLogs("KrabEar.Backend.ModelDownloader", level="ERROR"):
                with patch("huggingface_hub.snapshot_download", return_value=str(self.snapshot)):
                    with patch("backend.model_downloader._make_tqdm_class", return_value=None):
                        downloader._download_worker(MODEL)
            status = downloader.get_status(MODEL)
            self.assertEqual(status["status"], "error")
            self.assertFalse(status["cached"])
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            self.assertNotIn("done", [event["data"]["status"] for event in events])
            self.assertEqual(events[-1]["data"]["status"], "error")
        finally:
            bus.unsubscribe(queue)


if __name__ == "__main__":
    unittest.main()
