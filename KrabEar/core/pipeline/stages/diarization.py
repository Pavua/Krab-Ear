"""DiarizationStage — диаризация спикеров в pipeline."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..context import PipelineContext

logger = logging.getLogger("KrabEar.Pipeline.Diarization")

try:
    from core.config import settings as _app_settings
except Exception:  # pragma: no cover
    _app_settings = None  # type: ignore[assignment]


class DiarizationStage:
    """Запускает диаризацию спикеров и заполняет ctx.diarization / ctx.speaker_segments.

    Диаризация опциональна: любая ошибка логируется в ctx.errors, но не
    прерывает pipeline (soft-fail).

    Args:
        diarization_fn: callable(audio_path: str) → list[dict]
            Функция, вызывающая pyannote pipeline и возвращающая список сегментов.
            Если None — стадия пропускается с предупреждением.
    """

    def __init__(self, diarization_fn: Optional[Callable[[str], list]] = None) -> None:
        self._diarization_fn = diarization_fn

    @property
    def name(self) -> str:
        return "diarization"

    def should_run(self, ctx: PipelineContext) -> bool:
        """Запускать только если:
        - диаризация включена в настройках (проверяем через settings или ctx),
        - не preview-режим,
        - есть аудио для обработки (normalized_audio или audio_input — путь к файлу).
        """
        if ctx.is_preview:
            return False

        # Проверяем настройку DIARIZATION_ENABLED через settings (если доступны)
        if _app_settings is not None and not _app_settings.DIARIZATION_ENABLED:
            return False

        if self._diarization_fn is None:
            return False

        # Аудио должно быть файловым путём (ndarray не поддерживается pyannote напрямую)
        audio = ctx.normalized_audio if ctx.normalized_audio is not None else ctx.audio_input
        if audio is None:
            return False

        from pathlib import Path
        if isinstance(audio, (str, Path)):
            return True

        return False

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Запускает диаризацию и заполняет ctx.diarization, ctx.speaker_segments, ctx.num_speakers."""
        from pathlib import Path

        audio = ctx.normalized_audio if ctx.normalized_audio is not None else ctx.audio_input
        audio_path = str(Path(audio).expanduser().resolve()) if isinstance(audio, (str, Path)) else None

        if audio_path is None:
            ctx.errors.append("diarization: не удалось определить путь к аудиофайлу")
            return ctx

        try:
            speaker_segments: list[dict[str, Any]] = self._diarization_fn(audio_path)  # type: ignore[misc]

            num_speakers = len({seg.get("speaker") for seg in speaker_segments if seg.get("speaker")})

            ctx.speaker_segments = speaker_segments
            ctx.num_speakers = num_speakers
            ctx.diarization = {
                "enabled": True,
                "speaker_segments": speaker_segments,
                "num_speakers": num_speakers,
            }

            logger.debug(
                "Diarization завершён: %d сегментов, %d спикеров",
                len(speaker_segments),
                num_speakers,
            )

        except Exception as exc:
            logger.warning("Diarization ошибка: %s", exc)
            ctx.errors.append(f"diarization: {exc}")
            ctx.diarization = {
                "enabled": False,
                "speaker_segments": [],
                "num_speakers": 0,
                "error": str(exc),
            }

        return ctx
