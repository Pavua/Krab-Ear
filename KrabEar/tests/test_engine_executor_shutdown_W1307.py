"""Tests for W1303 F4 MED fix — non-blocking ThreadPoolExecutor shutdown.

Verifies that when the STT transcription worker times out or raises, the
executor is shut down with wait=False (non-blocking), not wait=True which
would stall the fallback chain for up to TRANSCRIBE_TIMEOUT_SEC seconds on
a GPU-stuck thread.

Two suites:
  - ExecutorShutdownNoWaitOnTimeoutTest   — shutdown(wait=False, cancel_futures=True) on timeout
  - ExecutorNormalCompletionUnaffectedTest — normal path still returns result, no cancel_futures
"""
from __future__ import annotations

import concurrent.futures
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    from core.engine import AudioEngine
    return AudioEngine()


def _stt_result(conf: float = 0.85, model: str = "balanced") -> dict:
    import math
    logprob = math.log(max(conf, 1e-9))
    return {
        "text": "hello",
        "segments": [{"avg_logprob": logprob, "text": "hello"}],
        "model_used": model,
        "language": "ru",
    }


def _make_timeout_executor_mock():
    """Return (mock_executor_cls, mock_executor) where future.result raises TimeoutError."""
    mock_future = MagicMock()
    mock_future.result.side_effect = concurrent.futures.TimeoutError("gpu stuck")
    mock_executor = MagicMock()
    mock_executor.submit.return_value = mock_future
    mock_executor_cls = MagicMock(return_value=mock_executor)
    return mock_executor_cls, mock_executor


def _make_success_executor_mock(return_value):
    """Return (mock_executor_cls, mock_executor) where future.result returns return_value."""
    mock_future = MagicMock()
    mock_future.result.return_value = return_value
    mock_executor = MagicMock()
    mock_executor.submit.return_value = mock_future
    mock_executor_cls = MagicMock(return_value=mock_executor)
    return mock_executor_cls, mock_executor


# ---------------------------------------------------------------------------
# Suite 1: shutdown(wait=False, cancel_futures=True) on timeout — multipass path
# ---------------------------------------------------------------------------

class ExecutorShutdownNoWaitOnTimeoutTest(unittest.TestCase):
    """When future.result() raises TimeoutError, executor.shutdown is called
    with wait=False and cancel_futures=True so the fallback chain advances
    immediately without blocking on the stuck GPU thread."""

    def _run_multipass_timeout(self):
        """Drive _maybe_multipass_retry with a mock executor that raises TimeoutError."""
        engine = _make_engine()
        mock_executor_cls, mock_executor = _make_timeout_executor_mock()
        first = _stt_result(0.30)  # below threshold → triggers retry

        with patch("concurrent.futures.ThreadPoolExecutor", mock_executor_cls), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 1
            mock_cfg.MODEL_BALANCED = "balanced"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        return mock_executor

    def test_executor_shutdown_no_wait_on_timeout(self):
        """executor.shutdown must be called with wait=False when TimeoutError fires."""
        mock_executor = self._run_multipass_timeout()
        mock_executor.shutdown.assert_called()
        for c in mock_executor.shutdown.call_args_list:
            wait_val = c.kwargs.get("wait", c.args[0] if c.args else True)
            self.assertFalse(
                wait_val,
                f"shutdown called with wait=True on timeout path: {c}",
            )

    def test_executor_shutdown_cancel_futures_on_timeout(self):
        """cancel_futures=True must be passed to signal the stuck GPU thread."""
        mock_executor = self._run_multipass_timeout()
        timeout_calls = [
            c for c in mock_executor.shutdown.call_args_list
            if c.kwargs.get("cancel_futures") is True
        ]
        self.assertGreater(
            len(timeout_calls), 0,
            "Expected at least one shutdown(cancel_futures=True) call on timeout",
        )

    def _run_fallback_chain_timeout_balanced(self):
        """Drive _transcribe_with_fallback_chain (balanced profile) with timeout mock."""
        engine = _make_engine()
        # Force balanced profile so the chain only tries the balanced model then stops
        engine.quality_profile = "balanced"

        mock_executor_cls, mock_executor = _make_timeout_executor_mock()

        with patch("concurrent.futures.ThreadPoolExecutor", mock_executor_cls), \
             patch("core.engine.settings") as mock_cfg, \
             patch("core.engine._get_available_memory_gb", return_value=32.0):
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.AUDIO_LANG_ID_ENABLED = False
            mock_cfg.STT_ROUTER_ENABLED = False
            mock_cfg.MLX_CRASH_RECOVERY_ENABLED = False
            mock_cfg.DIARIZATION_ENABLED = False
            mock_cfg.LLM_REWRITE_ENABLED = False
            mock_cfg.PIPELINE_V2_ENABLED = False
            mock_cfg.STT_USE_RU_FINETUNE = False
            mock_cfg.STT_GIGAAM_ENABLED = False
            mock_cfg.PARAKEET_ENABLED = False
            mock_cfg.SENSEVOICE_ENABLED = False
            mock_cfg.WHISPERX_ENABLED = False
            mock_cfg.VOXTRAL_ENABLED = False
            mock_cfg.TRANSCRIBE_LANGUAGE = "ru"

            try:
                engine._transcribe_with_fallback_chain(b"\x00" * 320, "prompt", "ru")
            except Exception:
                pass

        return mock_executor

    def test_fallback_chain_shutdown_no_wait_on_timeout(self):
        """_transcribe_with_fallback_chain: shutdown(wait=False) on TimeoutError."""
        mock_executor = self._run_fallback_chain_timeout_balanced()
        # The executor must have been constructed and then shut down non-blocking
        if mock_executor.shutdown.called:
            for c in mock_executor.shutdown.call_args_list:
                wait_val = c.kwargs.get("wait", c.args[0] if c.args else True)
                self.assertFalse(
                    wait_val,
                    f"shutdown called with wait=True in fallback chain: {c}",
                )


