"""Action Items Extractor для Krab Ear.

Извлекает из транскрипта:
- action items (задачи/поручения с исполнителем, дедлайном, приоритетом)
- decisions (принятые решения)
- questions (открытые вопросы)

Использует тот же LM Studio endpoint (qwen3-4b-abliterated), что и LLMRewriter.
Контракт extract(): НИКОГДА не raises, всегда возвращает структуру (пустую при ошибке).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import requests

logger = logging.getLogger("KrabEar.Backend.ActionItemsExtractor")

# Системные промпты для трёх языков
_SYSTEM_PROMPTS: dict[str, str] = {
    "ru": (
        "Ты аналитик встреч и переговоров. Проанализируй транскрипт и найди:\n"
        "1) action items — задачи/поручения с конкретным действием. "
        "Для каждого укажи: text (само действие), assignee (исполнитель или null), "
        "due (дедлайн/срок или null), priority (high/medium/low или null).\n"
        "2) decisions — принятые решения (краткие формулировки).\n"
        "3) questions — открытые вопросы, требующие ответа.\n\n"
        "Верни ТОЛЬКО валидный JSON в формате:\n"
        '{"action_items": [{"text": "...", "assignee": null, "due": null, "priority": null}], '
        '"decisions": ["..."], "questions": ["..."]}\n'
        "Без пояснений. Без markdown-блоков. Только JSON."
    ),
    "es": (
        "Eres un analista de reuniones. Analiza la transcripción y encuentra:\n"
        "1) action items — tareas/acciones concretas. "
        "Para cada una indica: text (acción), assignee (responsable o null), "
        "due (fecha límite o null), priority (high/medium/low o null).\n"
        "2) decisions — decisiones tomadas (formulaciones breves).\n"
        "3) questions — preguntas abiertas que requieren respuesta.\n\n"
        "Devuelve SOLO JSON válido en el formato:\n"
        '{"action_items": [{"text": "...", "assignee": null, "due": null, "priority": null}], '
        '"decisions": ["..."], "questions": ["..."]}\n'
        "Sin explicaciones. Sin bloques markdown. Solo JSON."
    ),
    "en": (
        "You are a meeting analyst. Analyze the transcript and find:\n"
        "1) action items — concrete tasks/actions. "
        "For each provide: text (the action), assignee (responsible person or null), "
        "due (deadline or null), priority (high/medium/low or null).\n"
        "2) decisions — decisions that were made (brief formulations).\n"
        "3) questions — open questions that need answers.\n\n"
        "Return ONLY valid JSON in the format:\n"
        '{"action_items": [{"text": "...", "assignee": null, "due": null, "priority": null}], '
        '"decisions": ["..."], "questions": ["..."]}\n'
        "No explanations. No markdown blocks. Just JSON."
    ),
}

_EMPTY_RESULT: dict[str, Any] = {
    "action_items": [],
    "decisions": [],
    "questions": [],
}

_VALID_PRIORITIES = {"high", "medium", "low", None}


def _empty_result() -> dict[str, Any]:
    """Возвращает копию пустой структуры."""
    return {
        "action_items": [],
        "decisions": [],
        "questions": [],
    }


def _normalize_result(raw: Any) -> dict[str, Any]:
    """Нормализует и валидирует сырой результат от LLM.

    Мягкая валидация: неизвестные поля игнорируются, некорректные типы — fallback.
    """
    if not isinstance(raw, dict):
        return _empty_result()

    # action_items
    raw_items = raw.get("action_items", [])
    if not isinstance(raw_items, list):
        raw_items = []

    action_items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        assignee_raw = item.get("assignee")
        assignee = str(assignee_raw).strip() if assignee_raw is not None else None
        if assignee == "null" or assignee == "":
            assignee = None

        due_raw = item.get("due")
        due = str(due_raw).strip() if due_raw is not None else None
        if due == "null" or due == "":
            due = None

        priority_raw = item.get("priority")
        priority = str(priority_raw).strip().lower() if priority_raw is not None else None
        if priority == "null" or priority == "":
            priority = None
        if priority not in _VALID_PRIORITIES:
            priority = None

        action_items.append({
            "text": text,
            "assignee": assignee,
            "due": due,
            "priority": priority,
        })

    # decisions
    raw_decisions = raw.get("decisions", [])
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    decisions = [str(d).strip() for d in raw_decisions if str(d).strip()]

    # questions
    raw_questions = raw.get("questions", [])
    if not isinstance(raw_questions, list):
        raw_questions = []
    questions = [str(q).strip() for q in raw_questions if str(q).strip()]

    return {
        "action_items": action_items,
        "decisions": decisions,
        "questions": questions,
    }


def _strip_json_markdown(text: str) -> str:
    """Убирает ```json ... ``` обёртки из ответа LLM."""
    s = text.strip()
    if s.startswith("```"):
        # Strip leading ```json or ``` line
        lines = s.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


class ActionItemsExtractor:
    """Извлекает action items, decisions, questions из транскрипта через LLM.

    Контракт extract(): НИКОГДА не raises. При любой ошибке возвращает пустую структуру.
    CircuitBreaker применяется через _llm_rewriter._circuit.

    Args:
        llm_rewriter: экземпляр LLMRewriter (может быть None если LLM выключен).
        settings: callable () -> dict | dict — текущие runtime-настройки.
    """

    def __init__(
        self,
        llm_rewriter: Optional[Any] = None,
        settings: Optional[Any] = None,
    ) -> None:
        self._llm_rewriter = llm_rewriter
        self._settings = settings
        self._last_error: Optional[str] = None
        self._last_latency_ms: Optional[int] = None

    def _get_settings(self) -> dict:
        """Возвращает текущие настройки."""
        if self._settings is None:
            return {}
        if callable(self._settings):
            try:
                return self._settings() or {}
            except Exception:
                return {}
        if isinstance(self._settings, dict):
            return self._settings
        return {}

    def extract(self, transcript: str, language: str = "ru") -> dict[str, Any]:
        """Извлекает action items, decisions, questions из транскрипта.

        Args:
            transcript: текст транскрипта.
            language: язык транскрипта ("ru", "es", "en"). Дефолт "ru".

        Returns:
            {
                "action_items": [{"text": str, "assignee": str|None, "due": str|None, "priority": str|None}],
                "decisions": [str],
                "questions": [str],
            }
        """
        try:
            return self._extract_impl(transcript, language)
        except Exception as exc:
            logger.exception("ActionItemsExtractor.extract: неожиданная ошибка: %s", exc)
            self._last_error = f"unexpected: {exc}"
            return _empty_result()

    def _extract_impl(self, transcript: str, language: str) -> dict[str, Any]:
        """Внутренняя реализация без defensive wrapper."""
        cleaned = (transcript or "").strip()
        if not cleaned:
            logger.debug("ActionItemsExtractor: пустой транскрипт, возврат пустой структуры")
            return _empty_result()

        if self._llm_rewriter is None:
            logger.debug("ActionItemsExtractor: LLMRewriter не настроен, возврат пустой структуры")
            self._last_error = "no_llm_rewriter"
            return _empty_result()

        # Circuit breaker check через _llm_rewriter._circuit
        circuit = getattr(self._llm_rewriter, "_circuit", None)
        if circuit is not None and not circuit.allow_request():
            logger.debug("ActionItemsExtractor: circuit open, возврат пустой структуры")
            self._last_error = "circuit_open"
            return _empty_result()

        lang_key = (language or "ru").lower()
        if lang_key not in _SYSTEM_PROMPTS:
            lang_key = "ru"
        system_prompt = _SYSTEM_PROMPTS[lang_key]

        # Оценка max_tokens: JSON-структура обычно ~2x слов транскрипта
        word_count = len(cleaned.split())
        max_tokens = max(512, min(int(word_count * 0.5) + 200, 2048))

        payload = {
            "model": self._llm_rewriter._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": cleaned},
            ],
            "temperature": 0.1,  # детерминистичность
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._llm_rewriter._api_key}",
        }

        start = time.monotonic()
        try:
            response = self._llm_rewriter._session.post(
                f"{self._llm_rewriter._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._llm_rewriter._timeout * 3,  # extraction занимает дольше
            )
        except requests.Timeout:
            if circuit is not None:
                circuit.record_failure()
            self._last_error = "timeout"
            logger.warning("ActionItemsExtractor: timeout при запросе к LLM")
            return _empty_result()
        except (requests.ConnectionError, requests.RequestException) as exc:
            if circuit is not None:
                circuit.record_failure()
            self._last_error = f"connection_error: {exc}"
            logger.warning("ActionItemsExtractor: ошибка подключения к LLM: %s", exc)
            return _empty_result()

        self._last_latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code != 200:
            if circuit is not None:
                circuit.record_failure()
            self._last_error = f"http_{response.status_code}"
            logger.warning(
                "ActionItemsExtractor: HTTP %d от LLM endpoint", response.status_code
            )
            return _empty_result()

        # Парсим JSON ответа
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            if circuit is not None:
                circuit.record_failure()
            self._last_error = f"parse_response_error: {exc}"
            logger.warning("ActionItemsExtractor: ошибка парсинга ответа LLM: %s", exc)
            return _empty_result()

        # Убираем markdown-обёртки
        content_clean = _strip_json_markdown(content or "")
        if not content_clean:
            if circuit is not None:
                circuit.record_failure()
            self._last_error = "empty_response"
            return _empty_result()

        # Парсим JSON из ответа LLM
        try:
            raw_result = json.loads(content_clean)
        except (json.JSONDecodeError, ValueError) as exc:
            # Невалидный JSON → graceful empty
            if circuit is not None:
                circuit.record_failure()
            self._last_error = f"invalid_json: {exc}"
            logger.warning(
                "ActionItemsExtractor: LLM вернул невалидный JSON (%s), возврат пустой структуры. "
                "Первые 200 символов ответа: %.200s",
                exc, content_clean,
            )
            return _empty_result()

        # Успех — сбрасываем circuit breaker
        if circuit is not None:
            circuit.record_success()
        self._last_error = None

        result = _normalize_result(raw_result)
        logger.info(
            "ActionItemsExtractor: extracted %d action_items, %d decisions, %d questions "
            "(%d ms, lang=%s)",
            len(result["action_items"]),
            len(result["decisions"]),
            len(result["questions"]),
            self._last_latency_ms,
            lang_key,
        )
        return result

    def status(self) -> dict[str, Any]:
        """Статус экстрактора для диагностики."""
        has_rewriter = self._llm_rewriter is not None
        circuit = getattr(self._llm_rewriter, "_circuit", None)
        return {
            "available": has_rewriter,
            "circuit_state": circuit.state if circuit else "n/a",
            "last_error": self._last_error,
            "last_latency_ms": self._last_latency_ms,
        }
