"""W1223 security tests for Voxtral adapter in AudioEngine.

Covers three HIGH findings from W1219 audit:
  F1: _voxtral_generate() must be called under mlx_lock()
  F2: adapter branch in fallback chain must use ThreadPoolExecutor + timeout guard
  F3: snapshot_download must validate VOXTRAL_MODEL against _VOXTRAL_REPO_ALLOWLIST

All tests use AudioEngine.__new__() + mocks — no real MLX/mistral-inference required.
"""

from __future__ import annotations

import concurrent.futures
import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.engine as _engine_mod
from core import stt_budget
from core.engine import AudioEngine, _VOXTRAL_REPO_ALLOWLIST


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_engine() -> AudioEngine:
    """Minimal AudioEngine without __init__ — for unit tests."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models = {}
    engine._router = None
    engine._voxtral_model = None
    engine._voxtral_load_error = None
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._whisperx_model = None
    engine._whisperx_load_error = None
    engine._parakeet_model = None
    engine._parakeet_load_error = None
    return engine


def _mock_settings(mock: Any, voxtral_model: str = "mistralai/Voxtral-Mini-3B-2507") -> None:
    mock.VOXTRAL_ENABLED = True
    mock.VOXTRAL_MODEL = voxtral_model
    mock.VOXTRAL_REASONING_ENABLED = False
    mock.WHISPERX_ENABLED = False
    mock.WHISPERX_MODEL = "large-v3"
    mock.WHISPERX_DEVICE = "cpu"
    mock.WHISPERX_DIARIZATION = False
    mock.WHISPERX_WORD_TIMESTAMPS = True
    mock.SENSEVOICE_ENABLED = False
    mock.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    mock.SENSEVOICE_EMOTION_TO_HISTORY = True
    mock.PARAKEET_ENABLED = False
    mock.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
    mock.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    mock.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    mock.TRANSCRIBE_TIMEOUT_SEC = 30
    mock.NETWORK_MODE = "offline_strict"
    mock.model_max_list = ["mlx-community/whisper-large-v3-turbo"]
    mock.HF_TOKEN = ""


# ---------------------------------------------------------------------------
# F1: _voxtral_generate() must be called inside mlx_lock()
# ---------------------------------------------------------------------------

class TestVoxtralGenerateHoldsMlxLock(unittest.TestCase):
    """F1 — _voxtral_generate must execute while mlx_lock is held."""

    @patch("core.engine.settings")
    @patch("core.engine._voxtral_generate")
    @patch("core.engine._VoxtralAudioChunk")
    @patch("core.engine._VoxtralUserMessage", create=True)
    @patch("core.engine._VoxtralChatRequest", create=True)
    @patch("core.engine._voxtral_available", True)
    def test_voxtral_generate_holds_mlx_lock(
        self,
        mock_chat_req: Any,
        mock_user_msg: Any,
        mock_audio_chunk: Any,
        mock_generate: Any,
        mock_settings: Any,
    ) -> None:
        """_voxtral_generate() must be called while mlx_lock is held."""
        _mock_settings(mock_settings)

        engine = _bare_engine()

        # Pre-load a fake model so _load_voxtral_model() returns immediately.
        fake_model = MagicMock()
        fake_tokenizer = MagicMock()
        encoded = MagicMock()
        encoded.tokens = [1, 2, 3]
        fake_tokenizer.encode_chat_completion.return_value = (encoded, None)
        fake_tokenizer.instruct_tokenizer.tokenizer.eos_id = 2
        fake_tokenizer.instruct_tokenizer.tokenizer.decode.return_value = "hello"
        engine._voxtral_model = (fake_model, fake_tokenizer)

        # Track whether mlx_lock was held during generate call.
        lock_held_during_generate: list[bool] = []

        from core.mlx_lock import mlx_lock

        original_generate = _engine_mod._voxtral_generate

        def _spy_generate(*args: Any, **kwargs: Any) -> Any:
            # Check if the mlx_lock RLock is currently locked by trying a
            # non-blocking acquire from a background thread.
            # If the lock is held by the main thread, the background thread
            # cannot acquire it → is_set() on `acquired` confirms lock is held.
            acquired = threading.Event()

            def _try_acquire() -> None:
                import core.mlx_lock as _mod
                got = _mod._mlx_lock.acquire(blocking=False)
                if got:
                    # Could acquire → lock NOT held (bad)
                    _mod._mlx_lock.release()
                else:
                    # Could not acquire → lock IS held (good)
                    acquired.set()

            t = threading.Thread(target=_try_acquire, daemon=True)
            t.start()
            t.join(timeout=1.0)
            lock_held_during_generate.append(acquired.is_set())
            return ([10, 20], None)

        mock_generate.side_effect = _spy_generate
        mock_audio_chunk.return_value = MagicMock()

        completion_mock = MagicMock()
        mock_chat_req.return_value = completion_mock
        mock_user_msg.return_value = MagicMock()

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            engine._transcribe_voxtral(b"\x00" * 32)

        self.assertTrue(
            any(lock_held_during_generate),
            "_voxtral_generate was NOT called while mlx_lock was held (F1 regression)",
        )


# ---------------------------------------------------------------------------
# F2: adapter branch in fallback chain uses ThreadPoolExecutor + timeout
# ---------------------------------------------------------------------------

class TestVoxtralAdapterBranchUsesTranscribeTimeout(unittest.TestCase):
    """F2 — adapter branch must wrap adapter_fn() in ThreadPoolExecutor with timeout."""

    @patch("core.engine.settings")
    def test_voxtral_adapter_branch_uses_transcribe_timeout(self, mock_settings: Any) -> None:
        """Fallback chain adapter branch uses ThreadPoolExecutor.result(timeout=...) for Voxtral."""
        _mock_settings(mock_settings)
        engine = _bare_engine()
        engine._unavailable_models["mlx-community/whisper-large-v3-turbo"] = __import__("time").monotonic()

        calls_with_timeout: list[float] = []

        class FakeExecutor:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            def __enter__(self) -> "FakeExecutor":
                return self

            def __exit__(self, *a: Any) -> None:
                pass

            def submit(self, fn: Any, *a: Any, **kw: Any) -> "FakeFuture":
                return FakeFuture(fn)

            def shutdown(self, wait: bool = True, **kw: Any) -> None: pass

        class FakeFuture:
            def __init__(self, fn: Any) -> None:
                self._fn = fn

            def result(self, timeout: float | None = None) -> Any:
                if timeout is not None:
                    calls_with_timeout.append(timeout)
                return self._fn()

            def cancel(self) -> None:
                pass

        expected_result = {
            "text": "Voxtral result",
            "engine": "voxtral",
            "language": "ru",
            "segments": [],
            "reasoning": None,
        }

        with patch("core.engine._profiler") as mock_profiler, \
             patch("concurrent.futures.ThreadPoolExecutor", FakeExecutor), \
             patch.object(engine, "_transcribe_voxtral", return_value=expected_result):
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")

        self.assertIsNotNone(result, "Expected a transcription result from Voxtral branch")
        # At least one future.result() call must have passed a timeout
        self.assertTrue(
            len(calls_with_timeout) > 0,
            "adapter_fn() was not wrapped with future.result(timeout=...) — F2 regression",
        )
        # Спека 2026-08-26 (§4.8): контракт "== TRANSCRIBE_TIMEOUT_SEC=30" снят —
        # adapter-таймаут теперь идёт через stt_budget.resolve_attempt_timeout_sec
        # с floor'ом ADAPTER_MIN_BUDGET_SEC (внешний таймаут не смеет быть короче
        # внутренних таймаутов GigaAM-subprocess). Проверяем floor, а не старую
        # константу — settings.TRANSCRIBE_TIMEOUT_SEC в этой ветке больше не читается.
        self.assertGreaterEqual(
            calls_with_timeout[0],
            stt_budget.ADAPTER_MIN_BUDGET_SEC,
            msg="Timeout passed to future.result() must respect ADAPTER_MIN_BUDGET_SEC floor (§4.8)",
        )

    @patch("core.engine.settings")
    def test_voxtral_adapter_timeout_marks_unavailable(self, mock_settings: Any) -> None:
        """When adapter times out, model is added to _unavailable_models and chain continues."""
        _mock_settings(mock_settings)
        engine = _bare_engine()
        engine._unavailable_models["mlx-community/whisper-large-v3-turbo"] = __import__("time").monotonic()

        timeout_exc = concurrent.futures.TimeoutError()

        class TimingOutFuture:
            def result(self, timeout: float | None = None) -> Any:
                raise timeout_exc

            def cancel(self) -> None:
                pass

        class TimingOutExecutor:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            def __enter__(self) -> "TimingOutExecutor":
                return self

            def __exit__(self, *a: Any) -> None:
                pass

            def submit(self, fn: Any, *a: Any, **kw: Any) -> TimingOutFuture:
                return TimingOutFuture()

            def shutdown(self, wait: bool = True, **kw: Any) -> None: pass

        with patch("core.engine._profiler") as mock_profiler, \
             patch("concurrent.futures.ThreadPoolExecutor", TimingOutExecutor):
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            # Should not raise — chain should swallow and continue (all models unavailable)
            try:
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")
            except Exception:
                pass  # Raising after exhausting chain is OK

        self.assertIn(
            engine._VOXTRAL_MARKER,
            engine._unavailable_models,
            "Voxtral marker must be added to _unavailable_models after timeout",
        )


# ---------------------------------------------------------------------------
# F3: snapshot_download validates VOXTRAL_MODEL against allowlist
# ---------------------------------------------------------------------------

class TestVoxtralModelRepoAllowlist(unittest.TestCase):
    """F3 — _load_voxtral_model() must reject repos not in _VOXTRAL_REPO_ALLOWLIST."""

    def test_voxtral_model_repo_allowlist_rejects_unknown(self) -> None:
        """Unknown VOXTRAL_MODEL repo raises RuntimeError before snapshot_download."""
        engine = _bare_engine()

        with patch("core.engine.settings") as mock_settings, \
             patch("core.engine._voxtral_available", True), \
             patch("core.engine._profiler") as mock_profiler:
            mock_settings.VOXTRAL_MODEL = "evil/arbitrary-model-injection"
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

            with self.assertRaises(RuntimeError) as ctx:
                engine._load_voxtral_model()

        self.assertIn(
            "evil/arbitrary-model-injection",
            str(ctx.exception),
            "Error message should include the rejected repo ID",
        )
        self.assertIn(
            "допустимы только",
            str(ctx.exception),
            "Error message should list allowed repos",
        )

    def test_voxtral_model_repo_allowlist_accepts_known(self) -> None:
        """Known VOXTRAL_MODEL repos pass validation and proceed to snapshot_download."""
        for repo_id in sorted(_VOXTRAL_REPO_ALLOWLIST):
            with self.subTest(repo_id=repo_id):
                engine = _bare_engine()

                fake_model = MagicMock()
                fake_tokenizer = MagicMock()

                with patch("core.engine.settings") as mock_settings, \
                     patch("core.engine._voxtral_available", True), \
                     patch("core.engine._profiler") as mock_profiler:
                    mock_settings.VOXTRAL_MODEL = repo_id
                    mock_profiler.start_span.return_value.__enter__ = lambda s: s
                    mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

                    with patch("core.engine.snapshot_download", create=True) as mock_dl, \
                         patch("builtins.__import__") as mock_import:
                        # Patch snapshot_download via huggingface_hub inside the method
                        hf_hub_mock = MagicMock()
                        hf_hub_mock.snapshot_download.return_value = "/tmp/fake_model_path"

                        def _side_import(name: str, *a: Any, **kw: Any) -> Any:
                            if name == "huggingface_hub":
                                return hf_hub_mock
                            return __builtins__.__import__(name, *a, **kw)  # type: ignore

                        mock_import.side_effect = _side_import

                        # Patch VoxtralTokenizer and VoxtralTransformer at module level
                        with patch("core.engine._VoxtralTokenizer") as mock_tok_cls, \
                             patch("core.engine._VoxtralTransformer") as mock_trans_cls:
                            mock_tok_cls.from_file.return_value = fake_tokenizer
                            mock_trans_cls.from_folder.return_value = fake_model

                            # Should NOT raise RuntimeError for allowlisted repos.
                            # (May raise other errors if huggingface_hub not available,
                            #  but the allowlist check itself must not block known repos.)
                            try:
                                result = engine._load_voxtral_model()
                                # If no exception, verify model was cached
                                self.assertIsNotNone(engine._voxtral_model)
                            except RuntimeError as exc:
                                # Only acceptable RuntimeError is from snapshot_download fail,
                                # NOT from allowlist rejection.
                                self.assertNotIn(
                                    "допустимы только",
                                    str(exc),
                                    f"Known repo '{repo_id}' was incorrectly rejected by allowlist",
                                )


if __name__ == "__main__":
    unittest.main()
