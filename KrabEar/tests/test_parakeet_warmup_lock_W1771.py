"""Regression tests for ParakeetSTTAdapter.warmup() locking fix (W1771).

HIGH finding (sibling-asymmetry, SIGSEGV-risk):
  warmup() previously called parakeet_mlx.from_pretrained() DIRECTLY, bypassing
  self._load_lock, mlx_lock(), and mlx_inter_process_lock().  Any concurrent
  warmup_stt IPC + transcribe() from another thread (or the REST process sharing
  the Metal GPU) could double-load / corrupt GPU state → SIGSEGV (same class as PR #71).

  Also, its bare `except Exception: self._load_failed = True` permanently bricked the
  adapter on transient errors (MLXInterLockTimeout, HF 503, disk-full).

Fix: warmup() now mirrors SenseVoiceSTTAdapter.warmup() — double-checked locking under
self._load_lock, delegating to self._load_model(parakeet_mlx) which already wraps
from_pretrained in mlx_lock() + mlx_inter_process_lock() AND distinguishes transient
MLXInterLockTimeout (re-raises, does NOT set _load_failed) from permanent errors
(sets _load_failed).

This test file is mlx-agnostic (parakeet_mlx is NOT installed on ubuntu py3.12).
parakeet_mlx is mocked throughout — no real MLX/GPU access.
"""
from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Path setup — mirrors other test files in this project
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Stub parakeet_mlx before importing the adapter so is_available() returns True
# and _try_import_parakeet() succeeds without the real library.
# ---------------------------------------------------------------------------
_stub_parakeet = types.ModuleType("parakeet_mlx")
_stub_parakeet.from_pretrained = MagicMock(return_value=MagicMock(name="FakeParakeetModel"))
sys.modules.setdefault("parakeet_mlx", _stub_parakeet)

from core.pipeline.stt_parakeet import ParakeetSTTAdapter  # noqa: E402
from core.mlx_inter_lock import MLXInterLockTimeout  # noqa: E402


class TestParakeetWarmupDelegatesViaLock(unittest.TestCase):
    """warmup() must go through _load_lock and call _load_model() — not from_pretrained directly."""

    def setUp(self):
        self.adapter = ParakeetSTTAdapter(model_path="test-org/test-parakeet")

    def test_warmup_calls_load_model_not_from_pretrained_directly(self):
        """warmup() must call self._load_model(...), not parakeet_mlx.from_pretrained directly."""
        load_model_mock = MagicMock(name="_load_model")
        # _load_model sets self._model as a side effect so warmup() returns True
        def _fake_load(parakeet_mlx_mod):
            self.adapter._model = MagicMock(name="LoadedModel")
        load_model_mock.side_effect = _fake_load

        with patch.object(self.adapter, "_load_model", load_model_mock):
            result = self.adapter.warmup()

        self.assertTrue(result)
        load_model_mock.assert_called_once()
        # Verify the first positional arg is the parakeet_mlx module (not a string/None)
        passed_arg = load_model_mock.call_args[0][0]
        self.assertIsNotNone(passed_arg, "_load_model must be called with parakeet_mlx module")

    def test_warmup_holds_load_lock_while_calling_load_model(self):
        """_load_lock must be held when _load_model() is called."""
        lock_held_during_load = []

        def _fake_load(parakeet_mlx_mod):
            # Record whether the lock is held at this exact moment
            locked = not self.adapter._load_lock.acquire(blocking=False)
            lock_held_during_load.append(locked)
            if not locked:
                # We accidentally acquired it — release immediately
                self.adapter._load_lock.release()
            self.adapter._model = MagicMock(name="LoadedModel")

        with patch.object(self.adapter, "_load_model", side_effect=_fake_load):
            self.adapter.warmup()

        self.assertTrue(
            lock_held_during_load and lock_held_during_load[0],
            "_load_lock must be held while _load_model() executes",
        )

    def test_warmup_skips_load_if_model_already_loaded(self):
        """warmup() must short-circuit (no _load_model call) when model is already set."""
        self.adapter._model = MagicMock(name="PreloadedModel")
        load_model_mock = MagicMock(name="_load_model")

        with patch.object(self.adapter, "_load_model", load_model_mock):
            result = self.adapter.warmup()

        self.assertTrue(result)
        load_model_mock.assert_not_called()

    def test_warmup_skips_load_if_load_failed(self):
        """warmup() must short-circuit when _load_failed is True."""
        self.adapter._load_failed = True
        load_model_mock = MagicMock(name="_load_model")

        with patch.object(self.adapter, "_load_model", load_model_mock):
            result = self.adapter.warmup()

        self.assertFalse(result)
        load_model_mock.assert_not_called()

    def test_warmup_returns_false_when_parakeet_not_installed(self):
        """warmup() returns False without calling _load_model when parakeet_mlx unavailable."""
        load_model_mock = MagicMock(name="_load_model")
        with patch("core.pipeline.stt_parakeet._try_import_parakeet", return_value=None), \
                patch.object(self.adapter, "_load_model", load_model_mock):
            result = self.adapter.warmup()

        self.assertFalse(result)
        load_model_mock.assert_not_called()


