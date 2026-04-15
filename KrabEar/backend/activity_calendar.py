"""ActivityCalendar — GitHub-style contribution graph данные для Krab Ear.

Агрегирует историю транскрипций по дням, вычисляет уровни активности (0–4),
строит календарную сетку и опционально рендерит SVG.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import logging

logger = logging.getLogger("KrabEar.Backend.ActivityCalendar")


# ---------------------------------------------------------------------------
# Dataclass-ы результата
# ---------------------------------------------------------------------------

@dataclass
class DayActivity:
    """Активность за один день."""

    date: str
    """Дата в формате YYYY-MM-DD."""

    recordings: int = 0
    """Количество записей за день."""

    duration_min: float = 0.0
    """Суммарная длительность аудио в минутах."""

    words: int = 0
    """Суммарное количество слов в транскрипциях."""

    level: int = 0
    """Уровень активности 0–4 (аналог GitHub contribution levels)."""

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "recordings": self.recordings,
            "duration_min": round(self.duration_min, 2),
            "words": self.words,
            "level": self.level,
        }


@dataclass
class CalendarData:
    """Данные activity calendar за период."""

    days: dict[str, DayActivity] = field(default_factory=dict)
    """Словарь date → DayActivity для всех дней периода."""

    weeks: list[list[DayActivity | None]] = field(default_factory=list)
    """Сетка 7 строк × N столбцов (строки = дни недели пн–вс, столбцы = недели).
    Ячейки вне диапазона = None."""

    total_active_days: int = 0
    """Количество дней с хотя бы одной записью."""

    longest_streak: int = 0
    """Самая длинная непрерывная серия активных дней."""

    current_streak: int = 0
    """Текущая непрерывная серия активных дней (начиная с сегодня или вчера)."""

    def to_dict(self) -> dict:
        return {
            "days": {d: v.to_dict() for d, v in self.days.items()},
            "weeks": [
                [cell.to_dict() if cell is not None else None for cell in row]
                for row in self.weeks
            ],
            "total_active_days": self.total_active_days,
            "longest_streak": self.longest_streak,
            "current_streak": self.current_streak,
        }


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _count_words(text: str) -> int:
    """Считает слова в тексте (по пробелам/пунктуации)."""
    if not text:
        return 0
    return len(re.findall(r"\S+", text))


def _parse_ts(ts: Any) -> date | None:
    """Конвертирует поле ts (ISO-строка или epoch float) в date или None."""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
        s = str(ts).strip()
        if not s:
            return None
        # Нормализуем и парсим ISO-формат через fromisoformat (Python 3.7+)
        normalized = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
            return dt.date()
        except ValueError:
            pass
        # Fallback: только дата YYYY-MM-DD
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            pass
        return None
    except Exception:
        return None


def _compute_level(recordings: int, thresholds: tuple[int, int, int, int]) -> int:
    """Вычисляет уровень активности 0–4 по числу записей."""
    if recordings == 0:
        return 0
    t1, t2, t3, t4 = thresholds
    if recordings >= t4:
        return 4
    if recordings >= t3:
        return 3
    if recordings >= t2:
        return 2
    if recordings >= t1:
        return 1
    return 1  # recordings > 0 → минимум 1


def _compute_thresholds(max_recordings: int) -> tuple[int, int, int, int]:
    """Вычисляет пороги уровней 1–4 на основе максимума за период."""
    if max_recordings <= 0:
        return (1, 3, 6, 10)
    t1 = 1
    t2 = max(2, max_recordings // 4)
    t3 = max(3, max_recordings // 2)
    t4 = max(4, max_recordings * 3 // 4)
    return (t1, t2, t3, t4)


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class ActivityCalendar:
    """Генератор GitHub-style activity calendar данных."""

    def generate_calendar(self, items: list, months: int = 12) -> CalendarData:
        """Строит CalendarData из списка элементов истории.

        Args:
            items: список объектов/словарей с полями ``ts``, ``text``,
                   ``audio_duration_sec`` (опционально).
            months: количество последних месяцев для охвата.

        Returns:
            CalendarData с заполненными days, weeks, стриками.
        """
        today = date.today()
        # Начало периода: первый день недели (понедельник) N месяцев назад
        period_start = today - timedelta(days=months * 30)
        # Выравниваем начало на понедельник
        period_start = period_start - timedelta(days=period_start.weekday())

        # --- Агрегация по дням ---
        daily: dict[str, DayActivity] = {}

        # Заполняем все дни периода пустыми объектами
        current = period_start
        while current <= today:
            date_key = current.isoformat()
            daily[date_key] = DayActivity(date=date_key)
            current += timedelta(days=1)

        for item in items:
            # Поддержка и dict, и объектов-атрибутов
            if isinstance(item, dict):
                ts = item.get("ts")
                text = item.get("text", "") or ""
                duration_sec = item.get("audio_duration_sec") or 0.0
            else:
                ts = getattr(item, "ts", None)
                text = getattr(item, "text", "") or ""
                duration_sec = getattr(item, "audio_duration_sec", None) or 0.0

            day = _parse_ts(ts)
            if day is None:
                continue
            date_key = day.isoformat()
            if date_key not in daily:
                continue  # вне окна периода

            da = daily[date_key]
            da.recordings += 1
            da.duration_min += float(duration_sec) / 60.0
            da.words += _count_words(text)

        # --- Вычисление уровней ---
        max_rec = max((da.recordings for da in daily.values()), default=0)
        thresholds = _compute_thresholds(max_rec)
        for da in daily.values():
            da.level = _compute_level(da.recordings, thresholds)

        # --- Стрики ---
        sorted_dates = sorted(daily.keys())
        total_active_days = sum(1 for da in daily.values() if da.recordings > 0)

        # longest_streak
        longest_streak = 0
        current_run = 0
        for dk in sorted_dates:
            if daily[dk].recordings > 0:
                current_run += 1
                longest_streak = max(longest_streak, current_run)
            else:
                current_run = 0

        # current_streak: считаем с сегодня назад
        current_streak = 0
        today_key = today.isoformat()
        yesterday_key = (today - timedelta(days=1)).isoformat()

        # Стрик считаем только если сегодня или вчера была активность
        if today_key in daily and daily[today_key].recordings > 0:
            start_day = today
        elif yesterday_key in daily and daily[yesterday_key].recordings > 0:
            start_day = today - timedelta(days=1)
        else:
            start_day = None

        if start_day is not None:
            check_day = start_day
            while True:
                dk = check_day.isoformat()
                if dk in daily and daily[dk].recordings > 0:
                    current_streak += 1
                    check_day -= timedelta(days=1)
                else:
                    break

        # --- Сетка weeks: 7 строк (пн=0 … вс=6), N столбцов ---
        # Столбцы = недели; строки = день недели
        weeks = self._build_weeks_grid(daily, period_start, today)

        return CalendarData(
            days=daily,
            weeks=weeks,
            total_active_days=total_active_days,
            longest_streak=longest_streak,
            current_streak=current_streak,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _build_weeks_grid(
        self,
        daily: dict[str, DayActivity],
        period_start: date,
        period_end: date,
    ) -> list[list[DayActivity | None]]:
        """Строит сетку 7×N для отображения calendar view.

        Возвращает список из 7 строк (пн–вс), каждая из N ячеек (недель).
        Ячейки вне диапазона дат = None.
        """
        # Число столбцов-недель
        total_days = (period_end - period_start).days + 1
        num_weeks = (total_days + 6) // 7 + 1

        # rows[weekday][week_index] = DayActivity | None
        rows: list[list[DayActivity | None]] = [[None] * num_weeks for _ in range(7)]

        current = period_start
        week_idx = 0
        while current <= period_end:
            weekday = current.weekday()  # 0=пн, 6=вс
            date_key = current.isoformat()
            rows[weekday][week_idx] = daily.get(date_key)
            # Переходим к следующей неделе в воскресенье
            if weekday == 6:
                week_idx += 1
            current += timedelta(days=1)

        # Обрезаем лишние None-колонки справа
        # Находим реальный последний используемый week_idx
        max_week = 0
        for row in rows:
            for i in range(len(row) - 1, -1, -1):
                if row[i] is not None:
                    max_week = max(max_week, i)
                    break

        trimmed = [row[:max_week + 1] for row in rows]
        return trimmed

    def generate_calendar_svg(
        self,
        items: list,
        months: int = 3,
        cell_size: int = 12,
    ) -> str:
        """Рендерит activity calendar в виде SVG строки.

        Args:
            items: список элементов истории.
            months: количество месяцев.
            cell_size: размер ячейки в пикселях (default 12).

        Returns:
            Строка SVG (inline, без DOCTYPE).
        """
        calendar = self.generate_calendar(items, months=months)
        weeks_grid = calendar.weeks  # list[7 rows] × N cols

        if not weeks_grid or not weeks_grid[0]:
            return '<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0"></svg>'

        num_cols = len(weeks_grid[0])
        num_rows = 7

        gap = 2
        cell_total = cell_size + gap
        pad_top = 20  # место для меток месяцев
        pad_left = 30  # место для меток дней недели

        svg_width = pad_left + num_cols * cell_total
        svg_height = pad_top + num_rows * cell_total

        # Цвета уровней (GitHub dark-style)
        colors = {
            0: "#161b22",
            1: "#0e4429",
            2: "#006d32",
            3: "#26a641",
            4: "#39d353",
        }
        bg_color = "#0d1117"
        text_color = "#8b949e"

        cells_svg: list[str] = []

        # Метки дней недели
        day_labels = ["Mo", "", "We", "", "Fr", "", "Su"]
        for row_idx, label in enumerate(day_labels):
            if label:
                y = pad_top + row_idx * cell_total + cell_size // 2 + 4
                cells_svg.append(
                    f'<text x="{pad_left - gap - 2}" y="{y}" '
                    f'font-size="9" fill="{text_color}" '
                    f'text-anchor="end" font-family="monospace">{label}</text>'
                )

        # Метки месяцев
        prev_month = None
        for col_idx in range(num_cols):
            # Берём первую непустую ячейку в колонке
            for row_idx in range(num_rows):
                cell = weeks_grid[row_idx][col_idx] if col_idx < len(weeks_grid[row_idx]) else None
                if cell is not None:
                    try:
                        d = date.fromisoformat(cell.date)
                        if d.month != prev_month:
                            prev_month = d.month
                            x = pad_left + col_idx * cell_total
                            month_name = d.strftime("%b")
                            cells_svg.append(
                                f'<text x="{x}" y="{pad_top - 4}" '
                                f'font-size="9" fill="{text_color}" '
                                f'font-family="monospace">{month_name}</text>'
                            )
                    except Exception:
                        pass
                    break

        # Ячейки
        for row_idx in range(num_rows):
            for col_idx, cell in enumerate(weeks_grid[row_idx]):
                x = pad_left + col_idx * cell_total
                y = pad_top + row_idx * cell_total
                level = cell.level if cell is not None else 0
                fill = colors.get(level, colors[0])
                title = ""
                if cell is not None:
                    title = (
                        f'<title>{cell.date}: {cell.recordings} recordings, '
                        f'{cell.words} words</title>'
                    )
                cells_svg.append(
                    f'<rect x="{x}" y="{y}" width="{cell_size}" height="{cell_size}" '
                    f'rx="2" ry="2" fill="{fill}">{title}</rect>'
                )

        svg_body = "\n".join(cells_svg)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{svg_width}" height="{svg_height}" '
            f'style="background:{bg_color}">\n'
            f'{svg_body}\n'
            f'</svg>'
        )
