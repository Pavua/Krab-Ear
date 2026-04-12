"""Трекер ежедневной статистики использования Krab Ear.

Отслеживает количество записей, суммарную длительность и количество слов
по периодам: сегодня, эта неделя, этот месяц, всё время.
Хранит историю за последние 30 дней и рассчитывает streak дней активности.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("KrabEar.Backend.UsageTracker")

_STATS_FILE = "usage_stats.json"
_HISTORY_DAYS = 30


class UsageTracker:
    """Потокобезопасный трекер ежедневного использования с персистентностью."""

    def __init__(self, data_dir: Optional[str | Path] = None) -> None:
        self._lock = threading.Lock()
        self._data_dir = Path(data_dir) if data_dir else None
        self._stats_file = (self._data_dir / _STATS_FILE) if self._data_dir else None
        # daily_history: dict[str date_iso -> {"recordings": int, "duration_sec": float, "words": int}]
        self._daily: dict[str, dict[str, Any]] = {}
        # all_time counters
        self._all_recordings = 0
        self._all_duration = 0.0
        self._all_words = 0
        self._load()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def record_usage(self, duration_sec: float, word_count: int) -> None:
        """Фиксирует одну транскрипцию: длительность и количество слов."""
        today_str = date.today().isoformat()
        with self._lock:
            day = self._daily.setdefault(today_str, {"recordings": 0, "duration_sec": 0.0, "words": 0})
            day["recordings"] += 1
            day["duration_sec"] += float(duration_sec)
            day["words"] += int(word_count)
            self._all_recordings += 1
            self._all_duration += float(duration_sec)
            self._all_words += int(word_count)
            self._prune_old_days()
        self._persist()

    def get_usage_stats(self) -> dict[str, Any]:
        """Возвращает агрегированную статистику использования."""
        today = date.today()
        with self._lock:
            daily_copy = dict(self._daily)
            all_recordings = self._all_recordings
            all_duration = self._all_duration
            all_words = self._all_words

        def _sum_period(days_back: int) -> dict[str, Any]:
            recordings = 0
            duration = 0.0
            words = 0
            for i in range(days_back):
                d = (today - timedelta(days=i)).isoformat()
                entry = daily_copy.get(d, {})
                recordings += entry.get("recordings", 0)
                duration += entry.get("duration_sec", 0.0)
                words += entry.get("words", 0)
            return {
                "recordings": recordings,
                "total_duration_sec": round(duration, 2),
                "total_words": words,
            }

        # daily_history: last 30 days sorted newest first
        history: List[dict[str, Any]] = []
        for i in range(_HISTORY_DAYS):
            d = (today - timedelta(days=i)).isoformat()
            entry = daily_copy.get(d)
            if entry:
                history.append({
                    "date": d,
                    "recordings": entry["recordings"],
                    "duration_sec": round(entry["duration_sec"], 2),
                    "words": entry["words"],
                })

        # streak: consecutive days from today backwards with >= 1 recording
        streak = 0
        for i in range(len(daily_copy) + 1):
            d = (today - timedelta(days=i)).isoformat()
            if daily_copy.get(d, {}).get("recordings", 0) >= 1:
                streak += 1
            else:
                break

        # peak day
        peak_day = None
        if daily_copy:
            peak_date = max(daily_copy, key=lambda k: daily_copy[k].get("recordings", 0))
            peak_day = {"date": peak_date, "recordings": daily_copy[peak_date]["recordings"]}

        return {
            "today": _sum_period(1),
            "this_week": _sum_period(7),
            "this_month": _sum_period(30),
            "all_time": {
                "recordings": all_recordings,
                "total_duration_sec": round(all_duration, 2),
                "total_words": all_words,
            },
            "daily_history": history,
            "streak_days": streak,
            "peak_day": peak_day,
        }

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _prune_old_days(self) -> None:
        """Удаляет записи старше _HISTORY_DAYS дней из daily (не из all_time)."""
        cutoff = (date.today() - timedelta(days=_HISTORY_DAYS)).isoformat()
        stale = [k for k in self._daily if k < cutoff]
        for k in stale:
            del self._daily[k]

    def _persist(self) -> None:
        """Сохраняет текущую статистику в JSON-файл."""
        if self._stats_file is None:
            return
        with self._lock:
            data = {
                "daily": self._daily,
                "all_time": {
                    "recordings": self._all_recordings,
                    "duration_sec": self._all_duration,
                    "words": self._all_words,
                },
            }
        try:
            self._stats_file.parent.mkdir(parents=True, exist_ok=True)
            self._stats_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Не удалось сохранить статистику в %s", self._stats_file)

    def _load(self) -> None:
        """Загружает статистику из JSON-файла при инициализации."""
        if self._stats_file is None or not self._stats_file.exists():
            return
        try:
            data = json.loads(self._stats_file.read_text(encoding="utf-8"))
            self._daily = data.get("daily", {})
            all_time = data.get("all_time", {})
            self._all_recordings = int(all_time.get("recordings", 0))
            self._all_duration = float(all_time.get("duration_sec", 0.0))
            self._all_words = int(all_time.get("words", 0))
            self._prune_old_days()
        except Exception:
            logger.exception("Не удалось загрузить статистику из %s", self._stats_file)
