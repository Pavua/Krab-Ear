"""PipelineContext — единственный объект, передаваемый между стадиями."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class StageMetric:
    stage: str
    duration_ms: int
    skipped: bool = False
    error: Optional[str] = None


@dataclass
class PipelineContext:
    # --- Вход ---
    # numpy array (float32 16kHz mono) или Path/str к файлу
    audio_input: Any                          # np.ndarray | str | Path

    # --- Runtime параметры (инжектируются перед запуском) ---
    cleanup_profile: str = "soft"             # "soft" | "strict"
    is_preview: bool = False
    domain: str = "casual"
    lang_hint: Optional[str] = None           # ISO 639-1 или None
    extra_vocabulary: list = field(default_factory=list)
    translation_mode: str = "off"

    # --- Промежуточные данные (заполняются стадиями) ---
    # AudioNormalizationStage → STTStage
    normalized_audio: Any = None              # str путь к нормализованному файлу или ndarray

    # STTStage output
    raw_text: str = ""
    segments: list = field(default_factory=list)
    language_detected: Optional[str] = None
    model_used: str = ""
    confidence: float = 0.0

    # DiarizationStage output
    diarization: dict = field(default_factory=dict)
    speaker_segments: list = field(default_factory=list)
    num_speakers: int = 0

    # TextCleanupStage output
    cleaned_text: str = ""

    # LLMRewriteStage output
    rewritten_text: str = ""
    llm_applied: bool = False
    llm_fallback_reason: Optional[str] = None
    llm_latency_ms: Optional[int] = None

    # TranslationStage output
    translation: Optional[str] = None
    translation_engine: Optional[str] = None

    # --- Финальный текст (выставляется последней стадией или executor'ом) ---
    final_text: str = ""

    # --- Мета ---
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    stage_metrics: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    # Временный путь (iCloud copy) — executor чистит его в finally
    _temp_path: Optional[str] = None