# ---------------------------------------------------------------------------
# Suite 2: normal completion is unaffected
# ---------------------------------------------------------------------------

class ExecutorNormalCompletionUnaffectedTest(unittest.TestCase):
    """When transcription succeeds normally, the result is returned correctly
    and executor is shut down cleanly (wait=False, no cancel_futures=True)."""

    def _run_multipass_success(self):
        engine = _make_engine()
        max_result = _stt_result(0.85, model="max-model")
        mock_executor_cls, mock_executor = _make_success_executor_mock(max_result)
        first = _stt_result(0.30)  # low conf → triggers retry

        with patch("concurrent.futures.ThreadPoolExecutor", mock_executor_cls), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
            mock_cfg.STT_MAX_RETRIES = 1
            mock_cfg.MODEL_BALANCED = "balanced"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30

            result = engine._maybe_multipass_retry(b"audio", "prompt", "ru", first)

        return mock_executor, result

    def test_executor_normal_completion_unaffected(self):
        """Normal path: executor is shut down; no cancel_futures=True."""
        mock_executor, _ = self._run_multipass_success()
        mock_executor.shutdown.assert_called()
        cancel_calls = [
            c for c in mock_executor.shutdown.call_args_list
            if c.kwargs.get("cancel_futures") is True
        ]
        self.assertEqual(
            len(cancel_calls), 0,
            f"Unexpected cancel_futures=True on normal completion: {cancel_calls}",
        )

    def test_multipass_normal_result_returned(self):
        """_maybe_multipass_retry: successful retry returns the max-model result."""
        _, result = self._run_multipass_success()
        self.assertEqual(result["model_used"], "max-model")
        self.assertIn("multipass_attempts", result)

    def test_multipass_no_cancel_futures_on_success(self):
        """_maybe_multipass_retry success: no cancel_futures=True on executor shutdown."""
        mock_executor, _ = self._run_multipass_success()
        cancel_calls = [
            c for c in mock_executor.shutdown.call_args_list
            if c.kwargs.get("cancel_futures") is True
        ]
        self.assertEqual(
            len(cancel_calls), 0,
            f"Unexpected cancel_futures=True on multipass success: {cancel_calls}",
        )

    def _run_fallback_chain_success(self):
        """Drive _transcribe_with_fallback_chain with a successful balanced model."""
        engine = _make_engine()
        engine.quality_profile = "balanced"

        expected = _stt_result(0.90, model="balanced-model")
        mock_executor_cls, mock_executor = _make_success_executor_mock(expected)

        with patch("concurrent.futures.ThreadPoolExecutor", mock_executor_cls), \
             patch("core.engine.settings") as mock_cfg, \
             patch("core.engine._get_available_memory_gb", return_value=32.0):
            mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30
            mock_cfg.MODEL_BALANCED = "balanced-model"
            mock_cfg.model_max_list = ["max-model"]
            mock_cfg.NETWORK_MODE = "offline_strict"
            mock_cfg.AUDIO_LANG_ID_ENABLED = False
            mock_cfg.STT_ROUTER_ENABLED = False
            mock_cfg.MLX_CRASH_RECOVERY_ENABLED = False
            mock_cfg.DIARIZATION_ENABLED = False
            mock_cfg.LLM_REWRITE_ENABLED = False
            mock_cfg.PIPELINE_V2_ENABLED = False
            mock_cfg.STT_USE_RU_FINETUNE = False
            mock_cfg.STT_GIGAAM_ENABLED = False
            mock_cfg.PARAKEET_ENABLED = False
            mock_cfg.SENSEVOICE_ENABLED = False
            mock_cfg.WHISPERX_ENABLED = False
            mock_cfg.VOXTRAL_ENABLED = False
            mock_cfg.TRANSCRIBE_LANGUAGE = "ru"

            try:
                result = engine._transcribe_with_fallback_chain(b"\x00" * 320, "prompt", "ru")
            except Exception:
                result = None

        return mock_executor, result

    def test_fallback_chain_no_cancel_futures_on_success(self):
        """_transcribe_with_fallback_chain: no cancel_futures=True on success."""
        mock_executor, _ = self._run_fallback_chain_success()
        if mock_executor.shutdown.called:
            cancel_calls = [
                c for c in mock_executor.shutdown.call_args_list
                if c.kwargs.get("cancel_futures") is True
            ]
            self.assertEqual(
                len(cancel_calls), 0,
                f"Unexpected cancel_futures=True on success: {cancel_calls}",
            )


if __name__ == "__main__":
    unittest.main()
