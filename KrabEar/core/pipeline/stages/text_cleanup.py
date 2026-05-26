"""TextCleanupStage — очистка транскрипции от артефактов Whisper."""

from __future__ import annotations

import logging

from core.utils import TextUtils
from core.pipeline.context import PipelineContext

logger = logging.getLogger("KrabEar.Pipeline.TextCleanup")


class TextCleanupStage:
    """Стадия очистки текста.

    Использует TextUtils.cleanup_transcript с профилем из ctx.cleanup_profile.
    Результат записывается в ctx.cleaned_text.
    """

    name = "text_cleanup"

    def should_run(self, ctx: PipelineContext) -> bool:
        """Запускается только если есть сырой текст."""
        return bool(ctx.raw_text and ctx.raw_text.strip())

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Очищает ctx.raw_text и сохраняет результат в ctx.cleaned_text."""
        try:
            profile = ctx.cleanup_profile or "soft"
            ctx.cleaned_text = TextUtils.cleanup_transcript(ctx.raw_text, profile=profile)
            logger.debug(
                "text_cleanup done: profile=%s raw_len=%d cleaned_len=%d",
                profile,
                len(ctx.raw_text),
                len(ctx.cleaned_text),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("text_cleanup failed: %s", exc)
            ctx.errors.append(f"text_cleanup: {exc}")
            ctx.cleaned_text = ctx.raw_text  # fallback: передаём нечищеный текст
        return ctx
