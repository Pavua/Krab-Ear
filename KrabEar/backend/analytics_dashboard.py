"""Комплексный дашборд аналитики Krab Ear.

AnalyticsDashboard агрегирует все доступные аналитические данные
за один вызов, кэшируя результат на 60 секунд.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.AnalyticsDashboard")

_CACHE_TTL_SEC = 60


class AnalyticsDashboard:
    """Агрегирует все аналитические данные Krab Ear в одном вызове.

    Результат кэшируется на ``_CACHE_TTL_SEC`` секунд. Параметр ``days``
    включается в ключ кэша, так что разные периоды кэшируются независимо.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # cache: key=(days,) -> (timestamp, result)
        self._cache: dict[int, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get_full_dashboard(self, store: Any, days: int = 30) -> dict[str, Any]:
        """Возвращает агрегированный дашборд аналитики.

        Args:
            store: экземпляр ``StateStore`` для доступа к истории.
            days: количество дней окна анализа (по умолчанию 30).

        Returns:
            dict с ключами: overview, today, trends, languages, quality,
            engagement, storage, performance.
        """
        days = max(1, int(days))
        with self._lock:
            cached = self._cache.get(days)
            if cached is not None:
                ts, result = cached
                if time.monotonic() - ts < _CACHE_TTL_SEC:
                    return result

        result = self._build_dashboard(store, days)

        with self._lock:
            self._cache[days] = (time.monotonic(), result)

        return result

    def invalidate_cache(self) -> None:
        """Сбрасывает кэш дашборда (полезно после новой транскрипции)."""
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Построение дашборда
    # ------------------------------------------------------------------

    def _build_dashboard(self, store: Any, days: int) -> dict[str, Any]:
        """Единственный проход по истории — собирает все метрики."""
        now = datetime.now(timezone.utc)
        today_str = date.today().isoformat()
        cutoff = now - timedelta(days=days)

        # Загружаем активные записи за один вызов
        try:
            with store._lock():
                active = store._load_active_items_unlocked()
        except Exception:
            logger.exception("Не удалось загрузить историю для дашборда")
            active = []

        # Метрики «за всё время»
        total_recordings = len(active)
        total_duration_sec = 0.0
        total_words = 0

        # Сегодня
        today_recordings = 0
        today_duration_sec = 0.0
        today_words = 0

        # Качество
        confidence_sum = 0.0
        confidence_count = 0
        low_confidence_count = 0  # < 0.7
        llm_rewrite_count = 0

        # Языки
        lang_counter: Counter[str] = Counter()
        translation_count = 0

        # Дата + час для вовлечённости
        hour_counter: Counter[int] = Counter()
        weekday_counter: Counter[str] = Counter()

        # Дневные тренды confidence в окне days
        daily_conf: dict[str, list[float]] = {}
        daily_durations: dict[str, float] = {}
        daily_word_counts: dict[str, int] = {}

        for item in active:
            # Длительность
            dur = getattr(item, "audio_duration_sec", None) or 0.0
            total_duration_sec += dur

            # Слова
            text = getattr(item, "text", "") or ""
            wc = len(text.split())
            total_words += wc

            # Разбираем ts
            ts_raw = getattr(item, "ts", None)
            item_dt: datetime | None = _parse_ts(ts_raw)
            item_date_str = item_dt.date().isoformat() if item_dt else None

            # Сегодня
            if item_date_str == today_str:
                today_recordings += 1
                today_duration_sec += dur
                today_words += wc

            # Час и день недели
            if item_dt is not None:
                hour_counter[item_dt.hour] += 1
                weekday_counter[item_dt.strftime("%A")] += 1

            # Confidence
            conf = getattr(item, "confidence", None)
            if conf is not None:
                try:
                    conf_f = float(conf)
                    confidence_sum += conf_f
                    confidence_count += 1
                    if conf_f < 0.7:
                        low_confidence_count += 1
                except (TypeError, ValueError):
                    pass

            # LLM rewrite
            if getattr(item, "llm_applied", False):
                llm_rewrite_count += 1

            # Язык
            lang = getattr(item, "source_lang", "") or ""
            if lang:
                lang_counter[lang] += 1

            # Перевод
            if (getattr(item, "translated_text", None) and
                    getattr(item, "translation_status", "") == "ok"):
                translation_count += 1

            # Дневные агрегаты для трендов (только в окне days)
            if item_dt is not None and item_dt >= cutoff and item_date_str:
                if conf is not None:
                    try:
                        conf_f2 = float(conf)
                        daily_conf.setdefault(item_date_str, []).append(conf_f2)
                    except (TypeError, ValueError):
                        pass
                daily_durations[item_date_str] = daily_durations.get(item_date_str, 0.0) + dur
                daily_word_counts[item_date_str] = daily_word_counts.get(item_date_str, 0) + wc

        # --- overview ---
        avg_daily = round(total_recordings / days, 2) if days else 0.0
        total_hours = round(total_duration_sec / 3600, 3)

        overview = {
            "total_recordings": total_recordings,
            "total_hours": total_hours,
            "total_words": total_words,
            "avg_daily": avg_daily,
        }

        # --- today ---
        today = {
            "recordings": today_recordings,
            "duration_min": round(today_duration_sec / 60, 2),
            "words": today_words,
        }

        # --- trends ---
        confidence_trend = _calc_trend(
            [{"date": d, "val": sum(vs) / len(vs)} for d, vs in sorted(daily_conf.items())]
        )
        pace_trend = _calc_trend(
            [{"date": d, "val": daily_word_counts.get(d, 0) / (daily_durations.get(d, 0) / 60 or 1)}
             for d in sorted(daily_word_counts)]
        )
        volume_trend = _calc_trend(
            [{"date": d, "val": daily_durations.get(d, 0.0)} for d in sorted(daily_durations)]
        )

        trends = {
            "confidence_trend": confidence_trend,
            "pace_trend": pace_trend,
            "volume_trend": volume_trend,
        }

        # --- languages ---
        total_lang = sum(lang_counter.values()) or 1
        lang_distribution = {lang: round(cnt / total_lang, 4)
                             for lang, cnt in lang_counter.most_common()}
        translation_rate = round(translation_count / total_recordings, 4) if total_recordings else 0.0

        languages = {
            "distribution": lang_distribution,
            "translation_rate": translation_rate,
        }

        # --- quality ---
        avg_confidence = round(confidence_sum / confidence_count, 4) if confidence_count else 0.0
        low_confidence_rate = round(low_confidence_count / confidence_count, 4) if confidence_count else 0.0
        llm_rewrite_rate = round(llm_rewrite_count / total_recordings, 4) if total_recordings else 0.0

        quality = {
            "avg_confidence": avg_confidence,
            "low_confidence_rate": low_confidence_rate,
            "llm_rewrite_rate": llm_rewrite_rate,
        }

        # --- engagement ---
        streak_days = _calc_streak(active)
        peak_hour = hour_counter.most_common(1)[0][0] if hour_counter else None
        most_active_day = weekday_counter.most_common(1)[0][0] if weekday_counter else None

        engagement = {
            "streak_days": streak_days,
            "peak_hour": peak_hour,
            "most_active_day": most_active_day,
        }

        # --- storage ---
        storage = _build_storage_info(store)

        # --- performance (из MetricsCollector) ---
        performance = _build_performance_info()

        return {
            "overview": overview,
            "today": today,
            "trends": trends,
            "languages": languages,
            "quality": quality,
            "engagement": engagement,
            "storage": storage,
            "performance": performance,
        }


# ------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------

def _parse_ts(raw: Any) -> datetime | None:
    """Парсит timestamp из различных форматов в timezone-aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _calc_trend(points: list[dict[str, Any]]) -> str:
    """Вычисляет тренд ('improving'|'stable'|'declining') по линейной регрессии."""
    vals = [p["val"] for p in points]
    n = len(vals)
    if n < 2:
        return "stable"
    x_mean = (n - 1) / 2.0
    y_mean = sum(vals) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vals))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return "stable"
    slope = num / den
    if slope > 0.001:
        return "improving"
    if slope < -0.001:
        return "declining"
    return "stable"


def _calc_streak(active: list[Any]) -> int:
    """Считает непрерывный streak активных дней (от сегодня назад)."""
    if not active:
        return 0
    active_dates: set[str] = set()
    for item in active:
        ts_raw = getattr(item, "ts", None)
        dt = _parse_ts(ts_raw)
        if dt:
            active_dates.add(dt.date().isoformat())
    streak = 0
    today = date.today()
    for i in range(len(active_dates) + 1):
        d = (today - timedelta(days=i)).isoformat()
        if d in active_dates:
            streak += 1
        else:
            break
    return streak


def _build_storage_info(store: Any) -> dict[str, Any]:
    """Собирает информацию о хранилище из StateStore."""
    try:
        data_dir = Path(store.data_dir)
        history_path = store.history_path
        history_bytes = history_path.stat().st_size if history_path.exists() else 0
        history_size_mb = round(history_bytes / (1024 * 1024), 3)

        # Подсчёт бэкапов
        backups_dir = data_dir / "backups"
        backups_count = len(list(backups_dir.glob("*.ndjson"))) if backups_dir.exists() else 0

        # Кэш-файлы (settings.json, *.json.tmp, stats)
        cache_bytes = sum(
            f.stat().st_size
            for f in data_dir.glob("*.json")
            if f.is_file()
        )
        cache_size_mb = round(cache_bytes / (1024 * 1024), 3)

        return {
            "history_size_mb": history_size_mb,
            "backups_count": backups_count,
            "cache_size_mb": cache_size_mb,
        }
    except Exception:
        logger.exception("Не удалось собрать storage info")
        return {
            "history_size_mb": 0.0,
            "backups_count": 0,
            "cache_size_mb": 0.0,
        }


def _build_performance_info() -> dict[str, Any]:
    """Считывает p50/p95 latency из глобального MetricsCollector."""
    try:
        from backend.metrics_collector import metrics
        summary = metrics.get_summary()
        stt = summary.get("stt_metrics", {})
        lat = stt.get("latency_ms", {})
        return {
            "avg_stt_latency_ms": lat.get("avg", 0.0),
            "p95_latency_ms": lat.get("p95", 0.0),
        }
    except Exception:
        logger.exception("Не удалось получить метрики производительности")
        return {
            "avg_stt_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
        }
