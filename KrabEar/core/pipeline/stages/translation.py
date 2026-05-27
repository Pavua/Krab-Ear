"""TranslationStage — перевод финального текста через Translator."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from core.pipeline.context import PipelineContext

logger = logging.getLogger("KrabEar.Pipeline.Translation")


class TranslationStage:
    """Стадия перевода транскрипта.

    should_run() возвращает False если:
    - translator не задан (None),
    - translation_mode == "off" (в ctx или через settings_get).

    process() НИКОГДА не raises — ошибки перевода идут в ctx.errors.
    Результат пишется в ctx.translation и ctx.translation_engine.
    """

    name = "translation"

    def __init__(
        self,
        translator,  # Translator | callable | None
        settings_get: Optional[Callable] = None,
    ) -> None:
        self._translator = translator
        self._settings_get = settings_get or (lambda k, d=None: d)

    def should_run(self, ctx: PipelineContext) -> bool:
        if self._translator is None:
            return False
        mode = ctx.translation_mode or self._settings_get("translation_mode", "off")
        return mode != "off"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        text_in = ctx.final_text or ctx.cleaned_text or ctx.raw_text
        mode = ctx.translation_mode or self._settings_get("translation_mode", "off")
        network_mode = self._settings_get("network_mode", "offline")
        translation_style = self._settings_get("translation_style", "neutral")
        glossary = self._settings_get("translation_glossary", None)

        try:
            result = self._translator.translate(
                text=text_in,
                mode=mode,
                network_mode=network_mode,
                translation_style=translation_style,
                glossary=glossary,
            )
        except Exception as exc:
            logger.error("TranslationStage unexpected error: %s", exc)
            ctx.errors.append(f"translation: {exc}")
            return ctx

        if result.ok:
            ctx.translation = result.text
            ctx.translation_engine = result.engine
            logger.debug(
                "Translation OK: %s -> %s via %s",
                result.source_lang, result.target_lang, result.engine,
            )
        else:
            logger.debug("Translation failed/skipped: status=%s", result.status)
            ctx.errors.append(f"translation: {result.status}")

        return ctx
