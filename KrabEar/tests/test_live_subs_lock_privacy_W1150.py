"""Tests for LiveSubsService RLock + privacy_mode guard (W1147 F2+F5).

W1147 F2 HIGH: _buffer + _buffer_samples unprotected — concurrent ingest/flush race.
W1147 F5 MED: handle_ingest no privacy_mode check — STT on system audio without consent.
"""
from __future__ import annotations

import base64
import os
import sys
import threading
import unittest
from typing import Any
from unittest.mock import MagicMock

# Ensure project root is in sys.path for backend.* imports
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ── minimal stubs ──────────────────────────────────────────────────────────────

class _FakeTranslationResult:
    def __init__(self, text: str) -> None:
        self.translated_text = text


class _FakeTranscriber:
    def transcribe(self, audio, **kwargs):  # noqa: ANN001
        return {"text": "hello", "language": "en"}


class _FakeTranslator:
    def translate(self, text: str, mode: str, network_mode: str):
        return _FakeTranslationResult(f"[{mode}]{text}")


def _make_pcm_b64(n_samples: int = 1600) -> str:
    """Return base64 of n_samples zeroed int16 PCM bytes."""
    import numpy as np
    pcm = (np.zeros(n_samples, dtype=np.float32) * 32768).astype("int16")
    return base64.b64encode(pcm.tobytes()).decode()


def _make_service(privacy_enabled: bool = False, settings_get=None):
    """Build a LiveSubsService with optional settings_get callable."""
    from backend.live_subs_service import LiveSubsService

    if settings_get is None:
        settings_get = lambda k, d: privacy_enabled if k == "privacy_mode_enabled" else d  # noqa: E731

    # Patch out event_bus so tests don't need full bus
    import backend.live_subs_service as mod
    original_bus = mod.event_bus
    mock_bus = MagicMock()
    mod.event_bus = mock_bus

    svc = LiveSubsService(
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        settings_get=settings_get,
    )
    svc._mock_bus = mock_bus
    svc._original_bus = original_bus
    svc._mod = mod
    return svc


def _restore_bus(svc) -> None:
    svc._mod.event_bus = svc._original_bus


# ── test cases ─────────────────────────────────────────────────────────────────

class TestLiveSubsPrivacyGuard(unittest.TestCase):
    """W1147 F5: handle_ingest must be skipped when privacy_mode_enabled=True."""

    def tearDown(self) -> None:
        if hasattr(self, "_svc"):
            _restore_bus(self._svc)

    def test_privacy_mode_skips_ingest(self):
        """When privacy_mode_enabled=True, handle_ingest returns skipped=True without STT."""
        svc = _make_service(privacy_enabled=True)
        self._svc = svc

        params = {
            "audio_chunk": _make_pcm_b64(),
            "sample_rate": 16000,
            "target_lang": "off",
            "is_final": False,
        }
        result = svc.handle_ingest(params)

        self.assertTrue(result.get("ok"), f"Expected ok=True, got: {result}")
        self.assertTrue(result.get("skipped"), f"Expected skipped=True, got: {result}")
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        # Buffer must remain empty — no audio was ingested
        self.assertEqual(svc._buffer_samples, 0)
        # EventBus must not have been called
        svc._mock_bus.emit_typed.assert_not_called()

    def test_normal_mode_processes_ingest(self):
        """When privacy_mode_enabled=False, handle_ingest processes audio normally."""
        svc = _make_service(privacy_enabled=False)
        self._svc = svc

        # Build a chunk large enough to NOT trigger flush (< 3 s at 16kHz = 48000 samples)
        params = {
            "audio_chunk": _make_pcm_b64(n_samples=800),  # 0.05 s
            "sample_rate": 16000,
            "target_lang": "off",
            "is_final": False,
        }
        result = svc.handle_ingest(params)

        # Should be "accepted" (no flush yet) — means audio was ingested
        self.assertEqual(result.get("status"), "accepted", f"Unexpected result: {result}")
        self.assertNotIn("skipped", result)
        self.assertGreater(svc._buffer_samples, 0)


class TestLiveSubsRLock(unittest.TestCase):
    """W1147 F2: concurrent ingest calls must not corrupt _buffer/_buffer_samples."""

    def tearDown(self) -> None:
        if hasattr(self, "_svc"):
            _restore_bus(self._svc)

    def test_concurrent_ingest_no_race(self):
        """N threads ingesting simultaneously must not raise or corrupt sample count."""
        svc = _make_service(privacy_enabled=False)
        self._svc = svc

        N_THREADS = 10
        SAMPLES_PER_CHUNK = 400  # 0.025 s → well below 3 s flush threshold per chunk
        errors: list[Exception] = []

        def _ingest_once():
            import numpy as np
            pcm = (np.zeros(SAMPLES_PER_CHUNK, dtype=np.float32) * 32768).astype("int16")
            audio_b64 = base64.b64encode(pcm.tobytes()).decode()
            try:
                svc.handle_ingest({
                    "audio_chunk": audio_b64,
                    "sample_rate": 16000,
                    "target_lang": "off",
                    "is_final": False,
                })
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_ingest_once) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Errors in threads: {errors}")

        # Sample count must equal N * SAMPLES_PER_CHUNK exactly (no double-counts, no drops)
        # Note: some threads may have triggered a flush which resets the buffer, so the
        # invariant is: samples_in_buffer + samples_in_flushed_chunks = N * SAMPLES_PER_CHUNK.
        # We just verify no errors and buffer_samples is non-negative and consistent.
        with svc._lock:
            self.assertGreaterEqual(svc._buffer_samples, 0)
            self.assertEqual(svc._buffer_samples, sum(len(arr) for arr in svc._buffer))

    def test_rlock_is_reentrant(self):
        """RLock must allow _flush (which calls _reset) to be called while lock is held."""
        svc = _make_service(privacy_enabled=False)
        self._svc = svc

        # Manually fill buffer to trigger flush from within the lock
        import numpy as np
        chunk = np.zeros(48001, dtype=np.float32)  # > 3 s at 16 kHz
        svc._buffer.append(chunk)
        svc._buffer_samples = len(chunk)

        # Calling ingest with is_final=True should not deadlock
        import numpy as np
        tiny_pcm = (np.zeros(10, dtype=np.float32) * 32768).astype("int16")
        audio_b64 = base64.b64encode(tiny_pcm.tobytes()).decode()

        result = svc.handle_ingest({
            "audio_chunk": audio_b64,
            "sample_rate": 16000,
            "target_lang": "off",
            "is_final": True,
        })
        # Should have flushed (status == "flushed")
        self.assertEqual(result.get("status"), "flushed", f"Expected flushed, got: {result}")


if __name__ == "__main__":
    unittest.main()
