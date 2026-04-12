"""STTStage — стадия распознавания речи в pipeline."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..context import PipelineContext

logger = logging.getLogger("KrabEar.Pipeline.STT")


class STTStage:
    """Выполняет распознавание речи (STT) через AudioEngine.transcribe.

    Принимает либо экземпляр AudioEngine, либо любой callable с сигнатурой
    совместимой с engine.transcribe (для тестов и подмены).

    Читает ctx.normalized_audio (если есть) или ctx.audio_input и заполняет:
    - ctx.raw_text
    - ctx.language_detected
    - ctx.model_used
    - ctx.confidence
    - ctx.segments

    При любой ошибке добавляет сообщение в ctx.errors и не поднимает исключение.

    Атрибут cacheable=True указывает PipelineExecutor, что результаты этой
    стадии можно кэшировать через StageCache.
    """

    cacheable: bool = True

    def __init__(self, engine: Any) -> None:
        """
        Args:
            engine: объект AudioEngine или callable(audio_data, **kwargs) → dict.
                    Если передан объект с методом .transcribe — используется он.
                    Если передан callable — вызывается напрямую.
        """
        if hasattr(engine, "transcribe"):
            self._transcribe_fn: Callable[..., dict] = engine.transcribe
        else:
            # Принимаем callable (lambda, функция, partial) напрямую
            self._transcribe_fn = engine

    @property
    def name(self) -> str:
        return "stt"

    def should_run(self, ctx: PipelineContext) -> bool:
        """Запускать стадию только если есть аудиоданные."""
        return ctx.normalized_audio is not None or ctx.audio_input is not None

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Вызывает транскрипцию и заполняет поля ctx."""
        audio = ctx.normalized_audio if ctx.normalized_audio is not None else ctx.audio_input

        try:
            result: dict = self._transcribe_fn(
                audio,
                cleanup_profile=ctx.cleanup_profile,
                is_preview=ctx.is_preview,
                domain=ctx.domain,
                extra_vocabulary=ctx.extra_vocabulary or None,
                lang_hint=ctx.lang_hint,
            )
        except Exception as exc:
            logger.error("STTStage: ошибка транскрипции: %s", exc)
            ctx.errors.append(f"stt: {exc}")
            return ctx

        if result.get("status") == "error" or result.get("error"):
            err_msg = result.get("error") or result.get("status", "unknown error")
            logger.error("STTStage: engine вернул ошибку: %s", err_msg)
            ctx.errors.append(f"stt: {err_msg}")
            return ctx

        ctx.raw_text = result.get("raw_text") or result.get("text", "")
        ctx.language_detected = result.get("language")
        ctx.model_used = result.get("model", "")
        ctx.confidence = float(result.get("confidence", 0.0))
        ctx.segments = result.get("segments", [])

        logger.debug(
            "STTStage завершён: %d символов, confidence=%.3f, model=%s",
            len(ctx.raw_text),
            ctx.confidence,
            ctx.model_used,
        )
        return ctx
