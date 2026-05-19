<<<<<<< HEAD
"""AnalyticsService — read-only analytics aggregators.

Extracted from BackendService Wave 392.
Pattern: sister to CallSessionService PR #420, RecordingCoreService PR #589.

Handlers (6):
  - handle_get_analytics_dashboard  — комплексный дашборд всех метрик
  - handle_get_sentiment_trends     — тренды тональности за N дней
  - handle_compare_periods          — сравнение двух временных периодов
  - handle_get_keyword_cloud        — данные облака ключевых слов
  - handle_get_timeline_view        — группировка истории по временным блокам
  - handle_get_activity_calendar    — GitHub-style activity calendar данные
=======
"""AnalyticsService — aggregate analytics handlers extracted from BackendService.

Extracted из BackendService Wave 174.
Pattern: thin facade, delegates к existing analytics collaborators.

Handlers (8):
  - handle_get_analytics_dashboard  — комплексный дашборд всех метрик аналитики
  - handle_generate_daily_digest    — ежедневный дайджест транскрипций
  - handle_compare_periods          — сравнение статистики двух периодов
  - handle_get_activity_calendar    — GitHub-style activity calendar данные
  - handle_get_recording_insights   — эвристические инсайты по записям
  - handle_get_sentiment_trends     — тренды тональности транскрипций за N дней
  - handle_get_keyword_cloud        — данные облака ключевых слов для word cloud
  - handle_get_metrics_dashboard    — снимок метрик реального времени (сессия, LLM, call_assist, конфиг)
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
"""

from __future__ import annotations

import logging
from typing import Any

<<<<<<< HEAD
=======
from backend.period_comparison import compare_periods as _compare_periods_fn

>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
logger = logging.getLogger("KrabEar.Backend.Analytics")


class AnalyticsService:
<<<<<<< HEAD
    """IPC-обработчики аналитики, вынесенные из BackendService.

    Все обработчики — read-only агрегаторы без side-effects на запись/state.
    """
=======
    """IPC-обработчики аналитики, вынесенные из BackendService."""
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))

    def __init__(
        self,
        *,
        analytics_dashboard: Any,
<<<<<<< HEAD
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
=======
        daily_digest: Any,
        activity_calendar: Any,
        recording_insights: Any,
        sentiment_trends: Any,
        keyword_cloud_gen: Any,
        store: Any,
        # handle_get_metrics_dashboard needs live service state — passed as callables
        get_session_state: Any,
        get_settings: Any,
    ) -> None:
        """
        Args:
            analytics_dashboard:  AnalyticsDashboard — полный дашборд метрик.
            daily_digest:         DailyDigestGenerator — дайджест за дату.
            activity_calendar:    ActivityCalendar — GitHub-style calendar data.
            recording_insights:   RecordingInsightsGenerator — эвристики по записям.
            sentiment_trends:     SentimentTrendAnalyzer — анализ тональности.
            keyword_cloud_gen:    KeywordCloudGenerator — word-cloud данные.
            store:                StateStore — доступ к истории.
            get_session_state:    callable() -> dict — live session/preview/llm/call state
                                  (injected от BackendService для metrics_dashboard).
            get_settings:         callable() -> dict — cached_settings() из BackendService.
        """
        self._analytics_dashboard = analytics_dashboard
        self._daily_digest = daily_digest
        self._activity_calendar = activity_calendar
        self._recording_insights = recording_insights
        self._sentiment_trends = sentiment_trends
        self._keyword_cloud_gen = keyword_cloud_gen
        self._store = store
        self._get_session_state = get_session_state
        self._get_settings = get_settings
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))

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
        return self._analytics_dashboard.get_full_dashboard(store=self._store, days=days)

<<<<<<< HEAD
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

=======
    def handle_generate_daily_digest(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует ежедневный дайджест транскрипций за указанную дату."""
        date_str = params.get("date")  # None → today
        digest = self._daily_digest.generate_digest(date_str=date_str, store=self._store)
        return {
            "date": digest.date,
            "total_recordings": digest.total_recordings,
            "total_duration_min": digest.total_duration_min,
            "total_words": digest.total_words,
            "languages_used": digest.languages_used,
            "top_topics": digest.top_topics,
            "highlights": digest.highlights,
            "markdown": digest.formatted_markdown,
        }

    def handle_compare_periods(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сравнивает статистику двух временных периодов."""
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
        p1_start = params.get("period1_start")
        p1_end = params.get("period1_end")
        p2_start = params.get("period2_start")
        p2_end = params.get("period2_end")
        if not all([p1_start, p1_end, p2_start, p2_end]):
<<<<<<< HEAD
            raise ValueError("Необходимы параметры: period1_start, period1_end, period2_start, period2_end")
=======
            raise ValueError(
                "Необходимы параметры: period1_start, period1_end, period2_start, period2_end"
            )
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
        report = _compare_periods_fn(
            store=self._store,
            period1_start=p1_start,
            period1_end=p1_end,
            period2_start=p2_start,
            period2_end=p2_end,
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

<<<<<<< HEAD
=======
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

    def handle_get_recording_insights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует эвристические инсайты по записям за последние N дней."""
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

>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
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

<<<<<<< HEAD
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
=======
    def handle_get_metrics_dashboard(self, params: dict[str, Any]) -> dict[str, Any]:
        """Снимок метрик реального времени: сессия, LLM, call_assist, конфиг."""
        settings = self._get_settings()
        session_state = self._get_session_state()
        return {
            "session": session_state.get("session", {}),
            "preview_loop": session_state.get("preview_loop", {}),
            "llm": {
                "enabled": settings.get("llm_rewrite_enabled", False),
                "model": settings.get("llm_model", "?"),
                "status": session_state.get("llm_status"),
            },
            "call_assist": session_state.get("call_assist"),
            "import": session_state.get("import", {"active": False}),
            "config_snapshot": {
                "quality": settings.get("quality_profile", "balanced"),
                "cleanup": settings.get("cleanup_profile", "soft"),
                "translation_mode": settings.get("translation_mode", "off"),
                "diarization": settings.get("diarization_enabled", False),
                "network_mode": settings.get("network_mode", "offline_default"),
            },
        }
>>>>>>> 170a59e3 (refactor(wave174): extract AnalyticsService (8 handlers from service.py))
