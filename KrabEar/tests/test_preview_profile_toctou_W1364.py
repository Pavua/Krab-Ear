"""W1364 — transcribe_preview profile TOCTOU race regression tests.

Before the fix, Transcriber.transcribe_preview() called:
    self.engine.set_quality_profile("balanced")   # (1)
    self.engine.transcribe(...)                    # (2) — MLX inference

A concurrent Transcriber.transcribe(quality_profile="max") could interleave:
    preview:   set_quality_profile("balanced")     # (1) OK
    main:      set_quality_profile("max")          # <-- RACE: sneaks in between (1) and (2)
    preview:   engine.transcribe()                 # (2) runs with "max" model — WRONG

W1364 fix: wrap (1)+(2) together inside mlx_lock() in transcribe_preview so that
the profile switch and the inference are atomic w.r.t. other MLX callers.

Tests use fully mocked FakeAudioEngine + patched mlx_lock so no MLX required.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcriber import Transcriber


# ---------------------------------------------------------------------------
# Shared fake engine
# ---------------------------------------------------------------------------

class FakeAudioEngine:
    """Minimal AudioEngine stub that records calls and simulates profile state."""

    def __init__(self):
        self.quality_profile = "balanced"
        self._llm_rewriter = None
        self._settings_get = lambda k, d: d
        self.transcribe_calls: list[dict[str, Any]] = []
        self.profile_switch_calls: list[str] = []
        # Allows tests to inject a delay to simulate slow inference
        self.transcribe_delay: float = 0.0

    def set_quality_profile(self, profile: str) -> bool:
        clean = profile.strip().lower()
        self.profile_switch_calls.append(clean)
        old = self.quality_profile
        self.quality_profile = clean
        return old != clean

    def transcribe(
        self,
        audio_data: Any,
        cleanup_profile: str = "soft",
        is_preview: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.transcribe_delay:
            time.sleep(self.transcribe_delay)
        record = {
            "audio_data": audio_data,
            "cleanup_profile": cleanup_profile,
            "is_preview": is_preview,
            "profile_at_call": self.quality_profile,
        }
        self.transcribe_calls.append(record)
        return {"text": f"ok:{audio_data}", "confidence": 0.9, "language": "ru"}


# ---------------------------------------------------------------------------
# 1. Unit: transcribe_preview always uses "balanced" profile atomically
# ---------------------------------------------------------------------------

class PreviewProfileAtomicityTests(unittest.TestCase):
    """Verify that profile switch and inference stay together inside mlx_lock."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_preview_does_not_corrupt_main_transcribe_profile(self):
        """After transcribe_preview(), the engine profile is "balanced".

        W1364 TOCTOU scenario: a main transcribe with quality_profile="max"
        was running. After the preview finishes, the state must reflect that
        the preview ran on "balanced" and did not silently leave the engine
        on the wrong profile from a racing call.

        Here we verify the Transcriber-level invariant: preview sets "balanced"
        before calling engine.transcribe and the engine records that profile.
        """
        # Simulate engine starting in "max" (from a prior main transcribe)
        self.fake_engine.quality_profile = "max"

        result = self.transcriber.transcribe_preview(b"preview_audio")

        self.assertIsNotNone(result)
        self.assertIn("text", result)

        # The engine.transcribe was called with is_preview=True
        self.assertEqual(len(self.fake_engine.transcribe_calls), 1)
        call_record = self.fake_engine.transcribe_calls[0]
        self.assertTrue(call_record["is_preview"])

        # profile was switched to "balanced" before the transcribe call
        self.assertIn("balanced", self.fake_engine.profile_switch_calls)
        # The engine profile at the time transcribe was invoked must be "balanced"
        self.assertEqual(call_record["profile_at_call"], "balanced")

    def test_preview_uses_soft_cleanup_profile(self):
        """transcribe_preview always passes cleanup_profile='soft'."""
        self.transcriber.transcribe_preview(b"audio")
        self.assertEqual(
            self.fake_engine.transcribe_calls[0]["cleanup_profile"], "soft"
        )

    def test_preview_passes_is_preview_true(self):
        """transcribe_preview always passes is_preview=True to engine."""
        self.transcriber.transcribe_preview(b"audio")
        self.assertTrue(self.fake_engine.transcribe_calls[0]["is_preview"])

    def test_preview_quality_profile_kwarg_ignored_always_balanced(self):
        """quality_profile kwarg is accepted but preview always forces 'balanced'."""
        self.fake_engine.quality_profile = "max"
        self.transcriber.transcribe_preview(b"audio", quality_profile="max")
        # The engine was still switched to balanced
        self.assertEqual(self.fake_engine.profile_switch_calls[-1], "balanced")
        self.assertEqual(self.fake_engine.transcribe_calls[0]["profile_at_call"], "balanced")


# ---------------------------------------------------------------------------
# 2. Concurrency: preview and main transcribe use correct profiles
# ---------------------------------------------------------------------------

