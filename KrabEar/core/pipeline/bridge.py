"""Pipeline bridge — точка входа для BackendService (за feature flag PIPELINE_V2).

transcribe_v2() — drop-in замена AudioEngine.transcribe() с pipeline v2.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from core.pipeline.context import PipelineContext
from core.pipeline.factory import create_default_pipeline
from core.pipeline.stage_cache import StageCache

logger = logging.getLogger("KrabEar.Pipeline.Bridge")


def transcribe_v2(
    engine: Any,
    audio_input: Any,
    settings: Any = None,
    llm_rewriter: Optional[Any] = None,
    translator: Optional[Any] = None,
    diarization_fn: Optional[Callable[[str], list]] = None,
    cleanup_profile: str = "soft",
    is_preview: bool = False,
    domain: str = "casual",
    extra_vocabulary: Optional[list] = None,
    lang_hint: Optional[str] = None,
    translation_mode: str = "off",
    **kwargs: Any,
) -> dict:
    """Запускает pipeline v2 и возвращает dict в legacy-формате AudioEngine.transcribe().

    Это новая точка входа, которую BackendService может использовать за feature
    flag PIPELINE_V2. Сигнатура намеренно совместима с engine.transcribe().

    Args:
        engine: AudioEngine или callable(audio_data, **kwargs) → dict.
        audio_input: np.ndarray (live buffer) или str/Path к аудиофайлу.
        settings: опциональный объект настроек (не используется напрямую, для
                  совместимости с вызывающим кодом). Настройки читаются через
                  settings_get из core.config.settings.
        llm_rewriter: LLMRewriter или None.
        translator: Translator или None.
        diarization_fn: callable или None (автоматически берётся из engine).
        cleanup_profile: "soft" | "strict".
        is_preview: True для live-preview (короткие буферы).
        domain: тематика для STT-промпта.
        extra_vocabulary: список дополнительных слов для STT.
        lang_hint: ISO 639-1 или None/auto.
        translation_mode: "off" | "ru" | "es" | …
        **kwargs: дополнительные параметры (игнорируются для совместимости).

    Returns:
        dict в legacy-формате: ключи text, raw_text, cleaned_text, confidence,
        duration_ms, engine, model, language, segments, diarization,
        llm_applied, llm_latency_ms, llm_fallback_reason.
    """
    ctx = PipelineContext(
        audio_input=audio_input,
        cleanup_profile=cleanup_profile,
        is_preview=is_preview,
        domain=domain,
        extra_vocabulary=extra_vocabulary or [],
        lang_hint=lang_hint,
        translation_mode=translation_mode,
    )

    # W1263 F3: instantiate StageCache and pass to PipelineExecutor via factory.
    # The cache enables LRU result reuse across repeated calls with the same audio.
    stage_cache = StageCache()

    pipeline = create_default_pipeline(
        engine=engine,
        llm_rewriter=llm_rewriter,
        translator=translator,
        diarization_fn=diarization_fn,
        stage_cache=stage_cache,
    )

    try:
        ctx = pipeline.run(ctx)
    except Exception as exc:
        # Защита от неожиданных ошибок executor'а (не должно происходить)
        logger.exception("transcribe_v2: неожиданная ошибка executor'а: %s", exc)
        return {
            "text": "",
            "raw_text": "",
            "cleaned_text": "",
            "llm_applied": False,
            "llm_latency_ms": None,
            "llm_fallback_reason": str(exc),
            "confidence": 0.0,
            "duration_ms": 0,
            "engine": "pipeline_v2_error",
            "model": "",
            "language": None,
            "segments": [],
            "diarization": {},
            "error": str(exc),
        }

    result = pipeline.to_legacy_dict(ctx)

    if ctx.errors:
        result["pipeline_errors"] = ctx.errors
        logger.warning("transcribe_v2: %d ошибок стадий: %s", len(ctx.errors), ctx.errors)

    return result
