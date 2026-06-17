"""Parakeet MLX STT adapter — Phase D.2.1 real implementation.

NVIDIA Parakeet TDT 0.6B via parakeet-mlx (Apple Silicon MLX port).
English-only, low-latency, strong WER on conversational/technical EN speech.

Library: https://github.com/senstella/parakeet-mlx
Install:  pip install parakeet-mlx
Model:    mlx-community/parakeet-tdt-0.6b-v2  (preferred, ~1.2 GB)
          mlx-community/parakeet-tdt-0.6b-v3  (latest, if available)

MLX thread-safety: parakeet-mlx uses MLX for inference — must be wrapped
in mlx_lock() to prevent concurrent Metal GPU access with Whisper MLX
(same race condition that caused SIGSEGV, fixed in PR #71).

Result schema (AlignedResult from parakeet-mlx):
    result.text          str             — full transcription
    result.sentences     list[AlignedSentence]
        .text            str             — sentence text
        .start / .end    float           — timestamps in seconds
        .tokens          list[AlignedToken]
            .text        str             — token/word
            .start/.end  float           — word-level timestamps
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .stt_adapter import STTAdapterBase, STTResult

logger = logging.getLogger("KrabEar.STT.Parakeet")

# Default model — v2 is stable and widely cached; v3 if available.
_DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"

# One-time warning sentinel (avoid log spam on repeated unavailable calls).
_warned_unavailable: bool = False


def _try_import_parakeet() -> Optional[Any]:
    """Attempt to import parakeet_mlx. Returns module or None."""
    try:
        import parakeet_mlx  # type: ignore[import]
        return parakeet_mlx
    except ImportError:
        return None


class ParakeetSTTAdapter(STTAdapterBase):
    """NVIDIA Parakeet TDT 0.6B — English-only, Apple Silicon MLX adapter.

    Uses parakeet-mlx library (https://github.com/senstella/parakeet-mlx).
    When parakeet-mlx is not installed, is_available() returns False and
    transcribe() logs a one-time warning and raises ImportError.

    Args:
        model_path: HuggingFace repo ID for the MLX Parakeet model.
                    Default: "mlx-community/parakeet-tdt-0.6b-v2".
    """

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path or _DEFAULT_MODEL
        self._model: Any = None  # lazy-loaded on first transcribe()
        self._load_failed: bool = False
        # Protects check-then-load against concurrent calls from multiple
        # threads (e.g. parallel warmup + first transcribe()).  Mirrors
        # SenseVoiceSTTAdapter._load_lock (W1218 F2).
        self._load_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # STTAdapterBase contract
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"parakeet-mlx/{self._model_path.split('/')[-1]}"

    @property
    def display_name(self) -> str:
        return f"Parakeet TDT ({self._model_path.split('/')[-1]})"

    def supports_language(self, language: str) -> bool:
        """Parakeet is English-only (EN)."""
        return language == "en"

    def is_available(self) -> bool:
        """Returns True if parakeet_mlx is importable."""
        return _try_import_parakeet() is not None

    def _load_model(self, parakeet_mlx: Any) -> None:
        """Load the parakeet-mlx model into self._model.

        Call only while holding self._load_lock (double-checked locking,
        mirrors SenseVoiceSTTAdapter, W1218 F2).  from_pretrained loads the
        model weights onto the Metal GPU, so — unlike PyTorch adapters — it is
        serialized through mlx_lock() and the cross-process flock exactly like
        inference below: a concurrent load + an in-flight inference on another
        thread would race the GPU → SIGSEGV (the race this module's header
        warns about; see PR #71).
        """
        try:
            from core.mlx_inter_lock import (
                MLXInterLockTimeout,
                mlx_inter_process_lock,
            )
            from core.mlx_lock import mlx_lock
        except ImportError:
            import contextlib
            MLXInterLockTimeout = Exception  # type: ignore[assignment,misc]
            mlx_inter_process_lock = contextlib.nullcontext  # type: ignore[assignment]
            mlx_lock = contextlib.nullcontext  # type: ignore[assignment]

        try:
            logger.info(
                "ParakeetSTTAdapter: loading model %s (first call)",
                self._model_path,
            )
            with mlx_inter_process_lock():
                with mlx_lock():
                    self._model = parakeet_mlx.from_pretrained(self._model_path)
            logger.info("ParakeetSTTAdapter: model loaded successfully")
        except MLXInterLockTimeout:
            # Transient cross-process lock timeout — do NOT mark the adapter
            # permanently failed; surface so the router can retry / fall back
            # without poisoning future loads.
            logger.error(
                "ParakeetSTTAdapter: MLX inter-process lock timeout during "
                "model load — aborting to prevent GPU-corruption SIGSEGV.",
                exc_info=True,
            )
            raise
        except Exception as exc:
            self._load_failed = True
            logger.error(
                "ParakeetSTTAdapter: failed to load model %s: %s",
                self._model_path,
                exc,
            )
            raise RuntimeError(
                f"ParakeetSTTAdapter: model load failed: {exc}"
            ) from exc

    def transcribe(
        self,
        audio: Any,
        *,
        language: str | None = None,
        max_duration_sec: float | None = None,
    ) -> STTResult:
        """Transcribe audio via parakeet-mlx, return unified STTResult.

        Args:
            audio: numpy.ndarray PCM float32 16 kHz mono, or str path to audio file.
            language: Ignored (Parakeet is EN-only; kept for interface compatibility).
            max_duration_sec: not enforced by Parakeet internally; caller truncates.

        Returns:
            STTResult with text, language="en", segments in metadata.

        Raises:
            ImportError: if parakeet_mlx is not installed.
            RuntimeError: if model loading or inference fails.
        """
        global _warned_unavailable

        parakeet_mlx = _try_import_parakeet()
        if parakeet_mlx is None:
            if not _warned_unavailable:
                logger.warning(
                    "ParakeetSTTAdapter: parakeet-mlx not installed. "
                    "Install with: pip install parakeet-mlx  "
                    "(adapter will be skipped until installed)"
                )
                _warned_unavailable = True
            raise ImportError(
                "parakeet-mlx not installed. Run: pip install parakeet-mlx"
            )

        # Lazy model load — cache as instance attribute.
        # Double-checked locking (mirrors SenseVoiceSTTAdapter, W1218 F2): the
        # first check is unsynchronized for speed; if a load is needed we take
        # _load_lock and re-check so a second thread (parallel warmup + first
        # transcribe()) cannot load the model twice.
        if self._model is None and not self._load_failed:
            with self._load_lock:
                if self._model is None and not self._load_failed:
                    self._load_model(parakeet_mlx)

        if self._load_failed or self._model is None:
            raise RuntimeError(
                "ParakeetSTTAdapter: model unavailable (load previously failed)"
            )

        # Serialize MLX GPU access at two levels:
        #   outer: mlx_inter_process_lock() — POSIX flock across OS processes
        #          (REST server + IPC server running concurrently).  Active only
        #          when KRAB_EAR_MLX_INTER_PROCESS_LOCK=1; no-op otherwise.
        #   inner: mlx_lock() — intra-process RLock across threads.
        # Both are needed here because parakeet-mlx uses MLX (Metal GPU) for
        # inference, unlike NeMo/PyTorch-based adapters (SenseVoice, GigaAM)
        # which run on PyTorch+MPS and therefore don't need either lock.
        # Same dual-lock pattern as mlx_whisper in AudioEngine._transcribe_mlx()
        # — see core/mlx_inter_lock.py usage note and PR #71 for rationale.
        #
        # W1636 (W1630 F2 HIGH): mlx_inter_process_lock raises MLXInterLockTimeout
        # on flock timeout (safe default). We catch it here, log loudly, and re-raise
        # so the STT router can mark this adapter temporarily unavailable instead of
        # silently running unguarded MLX and risking GPU-corruption SIGSEGV.
        try:
            from core.mlx_inter_lock import MLXInterLockTimeout, mlx_inter_process_lock
            from core.mlx_lock import mlx_lock
        except ImportError:
            import contextlib
            MLXInterLockTimeout = Exception  # type: ignore[assignment,misc]
            mlx_inter_process_lock = contextlib.nullcontext  # type: ignore[assignment]
            mlx_lock = contextlib.nullcontext  # type: ignore[assignment]

        try:
            with mlx_inter_process_lock():
                with mlx_lock():
                    raw_result = self._model.transcribe(audio)
        except MLXInterLockTimeout:
            logger.error(
                "ParakeetSTTAdapter: MLX inter-process lock timeout — "
                "cannot safely run inference without cross-process serialization. "
                "Aborting transcription to prevent GPU-corruption SIGSEGV.",
                exc_info=True,
            )
            raise

        # Build unified STTResult from AlignedResult.
        text: str = (raw_result.text or "").strip()

        # Extract segments from AlignedResult.sentences for metadata.
        segments: list[dict[str, Any]] = []
        if hasattr(raw_result, "sentences") and raw_result.sentences:
            for sent in raw_result.sentences:
                seg: dict[str, Any] = {
                    "text": getattr(sent, "text", ""),
                    "start": getattr(sent, "start", None),
                    "end": getattr(sent, "end", None),
                }
                # Include word-level tokens if present.
                if hasattr(sent, "tokens") and sent.tokens:
                    seg["tokens"] = [
                        {
                            "text": getattr(t, "text", ""),
                            "start": getattr(t, "start", None),
                            "end": getattr(t, "end", None),
                        }
                        for t in sent.tokens
                    ]
                segments.append(seg)

        # Parakeet does not expose a confidence score directly.
        # Approximate from segment count vs text length as a quality signal.
        confidence: Optional[float] = None

        return STTResult(
            text=text,
            engine=self.model_id,
            language="en",
            confidence=confidence,
            duration_sec=None,
            word_count=len(text.split()) if text else 0,
            metadata={
                "segments": segments,
                "model_path": self._model_path,
            },
        )

    def warmup(self) -> bool:
        """Pre-load model to reduce first-call latency.

        Returns True on success, False if parakeet-mlx not installed or load fails.

        Uses double-checked locking under self._load_lock and delegates to
        self._load_model() — identical pattern to SenseVoiceSTTAdapter.warmup()
        (W1218 F2).  This ensures:
          (1) from_pretrained is serialized through mlx_lock() + mlx_inter_process_lock()
              exactly as in transcribe(), preventing concurrent-warmup SIGSEGV.
          (2) Transient MLXInterLockTimeout does NOT permanently set _load_failed;
              _load_model() owns the transient-vs-permanent decision.
        """
        if not self.is_available():
            return False
        parakeet_mlx = _try_import_parakeet()
        if parakeet_mlx is None:
            return False
        if self._model is None and not self._load_failed:
            with self._load_lock:
                if self._model is None and not self._load_failed:
                    try:
                        self._load_model(parakeet_mlx)
                    except Exception:
                        return False
        return self._model is not None

    def unload(self) -> None:
        """Release model from memory."""
        self._model = None
        self._load_failed = False


# W1591 / W1538: backward-compat alias for tests that use the shorter name
ParakeetAdapter = ParakeetSTTAdapter