class ConcurrentPreviewAndTranscribeTests(unittest.TestCase):
    """Test that preview and main transcribe don't corrupt each other's profiles.

    Uses a real threading.RLock (the actual mlx_lock) so the lock is exercised.
    The FakeAudioEngine introduces a small delay so threads can interleave.
    """

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_concurrent_preview_and_transcribe_use_correct_profile(self):
        """preview always runs on 'balanced'; main transcribe runs on requested profile.

        Without the W1364 fix, a racing main transcribe(quality_profile="max") could
        switch the engine to "max" between preview's set_quality_profile("balanced") and
        preview's engine.transcribe(), causing preview to silently run on the wrong model.

        With the fix, the preview wraps both operations inside mlx_lock(), so the main
        transcribe's set_quality_profile cannot interleave.
        """
        # Introduce a small delay so threads overlap in time
        self.fake_engine.transcribe_delay = 0.02

        preview_profile_at_call: list[str] = []
        main_profile_at_call: list[str] = []
        errors: list[Exception] = []

        def run_preview():
            try:
                # Monkey-patch engine.transcribe to capture profile at time of call
                original_transcribe = self.fake_engine.transcribe

                def capturing_transcribe(audio_data, *, cleanup_profile="soft",
                                         is_preview=False, **kwargs):
                    if is_preview:
                        preview_profile_at_call.append(self.fake_engine.quality_profile)
                    else:
                        main_profile_at_call.append(self.fake_engine.quality_profile)
                    return original_transcribe(
                        audio_data,
                        cleanup_profile=cleanup_profile,
                        is_preview=is_preview,
                        **kwargs,
                    )

                self.fake_engine.transcribe = capturing_transcribe
                self.transcriber.transcribe_preview(b"preview_audio")
            except Exception as exc:
                errors.append(exc)

        def run_main():
            try:
                self.transcriber.transcribe(b"main_audio", quality_profile="max")
            except Exception as exc:
                errors.append(exc)

        # Launch both threads simultaneously
        barrier = threading.Barrier(2)

        def run_preview_barriered():
            barrier.wait()
            run_preview()

        def run_main_barriered():
            barrier.wait()
            run_main()

        t_preview = threading.Thread(target=run_preview_barriered, daemon=True)
        t_main = threading.Thread(target=run_main_barriered, daemon=True)
        t_preview.start()
        t_main.start()
        t_preview.join(timeout=5.0)
        t_main.join(timeout=5.0)

        self.assertEqual(errors, [], f"exceptions during concurrent test: {errors}")

        # Both calls must have completed
        total_calls = len(self.fake_engine.transcribe_calls)
        self.assertEqual(total_calls, 2, "both transcribe calls must complete")

        # The preview call must have seen "balanced" at the time engine.transcribe ran
        if preview_profile_at_call:
            self.assertEqual(
                preview_profile_at_call[0],
                "balanced",
                "W1364 REGRESSION: preview ran with wrong profile "
                f"'{preview_profile_at_call[0]}' (expected 'balanced'). "
                "The TOCTOU race is NOT fixed!",
            )

    def test_mlx_lock_imported_in_transcriber(self):
        """mlx_lock must be importable from backend.transcriber's namespace.

        This is a compile-time check: if the import is missing, the fix was
        accidentally reverted.
        """
        import importlib
        import backend.transcriber as tr_module
        self.assertTrue(
            hasattr(tr_module, "mlx_lock"),
            "mlx_lock must be imported at module level in backend/transcriber.py "
            "(W1364 fix: wraps preview's set_quality_profile + engine.transcribe "
            "atomically to prevent TOCTOU race).",
        )

    def test_preview_result_is_dict_with_text(self):
        """Basic contract: transcribe_preview returns dict with 'text' key."""
        result = self.transcriber.transcribe_preview(b"audio")
        self.assertIsInstance(result, dict)
        self.assertIn("text", result)


# ---------------------------------------------------------------------------
# 3. Lock coverage: verify mlx_lock is actually acquired during preview
# ---------------------------------------------------------------------------

class MlxLockAcquisitionTests(unittest.TestCase):
    """Verify mlx_lock() is called when transcribe_preview runs."""

    def setUp(self):
        self.fake_engine = FakeAudioEngine()
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_mlx_lock_acquired_during_transcribe_preview(self):
        """mlx_lock() is acquired and released around transcribe_preview.

        2026-08-01: мок переписан с contextmanager-генератора на обёртку вокруг
        НАСТОЯЩЕГО RLock. Прежний мок отражал только `with`-протокол, тогда как
        реальный `mlx_lock()` возвращает `threading.RLock` — у которого есть и
        `__enter__`, и `acquire(timeout=...)`. Пока продакшн пользовался лишь
        `with`, расхождение было незаметно; как только transcribe_preview стал
        брать лок с таймаутом (best-effort превью не ждёт занятый GPU дольше
        секунды), мок упал с `'_GeneratorContextManager' object has no attribute
        'acquire'`. Смысл проверки не изменился — по-прежнему пинуется факт
        захвата и освобождения, — но мок теперь не врёт про интерфейс.
        """
        lock_acquired_events: list[str] = []

        import threading as _threading

        class _RecordingLock:
            """Настоящий RLock + журнал захватов (оба протокола, как у оригинала)."""

            def __init__(self) -> None:
                self._lock = _threading.RLock()

            def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
                got = self._lock.acquire(blocking, timeout)
                if got:
                    lock_acquired_events.append("acquired")
                return got

            def release(self) -> None:
                lock_acquired_events.append("released")
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()
                return False

        # Patch at the module level where transcriber imports mlx_lock
        with patch("backend.transcriber.mlx_lock") as mock_mlx_lock:
            mock_mlx_lock.return_value = _RecordingLock()

            self.transcriber.transcribe_preview(b"audio")

        self.assertIn(
            "acquired",
            lock_acquired_events,
            "mlx_lock() was not acquired during transcribe_preview — W1364 fix missing!",
        )
        self.assertIn(
            "released",
            lock_acquired_events,
            "mlx_lock() was acquired but never released during transcribe_preview.",
        )


if __name__ == "__main__":
    unittest.main()
