"""GigaAMSTTAdapter — Phase D.2 STTAdapterBase wrapper around existing GigaAMAdapter.

Existing core/pipeline/stt_gigaam.py.GigaAMAdapter is preserved as the implementation;
this wrapper just provides the STTAdapterBase interface.

Supports only Russian ('ru') — GigaAM is trained exclusively on RU speech.
"""
from __future__ import annotations

from typing import Any

from .stt_adapter import STTAdapterBase, STTResult
from .stt_gigaam import GigaAMAdapter as _LegacyGigaAMAdapter


class GigaAMSTTAdapter(STTAdapterBase):
    """STTAdapterBase-compliant wrapper around the legacy GigaAMAdapter.

    The legacy adapter handles subprocess/in-process transport selection,
    model loading, and WAV I/O. This class only bridges the interface gap.

    Args:
        mode: GigaAM model variant — "rnnt" (default, higher quality) or "ctc"
              (faster). Full names ("v2_rnnt", etc.) also accepted by legacy adapter.
        device: PyTorch device — "cpu" or "mps" (Apple Silicon).
        transport: "auto" | "in_process" | "subprocess".
    """

    def __init__(
        self,
        mode: str = "rnnt",
        device: str = "cpu",
        transport: str = "auto",
    ) -> None:
        self._legacy = _LegacyGigaAMAdapter(
            mode=mode, device=device, transport=transport
        )
        self._mode = mode

    # ------------------------------------------------------------------
    # STTAdapterBase contract
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        mode_base = self._mode.replace("v2_", "").replace("v1_", "")
        return f"gigaam-{mode_base}"

    @property
    def display_name(self) -> str:
        return f"GigaAM ({self._mode.upper()})"

    def supports_language(self, language: str) -> bool:
        """GigaAM is RU-only."""
        return language == "ru"

    def is_available(self) -> bool:
        """Delegate to legacy is_available() if present; fall back to True.

        Legacy GigaAMAdapter does not expose is_available() — assume available.
        Real unavailability surfaces as ImportError / RuntimeError on first call.
        """
        try:
            fn = self._legacy.is_available
        except AttributeError:
            return True
        try:
            return bool(fn())
        except (AttributeError, Exception):
            return True

    def transcribe(
        self,
        audio: Any,
        *,
        language: str | None = None,
        max_duration_sec: float | None = None,
    ) -> STTResult:
        """Transcribe via legacy GigaAMAdapter, return unified STTResult.

        Args:
            audio: numpy.ndarray PCM float32 16 kHz mono.
            language: hint (ignored — GigaAM only supports 'ru').
            max_duration_sec: not enforced here (legacy handles timeout separately).
        """
        # Legacy transcribe() returns dict: {text, language, confidence, engine}
        result = self._legacy.transcribe(audio)
        text = result.get("text", "")
        return STTResult(
            text=text,
            engine=self.model_id,
            language=result.get("language") or language or "ru",
            confidence=result.get("confidence"),
            duration_sec=result.get("duration_sec"),
            word_count=len(text.split()) if text else 0,
            metadata=result,
        )

    def warmup(self) -> bool:
        """GigaAM lazy-loads on first transcribe; warmup is a no-op."""
        try:
            return bool(self._legacy.warmup()) if hasattr(self._legacy, "warmup") else True
        except (AttributeError, NotImplementedError):
            return True

    def unload(self) -> None:
        """Release subprocess worker / in-process model resources."""
        try:
            self._legacy.close()
        except (AttributeError, Exception):
            pass
