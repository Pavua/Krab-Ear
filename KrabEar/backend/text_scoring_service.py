"""TextScoringService — text/LLM scoring + auto-titling + term extraction.

Extracted from BackendService Wave 404.
Pattern: sister to AnalyticsService PR #602, AudioAnalyticsService Wave 73.

Handlers (3):
  - handle_warmup_rewriter   — ручной LLM warmup probe (Load Model кнопка)
  - handle_extract_terms     — извлечение ключевых терминов из текста
  - handle_generate_auto_title — автоматический заголовок транскрибации

Collaborators:
  - llm_rewriter       — LLMRewriter | None
  - term_extractor     — TermExtractor
  - auto_title_generator — AutoTitleGenerator
  - get_runtime_setting  — callable(key, default) → Any  (BackendService._get_runtime_setting)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.TextScoring")


class TextScoringService:
    """IPC-обработчики text/LLM scoring, term extraction и auto-titling."""

    def __init__(
        self,
        *,
        llm_rewriter: Optional[Any],
        term_extractor: Any,
        auto_title_generator: Any,
        get_runtime_setting: Callable[[str, Any], Any],
    ) -> None:
        """
        Args:
            llm_rewriter:          LLMRewriter | None — LLM для warmup probe.
            term_extractor:        TermExtractor — извлечение терминов.
            auto_title_generator:  AutoTitleGenerator — генерация заголовков.
            get_runtime_setting:   callable(key, default) — runtime settings lookup
                                   (передаётся как self._get_runtime_setting из BackendService).
        """
        self._llm_rewriter = llm_rewriter
        self._term_extractor = term_extractor
        self._auto_title_generator = auto_title_generator
        self._get_runtime_setting = get_runtime_setting

    # ------------------------------------------------------------------ #
    # warmup_rewriter                                                       #
    # ------------------------------------------------------------------ #

    def handle_warmup_rewriter(self, params: dict) -> dict:
        """IPC: warmup_rewriter — ручной запуск LLM rewriter warmup probe.

        Отправляет минимальный (max_tokens=1) запрос в LM Studio для прогрева модели.
        НЕ трогает circuit breaker — warmup не является user-facing вызовом.

        Params:
            timeout_sec (float | None): таймаут в секундах; по умолчанию из настроек.

        Returns:
            {
              "ok": bool,          # True если HTTP 200
              "latency_ms": int,   # время ответа в мс
              "error": str | None, # описание ошибки или None
              "model": str | None  # имя используемой модели
            }
        """
        if self._llm_rewriter is None:
            return {"ok": False, "latency_ms": 0, "error": "rewriter_disabled", "model": None}
        runtime_timeout = self._get_runtime_setting("rewriter_warmup_timeout_sec", 15)
        timeout_sec = float(params.get("timeout_sec") or runtime_timeout)
        result = self._llm_rewriter.warmup_probe(timeout_sec=timeout_sec)
        result["model"] = getattr(self._llm_rewriter, "_model", None)
        return result

    # ------------------------------------------------------------------ #
    # extract_terms                                                         #
    # ------------------------------------------------------------------ #

    def handle_extract_terms(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: extract_terms — извлекает ключевые термины из текста.

        Params:
            text     (str): исходный текст.
            language (str): язык текста (по умолчанию "ru").

        Returns:
            {"terms": [{"term", "score", "frequency", "language", "category"}, ...]}
        """
        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {"ok": True, "terms": [], "reason": "privacy_mode_active"}
        text = params.get("text", "")
        language = params.get("language", "ru")
        if not text:
            return {"terms": []}
        terms = self._term_extractor.extract_terms(text, language=language)
        return {
            "terms": [
                {
                    "term": t.term,
                    "score": t.confidence,
                    "frequency": t.frequency,
                    "language": language,
                    "category": "proper_noun" if t.is_proper_noun else "general",
                }
                for t in terms
            ]
        }

    # ------------------------------------------------------------------ #
    # generate_auto_title                                                   #
    # ------------------------------------------------------------------ #

    def handle_generate_auto_title(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: generate_auto_title — генерирует автоматический заголовок транскрибации.

        Параметры:
            text (str): текст транскрибации (обязательный).
            timestamp (str): ISO 8601 timestamp (опциональный) — включает дату в заголовок.
            max_length (int): максимальная длина заголовка (по умолчанию 50).
            with_date (bool): если true и timestamp указан — включает дату.
            items (list): список записей для пакетной генерации (альтернатива text).

        Ответ (одиночный):
            {title: str}

        Ответ (пакетный):
            {titles: [{id, title, generated_at}]}
        """
        # wave-1770 MED: gate consistent with handle_extract_terms — both analyze transcript text.
        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {"title": "", "titles": [], "reason": "privacy_mode_active"}
        # Пакетный режим
        items = params.get("items")
        if items is not None:
            if not isinstance(items, list):
                raise ValueError("Параметр 'items' должен быть списком")
            titles = self._auto_title_generator.batch_generate(items)
            return {"titles": titles}

        # Одиночный режим
        text = str(params.get("text", "") or "")
        timestamp = str(params.get("timestamp", "") or "")
        max_length = int(params.get("max_length", 50))
        with_date = bool(params.get("with_date", False))

        if not text:
            return {"title": "Запись"}

        if with_date and timestamp:
            title = self._auto_title_generator.generate_title_with_date(text, timestamp)
        else:
            title = self._auto_title_generator.generate_title(text, max_length=max_length)

        return {"title": title}
