"""Слой транскрибации backend-сервиса Krab Ear.

Класс Transcriber является высокоуровневым интерфейсом для AudioEngine,
позволяя переключать профили качества и управлять контекстом (словарями).
"""

from __future__ import annotations

import logging
from typing import Any

from core.engine import AudioEngine

logger = logging.getLogger("KrabEar.Backend.Transcriber")

class Transcriber:
    """Обёртка над AudioEngine для удобного вызова из API и IPC."""

    def __init__(self, engine: AudioEngine | None = None) -> None:
        """Инициализация. Если engine не передан, создаёт новый инстанс."""
        self.engine = engine or AudioEngine()

    def transcribe(
        self,
        audio_data: Any,
        quality_profile: str = "balanced",
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary: list[str] | None = None,
        lang_hint: str | None = None,
    ) -> dict[str, Any]:
        """Транскрибирует аудио с учётом выбранного профиля и контекста.

        Args:
            lang_hint: ISO 639-1 код языка или None/"auto" для авто-определения whisper'ом.
        """
        self.engine.set_quality_profile(quality_profile)
        return self.engine.transcribe(
            audio_data,
            cleanup_profile=cleanup_profile,
            is_preview=False,
            domain=domain,
            extra_vocabulary=extra_vocabulary,
            lang_hint=lang_hint,
        )

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict[str, Any]:
        """Быстрая транскрибация для realtime-превью (всегда в balanced режиме)."""
        # Preview всегда идёт в balanced для минимальной задержки
        self.engine.set_quality_profile("balanced")
        return self.engine.transcribe(audio_data, cleanup_profile="soft", is_preview=True)
