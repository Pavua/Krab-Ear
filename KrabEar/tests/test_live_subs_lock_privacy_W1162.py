"""Tests for W1147 F2+F5 fixes in LiveSubsService (W1162).

F2 HIGH: _lock (RLock) prevents concurrent ingest corruption.
F5 MED:  privacy_mode_enabled guard skips ingest + flush emission.

W1689 repair: tests updated to match current LiveSubsService API:
  - constructor uses settings_get= callable (not settings= dict)
  - lock attribute is _lock (threading.RLock, not _buffer_lock)
  - privacy guard lives in handle_ingest() / stop() (not ingest() / _flush())
  - handle_ingest() privacy response: {ok: True, skipped: True, reason: "privacy_mode_active"}

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_live_subs_lock_privacy_W1162 -v
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

import numpy as np  # noqa: E402

from backend.live_subs_service import LiveSubsService  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_bytes(duration_sec: float, sample_rate: int = 16000) -> bytes:
    n = int(duration_sec * sample_rate)
    return (np.zeros(n, dtype=np.int16)).tobytes()


def _make_service(
    stt_text: str = "hello",
    privacy_enabled: bool = False,
) -> LiveSubsService:
    """Build a LiveSubsService with stub collaborators.

    Uses settings_get= callable (the actual constructor API, not settings= dict).
    privacy_enabled controls what settings_get("privacy_mode_enabled", False) returns.
    """
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": stt_text, "language": "en"}

    tr_result = MagicMock()
    tr_result.translated_text = "привет"
    translator = MagicMock()
    translator.translate.return_value = tr_result

    settings_dict = {"privacy_mode_enabled": privacy_enabled}

    return LiveSubsService(
        transcriber=transcriber,
        translator=translator,
        settings_get=lambda k, d: settings_dict.get(k, d),
    )


# ── F2: concurrent buffer lock ────────────────────────────────────────────────

class TestBufferConcurrentIngestNoCorruption(unittest.TestCase):
    """W1147 F2: _lock (RLock) prevents interleaved writes from multiple threads."""

    def test_buffer_concurrent_ingest_no_corruption(self) -> None:
        """Many threads ingesting 0.5 s chunks concurrently must not raise exceptions.

        Without the lock two threads can interleave:
          T1 reads _buffer_samples → T2 reads _buffer_samples →
          T1 writes back → T2 writes back (overwrites T1's increment).
        With the RLock, concurrent ingest must not corrupt state or raise.
        """
        svc = _make_service(stt_text="")  # empty text → no flush side effects
        sample_rate = 16000
        chunk_samples = int(0.5 * sample_rate)  # 8000 samples each
        chunk_bytes = (np.zeros(chunk_samples, dtype=np.int16)).tobytes()
        n_threads = 20
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
        """LiveSubsService must expose a _lock threading.RLock."""
        svc = _make_service()
        self.assertTrue(hasattr(svc, "_lock"), "_lock attribute missing")
        self.assertIsInstance(svc._lock, type(threading.RLock()))

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


# ── F5: privacy mode guard (handle_ingest / stop level) ───────────────────────

class TestPrivacyModeSkipsIngest(unittest.TestCase):
    """W1147 F5: privacy_mode_enabled=True must short-circuit handle_ingest IPC handler."""

    def test_privacy_mode_skips_handle_ingest(self) -> None:
        """handle_ingest() returns skipped=True immediately when privacy mode on."""
        svc = _make_service(privacy_enabled=True)
        sample_rate = 16000
        chunk_b64 = base64.b64encode(
            (np.zeros(int(4 * sample_rate), dtype=np.int16)).tobytes()
        ).decode()
        response = svc.handle_ingest({
            "audio_chunk": chunk_b64,
            "sample_rate": sample_rate,
            "target_lang": "ru",
            "is_final": True,
        })
        self.assertTrue(response.get("ok"), "Response must have ok=True")
        self.assertTrue(response.get("skipped"), "Response must have skipped=True")
        self.assertEqual(response.get("reason"), "privacy_mode_active",
                         "Reason must be privacy_mode_active")
        self.assertEqual(svc._transcriber.transcribe.call_count, 0,
                         "transcriber must not be called in privacy mode")

    def test_privacy_mode_handle_ingest_returns_skipped(self) -> None:
        """handle_ingest() IPC handler returns ok+skipped response in privacy mode."""
        svc = _make_service(privacy_enabled=True)
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
        self.assertTrue(response.get("skipped"), "Response must include skipped=True")
        self.assertEqual(response.get("reason"), "privacy_mode_active")

    def test_privacy_mode_disabled_ingest_works_normally(self) -> None:
        """When privacy_mode_enabled=False, handle_ingest proceeds normally."""
        svc = _make_service(stt_text="test text", privacy_enabled=False)
        sample_rate = 16000
        chunk_b64 = base64.b64encode(
            (np.zeros(int(4 * sample_rate), dtype=np.int16)).tobytes()
        ).decode()
        response = svc.handle_ingest({
            "audio_chunk": chunk_b64,
            "sample_rate": sample_rate,
            "target_lang": "off",
            "is_final": True,
        })
        # Should flush (is_final=True with 4s chunk)
        self.assertIn(response.get("status"), ("flushed", "accepted"),
                      f"Unexpected status: {response}")
        self.assertFalse(response.get("skipped"), "Should not be skipped when privacy off")


class TestPrivacyModeSkipsEmit(unittest.TestCase):
    """W1147 F5: stop() must not emit events when privacy_mode_enabled is True."""

    def test_privacy_mode_stop_drops_buffer(self) -> None:
        """stop() called while privacy mode is on must clear buffer, not flush/emit."""
        svc = _make_service(privacy_enabled=True)
        # Prime buffer directly (bypass handle_ingest privacy guard)
        sample_rate = 16000
        with svc._lock:
            svc._buffer.append(np.zeros(sample_rate, dtype=np.float32))
            svc._buffer_samples = sample_rate

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc.stop()

        mock_bus.emit_typed.assert_not_called()
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(result.get("flushed"), "flushed must be False in privacy mode")
        self.assertTrue(result.get("skipped"), "skipped must be True in privacy mode")
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        # Buffer must be cleared
        self.assertEqual(svc._buffer_samples, 0)
        self.assertEqual(svc._buffer, [])

    def test_privacy_mode_off_stop_flushes(self) -> None:
        """stop() flushes buffer and emits event when privacy mode is off."""
        svc = _make_service(stt_text="hello", privacy_enabled=False)
        # Prime buffer with 1 s of audio
        sample_rate = 16000
        with svc._lock:
            svc._buffer.append(np.zeros(sample_rate, dtype=np.float32))
            svc._buffer_samples = sample_rate

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc.stop()

        mock_bus.emit_typed.assert_called_once()
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result.get("flushed"), "flushed must be True when privacy off")

    def test_privacy_mode_off_flush_emits(self) -> None:
        """_process_window() emits event when privacy mode is off and buffer has data.

        F3 (2026-08-12): _flush() было расщеплено на снапшот-под-локом
        (теперь в ingest()/stop()) и обработку-в-воркере (_process_window()) —
        последняя принимает уже готовый window-dict вместо (sample_rate,
        target_lang) и self._buffer.
        """
        svc = _make_service(stt_text="hello", privacy_enabled=False)
        sample_rate = 16000
        window = {
            "seq": 1,
            "audio": np.zeros(sample_rate, dtype=np.float32),
            "sample_rate": sample_rate,
            "target_lang": "off",
            "start_ts": 0.0,
            "end_ts": 1.0,
        }

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc._process_window(window)

        mock_bus.emit_typed.assert_called_once()
        self.assertEqual(result["text"], "hello")

    def test_privacy_mode_toggled_stop_drops_primed_buffer(self) -> None:
        """Privacy toggled on after buffer is primed: stop() drops audio, no emit."""
        # Start with privacy off so ingest builds up the buffer state
        settings_dict = {"privacy_mode_enabled": False}
        transcriber = MagicMock()
        transcriber.transcribe.return_value = {"text": "secret", "language": "ru"}
        translator = MagicMock()
        tr_result = MagicMock()
        tr_result.translated_text = None
        translator.translate.return_value = tr_result

        svc = LiveSubsService(
            transcriber=transcriber,
            translator=translator,
            settings_get=lambda k, d: settings_dict.get(k, d),
        )
        sample_rate = 16000
        # Prime buffer directly
        with svc._lock:
            svc._buffer.append(np.zeros(sample_rate, dtype=np.float32))
            svc._buffer_samples = sample_rate

        # Enable privacy mode before stop() fires
        settings_dict["privacy_mode_enabled"] = True

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc.stop()

        mock_bus.emit_typed.assert_not_called()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertEqual(svc._buffer_samples, 0)


# ── F1 new tests: stop() privacy gate (W1683 F1 HIGH) ─────────────────────────

class TestStopPrivacyGate(unittest.TestCase):
    """W1683 F1 HIGH: stop() must check privacy_mode_enabled before flushing.

    Audio buffered before a privacy toggle must NOT be transcribed or emitted
    when stop() fires. The guard existed only in handle_ingest(); now also in stop().
    """

    def test_stop_drops_buffer_in_privacy_mode(self) -> None:
        """stop() clears buffer without STT or EventBus emit when privacy is on."""
        svc = _make_service(stt_text="should_not_appear", privacy_enabled=True)
        sample_rate = 16000
        # Directly prime the buffer (simulating audio buffered before privacy toggle)
        with svc._lock:
            svc._buffer.append(np.zeros(sample_rate * 2, dtype=np.float32))
            svc._buffer_samples = sample_rate * 2

        self.assertGreater(svc.buffer_duration_sec(sample_rate), 0,
                           "Buffer must have content before stop()")

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc.stop()

        # EventBus must NOT be touched
        mock_bus.emit_typed.assert_not_called()
        # STT must NOT be called
        svc._transcriber.transcribe.assert_not_called()
        # Response shape
        self.assertEqual(result["status"], "stopped")
        self.assertFalse(result.get("flushed"),
                         "flushed must be False — buffer was dropped, not transcribed")
        self.assertTrue(result.get("skipped"), "skipped key must be truthy")
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        # Buffer must be empty after stop()
        self.assertEqual(svc._buffer, [])
        self.assertEqual(svc._buffer_samples, 0)

    def test_stop_flushes_normally_when_privacy_off(self) -> None:
        """stop() transcribes and emits when privacy_mode_enabled is False."""
        svc = _make_service(stt_text="valid transcript", privacy_enabled=False)
        sample_rate = 16000
        # Prime buffer
        with svc._lock:
            svc._buffer.append(np.zeros(sample_rate, dtype=np.float32))
            svc._buffer_samples = sample_rate

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc.stop()

        mock_bus.emit_typed.assert_called_once()
        svc._transcriber.transcribe.assert_called_once()
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result.get("flushed"),
                        "flushed must be True when privacy is off and buffer had data")
        self.assertFalse(result.get("skipped"), "skipped must not be set when privacy off")


if __name__ == "__main__":
    unittest.main()
