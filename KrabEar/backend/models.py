"""Модели данных backend-сервиса Krab Ear.

Модуль используется сервисом IPC и хранилищем истории для единообразной
сериализации/десериализации объектов.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any
import uuid


@dataclass(slots=True)
class HistoryItem:
    """Одна запись транскрибации в истории."""

    id: str
    ts: str
    text: str
    paste_status: str = "failed"
    source_text: str = ""
    translated_text: str = ""
    translation_mode: str = "off"
    source_lang: str = ""
    target_lang: str = ""
    translation_status: str = "not_requested"
    translation_engine: str = ""
    chat_id: str = ""
    message_id: str = ""
    # D.10a: LLM rewrite tracking
    cleaned_text: str = ""
    llm_applied: bool = False
    llm_latency_ms: int = 0
    # Diarization data (speaker segments, annotated segments, turns)
    diarization: dict | None = None
    # Audio file duration in seconds (for imported files)
    audio_duration_sec: float | None = None
    # STT confidence score (0.0–1.0); None for items recorded before this field was added
    confidence: float | None = None
    # User-defined tags for filtering and categorisation
    tags: list = field(default_factory=list)
    # Favorite/bookmark flag
    favorite: bool = False
    # SenseVoice emotion label: "happy"/"neutral"/"angry"/"sad"/"fearful"/
    # "disgusted"/"surprised"/None. Заполняется только когда SenseVoice adapter
    # активен и SENSEVOICE_EMOTION_TO_HISTORY=True. Для Whisper-записей = None.
    emotion: str | None = None
    # WhisperX word-level timestamps (Phase 4.3).
    # Каждый элемент: {"word": str, "start": float, "end": float, "confidence": float}.
    # None для записей без WhisperX (обратная совместимость).
    word_timestamps: list | None = None
    # WhisperX diarization speaker turns (Phase 4.3).
    # Каждый элемент: {"speaker": str, "start": float, "end": float}.
    # None для записей без WhisperX diarization.
    speaker_turns: list | None = None
    # Audio file path (for re-transcription support)
    audio_path: str = ""
    # Protected items are excluded from bulk operations
    is_protected: bool = False
    # Voxtral reasoning output (Phase 4.4).
    # Заполняется когда VOXTRAL_ENABLED=True + VOXTRAL_REASONING_ENABLED=True.
    # Содержит summary или Q&A ответ от Mistral Voxtral LM-decoder.
    # None для всех остальных движков (обратная совместимость).
    reasoning: str | None = None
    # Action items extracted from meeting transcripts via LLM.
    # Каждый элемент: {"text": str, "assignee": str, "due": str, "priority": str}.
    # None = ещё не извлекались. [] = извлекали, ничего не найдено.
    action_items: list | None = None
    # Decisions made during a meeting/conversation. None = не извлекались.
    decisions: list | None = None
    # Open questions identified in the transcript. None = не извлекались.
    questions: list | None = None

    @classmethod
    def create(
        cls,
        text: str,
        paste_status: str = "failed",
        source_text: str = "",
        translated_text: str = "",
        translation_mode: str = "off",
        source_lang: str = "",
        target_lang: str = "",
        translation_status: str = "not_requested",
        translation_engine: str = "",
        chat_id: str = "",
        message_id: str = "",
        cleaned_text: str = "",
        llm_applied: bool = False,
        llm_latency_ms: int = 0,
        diarization: dict | None = None,
        audio_duration_sec: float | None = None,
        confidence: float | None = None,
        tags: list | None = None,
        favorite: bool = False,
        emotion: str | None = None,
        word_timestamps: list | None = None,
        speaker_turns: list | None = None,
        reasoning: str | None = None,
        audio_path: str = "",
        is_protected: bool = False,
        action_items: list | None = None,
        decisions: list | None = None,
        questions: list | None = None,
    ) -> "HistoryItem":
        """Создаёт новую запись с корректным идентификатором и временем."""
        return cls(
            id=str(uuid.uuid4()),
            ts=datetime.now().isoformat(timespec="seconds"),
            text=text,
            paste_status=paste_status,
            source_text=source_text.strip(),
            translated_text=translated_text.strip(),
            translation_mode=translation_mode.strip() or "off",
            source_lang=source_lang.strip(),
            target_lang=target_lang.strip(),
            translation_status=translation_status.strip() or "not_requested",
            translation_engine=translation_engine.strip(),
            chat_id=str(chat_id).strip(),
            message_id=str(message_id).strip(),
            cleaned_text=(cleaned_text or "").strip(),
            llm_applied=bool(llm_applied),
            llm_latency_ms=int(llm_latency_ms or 0),
            diarization=diarization if isinstance(diarization, dict) else None,
            audio_duration_sec=round(float(audio_duration_sec), 3) if audio_duration_sec is not None else None,
            confidence=round(float(confidence), 4) if confidence is not None else None,
            tags=list(tags) if tags else [],
            favorite=bool(favorite),
            emotion=(str(emotion).strip().lower() or None) if emotion else None,
            word_timestamps=list(word_timestamps) if word_timestamps else None,
            speaker_turns=list(speaker_turns) if speaker_turns else None,
            reasoning=(str(reasoning).strip() or None) if reasoning else None,
            audio_path=str(audio_path).strip(),
            is_protected=bool(is_protected),
            action_items=list(action_items) if action_items is not None else None,
            decisions=list(decisions) if decisions is not None else None,
            questions=list(questions) if questions is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Преобразует dataclass в сериализуемый словарь."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HistoryItem":
        """Восстанавливает запись из JSON-словаря с мягкой валидацией."""
        return cls(
            id=str(payload.get("id", "")).strip(),
            ts=str(payload.get("ts", "")).strip(),
            text=str(payload.get("text", "")).strip(),
            paste_status=str(payload.get("paste_status", "failed")).strip() or "failed",
            source_text=str(payload.get("source_text", "")).strip(),
            translated_text=str(payload.get("translated_text", "")).strip(),
            translation_mode=str(payload.get("translation_mode", "off")).strip() or "off",
            source_lang=str(payload.get("source_lang", "")).strip(),
            target_lang=str(payload.get("target_lang", "")).strip(),
            translation_status=str(payload.get("translation_status", "not_requested")).strip() or "not_requested",
            translation_engine=str(payload.get("translation_engine", "")).strip(),
            chat_id=str(payload.get("chat_id", "")).strip(),
            message_id=str(payload.get("message_id", "")).strip(),
            cleaned_text=str(payload.get("cleaned_text", "")).strip(),
            llm_applied=bool(payload.get("llm_applied", False)),
            llm_latency_ms=int(payload.get("llm_latency_ms", 0) or 0),
            diarization=payload.get("diarization") if isinstance(payload.get("diarization"), dict) else None,
            audio_duration_sec=float(payload["audio_duration_sec"]) if payload.get("audio_duration_sec") is not None else None,
            confidence=float(payload["confidence"]) if payload.get("confidence") is not None else None,
            tags=[str(t) for t in payload["tags"]] if isinstance(payload.get("tags"), list) else [],
            favorite=bool(payload.get("favorite", False)),
            emotion=(str(payload["emotion"]).strip().lower() or None) if payload.get("emotion") else None,
            word_timestamps=payload.get("word_timestamps") if isinstance(payload.get("word_timestamps"), list) else None,
            speaker_turns=payload.get("speaker_turns") if isinstance(payload.get("speaker_turns"), list) else None,
            reasoning=(str(payload["reasoning"]).strip() or None) if payload.get("reasoning") else None,
            audio_path=str(payload.get("audio_path", "")).strip(),
            is_protected=bool(payload.get("is_protected", False)),
            action_items=payload.get("action_items") if isinstance(payload.get("action_items"), list) else None,
            decisions=payload.get("decisions") if isinstance(payload.get("decisions"), list) else None,
            questions=payload.get("questions") if isinstance(payload.get("questions"), list) else None,
        )


# Re-export для backwards compat (tests импортируют DEFAULT_SETTINGS из backend.models)
from core.config import DEFAULT_SETTINGS  # noqa: E402,F401
