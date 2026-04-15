"""PipelineExecutor — выполняет стадии последовательно, собирает метрики."""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from core.pipeline.base import PipelineStage
from core.pipeline.context import PipelineContext, StageMetric
from core.pipeline.stage_cache import StageCache

logger = logging.getLogger("KrabEar.Pipeline")


class PipelineExecutor:
    """Выполняет стадии последовательно, собирает метрики, чистит temp-файлы.

    Поддерживает опциональный StageCache: если передан — проверяет кэш перед
    запуском стадии, сохраняет результат после успешного выполнения.

    Кэшируемость стадии определяется наличием атрибута `cacheable = True`.
    Кэш-ключ строится из ctx.audio_input через StageCache.compute_hash().
    """

    def __init__(
        self,
        stages: List[PipelineStage],
        cache: Optional[StageCache] = None,
    ) -> None:
        self._stages = stages
        self._cache = cache

    def run(self, ctx: PipelineContext) -> PipelineContext:
        # Вычислить хэш аудиовхода один раз для всего run
        audio_hash: Optional[str] = None
        if self._cache is not None and ctx.audio_input is not None:
            try:
                audio_hash = StageCache.compute_hash(ctx.audio_input)
            except Exception as exc:  # pragma: no cover
                logger.warning("Не удалось вычислить хэш аудио для кэша: %s", exc)

        try:
            for stage in self._stages:
                if not stage.should_run(ctx):
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=0, skipped=True)
                    )
                    continue

                # --- Проверка кэша ---
                cached_result = None
                use_cache = (
                    self._cache is not None
                    and audio_hash is not None
                    and getattr(stage, "cacheable", False)
                )
                if use_cache:
                    cached_result = self._cache.get(stage.name, audio_hash)  # type: ignore[union-attr]
                    if cached_result is not None:
                        ctx = _apply_cached_result(stage.name, cached_result, ctx)
                        ctx.stage_metrics.append(
                            StageMetric(stage=stage.name, duration_ms=0, skipped=False,
                                        error=None)
                        )
                        logger.debug("Stage %s: cache hit, skipped execution", stage.name)
                        continue

                # --- Выполнение стадии ---
                t0 = time.monotonic()
                try:
                    ctx = stage.process(ctx)
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=duration_ms)
                    )
                    logger.debug("Stage %s: %d ms", stage.name, duration_ms)

                    # --- Сохранить в кэш (только при успехе, без ошибок стадии) ---
                    if use_cache and not _stage_had_error(stage.name, ctx):
                        snapshot = _extract_stage_result(stage.name, ctx)
                        if snapshot:
                            self._cache.put(stage.name, audio_hash, snapshot)  # type: ignore[union-attr]

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


# ---------------------------------------------------------------------------
# Вспомогательные функции для кэширования стадий
# ---------------------------------------------------------------------------

# Маппинг: имя стадии → поля PipelineContext, которые она заполняет
_STAGE_FIELDS: dict = {
    "stt": ["raw_text", "language_detected", "model_used", "confidence", "segments"],
    "text_cleanup": ["cleaned_text"],
    "llm_rewrite": ["rewritten_text", "llm_applied", "llm_fallback_reason", "llm_latency_ms"],
    "translation": ["translation", "translation_engine"],
    "diarization": ["diarization", "speaker_segments", "num_speakers"],
    "audio_normalization": ["normalized_audio"],
}


def _extract_stage_result(stage_name: str, ctx: PipelineContext) -> dict:
    """Извлечь snapshot полей, заполненных стадией, для сохранения в кэш."""
    fields = _STAGE_FIELDS.get(stage_name, [])
    if not fields:
        return {}
    snapshot = {}
    for field_name in fields:
        val = getattr(ctx, field_name, None)
        # Не кэшируем numpy arrays (normalized_audio) — слишком тяжело
        try:
            import numpy as np  # type: ignore
            if isinstance(val, np.ndarray):
                continue
        except ImportError:
            pass
        snapshot[field_name] = val
    return snapshot


def _apply_cached_result(
    stage_name: str, cached: dict, ctx: PipelineContext
) -> PipelineContext:
    """Применить кэшированный результат к ctx."""
    for field_name, value in cached.items():
        if hasattr(ctx, field_name):
            setattr(ctx, field_name, value)
    return ctx


def _stage_had_error(stage_name: str, ctx: PipelineContext) -> bool:
    """Проверить, добавила ли стадия ошибку в ctx.errors в этом запуске."""
    prefix = f"{stage_name}:"
    return any(e.startswith(prefix) for e in ctx.errors)
