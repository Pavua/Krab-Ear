"""Совместимость форматов GigaAM между версиями v1, v2 и v3.

Модуль существует как единая точка нормализации для in-process адаптера и
изолированного subprocess-воркера. GigaAM v1/v2 возвращает longform-сегменты
как словари, а v3 — как dataclass-объекты; без этого слоя оба продуктовых пути
могут молча вернуть пустой текст при успешном распознавании.
"""

from __future__ import annotations

from typing import Any


SUPPORTED_GIGAAM_ASR_MODES: tuple[str, ...] = (
    "v3_e2e_rnnt",
    "v3_e2e_ctc",
    "v3_rnnt",
    "v3_ctc",
    "v2_rnnt",
    "v2_ctc",
    "v1_rnnt",
    "v1_ctc",
    "rnnt",
    "ctc",
)

# Upstream GigaAM проверяет `audio.numel() > 25 * 16000` и бросает ошибку.
# Единая константа не даёт router scoring и реальной транскрибации разойтись.
GIGAAM_SHORTFORM_MAX_SEC = 25.0


def engine_name_from_mode(mode: str | None) -> str:
    """Возвращает стабильное имя движка без версии и e2e-префикса."""
    mode_base = mode or "rnnt"
    for prefix in ("v3_e2e_", "v3_", "v2_", "v1_"):
        if mode_base.startswith(prefix):
            mode_base = mode_base[len(prefix):]
            break
    return f"gigaam-{mode_base}"


def extract_longform_text(result: Any) -> tuple[str, int]:
    """Извлекает текст и число сегментов из longform-ответа GigaAM.

    Поддерживаются legacy ``list[dict]`` с полем ``transcription`` и v3
    ``LongformTranscriptionResult`` с ``segments: list[Segment]`` и полем
    ``Segment.text``. Fallback на ``result.text`` сохраняет совместимость с
    будущим контейнером, если библиотека снова поменяет форму сегментов.
    """
    raw_segments = getattr(result, "segments", result)
    if raw_segments is None or isinstance(raw_segments, (str, bytes, dict)):
        segments = [raw_segments] if isinstance(raw_segments, dict) else []
    else:
        try:
            segments = list(raw_segments)
        except TypeError:
            segments = []

    texts: list[str] = []
    for segment in segments:
        if isinstance(segment, dict):
            value = segment.get("transcription") or segment.get("text")
        else:
            value = getattr(segment, "text", None)
            if value is None:
                value = getattr(segment, "transcription", None)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())

    if texts:
        return "\n\n".join(texts), len(segments)

    fallback = getattr(result, "text", "")
    return (fallback.strip() if isinstance(fallback, str) else ""), len(segments)
