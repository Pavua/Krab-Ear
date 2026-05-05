"""Base class for STT adapter implementations.

Each concrete adapter (Whisper-MLX, GigaAM, Parakeet, Voxtral, SenseVoice, etc.)
implements transcribe() returning unified result schema.

Router (in stt_router.py) selects adapter based on:
- Detected language
- User-configured priority
- Model availability (loaded / loadable)
- Quality vs. speed preference
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class STTResult:
    """Unified transcription result across all adapters."""
    text: str
    engine: str  # e.g. "gigaam-rnnt", "parakeet-tdt-1.1b"
    language: Optional[str]  # ISO 639-1 if detected
    confidence: Optional[float]  # 0.0-1.0 if available
    duration_sec: Optional[float]
    word_count: int
    metadata: dict = field(default_factory=dict)  # adapter-specific extras


class STTAdapterBase(abc.ABC):
    """Abstract base for STT adapters.

    Subclasses implement: transcribe, model_id, supports_language, is_available.
    Optional: load_async, unload, warmup.
    """

    @property
    @abc.abstractmethod
    def model_id(self) -> str:
        """Stable identifier (e.g. 'gigaam-rnnt-v1', 'parakeet-tdt-1.1b').
        Used in STTResult.engine + audit logs.
        """

    @property
    def display_name(self) -> str:
        """Human-readable name for UI ('GigaAM RNNT', 'Parakeet TDT 1.1B')."""
        return self.model_id

    @abc.abstractmethod
    def transcribe(self, audio: Any, *, language: str | None = None,
                   max_duration_sec: float | None = None) -> STTResult:
        """Synchronous transcription. May raise on errors.

        Args:
            audio: numpy.ndarray PCM 16 kHz float32
            language: hint, ISO 639-1 ('ru', 'en', 'es') or None for auto
            max_duration_sec: optional cap for safety; None = unlimited
        """

    @abc.abstractmethod
    def supports_language(self, language: str) -> bool:
        """Returns True if adapter can handle ISO 639-1 lang code.
        E.g. GigaAM: only 'ru'. Whisper-MLX: most. Parakeet: 'en' (later 'ru')."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Returns True if adapter is loadable in current environment.
        Checks: model files present, dependencies imported, hw permission."""

    def warmup(self) -> bool:
        """Optional: pre-load model to reduce first-call latency.
        Returns True on success. Default: no-op (returns True).
        Subclasses override if loading is expensive."""
        return True

    def unload(self) -> None:
        """Optional: free model memory. Default: no-op.
        Override for memory pressure scenarios."""
        return

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.model_id} avail={self.is_available()}>"
