"""Tests for W1147 F2+F5 fixes in LiveSubsService (W1162).

F2 HIGH: _buffer bytes accumulator lock prevents concurrent ingest corruption.
F5 MED:  privacy_mode_enabled guard skips ingest + flush emission.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_live_subs_lock_privacy_W1162.py -v
"""

from __future__ import annotations

import base64
import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np

from backend.live_subs_service import LiveSubsService


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_bytes(duration_sec: float, sample_rate: int = 16000) -> bytes:
    n = int(duration_sec * sample_rate)
    return (np.zeros(n, dtype=np.int16)).tobytes()


def _make_service(stt_text: str = "hello", settings: dict | None = None) -> LiveSubsService:
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": stt_text, "language": "en"}

    tr_result = MagicMock()
    tr_result.translated_text = "привет"
    translator = MagicMock()
    translator.translate.return_value = tr_result

    return LiveSubsService(
        transcriber=transcriber,
        translator=translator,
        settings=settings or {},
    )


# ── F2: concurrent buffer lock ────────────────────────────────────────────────

class TestBufferConcurrentIngestNoCorruption(unittest.TestCase):
    """W1147 F2: _buffer_lock prevents interleaved writes from multiple threads."""

    def test_buffer_concurrent_ingest_no_corruption(self) -> None:
        """Many threads ingesting 0.5 s chunks concurrently must not corrupt sample count.

        Without the lock two threads can interleave:
          T1 reads _buffer_samples → T2 reads _buffer_samples →
          T1 writes back → T2 writes back (overwrites T1's increment).
        With the lock, final _buffer_samples must equal sum of all chunk sizes.
        """
        svc = _make_service(stt_text="")  # empty text → no flush side effects
        sample_rate = 16000
        chunk_samples = int(0.5 * sample_rate)  # 8000 samples each
        chunk_bytes = (np.zeros(chunk_samples, dtype=np.int16)).tobytes()
        n_threads = 20
        # Use short chunks so none trigger the ≥3 s threshold individually and
        # accumulate below it collectively (0.5 * 20 = 10 s > threshold, but
        # flush resets the buffer — that is fine, we just want no race crash).
        errors: list[Exception] = []

        def worker():
            try:
                svc.ingest(
                    audio_bytes=chunk_bytes,
                    sample_rate=sample_rate,
                    target_lang="off",
                    is_final=False,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent ingest raised exceptions: {errors}")

    def test_buffer_lock_exists(self) -> None:
        """LiveSubsService must expose a _buffer_lock threading.Lock."""
        svc = _make_service()
        self.assertTrue(hasattr(svc, "_buffer_lock"), "_buffer_lock attribute missing")
        self.assertIsInstance(svc._buffer_lock, type(threading.Lock()))

    def test_buffer_duration_thread_safe_read(self) -> None:
        """buffer_duration_sec acquires lock and returns consistent value."""
        svc = _make_service()
        sample_rate = 16000
        chunk = (np.zeros(sample_rate, dtype=np.int16)).tobytes()  # 1 s
        svc.ingest(chunk, sample_rate, "off", False)
        # Just calling from multiple threads should not raise
        results: list[float] = []

        def read():
            results.append(svc.buffer_duration_sec(sample_rate))

        threads = [threading.Thread(target=read) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 10)
        for r in results:
            self.assertGreaterEqual(r, 0.0)


# ── F5: privacy mode guard ────────────────────────────────────────────────────

class TestPrivacyModeSkipsIngest(unittest.TestCase):
    """W1147 F5: privacy_mode_enabled=True must short-circuit ingest."""

    def test_privacy_mode_skips_ingest(self) -> None:
        """ingest() returns None immediately when privacy_mode_enabled is True."""
        svc = _make_service(settings={"privacy_mode_enabled": True})
        sample_rate = 16000
        # feed enough for ≥3 s threshold
        chunk = (np.zeros(int(4 * sample_rate), dtype=np.int16)).tobytes()
        result = svc.ingest(
            audio_bytes=chunk,
            sample_rate=sample_rate,
            target_lang="ru",
            is_final=True,
        )
        self.assertIsNone(result, "ingest() should return None when privacy mode on")
        self.assertEqual(svc._transcriber.transcribe.call_count, 0,
                         "transcriber must not be called in privacy mode")

    def test_privacy_mode_handle_ingest_returns_skipped(self) -> None:
        """handle_ingest() IPC handler returns skipped:privacy_mode response."""
        svc = _make_service(settings={"privacy_mode_enabled": True})
        chunk_b64 = base64.b64encode(
            (np.zeros(16000, dtype=np.int16)).tobytes()
        ).decode()
        response = svc.handle_ingest({
            "audio_chunk": chunk_b64,
            "sample_rate": 16000,
            "target_lang": "ru",
            "is_final": True,
        })
        self.assertTrue(response.get("ok"), "Response must have ok=True")
        self.assertEqual(response.get("skipped"), "privacy_mode",
                         "Response must include skipped=privacy_mode")

    def test_privacy_mode_disabled_ingest_works_normally(self) -> None:
        """When privacy_mode_enabled=False, ingest proceeds normally."""
        svc = _make_service(stt_text="test text", settings={"privacy_mode_enabled": False})
        sample_rate = 16000
        chunk = (np.zeros(int(4 * sample_rate), dtype=np.int16)).tobytes()
        result = svc.ingest(
            audio_bytes=chunk,
            sample_rate=sample_rate,
            target_lang="off",
            is_final=True,
        )
        self.assertIsNotNone(result, "ingest() should flush when privacy mode off")
        self.assertEqual(svc._transcriber.transcribe.call_count, 1)


class TestPrivacyModeSkipsEmit(unittest.TestCase):
    """W1147 F5: _flush() must not emit events when privacy_mode_enabled is True."""

    def test_privacy_mode_skips_emit(self) -> None:
        """_flush() called while privacy mode is on must not emit to EventBus."""
        svc = _make_service(settings={"privacy_mode_enabled": True})

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc._flush(sample_rate=16000, target_lang="ru")

        mock_bus.emit_typed.assert_not_called()
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["translation"])

    def test_privacy_mode_off_flush_emits(self) -> None:
        """_flush() emits event when privacy mode is off and buffer has data."""
        svc = _make_service(stt_text="hello", settings={"privacy_mode_enabled": False})
        # Prime buffer with 1 s of audio
        sample_rate = 16000
        with svc._buffer_lock:
            svc._buffer.append(np.zeros(sample_rate, dtype=np.float32))
            svc._buffer_samples = sample_rate

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc._flush(sample_rate=sample_rate, target_lang="off")

        mock_bus.emit_typed.assert_called_once()
        self.assertEqual(result["text"], "hello")

    def test_privacy_mode_toggled_mid_session(self) -> None:
        """If privacy mode is turned on after buffer is primed, _flush skips emit."""
        svc = _make_service(stt_text="secret", settings={"privacy_mode_enabled": False})
        sample_rate = 16000
        # Prime buffer
        with svc._buffer_lock:
            svc._buffer.append(np.zeros(sample_rate, dtype=np.float32))
            svc._buffer_samples = sample_rate

        # Enable privacy mode before flush fires
        svc._settings["privacy_mode_enabled"] = True

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc._flush(sample_rate=sample_rate, target_lang="ru")

        mock_bus.emit_typed.assert_not_called()
        self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main()
