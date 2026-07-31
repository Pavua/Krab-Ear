"""Диаризованный конвейер длинных записей (W-C волны gigaam-mlx-diar).

Схема «diarize → slice → transcribe»: сначала pyannote размечает спикеров,
затем аудио режется по спикер-сегментам и каждый кусок распознаётся обычным
каскадом движка. При перекрывающейся речи это даёт каждому спикеру СВОЙ текст
— один общий проход ASR смешивает реплики (проверено на живых записях).

Порт идеи из poc_diarization/full_transcription.py (там — обратный порядок
transcribe→align; здесь надёжный прямой). Гейт конвейера — в engine.transcribe():
только файловые входы, только при DIARIZED_TRANSCRIPTION_ENABLED.

⚠️ Время удержания _diarization_run_lock = полный прогон диаризации файла
(минуты на часовой записи). Осознанно принято для v1 батч-режима; конфликт
только с live-диаризацией активной встречи. TODO (отдельная волна): почанковый
прогон диаризации.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("KrabEar.DiarizedTranscription")

# Синхронизировано с engine._GIGAAM_MAX_CHUNK_SEC: жёсткий предел GigaAM
# ~25 c на массив, режем с запасом. Для whisper-кандидатов ограничение тоже
# полезно (короткие сегменты = меньше галлюцинаций на паузах).
_MAX_SEGMENT_SEC = 20.0

# Сегменты одного спикера, разделённые паузой короче этой, склеиваются —
# меньше вызовов каскада и связнее текст.
_JOIN_GAP_SEC = 1.0

# Сегменты короче этого не транскрибируются отдельно (обычно это «угу»/шум),
# но остаются в diarization-метаданных результата.
_MIN_SEGMENT_SEC = 0.4


def merge_speaker_turns(
    turns: list[dict[str, Any]],
    max_sec: float = _MAX_SEGMENT_SEC,
    join_gap_sec: float = _JOIN_GAP_SEC,
) -> list[dict[str, Any]]:
    """Склеивает соседние сегменты ОДНОГО спикера до max_sec.

    Вход/выход: [{"start": float, "end": float, "speaker": str}, ...]
    (контракт engine._run_diarization). Сегменты разных спикеров никогда
    не смешиваются; пауза длиннее join_gap_sec разрывает склейку.
    """
    merged: list[dict[str, Any]] = []
    for turn in sorted(turns, key=lambda t: (float(t["start"]), float(t["end"]))):
        start, end, speaker = float(turn["start"]), float(turn["end"]), str(turn["speaker"])
        if merged:
            prev = merged[-1]
            same_speaker = prev["speaker"] == speaker
            gap = start - prev["end"]
            fits = (end - prev["start"]) <= max_sec
            if same_speaker and gap <= join_gap_sec and fits:
                prev["end"] = max(prev["end"], end)
                continue
        merged.append({"start": start, "end": end, "speaker": speaker})
    return merged


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def format_diarized_transcript(pieces: list[dict[str, Any]]) -> str:
    """Собирает `[mm:ss] SPEAKER_N: текст` построчно (пустые куски опускаются)."""
    lines = []
    for piece in pieces:
        text = (piece.get("text") or "").strip()
        if text:
            lines.append(
                f"[{format_timestamp(piece['start'])}] {piece['speaker']}: {text}"
            )
    return "\n".join(lines)


def run_diarized_transcription(
    engine: Any,
    audio_path: str | Path,
    language: str | None = None,
) -> dict[str, Any]:
    """Полный конвейер для файла. Возвращает результат в контракте
    engine.transcribe() (полный набор ключей раннего return: результат НЕ
    проходит rewrite/cleanup/paste — см. гейт в engine).
    """
    audio_path = str(audio_path)
    turns = engine._run_diarization(audio_path)
    speakers = sorted({t["speaker"] for t in turns})
    merged = merge_speaker_turns(turns)
    logger.info(
        "Diarized pipeline: %s — %d сегментов → %d после склейки, спикеров: %d",
        Path(audio_path).name, len(turns), len(merged), len(speakers),
    )

    import soundfile as sf

    audio, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = engine._resample_audio_to_mono_16k(audio, sample_rate)

    pieces: list[dict[str, Any]] = []
    engines_used: set[str] = set()
    confidences: list[float] = []
    for seg in merged:
        if seg["end"] - seg["start"] < _MIN_SEGMENT_SEC:
            continue
        lo = max(0, int(seg["start"] * 16000))
        hi = min(len(audio), int(seg["end"] * 16000))
        if hi <= lo:
            continue
        seg_result = engine._transcribe_with_fallback(
            np.ascontiguousarray(audio[lo:hi]),
            "",  # без initial_prompt: контекст чужих реплик здесь вреден
            language=language,
            audio_sample_rate=16000,
        )
        text = seg_result.get("text", "") if isinstance(seg_result, dict) else str(seg_result)
        if isinstance(seg_result, dict):
            engines_used.add(str(seg_result.get("engine", "")))
            if seg_result.get("confidence") is not None:
                try:
                    confidences.append(float(seg_result["confidence"]))
                except (TypeError, ValueError):
                    pass
        pieces.append({
            "start": seg["start"],
            "end": seg["end"],
            "speaker": seg["speaker"],
            "text": (text or "").strip(),
        })

    transcript = format_diarized_transcript(pieces)
    engines_used.discard("")
    confidence = float(np.mean(confidences)) if confidences else 0.0
    # Контракт раннего return transcribe(): консюмеры (history, экспорт)
    # ожидают полный набор ключей; llm_applied=False — rewrite не выполнялся.
    return {
        "text": transcript,
        "language": language or "ru",
        "confidence": confidence,
        "engine": "diarized+" + "/".join(sorted(engines_used)) if engines_used else "diarized",
        "model": "diarized_pipeline",
        "llm_applied": False,
        "segments": pieces,
        "diarization": turns,
        "emotion": None,
    }
