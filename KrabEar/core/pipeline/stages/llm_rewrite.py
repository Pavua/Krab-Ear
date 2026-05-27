"""LLMRewriteStage — пост-процессинг транскрипта через LLMRewriter."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from core.pipeline.context import PipelineContext

logger = logging.getLogger("KrabEar.Pipeline.LLMRewrite")


class LLMRewriteStage:
    """Стадия LLM-переписывания текста.

    should_run() возвращает False если:
    - rewriter не задан (None),
    - настройка llm_rewrite_enabled = False,
    - circuit breaker открыт (rewriter не позволяет запрос).

    process() НИКОГДА не raises — LLMRewriter.rewrite() уже гарантирует это.
    """

    name = "llm_rewrite"

    def __init__(
        self,
        rewriter,  # LLMRewriter | None — не импортируем для избегания circular deps
        settings_get: Optional[Callable] = None,
    ) -> None:
        self._rewriter = rewriter
        # settings_get(key, default) — callable для чтения настроек
        self._settings_get = settings_get or (lambda k, d=None: d)

    def should_run(self, ctx: PipelineContext) -> bool:
        if self._rewriter is None:
            return False
        if not bool(self._settings_get("llm_rewrite_enabled", False)):
            return False
        # Проверяем circuit breaker через allow_request() без потребления пробы
        circuit = getattr(self._rewriter, "_circuit", None)
        if circuit is not None and circuit.state == "open":
            return False
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        text_in = ctx.cleaned_text or ctx.raw_text
        try:
            result = self._rewriter.rewrite(text_in)
        except Exception as exc:
            # Защита на случай непредвиденной ошибки (контракт rewrite — never raises,
            # но мы всё равно ловим для robustness pipeline'а)
            logger.error("LLMRewriteStage unexpected error: %s", exc)
            ctx.errors.append(f"llm_rewrite: {exc}")
            ctx.rewritten_text = text_in
            ctx.llm_applied = False
            return ctx

        ctx.llm_applied = result.ok
        ctx.llm_latency_ms = result.latency_ms

        if result.ok:
            ctx.rewritten_text = result.text or text_in
            ctx.final_text = ctx.rewritten_text
            logger.debug(
                "LLM rewrite OK, latency=%dms", result.latency_ms or 0
            )
        else:
            ctx.llm_fallback_reason = result.fallback_reason
            ctx.rewritten_text = text_in
            logger.debug(
                "LLM rewrite skipped: %s", result.fallback_reason
            )

        return ctx
