"""Слой транскрибации backend-сервиса Krab Ear."""

from __future__ import annotations

import logging
from typing import Any

from core.engine import AudioEngine

logger = logging.getLogger("KrabEar.Backend.Transcriber")


class Transcriber:
    """Обёртка над AudioEngine с профилями качества balanced/max."""

    def __init__(self) -> None:
        self.engine = AudioEngine()

    def transcribe(
        self,
        audio_data: Any,
        quality_profile: str,
        cleanup_profile: str = "soft",
    ) -> str:
        """Транскрибирует данные в заданном профиле качества."""
        self.engine.set_quality_profile(quality_profile)
        return self.engine.transcribe(audio_data, cleanup_profile=cleanup_profile, is_preview=False)

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> str:
        """Транскрибирует короткий срез аудио для realtime preview."""
        # Preview всегда идёт в balanced для стабильности и минимальной задержки.
        # Это исключает деградацию из-за тяжёлых/недоступных max-кандидатов.
        self.engine.set_quality_profile("balanced")
        return self.engine.transcribe(audio_data, cleanup_profile="soft", is_preview=True)
