"""SearchAndAnalysisService — IPC-обработчики поиска и аналитики транскрипций.

Выделено из backend/service.py (Wave 757).
Охватывает 12 обработчиков по трём тематическим кластерам:

Кластер 1 — Семантический поиск (3 обработчика):
  - semantic_search         — поиск по истории через embeddings (sentence-transformers)
  - semantic_search_status  — статус: модель загружена/включена, размер индекса
  - semantic_search_reindex — перестроить индекс с нуля для всей истории

Кластер 2 — Извлечение задач/решений (3 обработчика):
  - extract_action_items       — LLM-извлечение задач/решений/вопросов для одного item
  - batch_extract_action_items — то же самое для пакета item_ids
  - get_pending_action_items   — все items, где action_items=None (ещё не анализировались)

Кластер 3 — Аналитика записей (6 обработчиков):
  - get_topic_timeline      — таймлайн смен тем разговора из истории транскрибаций
  - get_recording_insights  — эвристические инсайты по записям за последние N дней
  - compare_recordings      — сравнение нескольких записей side-by-side
  - generate_stats_report   — полный Markdown-отчёт статистики за период
  - generate_mini_stats_report — краткий 5-строчный Markdown-отчёт состояния

Связи модуля:
  1) SemanticSearcher        — embeddings-индекс истории (multilingual-e5-base).
  2) ActionItemsExtractor    — LLM-извлечение задач/решений/вопросов.
  3) store (StateStore)      — доступ к истории транскрипций.
  4) TopicTracker            — анализ смен тем в записях.
  5) RecordingInsightsGenerator — эвристические инсайты по паттернам записей.
  6) RecordingComparison     — side-by-side сравнение нескольких записей.
  7) StatsReportGenerator    — Markdown-отчёт статистики.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger("KrabEar.Backend.SearchAndAnalysis")


def _keyword_fallback_search(query: str, items: list[dict], top_k: int = 10) -> list[dict]:
    """Простой keyword fallback для semantic_search когда модель недоступна.

    Импортируем отсюда локально чтобы избежать circular import на уровне модуля.
    """
    from backend.semantic_search import keyword_fallback_search
    return keyword_fallback_search(query, items, top_k=top_k)


class SearchAndAnalysisService:
    """Обработчики IPC для семантического поиска и аналитики записей."""

    def __init__(
        self,
        *,
        store: Any,
        semantic_searcher: Any,
        action_items_extractor: Optional[Any],
        topic_tracker: Any,
        recording_insights: Any,
        recording_comparison: Any,
        stats_report: Any,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """
        Args:
            store:                StateStore — хранилище истории транскрипций.
            semantic_searcher:    SemanticSearcher — embeddings-индекс.
            action_items_extractor: ActionItemsExtractor | None — LLM-извлечение;
                                    None когда LLM_ENABLED=False.
            topic_tracker:        TopicTracker — анализ смен тем.
            recording_insights:   RecordingInsightsGenerator — эвристические инсайты.
            recording_comparison: RecordingComparison — side-by-side сравнение.
            stats_report:         StatsReportGenerator — Markdown-отчёты статистики.
            settings_get:         callable(key, default) → Any — runtime settings lookup
                                  (передаётся как BackendService._get_runtime_setting).
                                  Используется для privacy_mode_enabled guard.
        """
        self._store = store
        self._semantic_searcher = semantic_searcher
        self._action_items_extractor = action_items_extractor
        self._topic_tracker = topic_tracker
        self._recording_insights = recording_insights
        self._recording_comparison = recording_comparison
        self._stats_report = stats_report
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)

    # ================================================================== #
    # Кластер 1 — Семантический поиск                                     #
    # ================================================================== #

    def handle_semantic_search(self, params: dict) -> dict:
        """IPC: semantic_search — семантический поиск по истории транскрипций через embeddings.

        Privacy guard (wave-31 D2): когда privacy_mode_enabled=True возвращает пустой ответ
        без доступа к тексту транскрипций — семантический поиск по определению возвращает
        transcript-derived context, который не должен утекать в privacy mode.

        Params:
            query     — поисковый запрос (строка, обязательный)
            top_k     — максимальное число результатов (int, default 10)
            fallback  — bool, использовать keyword fallback если модель недоступна (default True)
        Returns:
            {"results": [{"id": str, "score": float}], "mode": "semantic"|"keyword"|"disabled"}
        """
        # wave-31 D2: privacy guard — both semantic and keyword fallback paths expose
        # transcript-derived data; block entirely in privacy mode.
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "results": [],
                "mode": "disabled",
                "reason": "privacy_mode_active",
            }

        query = str(params.get("query", "")).strip()
        if not query:
            raise ValueError("Параметр query обязателен")
        top_k = int(params.get("top_k", 10))
        top_k = max(1, min(top_k, 100))
        use_fallback = bool(params.get("fallback", True))

        if not self._semantic_searcher.is_enabled:
            if use_fallback:
                items = [{"id": it.id, "text": it.text}
                         for it in self._store._load_active_items_with_lock()]
                results = _keyword_fallback_search(query, items, top_k=top_k)
                return {"results": results, "mode": "keyword", "reason": "semantic_disabled"}
            return {"results": [], "mode": "disabled"}

        results = self._semantic_searcher.search(query, top_k=top_k)
        if not results and use_fallback:
            items = [{"id": it.id, "text": it.text}
                     for it in self._store._load_active_items_with_lock()]
            results = _keyword_fallback_search(query, items, top_k=top_k)
            return {"results": results, "mode": "keyword", "reason": "model_unavailable"}

        return {"results": results, "mode": "semantic"}

    def handle_semantic_search_status(self, params: dict) -> dict:
        """IPC: semantic_search_status — возвращает статус семантического поиска.

        Returns:
            {"enabled": bool, "model_loaded": bool, "model_name": str,
             "model_error": str|null, "indexed_count": int}
        """
        return self._semantic_searcher.status()

    def handle_semantic_search_reindex(self, params: dict) -> dict:
        """IPC: semantic_search_reindex — переиндексирует всю историю транскрипций.

        Privacy guard (wave-31 D2): когда privacy_mode_enabled=True возвращает
        {"indexed": 0, "reason": "privacy_mode_active"} без доступа к текстам.
        Reindex читает тексты из store и индексирует их через embeddings — утечка
        в privacy mode недопустима.

        Params:
            force — bool, перестроить индекс с нуля (default False)
        Returns:
            {"indexed": int, "skipped": int, "errors": int}
        """
        # wave-31 D2: privacy guard — reindex reads transcript texts from store and
        # persists their embeddings; must be blocked in privacy mode.
        if self._settings_get("privacy_mode_enabled", False):
            return {"indexed": 0, "reason": "privacy_mode_active"}

        if not self._semantic_searcher.is_enabled:
            return {"indexed": 0, "skipped": 0, "errors": 0, "reason": "semantic_search_disabled"}
        force = bool(params.get("force", False))
        items = [{"id": it.id, "text": it.text}
                 for it in self._store._load_active_items_with_lock()]
        result = self._semantic_searcher.index_all(items, force=force)
        return result

    def handle_semantic_search_reset(self, params: dict) -> dict:
        """IPC: semantic_search_reset — сбрасывает зафиксированную ошибку загрузки модели.

        Позволяет повторную попытку загрузки SentenceTransformer после временного сбоя
        (сеть, HuggingFace недоступен, недостаточно RAM и т.п.).  Без этого метода
        ``_model_error`` остаётся установленным навсегда и любые вызовы semantic_search
        молча возвращают пустой результат.

        Returns:
            {"reset": bool, "previous_error": str|None}
        """
        return self._semantic_searcher.reset_model_error()

    # ================================================================== #
    # Кластер 2 — Извлечение задач / решений / вопросов                   #
    # ================================================================== #

    def handle_extract_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: extract_action_items — извлекает задачи/решения/вопросы из транскрипта.

        Privacy guard: когда privacy_mode_enabled=True возвращает пустой ответ без
        передачи текста транскрипта в LLM.

        Params:
            id       — item_id истории (обязательный).
            language — язык транскрипта, "ru"|"es"|"en" (default "ru").
        Returns:
            {id, ok, action_items, decisions, questions, fallback_reason, latency_ms}
        """
        if self._settings_get("privacy_mode_enabled", False):
            item_id = str(params.get("id", "")).strip()
            return {
                "id": item_id,
                "ok": True,
                "action_items": [],
                "decisions": [],
                "questions": [],
                "fallback_reason": None,
                "latency_ms": 0,
                "reason": "privacy_mode_active",
                "privacy_mode_active": True,
            }

        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("Параметр id обязателен")

        if self._action_items_extractor is None:
            raise RuntimeError("LLM не включён (LLM_ENABLED=False)")

        with self._store._lock():
            items = self._store._load_active_items_unlocked()
        target = next((it for it in items if it.id == item_id), None)
        if target is None:
            raise RuntimeError(f"Элемент не найден: {item_id}")

        text = target.text or ""
        language = str(params.get("language", "ru")).lower()

        result = self._action_items_extractor.extract(text, language=language)

        if result.ok:
            self._store.update_history_item_action_items(
                item_id=item_id,
                action_items=[ai.to_dict() for ai in result.action_items],
                decisions=result.decisions,
                questions=result.questions,
            )

        return {
            "id": item_id,
            "ok": result.ok,
            "action_items": [ai.to_dict() for ai in result.action_items],
            "decisions": result.decisions,
            "questions": result.questions,
            "fallback_reason": result.fallback_reason,
            "latency_ms": result.latency_ms,
        }

    # Maximum number of item_ids accepted in a single batch call.
    # Each ID triggers a serial LLM call — unbounded list is a DoS vector.
    MAX_BATCH_ACTION_ITEMS: int = 20

    def handle_batch_extract_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: batch_extract_action_items — пакетное извлечение для нескольких item_id.

        DoS guard: максимум MAX_BATCH_ACTION_ITEMS id за один вызов (serial LLM calls).
        Privacy guard: когда privacy_mode_enabled=True возвращает пустой ответ.

        Params:
            ids      — список item_id (list[str], обязательный, не более 20).
            language — язык транскрипта, "ru"|"es"|"en" (default "ru").
        Returns:
            {"results": [...], "count": int}
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "results": [],
                "count": 0,
                "reason": "privacy_mode_active",
                "privacy_mode_active": True,
            }

        item_ids = params.get("ids", [])
        if not isinstance(item_ids, list):
            raise RuntimeError("Параметр ids должен быть списком")
        if len(item_ids) > self.MAX_BATCH_ACTION_ITEMS:
            raise RuntimeError(
                f"Слишком много элементов: {len(item_ids)} > {self.MAX_BATCH_ACTION_ITEMS}"
            )
        language = str(params.get("language", "ru")).lower()

        if self._action_items_extractor is None:
            raise RuntimeError("LLM не включён (LLM_ENABLED=False)")

        with self._store._lock():
            all_items = self._store._load_active_items_unlocked()
        items_by_id = {it.id: it for it in all_items}

        results = []
        for item_id in item_ids:
            item_id = str(item_id).strip()
            target = items_by_id.get(item_id)
            if target is None:
                results.append({"id": item_id, "ok": False, "error": "not_found"})
                continue
            text = target.text or ""
            result = self._action_items_extractor.extract(text, language=language)
            if result.ok:
                self._store.update_history_item_action_items(
                    item_id=item_id,
                    action_items=[ai.to_dict() for ai in result.action_items],
                    decisions=result.decisions,
                    questions=result.questions,
                )
            results.append({
                "id": item_id,
                "ok": result.ok,
                "action_items": [ai.to_dict() for ai in result.action_items],
                "decisions": result.decisions,
                "questions": result.questions,
                "fallback_reason": result.fallback_reason,
                "latency_ms": result.latency_ms,
            })

        return {"results": results, "count": len(results)}

    def handle_get_pending_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_pending_action_items — items у которых action_items=None.

        Params:
            min_duration_sec — минимальная длительность аудио (float, опционально).
        Returns:
            {"pending": [...], "count": int}
        """
        min_duration = float(params.get("min_duration_sec", 0.0))

        with self._store._lock():
            items = self._store._load_active_items_unlocked()

        pending = []
        for item in items:
            if item.action_items is not None:
                continue
            if min_duration > 0 and (item.audio_duration_sec or 0.0) < min_duration:
                continue
            pending.append({
                "id": item.id,
                "ts": item.ts,
                "text_preview": (item.text or "")[:100],
                "audio_duration_sec": item.audio_duration_sec,
            })

        return {"pending": pending, "count": len(pending)}

    def trigger_auto_extract_action_items(
        self, item_id: str, text: str, language: str, duration_sec: float
    ) -> None:
        """Авто-извлечение action items в daemon-потоке после сохранения транскрипции."""
        if self._action_items_extractor is None:
            return

        def _run() -> None:
            try:
                logger.info("Auto-extract action items: item=%s dur=%.1fs", item_id, duration_sec)
                result = self._action_items_extractor.extract(text, language=language)
                if result["action_items"] or result["decisions"] or result["questions"]:
                    self._store.update_history_item_action_items(
                        item_id=item_id,
                        action_items=result["action_items"],
                        decisions=result["decisions"],
                        questions=result["questions"],
                    )
            except Exception:
                logger.exception("Auto-extract action items failed for item=%s", item_id)

        threading.Thread(
            target=_run, daemon=True, name=f"ai-{item_id[:8]}"
        ).start()

    # ================================================================== #
    # Кластер 3 — Аналитика записей                                       #
    # ================================================================== #

    def handle_get_topic_timeline(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_topic_timeline — таймлайн смен тем разговора из истории транскрибаций.

        Privacy guard (W1728): когда privacy_mode_enabled=True возвращает пустой ответ без
        доступа к тексту транскрипций (аналогично compare_recordings — W1408 F1 / W1710).

        Params:
            window_size (int): размер скользящего окна (1..1000, по умолчанию 5).
            limit       (int): максимальное количество последних записей (1..10000,
                               по умолчанию 100). limit <= 0 трактуется как default (100) —
                               НЕ «все записи» (W1281-F2 DoS-защита).
        Returns:
            {segments, total_shifts, current_topic}
        """
        # W1728 privacy guard — transcript-derived topics must not leak in privacy mode
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "segments": [],
                "total_shifts": 0,
                "current_topic": None,
                "reason": "privacy_mode_active",
                "privacy_mode_active": True,
            }

        # W1728 DoS clamp: window_size unbounded → O(n²) work; cap at 1000
        window_size = max(1, min(int(params.get("window_size", 5) or 5), 1000))
        # W1728/W1281-F2 DoS clamp: limit must always slice the history.
        # limit <= 0 (incl. negative) falls back to the default (100), never
        # "process all records" — an attacker passing limit=0 or limit=-1 must
        # not force unbounded work over the entire (unbounded) history.
        raw_limit = int(params.get("limit", 100) or 100)
        limit = raw_limit if raw_limit > 0 else 100
        limit = min(limit, 10000)
        try:
            with self._store._lock():
                items = self._store._load_active_items_unlocked()
        except Exception:
            items = []

        items = items[-limit:]

        timeline = self._topic_tracker.get_topic_timeline(items, window_size=window_size)
        current_topic = self._topic_tracker.get_current_topic(items, last_n=window_size)
        shifts = sum(1 for entry in timeline if entry.get("is_shift"))

        return {
            "segments": timeline,
            "total_shifts": shifts,
            "current_topic": current_topic,
        }

    def handle_get_recording_insights(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_recording_insights — эвристические инсайты по записям за N дней.

        Privacy guard (W1728): когда privacy_mode_enabled=True возвращает пустой ответ без
        доступа к тексту транскрипций (инсайты строятся из keyword-анализа транскриптов).

        Params:
            days (int): сколько дней анализировать (default 7).
        Returns:
            {insights: [...], count, days}
        """
        # W1728 privacy guard — insights are derived from transcript text (keyword analysis)
        if self._settings_get("privacy_mode_enabled", False):
            days = int(params.get("days", 7))
            return {
                "insights": [],
                "count": 0,
                "days": days,
                "reason": "privacy_mode_active",
                "privacy_mode_active": True,
            }
        days = int(params.get("days", 7))
        try:
            with self._store._lock():
                items = self._store._load_active_items_unlocked()
        except Exception:
            items = []
        insights = self._recording_insights.generate_insights(items, days=days)
        return {
            "insights": [i.to_dict() for i in insights],
            "count": len(insights),
            "days": days,
        }

    def handle_compare_recordings(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: compare_recordings — сравнение нескольких записей side-by-side.

        Privacy guard (W1408 F1 — restored W1710): когда privacy_mode_enabled=True
        возвращает пустой ответ без доступа к тексту транскрипций.

        Params:
            item_ids (list[str]): список item_id для сравнения (обязательный).
        Returns:
            Словарь с матрицей сходства, статистикой, общими/уникальными словами.
        """
        # W1408 F1 privacy guard — восстановлен в W1710 после cherry-pick drop
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "items": [],
                "text_similarity_matrix": [],
                "duration_comparison": {},
                "confidence_comparison": {},
                "language_distribution": {},
                "common_words": [],
                "unique_words_per_item": [],
                "reason": "privacy_mode_active",
                "privacy_mode_active": True,
            }
        from backend.recording_comparison import _view_to_dict as _comparison_view_to_dict
        item_ids = params.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise ValueError("Параметр item_ids обязателен (список строк)")
        view = self._recording_comparison.compare(item_ids=item_ids, store=self._store)
        return _comparison_view_to_dict(view)

    def handle_generate_stats_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: generate_stats_report — полный Markdown-отчёт статистики за период.

        Privacy guard (wave-34 D2): когда privacy_mode_enabled=True возвращает ошибку
        без доступа к тексту транскрипций — отчёт включает топ-слова, паттерны,
        спикеров и другие данные, производные от текстов транскрипций.

        Params:
            days (int): период анализа в днях (default 30).
        Returns:
            {markdown, days}
        """
        # wave-34 D2 privacy guard — stats report reads transcript text (top words,
        # speaker aliases, patterns); must be blocked in privacy mode.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "reason": "privacy_mode_active"}
        days = int(params.get("days", 30))
        markdown = self._stats_report.generate_report(store=self._store, days=days)
        return {"markdown": markdown, "days": days}

    def handle_generate_mini_stats_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: generate_mini_stats_report — краткий 5-строчный Markdown-отчёт состояния.

        Privacy guard (wave-34 D2): когда privacy_mode_enabled=True возвращает ошибку
        без доступа к тексту транскрипций — мини-отчёт включает топ-слова и данные
        из истории транскрипций.

        Returns:
            {markdown}
        """
        # wave-34 D2 privacy guard — mini report reads transcript text and vocabulary;
        # must be blocked in privacy mode.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "reason": "privacy_mode_active"}
        markdown = self._stats_report.generate_mini_report(store=self._store)
        return {"markdown": markdown}
