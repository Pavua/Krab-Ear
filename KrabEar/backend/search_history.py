"""SearchHistoryManager — хранение истории поисковых запросов Krab Ear.

Отслеживает поисковые запросы пользователя, позволяет получать
последние и наиболее популярные запросы.
Данные сохраняются в {data_dir}/search_history.json (последние 500 записей).
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
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
                self._entries = data["entries"]
        except Exception as exc:
            logger.warning("Не удалось загрузить историю поиска: %s", exc)

    def _save(self) -> None:
        """Сохраняет историю поиска в файл (не бросает исключений)."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"entries": self._entries}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
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
            self._save()

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_get_recent_searches(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_recent_searches — последние поисковые запросы."""
        limit = int(params.get("limit", 20))
        return {"searches": self.get_recent_searches(limit=limit)}

    def handle_get_popular_searches(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_popular_searches — самые частые запросы."""
        limit = int(params.get("limit", 10))
        return {"searches": self.get_popular_searches(limit=limit)}

    def handle_clear_search_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: clear_search_history — удаляет всю историю поиска."""
        self.clear_search_history()
        return {"ok": True}
