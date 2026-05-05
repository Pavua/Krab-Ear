"""STT engine router — selects adapter based on language, settings, availability."""
from __future__ import annotations

import logging
from typing import Optional

from .stt_adapter import STTAdapterBase, STTResult

logger = logging.getLogger("KrabEar.STT.Router")


class STTRouter:
    """Routes transcribe() calls to best-fit adapter.

    Priority order (configurable):
    1. User-pinned engine (settings.stt_force_engine)
    2. Quality-first preferred for given language
    3. Speed-first if real-time required (settings.stt_streaming_enabled)
    4. Fallback chain (whisper as default)
    """

    def __init__(self, adapters: list[STTAdapterBase], settings_provider=None):
        self._adapters: list[STTAdapterBase] = list(adapters)
        self._settings_provider = settings_provider or (lambda: {})

    def select_adapter(self, language: str | None = None,
                       prefer_speed: bool = False) -> Optional[STTAdapterBase]:
        """Returns best-fit adapter for given criteria. None if no adapter available."""
        settings = self._settings_provider() or {}

        # 1. User-pinned engine
        forced = settings.get("stt_force_engine")
        if forced:
            for a in self._adapters:
                if a.model_id == forced and a.is_available():
                    return a

        # 2. Filter by language support + availability
        candidates = [a for a in self._adapters if a.is_available()]
        if language:
            candidates = [a for a in candidates if a.supports_language(language)]

        if not candidates:
            return None

        # 3. Apply quality/speed preference (placeholder — extend later)
        # For now: first candidate. Future: sort by quality_score / speed_score from each adapter.
        return candidates[0]

    def transcribe(self, audio, *, language: str | None = None,
                   prefer_speed: bool = False, **kwargs) -> STTResult:
        """High-level entry point — picks adapter + delegates."""
        adapter = self.select_adapter(language=language, prefer_speed=prefer_speed)
        if adapter is None:
            raise RuntimeError(f"No STT adapter available for language={language}")
        logger.info("Routed transcribe to %s (lang=%s)", adapter.model_id, language)
        return adapter.transcribe(audio, language=language, **kwargs)
