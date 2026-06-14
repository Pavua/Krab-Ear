"""PlaybackTracker — отслеживание воспроизведения/прослушивания записей Krab Ear.

Сохраняет метаданные воспроизведения (сколько раз воспроизводилась запись,
суммарное время прослушивания, время последнего воспроизведения) в
{data_dir}/playback_stats.json. Потокобезопасен.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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

    def __init__(
        self,
        data_dir: str | Path | None = None,
        privacy_mode_enabled: bool = False,
        privacy_mode_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._stats: dict[str, dict[str, Any]] = {}
        self._privacy_mode_enabled: bool = privacy_mode_enabled
        # wave-25 A3 (a): callable → runtime privacy lookup overrides static bool.
        # When set, _privacy_mode_fn() is checked first; static flag is fallback.
        self._privacy_mode_fn: Optional[Callable[[], bool]] = privacy_mode_fn
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
            payload = json.dumps(self._stats, ensure_ascii=False, indent=2)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=".playback_stats_tmp_",
                suffix=".json",
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            _log.warning("Не удалось сохранить статистику воспроизведения: %s", exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def set_privacy_mode(self, enabled: bool) -> None:
        """Устанавливает режим конфиденциальности.

        Когда privacy_mode=True, record_playback() ничего не записывает и не
        сохраняет на диск (персистентность отключена для защиты приватности).
        """
        self._privacy_mode_enabled = bool(enabled)

    def _is_privacy_mode(self) -> bool:
        """Возвращает True если privacy_mode активен.

        Приоритет: callable (runtime) → static bool (конструктор / set_privacy_mode).
        """
        if self._privacy_mode_fn is not None:
            return bool(self._privacy_mode_fn())
        return self._privacy_mode_enabled

    def record_playback(self, item_id: str, duration_listened_sec: float = 0.0) -> dict[str, Any]:
        """Регистрирует событие воспроизведения записи.

        Args:
            item_id: идентификатор записи истории.
            duration_listened_sec: сколько секунд прослушано в этот раз (≥ 0).

        Returns:
            dict с текущей статистикой, либо {"ok": True, "reason": "privacy_mode_active"}
            если privacy_mode активен (no-op, не сохраняет на диск).

        Note:
            Если privacy_mode включён — вызов игнорируется полностью
            (событие не записывается и не сохраняется на диск).
        """
        item_id = str(item_id).strip()
        if not item_id:
            raise ValueError("item_id не может быть пустым")
        # wave-25 A3 (b): privacy gate — skip recording entirely when privacy mode is on.
        if self._is_privacy_mode():
            _log.debug("record_playback: пропуск (privacy_mode=True) для item_id=%r", item_id)
            return {"ok": True, "reason": "privacy_mode_active"}
        duration_float = float(duration_listened_sec)
        # F1: cap single playback duration to 24h; reject non-finite values.
        if not math.isfinite(duration_float) or duration_float > 86400:
            _log.warning(
                "record_playback: invalid duration_listened_sec=%r for item_id=%r — rejected",
                duration_listened_sec,
                item_id,
            )
            return {"ok": False, "reason": "invalid_duration"}
        duration = max(0.0, duration_float)
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            # wave-25 A3 (c): DoS cap — prevent unbounded key growth from attacker-controlled item_ids.
            if item_id not in self._stats and len(self._stats) >= 10_000:
                _log.warning("record_playback: tracker_full (cap=10_000), item_id=%r ignored", item_id)
                return {"ok": False, "reason": "tracker_full"}
            entry = self._stats.setdefault(
                item_id,
                {"play_count": 0, "total_listened_sec": 0.0, "last_played": None},
            )
            entry["play_count"] = int(entry.get("play_count", 0)) + 1
            old_total = float(entry.get("total_listened_sec", 0.0))
            new_total = old_total + duration
            # F1: final Infinity guard — clamp if accumulation somehow produced non-finite.
            if not math.isfinite(new_total):
                new_total = old_total
            entry["total_listened_sec"] = new_total
            entry["last_played"] = now_iso
            self._save()
        return self.get_playback_stats(item_id)

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

    def clear_all(self) -> None:
        """W1765: полная очистка всей статистики воспроизведения для privacy-purge.

        Удаляет из памяти и с диска playback_stats.json (счётчики воспроизведения /
        суммарное время прослушивания — косвенные ПДн, раскрывают паттерны).

        Атомарность: выполняется под self._lock. При ошибке unlink исключение
        всплывает наружу — handle_purge_all_data зарегистрирует шаг в secondary_errors.
        """
        with self._lock:
            self._stats.clear()
            if self._path is not None:
                self._path.unlink(missing_ok=True)
        _log.info("clear_all: playback_stats.json удалён (privacy-purge)")

    def remove_stats(self, item_id: str) -> bool:
        """Удаляет статистику воспроизведения для указанной записи.

        Вызывать после удаления соответствующей записи из истории, чтобы
        не накапливать осиротевшие ключи в playback_stats.json.

        Args:
            item_id: идентификатор записи истории.

        Returns:
            True если ключ существовал и был удалён, False если ключ не найден.
        """
        item_id = str(item_id).strip()
        if not item_id:
            return False
        with self._lock:
            if item_id not in self._stats:
                return False
            del self._stats[item_id]
            self._save()
        return True

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_record_playback(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: record_playback — зарегистрировать событие воспроизведения."""
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        # wave-security: безопасная коэрция — non-numeric/None/список → 0.0,
        # NaN/Inf → 0.0, числовые строки ("12.5") парсятся корректно.
        # float("abc") бросал ValueError и крашил IPC-хендлер.
        raw_duration = params.get("duration_listened_sec", 0.0)
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = 0.0
        if not math.isfinite(duration):
            duration = 0.0
        # record_playback now returns the result dict directly (privacy no-op / tracker_full / stats)
        return self.record_playback(item_id, duration_listened_sec=duration)

    def handle_get_playback_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_playback_stats — статистика воспроизведения записи."""
        # wave-41: privacy gate — read paths must not expose playback history in privacy mode.
        if self._is_privacy_mode():
            return {"item_id": "", "play_count": 0, "total_listened_sec": 0.0, "last_played": None}
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        return self.get_playback_stats(item_id)

    # wave-1770 MED: maximum page size returned per IPC call.
    # Prevents DoS via limit=2147483647 forcing a huge list allocation.
    _MAX_IPC_LIMIT = 1000

    def handle_get_most_replayed(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_most_replayed — топ наиболее часто воспроизводимых записей."""
        # wave-41: privacy gate — suppress replay history in privacy mode.
        if self._is_privacy_mode():
            return {"items": [], "count": 0}
        # wave-1770 MED: cap limit to prevent DoS via unbounded allocation.
        limit = max(1, min(int(params.get("limit", 10)), self._MAX_IPC_LIMIT))
        items = self.get_most_replayed(limit=limit)
        return {"items": items, "count": len(items)}

    def handle_get_never_played(self, params: dict[str, Any], store: Any) -> dict[str, Any]:
        """IPC: get_never_played — записи истории, которые ни разу не воспроизводились."""
        # wave-41: privacy gate — suppress cross-referencing history with playback data.
        if self._is_privacy_mode():
            return {"items": [], "count": 0}
        # wave-1770 MED: cap limit to prevent DoS via unbounded allocation.
        limit = max(1, min(int(params.get("limit", 50)), self._MAX_IPC_LIMIT))
        items = self.get_never_played(store=store, limit=limit)
        return {"items": items, "count": len(items)}
