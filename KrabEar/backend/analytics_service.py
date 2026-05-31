"""AnalyticsService — read-only analytics aggregators.

Extracted from BackendService Wave 392.
Pattern: sister to CallSessionService PR #420, RecordingCoreService PR #589.

Handlers (7):
  - handle_get_analytics_dashboard  — комплексный дашборд всех метрик
  - handle_get_sentiment_trends     — тренды тональности за N дней
  - handle_compare_periods          — сравнение двух временных периодов
  - handle_get_keyword_cloud        — данные облака ключевых слов
  - handle_get_timeline_view        — группировка истории по временным блокам
  - handle_get_activity_calendar    — GitHub-style activity calendar данные
  - handle_get_recording_stats      — кумулятивная статистика записей (W773)
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.observability import add_breadcrumb

logger = logging.getLogger("KrabEar.Backend.Analytics")


class AnalyticsService:
    """IPC-обработчики аналитики, вынесенные из BackendService.

    Все обработчики — read-only агрегаторы без side-effects на запись/state.
    """

    def __init__(
        self,
        *,
        analytics_dashboard: Any,
        sentiment_trends: Any,
        activity_calendar: Any,
        keyword_cloud_gen: Any,
        timeline_view: Any,
        store: Any,
    ) -> None:
        """
        Args:
            analytics_dashboard: AnalyticsDashboard  — полный дашборд аналитики.
            sentiment_trends:    SentimentTrendAnalyzer — тренды тональности.
            activity_calendar:   ActivityCalendar     — GitHub-style calendar.
            keyword_cloud_gen:   KeywordCloudGenerator — облако ключевых слов.
            timeline_view:       TimelineViewGenerator — timeline группировка.
            store:               StateStore            — доступ к истории.
        """
        self._analytics_dashboard = analytics_dashboard
        self._sentiment_trends = sentiment_trends
        self._activity_calendar = activity_calendar
        self._keyword_cloud_gen = keyword_cloud_gen
        self._timeline_view = timeline_view
        self._store = store

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def handle_get_analytics_dashboard(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_analytics_dashboard — комплексный дашборд всех метрик аналитики.

        Параметры:
            days (int): окно анализа в днях (по умолчанию 30, макс. 365)

        Возвращает:
            overview, today, trends, languages, quality, engagement, storage, performance
        """
        days = max(1, min(int(params.get("days", 30) or 30), 365))
        _t0 = _time.monotonic()
        result = self._analytics_dashboard.get_full_dashboard(store=self._store, days=days)
        add_breadcrumb(
            category="analytics",
            message="get_analytics_dashboard",
            data={"days": days, "duration_ms": round((_time.monotonic() - _t0) * 1000)},
        )
        return result

    def handle_get_sentiment_trends(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует тренды тональности транскрипций за последние N дней."""
        days = int(params.get("days", 30))
        try:
            with self._store._lock():
                items = self._store._load_active_items_unlocked()
        except Exception:
            items = []
        report = self._sentiment_trends.analyze_sentiment_trends(items, days=days)
        return self._sentiment_trends.to_dict(report)

    def handle_compare_periods(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сравнивает статистику двух временных периодов."""
        from backend.period_comparison import compare_periods as _compare_periods_fn

        p1_start = params.get("period1_start")
        p1_end = params.get("period1_end")
        p2_start = params.get("period2_start")
        p2_end = params.get("period2_end")
        if not all([p1_start, p1_end, p2_start, p2_end]):
            raise ValueError("Необходимы параметры: period1_start, period1_end, period2_start, period2_end")
        report = _compare_periods_fn(
            store=self._store,
            period1_start=p1_start,
            period1_end=p1_end,
            period2_start=p2_start,
            period2_end=p2_end,
        )
        add_breadcrumb(
            category="analytics",
            message="compare_periods",
            data={
                "recordings_change_pct": report.recordings_change_pct,
                "duration_change_pct": report.duration_change_pct,
            },
        )
        return {
            "period1": {
                "recordings": report.period1.recordings,
                "duration_sec": report.period1.duration_sec,
                "words": report.period1.words,
                "avg_confidence": report.period1.avg_confidence,
                "languages": report.period1.languages,
            },
            "period2": {
                "recordings": report.period2.recordings,
                "duration_sec": report.period2.duration_sec,
                "words": report.period2.words,
                "avg_confidence": report.period2.avg_confidence,
                "languages": report.period2.languages,
            },
            "recordings_change_pct": report.recordings_change_pct,
            "duration_change_pct": report.duration_change_pct,
            "confidence_change": report.confidence_change,
            "new_languages": report.new_languages,
            "summary": report.summary,
        }

    def handle_get_keyword_cloud(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует данные облака ключевых слов из истории транскрипций."""
        max_words = int(params.get("max_words", 100))
        language = params.get("language")
        try:
            with self._store._lock():
                items = self._store._load_active_items_unlocked()
        except Exception:
            items = []
        cloud_words = self._keyword_cloud_gen.generate_cloud(
            items, max_words=max_words, language=language
        )
        add_breadcrumb(
            category="analytics",
            message="get_keyword_cloud",
            data={"word_count": len(cloud_words), "max_words": max_words},
        )
        return {
            "words": [
                {
                    "word": cw.word,
                    "count": cw.count,
                    "weight": cw.weight,
                    "font_size": cw.font_size,
                }
                for cw in cloud_words
            ]
        }

    def handle_get_timeline_view(self, params: dict[str, Any]) -> dict[str, Any]:
        """Группирует историю транскрипций по временным блокам (timeline).

        Параметры:
          - group_by: str — гранулярность: "hour", "day", "week" (по умолчанию "day").
          - limit: int — макс. записей для анализа (по умолчанию 500, макс. 5000).
          - include_heatmap: bool — включить activity heatmap (по умолчанию False).
          - heatmap_days: int — горизонт heatmap в днях (по умолчанию 30).
        """
        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))
        include_heatmap = bool(params.get("include_heatmap", False))
        heatmap_days = max(1, min(int(params.get("heatmap_days", 30)), 365))

        raw_items = self._store._load_active_items_with_lock()[:limit]
        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        result: dict[str, Any] = {
            "blocks": [b.to_dict() for b in blocks],
            "total_blocks": len(blocks),
            "group_by": group_by,
        }

        if include_heatmap:
            heatmap = self._timeline_view.generate_activity_heatmap(raw_items, days=heatmap_days)
            result["activity_heatmap"] = heatmap

        return result

    def handle_get_activity_calendar(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает GitHub-style activity calendar данные за последние N месяцев."""
        months = int(params.get("months", 12))
        months = max(1, min(months, 24))
        include_svg = bool(params.get("include_svg", False))
        cell_size = int(params.get("cell_size", 12))
        try:
            with self._store._lock():
                items = self._store._load_active_items_unlocked()
        except Exception:
            items = []
        calendar = self._activity_calendar.generate_calendar(items, months=months)
        result = calendar.to_dict()
        if include_svg:
            result["svg"] = self._activity_calendar.generate_calendar_svg(
                items, months=months, cell_size=cell_size
            )
        return result

    def handle_get_recording_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Кумулятивная статистика записей: длительность, языки, LLM, диаризация.

        Сканирует всю активную историю и агрегирует метаданные.
        Extracted from BackendService W773.
        """
        active = self._store._load_active_items_with_lock()

        now = datetime.now(timezone.utc)
        today_iso = now.date().isoformat()
        week_start = (now - timedelta(days=now.weekday())).date().isoformat()

        total_count = 0
        total_dur = 0.0
        today_count = 0
        today_dur = 0.0
        week_count = 0
        week_dur = 0.0
        llm_count = 0
        diar_count = 0
        lang_counts: dict[str, int] = {}

        for item in active:
            total_count += 1
            dur = item.audio_duration_sec or 0.0
            total_dur += dur

            day_str = item.ts[:10]
            if day_str == today_iso:
                today_count += 1
                today_dur += dur
            if day_str >= week_start:
                week_count += 1
                week_dur += dur

            if item.llm_applied:
                llm_count += 1
            if isinstance(item.diarization, dict) and item.diarization.get("enabled"):
                diar_count += 1

            lang = item.source_lang.strip()
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

        n = total_count or 1  # avoid division by zero
        return {
            "total_count": total_count,
            "total_duration_sec": round(total_dur, 2),
            "today_count": today_count,
            "today_duration_sec": round(today_dur, 2),
            "week_count": week_count,
            "week_duration_sec": round(week_dur, 2),
            "avg_duration_sec": round(total_dur / n, 2) if total_count else 0.0,
            "most_used_lang": max(lang_counts, key=lambda k: lang_counts[k]) if lang_counts else "",
            "lang_distribution": [
                {"lang": lang, "count": cnt}
                for lang, cnt in sorted(lang_counts.items(), key=lambda p: p[1], reverse=True)[:10]
            ],
            "llm_applied_count": llm_count,
            "llm_correction_rate": round(llm_count / n, 4) if total_count else 0.0,
            "diarization_used_count": diar_count,
            "diarization_usage_rate": round(diar_count / n, 4) if total_count else 0.0,
        }
