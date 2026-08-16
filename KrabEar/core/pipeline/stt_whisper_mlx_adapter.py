"""WhisperMLXAdapter — Phase D.2 STTAdapterBase wrapper around mlx_whisper.

Calls mlx_whisper.transcribe() directly (same path as engine.py._transcribe_model).
Wraps the call in mlx_lock() to prevent concurrent GPU access (SIGSEGV risk).

Supports all languages that Whisper supports (~99 languages including ru/es/en).
"""
from __future__ import annotations

import logging
from typing import Any

from .stt_adapter import STTAdapterBase, STTResult

try:
    from core.mlx_lock import mlx_lock
    from core.mlx_inter_lock import MLXInterLockTimeout, mlx_inter_process_lock
    from core.mlx_subprocess import MLXTimeoutError, get_watchdog
except ImportError:
    import contextlib

    mlx_lock = contextlib.nullcontext  # type: ignore[assignment]

    def mlx_inter_process_lock(**_kw):  # type: ignore[assignment]
        return contextlib.nullcontext()

    class MLXInterLockTimeout(Exception):  # type: ignore[no-redef]
        pass

    class MLXTimeoutError(RuntimeError):  # type: ignore[no-redef]
        pass

    get_watchdog = None  # type: ignore[assignment]

logger = logging.getLogger("KrabEar.STT.WhisperMLX")

# Supported language codes for Whisper (non-exhaustive — commonly used ones).
# Whisper natively handles most ISO 639-1 codes; we conservatively claim
# multilingual support by returning True for any non-empty string.
_WHISPER_UNSUPPORTED_LANGUAGES: frozenset[str] = frozenset()


class WhisperMLXAdapter(STTAdapterBase):
    """STTAdapterBase wrapper around mlx_whisper.transcribe().

    mlx_whisper must be installed in the active virtualenv. If not installed,
    is_available() returns False and transcribe() raises ImportError.

    Args:
        model_path: HuggingFace repo ID or local path for the MLX Whisper model.
                    Default: "mlx-community/whisper-large-v3-mlx".
        language: Fixed language override. None = auto-detect (default).
        temperature: Decoding temperature. 0.0 = greedy (default).
    """

    _DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"

    def __init__(
        self,
        model_path: str | None = None,
        language: str | None = None,
        temperature: float = 0.0,
    ) -> None:
        self._model_path = model_path or self._DEFAULT_MODEL
        self._language_override = language
        self._temperature = temperature

    # ------------------------------------------------------------------
    # STTAdapterBase contract
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        # Derive short stable ID from repo name (last path segment).
        return f"whisper-mlx/{self._model_path.split('/')[-1]}"

    @property
    def display_name(self) -> str:
        return f"Whisper MLX ({self._model_path.split('/')[-1]})"

    def supports_language(self, language: str) -> bool:
        """Whisper supports ~99 languages; returns True for any non-empty code."""
        return bool(language) and language not in _WHISPER_UNSUPPORTED_LANGUAGES

    def is_available(self) -> bool:
        """Returns True if mlx_whisper can be imported."""
        try:
            import mlx_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def transcribe(
        self,
        audio: Any,
        *,
        language: str | None = None,
        max_duration_sec: float | None = None,
    ) -> STTResult:
        """Transcribe via mlx_whisper.transcribe(), return unified STTResult.

        Args:
            audio: numpy.ndarray PCM float32 16 kHz mono.
            language: ISO 639-1 language hint. None = Whisper auto-detect.
            max_duration_sec: not enforced (Whisper handles internally).
        """
        effective_language = self._language_override or language

        params: dict[str, Any] = {
            "path_or_hf_repo": self._model_path,
            "language": effective_language,
            "temperature": self._temperature,
            "verbose": False,
        }

        # Try with optional params first, fall back if version doesn't support them.
        variants = [
            {**params, "condition_on_previous_text": False, "no_speech_threshold": 0.6},
            {**params, "condition_on_previous_text": False},
            params,
        ]

        timeout_sec = float(getattr(self, "_timeout_sec", 45.0) or 45.0)

        from core.mlx_whisper_session import (
            mlx_whisper_worker_enabled,
            transcribe_via_mlx_worker,
        )

        result: dict[str, Any] = {}
        last_err: Exception | None = None

        if mlx_whisper_worker_enabled():
            with mlx_inter_process_lock():
                for p in variants:
                    try:
                        result = transcribe_via_mlx_worker(
                            audio, p, timeout_sec, self._model_path,
                        )
                        break
                    except TypeError as exc:
                        last_err = exc
                        continue
                    except MLXTimeoutError as exc:
                        logger.error(
                            "WhisperMLXAdapter: worker timeout %.1fs (model=%s)",
                            getattr(exc, "timeout_sec", timeout_sec),
                            self._model_path,
                        )
                        raise
                else:
                    raise last_err or RuntimeError("mlx_whisper.transcribe failed")
        else:
            try:
                import mlx_whisper  # type: ignore[import]
            except ImportError as exc:
                raise ImportError(
                    "mlx_whisper not installed. Run: pip install mlx-whisper"
                ) from exc

            with mlx_inter_process_lock(), mlx_lock():  # W1635: cross-process flock (outer) + intra-process RLock (inner)
                for p in variants:
                    try:
                        if get_watchdog is not None:
                            captured_p = p
                            result = get_watchdog().run_with_timeout(
                                fn=lambda: mlx_whisper.transcribe(audio, **captured_p),
                                timeout_sec=timeout_sec,
                                model_name=self._model_path,
                            )
                        else:
                            result = mlx_whisper.transcribe(audio, **p)
                        break
                    except TypeError as exc:
                        last_err = exc
                        continue
                    except MLXTimeoutError as exc:
                        # KRAB-EAR-BACKEND-1V: не повторять variants при зависшем GPU
                        logger.error(
                            "WhisperMLXAdapter: watchdog timeout %.1fs (model=%s)",
                            getattr(exc, "timeout_sec", timeout_sec),
                            self._model_path,
                        )
                        raise
                else:
                    raise last_err or RuntimeError("mlx_whisper.transcribe failed")

                # W63 rule: free MLX metal buffer cache after inference to prevent
                # RAM growth on long sessions (same fix as engine.py line 545/920).
                try:
                    import mlx.core as mx
                    mx.clear_cache()
                except Exception:  # noqa: BLE001
                    pass  # MLX not installed or older version without clear_cache

        text: str = result.get("text", "") or ""
        # Whisper result may include segments with per-segment confidence-ish values.
        # No direct confidence score in mlx_whisper output — leave as None.
        detected_lang: str | None = result.get("language") or effective_language

        return STTResult(
            text=text.strip(),
            engine=self.model_id,
            language=detected_lang,
            confidence=None,
            duration_sec=None,
            word_count=len(text.split()) if text else 0,
            metadata=result,
        )

    def warmup(self) -> bool:
        """Whisper MLX lazy-loads on first call; warmup is a no-op."""
        return True

    def unload(self) -> None:
        """mlx_whisper holds model in process memory; explicit unload not supported."""
        return
