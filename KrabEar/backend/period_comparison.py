"""Сравнение статистики использования Krab Ear по периодам.

Позволяет сравнивать два произвольных периода (week/month/custom) и
генерировать человекочитаемый отчёт об изменениях.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

_SENTINEL = object()


@dataclass
class PeriodStats:
    """Статистика за один период."""

    recordings: int
    duration_sec: float
    words: int
    avg_confidence: float  # 0.0 если нет данных
    languages: list[str] = field(default_factory=list)


@dataclass
class ComparisonReport:
    """Отчёт сравнения двух периодов."""

    period1: PeriodStats
    period2: PeriodStats
    recordings_change_pct: "float | str"  # "no_baseline" если не было записей в period1
    duration_change_pct: "float | str"    # "no_baseline" если не было длительности в period1
    confidence_change: float  # абсолютное изменение (не %)
    new_languages: list[str]  # языки в period2, отсутствующие в period1
    summary: str


def _pct_change(old: float, new: float) -> "float | str":
    """Процентное изменение от old к new.

    Returns:
        float — процентное изменение, если old != 0.
        "no_baseline" — если old == 0 (нет базы для сравнения).
    """
    if old == 0.0:
        return "no_baseline"
    return round((new - old) / old * 100, 2)


def _collect_stats(store: Any, start_iso: str, end_iso: str) -> PeriodStats:
    """Собирает PeriodStats из store за период [start_iso, end_iso]."""
    items, cursor = store.get_history_page_filtered(
        cursor=None,
        limit=500,
        paste_status=None,
        translation_mode=None,
        translation_status=None,
        from_ts=start_iso,
        to_ts=end_iso,
    )
    all_items = list(items)

    # Если записей больше 500 — продолжаем листать
    while cursor is not None:
        items, cursor = store.get_history_page_filtered(
            cursor=cursor,
            limit=500,
            paste_status=None,
            translation_mode=None,
            translation_status=None,
            from_ts=start_iso,
            to_ts=end_iso,
        )
        all_items.extend(items)

    recordings = len(all_items)
    duration_sec = 0.0
    words = 0
    conf_sum = 0.0
    conf_count = 0
    langs: set[str] = set()

    for item in all_items:
        duration_sec += float(item.get("audio_duration_sec") or 0.0)
        text = item.get("text") or ""
        words += len(text.split()) if text.strip() else 0
        conf = item.get("confidence")
        if conf is not None:
            conf_sum += float(conf)
            conf_count += 1
        src_lang = (item.get("source_lang") or "").strip()
        if src_lang:
            langs.add(src_lang)

    avg_conf = round(conf_sum / conf_count, 4) if conf_count > 0 else 0.0

    return PeriodStats(
        recordings=recordings,
        duration_sec=round(duration_sec, 2),
        words=words,
        avg_confidence=avg_conf,
        languages=sorted(langs),
    )


def _iso_date(d: Any) -> str:
    """Приводит date/datetime/str к ISO-строке YYYY-MM-DD."""
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def compare_periods(
    store: Any,
    period1_start: Any,
    period1_end: Any,
    period2_start: Any,
    period2_end: Any,
) -> ComparisonReport:
    """Сравнивает статистику двух периодов и возвращает ComparisonReport.

    Args:
        store: StateStore-совместимый объект с методом get_history_page_filtered.
        period1_start: начало первого периода (date/datetime/str YYYY-MM-DD).
        period1_end: конец первого периода (включительно).
        period2_start: начало второго периода.
        period2_end: конец второго периода (включительно).

    Returns:
        ComparisonReport с delta-метриками и человекочитаемым summary.
    """
    p1_start = _iso_date(period1_start)
    p1_end_date = _iso_date(period1_end)
    p2_start = _iso_date(period2_start)
    p2_end_date = _iso_date(period2_end)

    if p1_start > p1_end_date:
        raise ValueError(
            f"period start must be <= end: period1 {p1_start} > {p1_end_date}"
        )
    if p2_start > p2_end_date:
        raise ValueError(
            f"period start must be <= end: period2 {p2_start} > {p2_end_date}"
        )

    p1_end = p1_end_date + "T23:59:59"
    p2_end = p2_end_date + "T23:59:59"

    stats1 = _collect_stats(store, p1_start, p1_end)
    stats2 = _collect_stats(store, p2_start, p2_end)

    rec_change = _pct_change(stats1.recordings, stats2.recordings)
    dur_change = _pct_change(stats1.duration_sec, stats2.duration_sec)
    conf_change = round(stats2.avg_confidence - stats1.avg_confidence, 4)

    langs1 = set(stats1.languages)
    langs2 = set(stats2.languages)
    new_langs = sorted(langs2 - langs1)

    # Генерируем summary
    lines = []

    def _fmt_pct(val: "float | str") -> str:
        if val == "no_baseline":
            return "нет базы"
        sign = "+" if val >= 0 else ""  # type: ignore[operator]
        return f"{sign}{val:.1f}%"  # type: ignore[str-bytes-safe]

    lines.append(
        f"Записей: {stats2.recordings} (было {stats1.recordings}, {_fmt_pct(rec_change)})"
    )
    lines.append(
        f"Длительность: {stats2.duration_sec:.0f}s (было {stats1.duration_sec:.0f}s, {_fmt_pct(dur_change)})"
    )
    if stats1.avg_confidence > 0 or stats2.avg_confidence > 0:
        conf_sign = "+" if conf_change >= 0 else ""
        lines.append(
            f"Качество распознавания: {stats2.avg_confidence:.2%} "
            f"({conf_sign}{conf_change:+.4f} от {stats1.avg_confidence:.2%})"
        )
    if new_langs:
        lines.append(f"Новые языки: {', '.join(new_langs)}")

    summary = "; ".join(lines) if lines else "Нет данных для сравнения."

    return ComparisonReport(
        period1=stats1,
        period2=stats2,
        recordings_change_pct=rec_change,
        duration_change_pct=dur_change,
        confidence_change=conf_change,
        new_languages=new_langs,
        summary=summary,
    )


def compare_weeks(store: Any, weeks_back: int = 2) -> ComparisonReport:
    """Сравнивает текущую неделю с предыдущей (или N недель назад).

    Args:
        store: StateStore-совместимый объект.
        weeks_back: сколько недель назад взять период 1 (default=2 → прошлая неделя).

    Returns:
        ComparisonReport: period1 = прошлая неделя, period2 = текущая неделя.
    """
    today = date.today()
    # Начало текущей недели (понедельник)
    week_start = today - timedelta(days=today.weekday())

    p2_start = week_start
    p2_end = today

    p1_end = week_start - timedelta(days=1)
    p1_start = p1_end - timedelta(days=(weeks_back - 1) * 7)

    return compare_periods(store, p1_start, p1_end, p2_start, p2_end)


def compare_months(store: Any) -> ComparisonReport:
    """Сравнивает текущий месяц с предыдущим.

    Returns:
        ComparisonReport: period1 = прошлый месяц, period2 = текущий месяц.
    """
    today = date.today()
    p2_start = today.replace(day=1)
    p2_end = today

    # Последний день прошлого месяца
    p1_end = p2_start - timedelta(days=1)
    p1_start = p1_end.replace(day=1)

    return compare_periods(store, p1_start, p1_end, p2_start, p2_end)


class PeriodComparisonService:
    """IPC-обёртка над функциями сравнения периодов."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def handle_compare_periods(self, params: dict) -> dict:
        """IPC: сравнить два произвольных периода.

        Параметры:
            period1_start, period1_end, period2_start, period2_end — ISO даты YYYY-MM-DD.
            mode — "custom" | "weeks" | "months" (default "custom").
            weeks_back — int, только для mode=weeks (default 2).
        """
        mode = str(params.get("mode", "custom")).strip()

        if mode == "weeks":
            weeks_back = int(params.get("weeks_back", 2))
            report = compare_weeks(self.store, weeks_back=weeks_back)
        elif mode == "months":
            report = compare_months(self.store)
        else:
            p1_start = params.get("period1_start")
            p1_end = params.get("period1_end")
            p2_start = params.get("period2_start")
            p2_end = params.get("period2_end")
            if not all([p1_start, p1_end, p2_start, p2_end]):
                raise ValueError(
                    "Необходимы period1_start, period1_end, period2_start, period2_end"
                )
            report = compare_periods(self.store, p1_start, p1_end, p2_start, p2_end)

        return _report_to_dict(report)


def _stats_to_dict(s: PeriodStats) -> dict:
    return {
        "recordings": s.recordings,
        "duration_sec": s.duration_sec,
        "words": s.words,
        "avg_confidence": s.avg_confidence,
        "languages": s.languages,
    }


def _report_to_dict(r: ComparisonReport) -> dict:
    return {
        "period1": _stats_to_dict(r.period1),
        "period2": _stats_to_dict(r.period2),
        "recordings_change_pct": r.recordings_change_pct,
        "duration_change_pct": r.duration_change_pct,
        "confidence_change": r.confidence_change,
        "new_languages": r.new_languages,
        "summary": r.summary,
    }
