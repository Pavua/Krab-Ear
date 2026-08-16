"""P0 2026-08-16: REST STT singleflight — один native decode в процессе.

Параллельные POST /v1/stt/transcribe каждый поднимали свой ThreadPoolExecutor,
поэтому два запроса грузили MLX одновременно. Handoff:
docs/HANDOFF_WHISPER_TURBO_SEGV_2026-08-16_RU.md пункт 4.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SttSingleflightLockTest(unittest.TestCase):
    def test_second_acquire_times_out_while_held(self):
        from backend.rest_server import (
            release_stt_singleflight,
            try_acquire_stt_singleflight,
        )

        self.assertTrue(try_acquire_stt_singleflight(0.5))
        try:
            self.assertFalse(try_acquire_stt_singleflight(0.15))
        finally:
            release_stt_singleflight()

    def test_release_allows_next_acquire(self):
        from backend.rest_server import (
            release_stt_singleflight,
            try_acquire_stt_singleflight,
        )

        self.assertTrue(try_acquire_stt_singleflight(0.5))
        release_stt_singleflight()
        self.assertTrue(try_acquire_stt_singleflight(0.5))
        release_stt_singleflight()

    def test_two_threads_serialized(self):
        from backend.rest_server import (
            release_stt_singleflight,
            try_acquire_stt_singleflight,
        )

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()
        started = threading.Barrier(2)

        def _worker():
            nonlocal in_flight, max_in_flight
            started.wait(timeout=2.0)
            self.assertTrue(try_acquire_stt_singleflight(2.0))
            try:
                with lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                threading.Event().wait(0.2)
                with lock:
                    in_flight -= 1
            finally:
                release_stt_singleflight()

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(max_in_flight, 1)


class TranscribeBusyWhenLockHeldTest(unittest.TestCase):
    """HTTP-ветка: не взяли lock → 503 stt_busy, transcribe не звали."""

    def test_stt_busy_does_not_call_transcribe(self):
        import io
        import struct

        try:
            import backend.rest_server as rs
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"rest_server unavailable: {exc}")

        wav = io.BytesIO()
        wav.write(b"RIFF")
        wav.write(struct.pack("<I", 36))
        wav.write(b"WAVE")
        wav.write(b"fmt ")
        wav.write(struct.pack("<I", 16))
        wav.write(struct.pack("<H", 1))
        wav.write(struct.pack("<H", 1))
        wav.write(struct.pack("<I", 16000))
        wav.write(struct.pack("<I", 32000))
        wav.write(struct.pack("<H", 2))
        wav.write(struct.pack("<H", 16))
        wav.write(b"data")
        wav.write(struct.pack("<I", 0))
        wav_bytes = wav.getvalue()

        transcriber = MagicMock()
        store = MagicMock()
        store.load_vocabulary.return_value = []
        store.is_idempotent.return_value = False
        store.load_settings.return_value = {}
        engine = MagicMock()
        engine.normalize_audio = MagicMock()
        mock_info = MagicMock()
        mock_info.duration = 1.0

        rs.app.config["TESTING"] = True
        orig_limiter = rs.limiter.enabled
        rs.limiter.enabled = False
        try:
            with patch.object(rs, "try_acquire_stt_singleflight", return_value=False), \
                 patch.object(rs, "transcriber", transcriber), \
                 patch.object(rs, "store", store), \
                 patch.object(rs, "engine", engine), \
                 patch("soundfile.info", return_value=mock_info):
                client = rs.app.test_client()
                resp = client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_bytes), "voice.wav")},
                    content_type="multipart/form-data",
                )
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.get_json().get("error"), "stt_busy")
            transcriber.transcribe.assert_not_called()
        finally:
            rs.limiter.enabled = orig_limiter


if __name__ == "__main__":
    unittest.main()
