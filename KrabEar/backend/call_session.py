"""Модель данных и машина состояний для звонковой сессии (Phase 3 Call Automation).

Описывает жизненный цикл автоматического исходящего звонка:
  idle → dialing → connected → talking → ending → completed / failed
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CallStatus(str, enum.Enum):
    """Статус звонковой сессии."""

    IDLE = "idle"
    DIALING = "dialing"
    CONNECTED = "connected"
    TALKING = "talking"
    ENDING = "ending"
    COMPLETED = "completed"
    FAILED = "failed"


class Speaker(str, enum.Enum):
    """Источник реплики в транскрипте звонка."""

    USER = "user"
    BOT = "bot"
    OPERATOR = "operator"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.IDLE: frozenset({CallStatus.DIALING}),
    CallStatus.DIALING: frozenset({CallStatus.CONNECTED, CallStatus.FAILED}),
    CallStatus.CONNECTED: frozenset({CallStatus.TALKING, CallStatus.ENDING, CallStatus.FAILED}),
    CallStatus.TALKING: frozenset({CallStatus.ENDING, CallStatus.FAILED}),
    CallStatus.ENDING: frozenset({CallStatus.COMPLETED, CallStatus.FAILED}),
    CallStatus.COMPLETED: frozenset(),
    CallStatus.FAILED: frozenset(),
}


class CallSessionStateMachine:
    """Валидирует переходы состояний звонковой сессии.

    Пример использования::

        sm = CallSessionStateMachine(CallStatus.IDLE)
        sm.transition(CallStatus.DIALING)  # OK
        sm.transition(CallStatus.COMPLETED)  # ValueError — нельзя сразу в COMPLETED
    """

    def __init__(self, initial: CallStatus = CallStatus.IDLE) -> None:
        self._status = initial

    @property
    def status(self) -> CallStatus:
        return self._status

    def transition(self, new_status: CallStatus) -> CallStatus:
        """Выполняет переход в новое состояние.

        Raises:
            ValueError: если переход недопустим.
        """
        allowed = _VALID_TRANSITIONS.get(self._status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"Недопустимый переход: {self._status.value!r} → {new_status.value!r}. "
                f"Допустимые: {[s.value for s in sorted(allowed, key=lambda s: s.value)]}"
            )
        self._status = new_status
        return self._status

    def can_transition(self, new_status: CallStatus) -> bool:
        """Возвращает True, если переход допустим без его выполнения."""
        return new_status in _VALID_TRANSITIONS.get(self._status, frozenset())

    def is_terminal(self) -> bool:
        """Возвращает True, если текущее состояние терминальное."""
        return not _VALID_TRANSITIONS.get(self._status)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class TranscriptEntry:
    """Одна реплика в транскрипте звонка."""

    speaker: str
    text: str
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {"speaker": self.speaker, "text": self.text, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptEntry":
        return cls(
            speaker=str(data.get("speaker", "")),
            text=str(data.get("text", "")),
            ts=str(data.get("ts", _now_iso())),
        )


@dataclass
class CallSession:
    """Полная запись автоматической звонковой сессии."""

    id: str
    created_at: str
    phone_number: str
    goal_text: str
    status: str  # CallStatus.value — строка для JSON-сериализации
    transcript_history: list[TranscriptEntry] = field(default_factory=list)
    started_at: str | None = None
    ended_at: str | None = None
    cost_usd: float = 0.0
    operator_interruptions: list[dict[str, Any]] = field(default_factory=list)
    end_reason: str | None = None
    duration_sec: float | None = None

    # ----- Factory -----

    @classmethod
    def create(cls, phone_number: str, goal_text: str) -> "CallSession":
        """Создаёт новую сессию в состоянии IDLE."""
        return cls(
            id=f"cs_{uuid.uuid4().hex[:12]}",
            created_at=_now_iso(),
            phone_number=phone_number.strip(),
            goal_text=goal_text.strip(),
            status=CallStatus.IDLE.value,
        )

    # ----- Serialization -----

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "phone_number": self.phone_number,
            "goal_text": self.goal_text,
            "status": self.status,
            "transcript_history": [e.to_dict() for e in self.transcript_history],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cost_usd": self.cost_usd,
            "operator_interruptions": list(self.operator_interruptions),
            "end_reason": self.end_reason,
            "duration_sec": self.duration_sec,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallSession":
        raw_history = data.get("transcript_history") or []
        history = [
            TranscriptEntry.from_dict(e) if isinstance(e, dict) else e
            for e in raw_history
        ]
        interruptions = data.get("operator_interruptions") or []
        return cls(
            id=str(data.get("id", "")),
            created_at=str(data.get("created_at", _now_iso())),
            phone_number=str(data.get("phone_number", "")),
            goal_text=str(data.get("goal_text", "")),
            status=str(data.get("status", CallStatus.IDLE.value)),
            transcript_history=history,
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            cost_usd=float(data.get("cost_usd") or 0.0),
            operator_interruptions=list(interruptions),
            end_reason=data.get("end_reason"),
            duration_sec=data.get("duration_sec"),
        )
