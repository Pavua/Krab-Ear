"""Сравнение статистики использования Krab Ear по периодам.

Позволяет сравнивать два произвольных периода (week/month/custom) и
генерировать человекочитаемый отчёт об изменениях.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

_SENTINEL = object()


def _finite(value: Any, default: float = 0.0) -> float:
    """Приводит value к конечному float.

    Защита от NaN/Inf в пользовательских данных (confidence/audio_duration_sec
    в истории могут быть NaN/Inf — например после повреждённого NDJSON или
    деления на ноль выше по стеку). Такие значения отравляют агрегаты и делают
    итоговый JSON невалидным (``json.dumps`` по умолчанию пишет ``NaN``/``Infinity``,
    которые не парсятся строгими JSON-декодерами в Swift/браузере).

    Returns:
        float(value), если оно конечно; иначе ``default``.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


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
    old = _finite(old)
    new = _finite(new)
    if old == 0.0:
        return "no_baseline"
    result = (new - old) / old * 100
    # old != 0 гарантирует отсутствие 0/0, но new/old с огромными значениями
    # теоретически может дать inf — финализируем на всякий случай.
    if not math.isfinite(result):
        return "no_baseline"
    return round(result, 2)


def _stats_from_item_dicts(item_dicts: list[dict[str, Any]]) -> PeriodStats:
    """Агрегирует PeriodStats из готового in-memory списка item-словарей.

    NaN/Inf в ``audio_duration_sec`` / ``confidence`` отбрасываются через
    :func:`_finite`, чтобы не отравлять суммы (и не порождать ``NaN`` в JSON).
    """
    recordings = len(item_dicts)
    duration_sec = 0.0
    words = 0
    conf_sum = 0.0
    conf_count = 0
    langs: set[str] = set()

    for item in item_dicts:
        duration_sec += _finite(item.get("audio_duration_sec"))
        text = item.get("text") or ""
        words += len(text.split()) if text.strip() else 0
        conf = item.get("confidence")
        if conf is not None:
            # NaN/Inf confidence пропускаем целиком (не учитываем ни в сумме,
            # ни в счётчике) — иначе среднее становится NaN.
            f_conf = float("nan")
            try:
                f_conf = float(conf)
            except (TypeError, ValueError):
                f_conf = float("nan")
            if math.isfinite(f_conf):
                conf_sum += f_conf
                conf_count += 1
        src_lang = (item.get("source_lang") or "").strip()
        if src_lang:
            langs.add(src_lang)

    avg_conf = round(conf_sum / conf_count, 4) if conf_count > 0 else 0.0

    return PeriodStats(
        recordings=recordings,
        duration_sec=round(_finite(duration_sec), 2),
        words=words,
        avg_confidence=_finite(avg_conf),
        languages=sorted(langs),
    )


def _collect_stats(store: Any, start_iso: str, end_iso: str) -> PeriodStats:
    """Собирает PeriodStats из store за период [start_iso, end_iso].

    Legacy-путь через постраничный ``get_history_page_filtered``. Используется
    только как fallback, когда у store нет ``_load_active_items_with_lock``
    (см. :func:`compare_periods`, который грузит историю ОДИН раз и делит её
    in-memory, избегая квадратичного перечитывания NDJSON).
    """
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

    return _stats_from_item_dicts(all_items)


def _iso_date(d: Any) -> str:
    """Приводит date/datetime/str к ISO-строке YYYY-MM-DD."""
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def _to_item_dict(item: Any) -> dict[str, Any] | None:
    """Нормализует элемент истории в dict.

    ``_load_active_items_with_lock`` возвращает ``HistoryItem`` (с ``.to_dict()``);
    тесты/иные источники могут отдавать уже готовые dict. Возвращает None, если
    объект не приводится к dict (его пропускаем).
    """
    if isinstance(item, dict):
        return item
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
        except Exception:
            return None
        if isinstance(d, dict):
            return d
    return None


def _ts_naive(ts: Any) -> str:
    """Нормализует ISO-timestamp к tz-naive строке для лексикографического сравнения.

    Совпадает с ``StateStore._ts_to_naive_utc_str`` (tz-aware ``+00:00``/``Z``
    приводится к naive), чтобы in-memory фильтрация по диапазону дат давала тот
    же результат, что и постраничный store-путь.
    """
    s = str(ts or "")
    if s.endswith("+00:00"):
        return s[:-6]
    if s.endswith("Z"):
        return s[:-1]
    return s


