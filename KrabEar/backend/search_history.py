"""SearchHistoryManager — хранение истории поисковых запросов Krab Ear.

Отслеживает поисковые запросы пользователя, позволяет получать
последние и наиболее популярные запросы.
Данные сохраняются в {data_dir}/search_history.json (последние 500 записей).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.SearchHistory")

_SEARCH_HISTORY_FILE = "search_history.json"
_MAX_ENTRIES = 500


class SearchHistoryManager:
    """Менеджер истории поисковых запросов.

    Структура search_history.json:
    {
        "entries": [
            {
                "query": str,
                "results_count": int,
                "ts": ISO8601
            },
            ...
        ]
    }
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        settings_fn: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """
        Args:
            data_dir:    Директория данных для персистентности (опционально).
            settings_fn: callable(key, default) → Any — runtime settings lookup
                         для privacy_mode_enabled guard (wave-35 C3).
                         Если не передан — privacy gate отключён.
        """
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._settings_get: Callable[[str, Any], Any] = settings_fn or (lambda k, d: d)
        if data_dir is not None:
            self._path: Path | None = Path(data_dir) / _SEARCH_HISTORY_FILE
            self._load()
        else:
            self._path = None

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает историю поиска из файла (не бросает исключений)."""
        if self._path is None or not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                self._entries = data["entries"][-_MAX_ENTRIES:]
        except Exception as exc:
            logger.warning("Не удалось загрузить историю поиска: %s", exc)

    def _save(self) -> None:
        """Сохраняет историю поиска в файл (не бросает исключений)."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".json.tmp")
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump({"entries": self._entries}, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            tmp_path.replace(self._path)
        except Exception as exc:
            logger.error("Не удалось сохранить историю поиска: %s", exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def record_search(self, query: str, results_count: int = 0) -> None:
        """Записывает поисковый запрос в историю.

        Args:
            query: Строка поискового запроса (пустые игнорируются).
            results_count: Количество результатов поиска.
        """
        query = query.strip()
        if not query:
            return
        if len(query) > 1000:
            query = query[:1000]

        entry: dict[str, Any] = {
            "query": query,
            "results_count": int(results_count),
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }
        with self._lock:
            self._entries.append(entry)
            # Обрезаем до максимального размера (самые старые записи удаляются первыми)
            if len(self._entries) > _MAX_ENTRIES:
                self._entries = self._entries[-_MAX_ENTRIES:]
            self._save()

    def get_recent_searches(self, limit: int = 20) -> list[dict[str, Any]]:
        """Возвращает последние поисковые запросы (от новых к старым).

        Args:
            limit: Максимальное число записей.

        Returns:
            Список словарей с ключами query, results_count, ts.
        """
        limit = max(1, int(limit))
        with self._lock:
            recent = self._entries[-limit:]
        return list(reversed(recent))

    def get_popular_searches(self, limit: int = 10) -> list[dict[str, Any]]:
        """Возвращает наиболее популярные запросы по частоте.

        Args:
            limit: Максимальное число записей.

        Returns:
            Список словарей с ключами query и count, отсортированных по убыванию count.
        """
        limit = max(1, int(limit))
        with self._lock:
            queries = [e["query"] for e in self._entries]
        counts = Counter(queries)
        return [
            {"query": q, "count": c}
            for q, c in counts.most_common(limit)
        ]

    def clear_search_history(self) -> None:
        """Очищает всю историю поисковых запросов."""
        with self._lock:
            self._entries.clear()
            if self._path and self._path.exists():
                try:
                    self._path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("Не удалось удалить %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_get_recent_searches(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_recent_searches — последние поисковые запросы.

        Privacy guard (wave-35 C3): когда privacy_mode_enabled=True возвращает
        пустой список — история поисковых запросов раскрывает пользовательскую
        активность поиска в privacy mode.
        """
        # wave-35 C3: privacy gate — search queries reveal user activity
        if self._settings_get("privacy_mode_enabled", False):
            return {"searches": [], "reason": "privacy_mode_active"}
        limit = int(params.get("limit", 20))
        return {"searches": self.get_recent_searches(limit=limit)}

    def handle_get_popular_searches(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_popular_searches — самые частые запросы.

        Privacy guard (wave-35 C3): когда privacy_mode_enabled=True возвращает
        пустой список — популярные запросы раскрывают паттерны активности поиска.
        """
        # wave-35 C3: privacy gate — popular queries reveal user search patterns
        if self._settings_get("privacy_mode_enabled", False):
            return {"searches": [], "reason": "privacy_mode_active"}
        limit = int(params.get("limit", 10))
        return {"searches": self.get_popular_searches(limit=limit)}

    def handle_clear_search_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: clear_search_history — удаляет всю историю поиска."""
        self.clear_search_history()
        return {"ok": True}
