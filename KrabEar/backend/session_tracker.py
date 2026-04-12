"""Трекер сессий записи для Krab Ear.

Отслеживает метаданные каждой сессии записи: устройство, модель STT,
задержку, уверенность, переводы, LLM rewrite и итог paste.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("KrabEar.Backend.SessionTracker")

# Поля метаданных сессии с типами и значениями по умолчанию
_SESSION_FIELDS = (
    "session_id",
    "started_at",
    "ended_at",
    "duration_sec",
    "audio_device",
    "quality_preset",
    "stt_model",
    "stt_latency_ms",
    "confidence",
    "word_count",
    "had_diarization",
    "had_llm_rewrite",
    "had_translation",
    "paste_status",
)


class SessionTracker:
    """Трекер сессий записи со скользящим буфером и опциональным NDJSON-логом."""

    def __init__(
        self,
        data_dir: Optional[str | Path] = None,
        max_sessions: int = 1000,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: deque[dict[str, Any]] = deque(maxlen=max_sessions)
        self._active_session: Optional[dict[str, Any]] = None
        self._data_dir = Path(data_dir) if data_dir else None
        self._sessions_file = (self._data_dir / "sessions.ndjson") if self._data_dir else None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def start_session(
        self,
        audio_device: str = "",
        quality_preset: str = "balanced",
        stt_model: str = "",
    ) -> str:
        """Открывает новую сессию и возвращает session_id."""
        session_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._active_session = {
                "session_id": session_id,
                "started_at": started_at,
                "ended_at": None,
                "duration_sec": 0.0,
                "audio_device": audio_device,
                "quality_preset": quality_preset,
                "stt_model": stt_model,
                "stt_latency_ms": 0,
                "confidence": 0.0,
                "word_count": 0,
                "had_diarization": False,
                "had_llm_rewrite": False,
                "had_translation": False,
                "paste_status": "pending",
            }
        logger.debug("Сессия начата: %s", session_id)
        return session_id

    def end_session(self, result: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Завершает активную сессию, обогащает метаданными из result, сохраняет.

        result может содержать любой набор ключей из схемы сессии.
        Возвращает финальную запись сессии или None если нет активной сессии.
        """
        with self._lock:
            if self._active_session is None:
                logger.warning("end_session вызван без активной сессии")
                return None
            session = dict(self._active_session)
            self._active_session = None

        ended_at = datetime.now(timezone.utc).isoformat()
        session["ended_at"] = ended_at

        # Обогащаем поля из result
        if "duration_sec" in result:
            session["duration_sec"] = float(result["duration_sec"])
        if "stt_latency_ms" in result:
            session["stt_latency_ms"] = int(result["stt_latency_ms"])
        if "confidence" in result:
            session["confidence"] = float(result["confidence"])
        if "word_count" in result:
            session["word_count"] = int(result["word_count"])
        elif "text" in result and result["text"]:
            session["word_count"] = len(str(result["text"]).split())
        if "had_diarization" in result:
            session["had_diarization"] = bool(result["had_diarization"])
        if "had_llm_rewrite" in result:
            session["had_llm_rewrite"] = bool(result["had_llm_rewrite"])
        if "had_translation" in result:
            session["had_translation"] = bool(result["had_translation"])
        elif "translation_status" in result:
            session["had_translation"] = result["translation_status"] not in ("off", "not_requested", None, "")
        if "paste_status" in result:
            session["paste_status"] = str(result["paste_status"])
        if "stt_model" in result and result["stt_model"]:
            session["stt_model"] = str(result["stt_model"])
        if "audio_device" in result and result["audio_device"]:
            session["audio_device"] = str(result["audio_device"])
        if "quality_preset" in result and result["quality_preset"]:
            session["quality_preset"] = str(result["quality_preset"])

        with self._lock:
            self._sessions.append(session)

        self._persist(session)
        logger.debug("Сессия завершена: %s, duration=%.2fs", session["session_id"], session["duration_sec"])
        return session

    def get_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Возвращает последние `limit` сессий (от новых к старым)."""
        with self._lock:
            all_sessions = list(self._sessions)
        return list(reversed(all_sessions))[:limit]

    def get_session_stats(self) -> dict[str, Any]:
        """Агрегированная статистика по всем накопленным сессиям."""
        with self._lock:
            sessions = list(self._sessions)

        total = len(sessions)
        if total == 0:
            return {
                "total_sessions": 0,
                "avg_duration_sec": 0.0,
                "avg_confidence": 0.0,
                "avg_word_count": 0.0,
                "avg_stt_latency_ms": 0.0,
                "diarization_rate": 0.0,
                "llm_rewrite_rate": 0.0,
                "translation_rate": 0.0,
                "paste_ok_rate": 0.0,
            }

        durations = [s["duration_sec"] for s in sessions if s.get("duration_sec", 0) > 0]
        confidences = [s["confidence"] for s in sessions if s.get("confidence", 0) > 0]
        latencies = [s["stt_latency_ms"] for s in sessions if s.get("stt_latency_ms", 0) > 0]
        word_counts = [s["word_count"] for s in sessions if s.get("word_count", 0) > 0]
        had_diar = sum(1 for s in sessions if s.get("had_diarization"))
        had_llm = sum(1 for s in sessions if s.get("had_llm_rewrite"))
        had_trans = sum(1 for s in sessions if s.get("had_translation"))
        paste_ok = sum(1 for s in sessions if s.get("paste_status") == "ok")

        return {
            "total_sessions": total,
            "avg_duration_sec": round(sum(durations) / len(durations), 3) if durations else 0.0,
            "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
            "avg_word_count": round(sum(word_counts) / len(word_counts), 1) if word_counts else 0.0,
            "avg_stt_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            "diarization_rate": round(had_diar / total, 4),
            "llm_rewrite_rate": round(had_llm / total, 4),
            "translation_rate": round(had_trans / total, 4),
            "paste_ok_rate": round(paste_ok / total, 4),
        }

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _persist(self, session: dict[str, Any]) -> None:
        """Дописывает одну запись в sessions.ndjson (опционально)."""
        if self._sessions_file is None:
            return
        try:
            self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._sessions_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(session, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("Не удалось сохранить сессию в %s", self._sessions_file)
