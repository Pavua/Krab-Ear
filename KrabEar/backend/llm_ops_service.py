"""LLMOpsService — обработчики IPC-методов взаимодействия с LLM.

Извлечено из service.py (BackendService, W783) для уменьшения монолита.

Методы:
    list_llm_models               — список моделей из LM Studio /api/v1/models
    get_last_llm_diff             — последний word-level diff от LLM rewriter'а
    replace_word_in_last_transcript — замена слова в последней транскрипции

Зависимости:
    store         — StateStore (чтение/запись истории)
    settings_svc  — SettingsService (cached_settings для llm_base_url/llm_api_key)
    transcriber   — Transcriber (доступ к engine._last_llm_diff)
"""

from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.state_store import StateStore
    from backend.settings_service import SettingsService
    from backend.transcriber import Transcriber

logger = logging.getLogger(__name__)


class LLMOpsService:
    """Обработчики IPC-команд LLM discovery, result inspection и transcript editing."""

    def __init__(
        self,
        store: "StateStore",
        settings_svc: "SettingsService",
        transcriber: "Transcriber",
    ) -> None:
        self._store = store
        self._settings_svc = settings_svc
        self._transcriber = transcriber

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_list_llm_models(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список моделей доступных в LM Studio через /api/v1/models.

        Используется GUI для динамического заполнения dropdown'а выбора LLM-модели.
        При недоступности LM Studio возвращает пустой список с описанием ошибки.
        Таймаут 3 секунды — не блокирует UI.
        """
        try:
            import requests as _requests
            cached = self._settings_svc.cached_settings()
            base_url = str(cached.get("llm_base_url", "http://127.0.0.1:1234/v1")).rstrip("/")
            api_key = str(cached.get("llm_api_key", ""))
            headers: dict[str, str] = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            # Wave 68 (LM Studio probe fix): /v1/models возвращает 200 но логирует ERROR
            # в LM Studio. /api/v1/models — корректный endpoint. Same pattern as PR #396
            # для llm_rewriter.py:1064 (passive_health_check).
            _host = re.sub(r"/v\d+$", "", base_url)
            resp = _requests.get(
                f"{_host}/api/v1/models",
                headers=headers,
                timeout=3,
            )
            if resp.status_code != 200:
                return {"models": [], "error": f"http_{resp.status_code}"}
            data = resp.json()
            ids = [
                item.get("id")
                for item in data.get("data", [])
                if item.get("id")
            ]
            recommended_models = [
                "qwen3-4b-abliterated",
                "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
                "qwen3-8b-abliterated",
            ]
            return {
                "models": sorted(ids),
                "recommended_models": recommended_models,
                "error": None,
            }
        except Exception as exc:
            return {"models": [], "recommended_models": [], "error": str(exc)}

    def handle_get_last_llm_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последний word-level diff от LLM rewriter'а."""
        engine = self._transcriber.engine
        diff = getattr(engine, '_last_llm_diff', None)
        if diff is None:
            return {"available": False, "diff": None}
        return {
            "available": True,
            "diff": {
                "similarity_ratio": diff.similarity_ratio,
                "words_added": diff.words_added,
                "words_removed": diff.words_removed,
                "words_unchanged": diff.words_unchanged,
                "summary": diff.summary,
                "changes": [
                    {"type": c.type, "text": c.text, "position": c.position}
                    for c in diff.changes
                ],
            },
        }

    def handle_replace_word_in_last_transcript(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Заменяет слово в последней (или указанной) записи истории без перезаписи.

        Параметры:
          - old_word: str — слово для замены (не пустое).
          - new_word: str — новое слово (не пустое).
          - history_id: str | None — ID записи; если не указан, берётся последняя запись.

        Возвращает:
            {"ok": bool, "replaced_count": int, "history_id": str | None, "new_text": str | None}

        Ошибки (ok=False):
          - "missing_words"      — old_word или new_word пусты.
          - "no_recent_history"  — история пуста и history_id не указан.
          - "item_not_found"     — запись с history_id не найдена.
          - "word_not_found"     — слово не найдено в тексте (с учётом границ слова).
        """
        old = str(params.get("old_word", "")).strip()
        new = str(params.get("new_word", "")).strip()
        if not old or not new:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "missing_words"}

        history_id = str(params.get("history_id", "")).strip() or None

        if history_id is None:
            # Берём самую последнюю запись
            with self._store._lock():
                active = self._store._load_active_items_unlocked()
            history_id = active[-1].id if active else None

        if history_id is None:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "no_recent_history"}

        item = self._store.get_history_item_by_id(history_id)
        if item is None:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "item_not_found"}

        # Замена с учётом границ слова и без учёта регистра
        pattern = re.compile(r'\b' + re.escape(old) + r'\b', re.IGNORECASE)
        new_text, replaced_count = pattern.subn(new, item.text)

        if replaced_count == 0:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "word_not_found"}

        self._store.update_history_item_text(history_id, new_text)
        logger.info(
            "replace_word_in_last_transcript: history_id=%s old=%r new=%r count=%d",
            history_id,
            old,
            new,
            replaced_count,
        )
        return {"ok": True, "replaced_count": replaced_count, "history_id": history_id, "new_text": new_text}
