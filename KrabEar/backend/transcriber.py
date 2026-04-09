"""Слой транскрибации backend-сервиса Krab Ear.

Класс Transcriber является высокоуровневым интерфейсом для AudioEngine,
позволяя переключать профили качества и управлять контекстом (словарями).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TYPE_CHECKING

from core.engine import AudioEngine

if TYPE_CHECKING:
    from backend.llm_rewriter import LLMRewriter

logger = logging.getLogger("KrabEar.Backend.Transcriber")


class Transcriber:
    """Обёртка над AudioEngine для удобного вызова из API и IPC."""

    def __init__(
        self,
        engine: AudioEngine | None = None,
        llm_rewriter: Optional["LLMRewriter"] = None,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """Инициализация.

        Args:
            engine: опциональный AudioEngine. Если None — создаётся новый с
                    инжекцией llm_rewriter и settings_get.
            llm_rewriter: D.10a LLM клиент для post-cleanup rewrite'а (прокидывается в AudioEngine).
            settings_get: callback для runtime toggle'ов (прокидывается в AudioEngine).
        """
        if engine is None:
            self.engine = AudioEngine(llm_rewriter=llm_rewriter, settings_get=settings_get)
        else:
            self.engine = engine
            if llm_rewriter is not None and engine._llm_rewriter is None:
                engine._llm_rewriter = llm_rewriter
            if settings_get is not None:
                engine._settings_get = settings_get

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