def _load_all_items_once(store: Any) -> list[dict[str, Any]] | None:
    """Грузит ВСЮ активную историю одним обращением к store.

    Returns:
        list[dict] — нормализованные item-словари, если store поддерживает
        ``_load_active_items_with_lock`` и вернул настоящий ``list``.
        None — если метод отсутствует/недоступен или вернул не-list (напр.
        un-configured ``MagicMock`` в legacy-тестах) → вызывающий код
        откатывается на постраничный путь.
    """
    loader = getattr(store, "_load_active_items_with_lock", None)
    if not callable(loader):
        return None
    try:
        raw = loader()
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for it in raw:
        d = _to_item_dict(it)
        if d is not None:
            out.append(d)
    return out


def _slice_period(
    all_items: list[dict[str, Any]], start_iso: str, end_iso: str
) -> list[dict[str, Any]]:
    """Фильтрует загруженный in-memory список по диапазону [start_iso, end_iso].

    Границы сравниваются лексикографически в tz-naive форме — точно так же, как
    в ``StateStore._matches_filters`` (from_ts/to_ts уже содержат T00:00:00 /
    T23:59:59 суффиксы для дневных границ).
    """
    result: list[dict[str, Any]] = []
    for item in all_items:
        ts = _ts_naive(item.get("ts"))
        if not ts:
            continue
        if ts < start_iso or ts > end_iso:
            continue
        result.append(item)
    return result


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

    Производительность: история активных записей грузится ОДИН раз
    (``_load_active_items_with_lock``), затем делится между периодами in-memory.
    Раньше каждый период листался через ``get_history_page_filtered``, а тот
    перечитывает весь NDJSON на КАЖДЫЙ вызов — для истории > 500 записей это
    давало квадратичное перечитывание диска. Fallback на постраничный путь
    сохранён для store-ов без ``_load_active_items_with_lock``.
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
    p1_start_bound = p1_start + "T00:00:00"
    p2_start_bound = p2_start + "T00:00:00"

    # Загружаем историю один раз; делим in-memory (без квадратичного reload).
    all_items = _load_all_items_once(store)
    if all_items is not None:
        stats1 = _stats_from_item_dicts(
            _slice_period(all_items, p1_start_bound, p1_end)
        )
        stats2 = _stats_from_item_dicts(
            _slice_period(all_items, p2_start_bound, p2_end)
        )
    else:
        # Legacy fallback: постраничный store-путь (по одному периоду за раз).
        stats1 = _collect_stats(store, p1_start, p1_end)
        stats2 = _collect_stats(store, p2_start, p2_end)

    rec_change = _pct_change(stats1.recordings, stats2.recordings)
    dur_change = _pct_change(stats1.duration_sec, stats2.duration_sec)
    conf_change = round(
        _finite(stats2.avg_confidence) - _finite(stats1.avg_confidence), 4
    )

    langs1 = set(stats1.languages)
    langs2 = set(stats2.languages)
    new_langs = sorted(langs2 - langs1)

    # Генерируем summary
    lines = []

    def _fmt_pct(val: "float | str") -> str:
        if not isinstance(val, (int, float)):
            return "нет базы"
        sign = "+" if val >= 0 else ""
        return f"{sign}{val:.1f}%"

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
        confidence_change=_finite(conf_change),
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
            mode — "explicit" | "custom" | "weeks" | "months" (default "explicit").
                "explicit" и "custom" эквивалентны — задают явные даты (backward compat).
            weeks_back — int, только для mode=weeks (default 2).
        """
        mode = str(params.get("mode", "explicit")).strip()

        if mode == "weeks":
            weeks_back = int(params.get("weeks_back", 2))
            report = compare_weeks(self.store, weeks_back=weeks_back)
        elif mode == "months":
            report = compare_months(self.store)
        else:
            # "explicit", "custom", or any unrecognised value → explicit-date mode
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
        "duration_sec": _finite(s.duration_sec),
        "words": s.words,
        "avg_confidence": _finite(s.avg_confidence),
        "languages": s.languages,
    }


def _report_to_dict(r: ComparisonReport) -> dict:
    rec_change = r.recordings_change_pct
    dur_change = r.duration_change_pct
    return {
        "period1": _stats_to_dict(r.period1),
        "period2": _stats_to_dict(r.period2),
        # pct-поля могут быть str ("no_baseline") — финализируем только числа.
        "recordings_change_pct": _finite(rec_change) if isinstance(rec_change, (int, float)) else rec_change,
        "duration_change_pct": _finite(dur_change) if isinstance(dur_change, (int, float)) else dur_change,
        "confidence_change": _finite(r.confidence_change),
        "new_languages": r.new_languages,
        "summary": r.summary,
    }
