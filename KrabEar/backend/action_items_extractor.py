"""ActionItemsExtractor — извлечение задач, решений и вопросов из meeting-транскриптов.

Использует qwen3-4b через LM Studio (OpenAI-compatible endpoint).
Интегрируется с CircuitBreaker из LLMRewriter для защиты от сбоев LM Studio.

Контракт: extract() НИКОГДА не raises. При любой ошибке возвращает пустую структуру.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import requests

from backend.llm_rewriter import CircuitBreaker

logger = logging.getLogger("KrabEar.Backend.ActionItemsExtractor")

# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

ACTION_PRIORITIES = {"low", "medium", "high"}


@dataclass
class ActionItem:
    """Одна задача, извлечённая из транскрипта."""

    text: str
    assignee: str = ""
    due: str = ""
    priority: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ActionItem":
        priority = str(d.get("priority", "medium")).lower()
        if priority not in ACTION_PRIORITIES:
            priority = "medium"
        return cls(
            text=str(d.get("text", "")).strip(),
            assignee=str(d.get("assignee", "")).strip(),
            due=str(d.get("due", "")).strip(),
            priority=priority,
        )


@dataclass
class ActionItemsResult:
    """Результат извлечения задач/решений/вопросов из транскрипта."""

    action_items: list[ActionItem] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    ok: bool = True
    fallback_reason: Optional[str] = None
    latency_ms: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_items": [ai.to_dict() for ai in self.action_items],
            "decisions": list(self.decisions),
            "questions": list(self.questions),
            "ok": self.ok,
            "fallback_reason": self.fallback_reason,
            "latency_ms": self.latency_ms,
        }

    @property
    def is_empty(self) -> bool:
        return not self.action_items and not self.decisions and not self.questions

    @classmethod
    def empty(cls, reason: str, latency_ms: Optional[int] = None) -> "ActionItemsResult":
        return cls(ok=False, fallback_reason=reason, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# Системные промпты по языкам
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS: dict[str, str] = {
    "ru": (
        "Ты аналитик встреч. Тебе дают транскрипт встречи или разговора. "
        "Извлеки из него: задачи (action items), принятые решения (decisions) и открытые вопросы (questions). "
        "\n\nОТВЕЧАЙ ТОЛЬКО JSON, без markdown, без пояснений. Формат:\n"
        '{"action_items": [{"text": "...", "assignee": "...", "due": "...", "priority": "high|medium|low"}], '
        '"decisions": ["..."], "questions": ["..."]}\n\n'
        "Если задач/решений/вопросов нет — верни пустые списки. "
        "Поле assignee — пустая строка если не упомянут. "
        "Поле due — пустая строка если срок не указан. "
        "Приоритет: high если срочно/важно, medium по умолчанию, low если второстепенно."
    ),
    "es": (
        "Eres un analista de reuniones. Se te proporciona una transcripción de una reunión o conversación. "
        "Extrae: tareas (action items), decisiones tomadas (decisions) y preguntas abiertas (questions). "
        "\n\nRESPONDE SOLO JSON, sin markdown, sin explicaciones. Formato:\n"
        '{"action_items": [{"text": "...", "assignee": "...", "due": "...", "priority": "high|medium|low"}], '
        '"decisions": ["..."], "questions": ["..."]}\n\n'
        "Si no hay tareas/decisiones/preguntas — devuelve listas vacías. "
        "Campo assignee — cadena vacía si no se menciona. "
        "Campo due — cadena vacía si no se indica fecha. "
        "Prioridad: high si urgente/importante, medium por defecto, low si secundario."
    ),
    "en": (
        "You are a meeting analyst. You are given a transcript of a meeting or conversation. "
        "Extract: action items, decisions made, and open questions. "
        "\n\nRESPOND ONLY JSON, no markdown, no explanations. Format:\n"
        '{"action_items": [{"text": "...", "assignee": "...", "due": "...", "priority": "high|medium|low"}], '
        '"decisions": ["..."], "questions": ["..."]}\n\n'
        "If there are no items/decisions/questions — return empty lists. "
        "Field assignee — empty string if not mentioned. "
        "Field due — empty string if no deadline given. "
        "Priority: high if urgent/important, medium by default, low if secondary."
    ),
}


# ---------------------------------------------------------------------------
# ActionItemsExtractor
# ---------------------------------------------------------------------------

class ActionItemsExtractor:
    """Извлечение задач, решений и вопросов из транскрипта через LLM (LM Studio).

    Контракт: extract() НИКОГДА не raises. Всегда возвращает ActionItemsResult.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_sec: float = 20.0,
        circuit_fail_threshold: int = 3,
        circuit_initial_reset_sec: int = 60,
        circuit_max_reset_sec: int = 600,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_sec
        self._circuit = CircuitBreaker(
            fail_threshold=circuit_fail_threshold,
            initial_reset_sec=circuit_initial_reset_sec,
            max_reset_sec=circuit_max_reset_sec,
        )
        self._session = requests.Session()
        self._last_error: Optional[str] = None

    def extract(self, transcript: str, language: str = "ru") -> ActionItemsResult:
        """Извлекает задачи, решения и вопросы из транскрипта.

        Контракт: НИКОГДА не raises. При любой ошибке → ActionItemsResult.empty(...).
        """
        try:
            return self._extract_impl(transcript, language)
        except Exception as exc:
            logger.exception("ActionItemsExtractor.extract: unexpected error: %s", exc)
            return ActionItemsResult.empty(f"unexpected_error: {exc}")

    def _extract_impl(self, transcript: str, language: str) -> ActionItemsResult:
        """Внутренняя реализация — вызывается из extract() с защитой от исключений."""
        cleaned = (transcript or "").strip()
        if not cleaned:
            return ActionItemsResult.empty("empty_input")

        if not self._circuit.allow_request():
            logger.debug("ActionItemsExtractor: circuit open, skipping")
            return ActionItemsResult.empty("circuit_open")

        lang_key = (language or "ru").lower()
        if lang_key not in _SYSTEM_PROMPTS:
            lang_key = "ru"
        system_prompt = _SYSTEM_PROMPTS[lang_key]

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": cleaned},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        start = time.monotonic()
        try:
            response = self._session.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except requests.Timeout:
            self._circuit.record_failure()
            self._last_error = "timeout"
            return ActionItemsResult.empty("timeout")
        except (requests.ConnectionError, requests.RequestException) as exc:
            self._circuit.record_failure()
            self._last_error = f"connection_error: {exc}"
            return ActionItemsResult.empty("connection_error")

        latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code != 200:
            self._circuit.record_failure()
            self._last_error = f"http_{response.status_code}"
            return ActionItemsResult.empty(f"http_{response.status_code}", latency_ms)

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._circuit.record_failure()
            self._last_error = f"parse_error: {exc}"
            return ActionItemsResult.empty("parse_error", latency_ms)

        # Parse JSON from LLM response
        result = self._parse_llm_json(content, latency_ms)
        if result.ok:
            self._circuit.record_success()
            self._last_error = None
        else:
            self._circuit.record_failure()
            self._last_error = result.fallback_reason

        return result

    def _parse_llm_json(self, content: str, latency_ms: int) -> ActionItemsResult:
        """Разбирает JSON-ответ LLM. При любой ошибке возвращает empty struct."""
        text = (content or "").strip()
        if not text:
            return ActionItemsResult.empty("empty_response", latency_ms)

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first line (```json or ```) and last line (```)
            inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            text = "\n".join(inner).strip()

        # Find JSON object boundaries
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning("ActionItemsExtractor: no JSON object found in LLM response")
            return ActionItemsResult.empty("no_json", latency_ms)

        json_str = text[start:end]
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("ActionItemsExtractor: JSON parse error: %s", exc)
            return ActionItemsResult.empty("invalid_json", latency_ms)

        if not isinstance(data, dict):
            return ActionItemsResult.empty("invalid_json_structure", latency_ms)

        # Parse action_items
        raw_items = data.get("action_items", [])
        action_items: list[ActionItem] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict) and item.get("text", "").strip():
                    action_items.append(ActionItem.from_dict(item))

        # Parse decisions
        raw_decisions = data.get("decisions", [])
        decisions: list[str] = []
        if isinstance(raw_decisions, list):
            for d in raw_decisions:
                s = str(d).strip()
                if s:
                    decisions.append(s)

        # Parse questions
        raw_questions = data.get("questions", [])
        questions: list[str] = []
        if isinstance(raw_questions, list):
            for q in raw_questions:
                s = str(q).strip()
                if s:
                    questions.append(s)

        return ActionItemsResult(
            action_items=action_items,
            decisions=decisions,
            questions=questions,
            ok=True,
            fallback_reason=None,
            latency_ms=latency_ms,
        )

    @property
    def circuit_state(self) -> str:
        return self._circuit.state

    def status(self) -> dict[str, Any]:
        return {
            "circuit_state": self._circuit.state,
            "last_error": self._last_error,
            "model": self._model,
        }

    def close(self):
        """Закрывает HTTP session."""
        self._session.close()
