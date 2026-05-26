"""PipelineFactory — собирает PipelineExecutor из всех 6 стадий."""

from __future__ import annotations

from typing import Any, Callable, Optional

from core.pipeline.executor import PipelineExecutor
from core.pipeline.stage_cache import StageCache
from core.pipeline.stages.audio_normalization import AudioNormalizationStage
from core.pipeline.stages.stt import STTStage
from core.pipeline.stages.diarization import DiarizationStage
from core.pipeline.stages.text_cleanup import TextCleanupStage
from core.pipeline.stages.llm_rewrite import LLMRewriteStage
from core.pipeline.stages.translation import TranslationStage


def create_default_pipeline(
    engine: Any,
    llm_rewriter: Optional[Any] = None,
    translator: Optional[Any] = None,
    diarization_fn: Optional[Callable[[str], list]] = None,
    settings_get: Optional[Callable] = None,
    stage_cache: Optional[StageCache] = None,
) -> PipelineExecutor:
    """Собирает и возвращает PipelineExecutor со всеми 6 стадиями.

    Порядок стадий:
        1. AudioNormalizationStage
        2. STTStage
        3. DiarizationStage
        4. TextCleanupStage
        5. LLMRewriteStage
        6. TranslationStage

    Args:
        engine: AudioEngine или callable совместимый с engine.transcribe().
        llm_rewriter: LLMRewriter или None — если None, LLMRewriteStage пропускается.
        translator: Translator или None — если None, TranslationStage пропускается.
        diarization_fn: callable(audio_path) → list[dict] или None.
            Если None — DiarizationStage пропускается.
            По умолчанию берётся engine.run_diarization, если такой метод есть.
        settings_get: callable(key, default=None) для чтения настроек.
            По умолчанию читает из core.config.settings.
        stage_cache: StageCache или None.  W1263 F3 — если передан, executor
            использует кэш для кэшируемых стадий (cacheable = True).

    Returns:
        PipelineExecutor с 6 стадиями.
    """
    # Дефолтный settings_get — читает из глобальных settings
    if settings_get is None:
        try:
            from core.config import settings as _settings

            def settings_get(key: str, default: Any = None) -> Any:  # type: ignore[misc]
                return getattr(_settings, key.upper(), default)
        except Exception:
            def settings_get(k, d=None): return d  # noqa: E731

    # Автоматически подбираем diarization_fn из engine если не задана явно
    if diarization_fn is None and hasattr(engine, "run_diarization"):
        diarization_fn = engine.run_diarization

    stages = [
        AudioNormalizationStage(),
        STTStage(engine),
        DiarizationStage(diarization_fn),
        TextCleanupStage(),
        LLMRewriteStage(llm_rewriter, settings_get=settings_get),
        TranslationStage(translator, settings_get=settings_get),
    ]

    return PipelineExecutor(stages, cache=stage_cache)
