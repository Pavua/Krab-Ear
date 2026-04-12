"""PipelineExecutor — выполняет стадии последовательно, собирает метрики."""

from __future__ import annotations

import logging
import os
import time
from typing import List

from core.pipeline.base import PipelineStage
from core.pipeline.context import PipelineContext, StageMetric

logger = logging.getLogger("KrabEar.Pipeline")


class PipelineExecutor:
    """Выполняет стадии последовательно, собирает метрики, чистит temp-файлы."""

    def __init__(self, stages: List[PipelineStage]) -> None:
        self._stages = stages

    def run(self, ctx: PipelineContext) -> PipelineContext:
        try:
            for stage in self._stages:
                if not stage.should_run(ctx):
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=0, skipped=True)
                    )
                    continue
                t0 = time.monotonic()
                try:
                    ctx = stage.process(ctx)
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=duration_ms)
                    )
                    logger.debug("Stage %s: %d ms", stage.name, duration_ms)
                except Exception as exc:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    ctx.errors.append(f"{stage.name}_exception: {exc}")
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=duration_ms, error=str(exc))
                    )
                    logger.exception("Unhandled exception in stage %s", stage.name)
                    # Продолжаем: стадия не смогла — следующие могут работать
        finally:
            self._cleanup(ctx)

        # Финальный текст: LLM rewrite > cleaned > raw
        ctx.final_text = ctx.rewritten_text or ctx.cleaned_text or ctx.raw_text
        return ctx

    def _cleanup(self, ctx: PipelineContext) -> None:
        if ctx._temp_path:
            try:
                os.unlink(ctx._temp_path)
            except OSError:
                pass
            ctx._temp_path = None

    def to_legacy_dict(self, ctx: PipelineContext) -> dict:
        """Конвертирует PipelineContext в dict-формат AudioEngine.transcribe()."""
        return {
            "text": ctx.final_text,
            "raw_text": ctx.raw_text,
            "cleaned_text": ctx.cleaned_text,
            "llm_applied": ctx.llm_applied,
            "llm_latency_ms": ctx.llm_latency_ms,
            "llm_fallback_reason": ctx.llm_fallback_reason,
            "confidence": round(ctx.confidence, 3),
            "duration_ms": sum(m.duration_ms for m in ctx.stage_metrics),
            "engine": "pipeline_v2",
            "model": ctx.model_used,
            "language": ctx.language_detected,
            "segments": ctx.segments if not ctx.is_preview else [],
            "diarization": ctx.diarization,
        }
