"""PlaybackTracker — отслеживание воспроизведения/прослушивания записей Krab Ear.

Сохраняет метаданные воспроизведения (сколько раз воспроизводилась запись,
суммарное время прослушивания, время последнего воспроизведения) в
{data_dir}/playback_stats.json. Потокобезопасен.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("KrabEar.Backend.PlaybackTracker")

_PLAYBACK_FILE = "playback_stats.json"


class PlaybackTracker:
    """Отслеживает воспроизведение записей и хранит статистику.

    Структура playback_stats.json:
    {
        "<item_id>": {
            "play_count": int,
            "total_listened_sec": float,
            "last_played": ISO8601 | null
        },
        ...
    }
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._stats: dict[str, dict[str, Any]] = {}
        if data_dir is not None:
            self._path: Path | None = Path(data_dir) / _PLAYBACK_FILE
            self._load()
        else:
            self._path = None

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает статистику воспроизведения из файла (не бросает исключений)."""
        if self._path is None or not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                self._stats = data
        except Exception as exc:
            _log.warning("Не удалось загрузить статистику воспроизведения: %s", exc)

    def _save(self) -> None:
        """Сохраняет статистику воспроизведения в файл (не бросает исключений).

        Использует атомарный паттерн tmp+fsync+rename для предотвращения потери
        данных при сбое в середине записи (BUG-3 HIGH, W877 audit).
        """
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self._path)
        except Exception as exc:
            _log.warning("Не удалось сохранить статистику воспроизведения: %s", exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def record_playback(self, item_id: str, duration_listened_sec: float = 0.0) -> None:
        """Регистрирует событие воспроизведения записи.

        Args:
            item_id: идентификатор записи истории.
            duration_listened_sec: сколько секунд прослушано в этот раз (≥ 0).
        """
        item_id = str(item_id).strip()
        if not item_id:
            raise ValueError("item_id не может быть пустым")
        duration = max(0.0, float(duration_listened_sec))
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            entry = self._stats.setdefault(
                item_id,
                {"play_count": 0, "total_listened_sec": 0.0, "last_played": None},
            )
            entry["play_count"] = int(entry.get("play_count", 0)) + 1
            entry["total_listened_sec"] = float(entry.get("total_listened_sec", 0.0)) + duration
            entry["last_played"] = now_iso
            self._save()

    def get_playback_stats(self, item_id: str) -> dict[str, Any]:
        """Возвращает статистику воспроизведения для указанной записи.

        Returns:
            dict с ключами: play_count, total_listened_sec, last_played (ISO8601 или None).
            Если запись никогда не воспроизводилась, возвращает нулевые значения.
        """
        item_id = str(item_id).strip()
        with self._lock:
            entry = self._stats.get(item_id)
            if entry is None:
                return {
                    "item_id": item_id,
                    "play_count": 0,
                    "total_listened_sec": 0.0,
                    "last_played": None,
                }
            return {
                "item_id": item_id,
                "play_count": int(entry.get("play_count", 0)),
                "total_listened_sec": float(entry.get("total_listened_sec", 0.0)),
                "last_played": entry.get("last_played"),
            }

    def get_most_replayed(self, limit: int = 10) -> list[dict[str, Any]]:
        """Возвращает топ-N наиболее часто воспроизводимых записей.

        Args:
            limit: максимальное количество записей в результате (≥ 1).

        Returns:
            Список dict, отсортированный по убыванию play_count.
            Каждый элемент содержит: item_id, play_count, total_listened_sec, last_played.
        """
        limit = max(1, int(limit))
        with self._lock:
            items = [
                {
                    "item_id": iid,
                    "play_count": int(v.get("play_count", 0)),
                    "total_listened_sec": float(v.get("total_listened_sec", 0.0)),
                    "last_played": v.get("last_played"),
                }
                for iid, v in self._stats.items()
                if int(v.get("play_count", 0)) > 0
            ]
        items.sort(key=lambda x: (x["play_count"], x["total_listened_sec"]), reverse=True)
        return items[:limit]

    def get_never_played(self, store: Any, limit: int = 50) -> list[dict[str, Any]]:
        """Возвращает записи истории, которые ни разу не воспроизводились.

        Args:
            store: объект StateStore с методом get_history_page_filtered.
            limit: максимальное количество записей в результате.

        Returns:
            Список dict — записи истории, отсутствующие в статистике воспроизведения.
            Каждый элемент содержит поля из HistoryItem.to_dict().
        """
        limit = max(1, int(limit))
        with self._lock:
            played_ids = set(self._stats.keys())

        result: list[dict[str, Any]] = []
        cursor = None
        while len(result) < limit:
            batch_limit = min(200, limit * 4)
            items, next_cursor = store.get_history_page_filtered(
                cursor=cursor,
                limit=batch_limit,
            )
            for item in items:
                item_dict = item.to_dict() if hasattr(item, "to_dict") else dict(item)
                iid = str(item_dict.get("id", ""))
                if iid and iid not in played_ids:
                    result.append(item_dict)
                    if len(result) >= limit:
                        break
            if not next_cursor or not items:
                break
            cursor = next_cursor

        return result[:limit]

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_record_playback(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: record_playback — зарегистрировать событие воспроизведения."""
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        duration = float(params.get("duration_listened_sec", 0.0))
        self.record_playback(item_id, duration_listened_sec=duration)
        return self.get_playback_stats(item_id)

    def handle_get_playback_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_playback_stats — статистика воспроизведения записи."""
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        return self.get_playback_stats(item_id)

    def handle_get_most_replayed(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_most_replayed — топ наиболее часто воспроизводимых записей."""
        limit = int(params.get("limit", 10))
        items = self.get_most_replayed(limit=limit)
        return {"items": items, "count": len(items)}

    def handle_get_never_played(self, params: dict[str, Any], store: Any) -> dict[str, Any]:
        """IPC: get_never_played — записи истории, которые ни разу не воспроизводились."""
        limit = int(params.get("limit", 50))
        items = self.get_never_played(store=store, limit=limit)
        return {"items": items, "count": len(items)}