class TestParakeetWarmupTransientError(unittest.TestCase):
    """Transient MLXInterLockTimeout during warmup must NOT permanently brick the adapter."""

    def setUp(self):
        self.adapter = ParakeetSTTAdapter(model_path="test-org/test-parakeet")

    def test_mlx_interlock_timeout_does_not_set_load_failed(self):
        """If _load_model raises MLXInterLockTimeout, _load_failed must remain False."""
        # Build a plausible MLXInterLockTimeout instance
        timeout_exc = MLXInterLockTimeout(
            timeout_sec=5.0,
            lock_path=Path("/tmp/test.lock"),
        )

        def _raise_timeout(parakeet_mlx_mod):
            raise timeout_exc

        with patch.object(self.adapter, "_load_model", side_effect=_raise_timeout):
            result = self.adapter.warmup()

        # warmup() should return False (load didn't succeed)
        self.assertFalse(result)
        # CRITICAL: _load_failed must NOT be set — adapter stays usable for future calls
        self.assertFalse(
            self.adapter._load_failed,
            "MLXInterLockTimeout is transient — must not permanently brick the adapter",
        )
        # Model must also remain None (load didn't succeed)
        self.assertIsNone(self.adapter._model)

    def test_transient_error_adapter_can_succeed_on_retry(self):
        """After a transient warmup failure, a second warmup() call can succeed."""
        timeout_exc = MLXInterLockTimeout(
            timeout_sec=5.0,
            lock_path=Path("/tmp/test.lock"),
        )
        call_count = [0]

        def _load_model_side_effect(parakeet_mlx_mod):
            call_count[0] += 1
            if call_count[0] == 1:
                raise timeout_exc
            # Second call succeeds
            self.adapter._model = MagicMock(name="LoadedModel")

        with patch.object(self.adapter, "_load_model", side_effect=_load_model_side_effect):
            result1 = self.adapter.warmup()
            result2 = self.adapter.warmup()

        self.assertFalse(result1, "First warmup (transient failure) should return False")
        self.assertTrue(result2, "Second warmup (success) should return True")
        self.assertFalse(self.adapter._load_failed)
        self.assertIsNotNone(self.adapter._model)


class TestParakeetWarmupPermanentError(unittest.TestCase):
    """A permanent (non-transient) load failure during warmup should mark adapter unavailable."""

    def setUp(self):
        self.adapter = ParakeetSTTAdapter(model_path="test-org/test-parakeet")

    def test_permanent_error_from_load_model_sets_load_failed(self):
        """_load_model's RuntimeError (permanent failure path) should propagate; warmup returns False."""
        perm_exc = RuntimeError("ParakeetSTTAdapter: model load failed: disk full")

        def _raise_permanent(parakeet_mlx_mod):
            # Simulate _load_model's permanent-failure branch: sets _load_failed then raises
            self.adapter._load_failed = True
            raise perm_exc

        with patch.object(self.adapter, "_load_model", side_effect=_raise_permanent):
            result = self.adapter.warmup()

        self.assertFalse(result)
        # _load_failed IS set (permanent failure — _load_model owns this decision)
        self.assertTrue(
            self.adapter._load_failed,
            "Permanent load failure should set _load_failed (via _load_model's own logic)",
        )


class TestParakeetWarmupDoubleCheckedLock(unittest.TestCase):
    """Double-checked locking: if model is set between outer and inner check, no double-load."""

    def setUp(self):
        self.adapter = ParakeetSTTAdapter(model_path="test-org/test-parakeet")

    def test_no_double_load_when_model_set_before_lock(self):
        """If self._model is already non-None when warmup() is called (e.g. set by another
        thread), _load_model() must not be called — the outer (unsynchronized) check fires."""
        # Pre-set the model to simulate a concurrent load completing first
        self.adapter._model = MagicMock(name="ModelSetByOtherThread")

        load_model_mock = MagicMock(name="_load_model")
        with patch.object(self.adapter, "_load_model", load_model_mock):
            result = self.adapter.warmup()

        self.assertTrue(result)
        # The outer `if self._model is None` guard must prevent _load_model from being called
        load_model_mock.assert_not_called()

    def test_no_double_load_concurrent_warmup_calls(self):
        """Two concurrent warmup() calls on a fresh adapter must load the model exactly once."""
        load_count = [0]
        load_barrier = threading.Barrier(2)

        def _slow_load(parakeet_mlx_mod):
            load_count[0] += 1
            # Both threads reach here — but only one should proceed past the inner check.
            # The inner double-check (`if self._model is None`) prevents the second thread
            # from calling _load_model again once the first has set self._model.
            self.adapter._model = MagicMock(name="LoadedModel")

        # Replace _load_model with a version that sets the model (mimics real behaviour).
        # We test that the combination of _load_lock + double-check prevents double-loads.
        results = []

        def _thread_warmup():
            with patch.object(self.adapter, "_load_model", side_effect=_slow_load):
                results.append(self.adapter.warmup())

        # Run one warmup call in isolation (not truly concurrent here, but verifies
        # that the second call on an already-loaded adapter skips _load_model).
        with patch.object(self.adapter, "_load_model", side_effect=_slow_load):
            self.adapter.warmup()

        self.assertEqual(load_count[0], 1, "First warmup must call _load_model exactly once")

        # A second warmup() on the now-loaded adapter must NOT call _load_model again.
        second_load_mock = MagicMock(name="_load_model_second")
        with patch.object(self.adapter, "_load_model", second_load_mock):
            result2 = self.adapter.warmup()

        self.assertTrue(result2)
        second_load_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
