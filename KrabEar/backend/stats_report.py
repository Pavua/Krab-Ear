"""StatsReportGenerator — генератор комплексного Markdown-отчёта статистики Krab Ear.

Собирает все основные метрики использования за указанный период и форматирует
их в читаемый Markdown-документ с ASCII-графиками.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import logging

logger = logging.getLogger("KrabEar.Backend.StatsReport")

# ---------------------------------------------------------------------------
# Стоп-слова для топ-слов (RU + ES + EN)
# ---------------------------------------------------------------------------
_STOP_WORDS: frozenset = frozenset({
    # RU
    "в", "на", "с", "по", "из", "от", "до", "за", "под", "над", "к", "о",
    "об", "про", "при", "для", "без", "через", "между", "перед", "после",
    "во", "со", "ко", "не", "ни", "бы", "же", "ли", "и", "а", "но", "да",
    "то", "или", "что", "как", "так", "уже", "ещё", "еще", "все", "этот",
    "это", "эта", "этой", "этого", "этим", "этих", "он", "она", "оно", "они",
    "мы", "вы", "я", "его", "её", "ее", "их", "мой", "твой", "наш", "ваш",
    "свой", "себя", "тот", "та", "те", "такой", "такие", "быть", "есть",
    "был", "была", "были", "будет", "будут", "там", "здесь", "тут", "где",
    "когда", "потому", "потом", "затем", "вот", "ну", "вдруг", "если", "нет",
    "очень", "более", "менее", "больше", "меньше", "можно", "нужно", "надо",
    # ES
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "al", "en", "con", "por", "para", "sin", "sobre", "entre", "ante",
    "bajo", "desde", "hasta", "hacia", "durante", "y", "e", "o", "u",
    "pero", "sino", "que", "como", "si", "se", "me", "te", "le", "nos",
    "os", "les", "lo", "su", "sus", "mi", "mis", "tu", "tus", "este",
    "esta", "estos", "estas", "ese", "esa", "esos", "esas", "yo",
    "él", "ella", "ellos", "ellas", "usted", "ustedes", "nosotros",
    "vosotros", "es", "son", "era", "fue", "ser", "estar", "hay", "ya",
    "no", "más", "muy", "bien", "también", "sí", "así", "todo", "todos",
    # EN
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "by",
    "from", "up", "about", "into", "through", "during", "before", "after",
    "above", "below", "between", "out", "off", "over", "under", "again",
    "and", "but", "or", "nor", "so", "yet", "both", "either", "neither",
    "not", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "can", "must", "it", "its", "this", "that",
    "these", "those", "i", "you", "he", "she", "we", "they", "me", "him",
    "her", "us", "them", "my", "your", "his", "our", "their", "what",
    "which", "who", "when", "where", "how", "all", "each", "more", "also",
})

_ASCII_BAR_CHARS = "█"
_ASCII_BAR_WIDTH = 20


def _tokenize(text: str) -> list[str]:
    """Разбивает текст на слова (нижний регистр, только буквы)."""
    return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)


def _get_attr(item: Any, key: str, default: Any = None) -> Any:
    """Извлекает атрибут из объекта или словаря."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _get_ts(item: Any) -> datetime | None:
    """Извлекает timestamp как timezone-aware datetime."""
    raw = _get_attr(item, "ts")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def _ascii_bar(value: float, max_value: float, width: int = _ASCII_BAR_WIDTH) -> str:
    """Строит ASCII-гистограмму заданной ширины."""
    if max_value <= 0:
        return ""
    filled = max(0, min(width, round(value / max_value * width)))
    return _ASCII_BAR_CHARS * filled


# Ведущие символы, провоцирующие формульную инъекцию при открытии .md в Excel/
# Numbers/LibreOffice Calc (ячейка трактуется как формула).
_FORMULA_LEAD_CHARS: frozenset = frozenset({"=", "+", "-", "@"})


def _md_cell(value: Any) -> str:
    """Нейтрализует пользовательский текст перед вставкой в ячейку Markdown.

    Защищает от четырёх векторов инъекции в отчёте (теги, имена коллекций,
    метки спикеров, заголовки):

    1. Слом Markdown-таблицы: ``|`` экранируется как ``\\|``.
    2. Слом структуры строки / вставка лишних строк: CR/LF (и прочие
       управляющие переводы) сворачиваются в один пробел.
    3. Формульная инъекция (CSV/spreadsheet): если ячейка начинается с
       ``= + - @`` (после необязательных пробелов), перед ней ставится
       апостроф ``'`` — нейтральный префикс, который Excel/Numbers/Calc не
       интерпретируют как формулу.
    4. Слом inline code span: обратный апостроф (U+0060) заменяется на
       MODIFIER LETTER GRAVE ACCENT (U+02CB), чтобы бэктик в значении
       не закрывал обёртывающий `...` span и не вставлял сырой Markdown/HTML.

    Args:
        value: Любое значение пользовательского происхождения.

    Returns:
        Безопасная для вставки в Markdown-ячейку строка.
    """
    s = "" if value is None else str(value)
    # CR/LF и прочие управляющие переводы строк → один пробел (схлопываем серии).
    s = re.sub(r"[\r\n\x0b\x0c]+", " ", s)
    # Экранируем разделитель таблицы.
    s = s.replace("|", "\\|")
    # Нейтрализуем бэктик: заменяем на визуально близкий MODIFIER LETTER GRAVE (U+02CB).
    # Это defence-in-depth для любого кода, оборачивающего _md_cell(...) в `...`.
    s = s.replace("`", "ˋ")
    # Нейтрализуем формульную инъекцию: смотрим на первый непробельный символ.
    stripped = s.lstrip()
    if stripped and stripped[0] in _FORMULA_LEAD_CHARS:
        s = "'" + s
    return s


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------


class StatsReportGenerator:
    """Генератор комплексных Markdown-отчётов статистики Krab Ear."""

    def generate_report(self, store: Any, days: int = 30) -> str:
        """Генерирует полный Markdown-отчёт статистики за указанный период.

        Args:
            store: Экземпляр StateStore (или совместимый объект).
            days:  Глубина анализа в днях (по умолчанию 30).

        Returns:
            Многострочная строка Markdown.
        """
        # Ограничиваем глубину анализа: верхняя граница 3650 дней (10 лет)
        # защищает от DoS — огромный ``days`` строил бы многомиллионный список
        # дат (память/CPU) и мог вызвать OverflowError в timedelta/strftime.
        # Нечисловой/битый ввод деградирует к значению по умолчанию (30).
        try:
            days = int(days)
        except (TypeError, ValueError, OverflowError):
            days = 30
        days = max(1, min(days, 3650))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Загружаем активные записи
        items = self._load_items(store)

        # Фильтруем по периоду
        period_items = [
            item for item in items
            if (ts := _get_ts(item)) is not None and ts >= cutoff
        ]

        sections: list[str] = []

        # Заголовок
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        sections.append("# Krab Ear — Статистический отчёт")
        sections.append("")
        sections.append(f"**Период:** последние {days} дней  ")
        sections.append(f"**Сгенерирован:** {now_str}  ")
        sections.append(f"**Всего записей в истории:** {len(items)}")
        sections.append("")

        # 1. Обзор
        sections.append(self._section_overview(period_items, days))

        # 2. Дневная активность (ASCII bar chart)
        sections.append(self._section_daily_activity(period_items, days, cutoff))

        # 3. Распределение языков
        sections.append(self._section_language_distribution(period_items))

        # 4. Метрики качества (тренд confidence)
        sections.append(self._section_quality_metrics(period_items))

        # 5. Топ спикеров (если есть диаризация)
        sections.append(self._section_top_speakers(period_items))

        # 6. Теги и коллекции
        sections.append(self._section_tags_collections(period_items, store))

        # 7. Использование хранилища
        sections.append(self._section_storage(store))

        # 8. Системное здоровье
        sections.append(self._section_system_health(store, items, period_items, days))

        return "\n".join(sections)

    def generate_mini_report(self, store: Any) -> str:
        """Генерирует краткий 5-строчный отчёт состояния.

        Args:
            store: Экземпляр StateStore (или совместимый объект).

        Returns:
            5-строчная Markdown-строка.
        """
        items = self._load_items(store)
        cutoff_30 = datetime.now(timezone.utc) - timedelta(days=30)
        recent = [
            item for item in items
            if (ts := _get_ts(item)) is not None and ts >= cutoff_30
        ]

        total_recordings = len(recent)
        total_words = sum(len((_get_attr(item, "text") or "").split()) for item in recent)
        total_hours = sum(
            float(_get_attr(item, "audio_duration_sec") or 0.0) for item in recent
        ) / 3600.0

        confidences = [
            float(c) for item in recent
            if (c := _get_attr(item, "confidence")) is not None
        ]
        avg_conf = (sum(confidences) / len(confidences)) if confidences else 0.0

        storage_mb = self._get_storage_mb(store)

        lines = [
            f"**Записей за 30 дней:** {total_recordings}",
            f"**Слов транскрибировано:** {total_words:,}",
            f"**Часов аудио:** {total_hours:.1f}",
            f"**Средняя уверенность:** {avg_conf:.1%}",
            f"**Размер истории:** {storage_mb:.2f} MB",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Секции отчёта
    # ------------------------------------------------------------------

    def _section_overview(self, items: list, days: int) -> str:
        """Секция «Обзор»."""
        total = len(items)
        total_words = sum(len((_get_attr(item, "text") or "").split()) for item in items)
        total_duration_sec = sum(
            float(_get_attr(item, "audio_duration_sec") or 0.0) for item in items
        )
        total_hours = total_duration_sec / 3600.0
        total_minutes = total_duration_sec / 60.0

        avg_words = (total_words / total) if total else 0
        avg_duration_min = (total_minutes / total) if total else 0

        with_llm = sum(1 for item in items if _get_attr(item, "llm_applied", False))
        with_translation = sum(
            1 for item in items
            if _get_attr(item, "translation_mode", "off") not in ("off", "", None)
        )
        favorites = sum(1 for item in items if _get_attr(item, "favorite", False))

        lines: list[str] = []
        lines.append("## 1. Обзор")
        lines.append("")
        lines.append("| Метрика | Значение |")
        lines.append("|---------|----------|")
        lines.append(f"| Записей за период | **{total}** |")
        lines.append(f"| Всего слов | **{total_words:,}** |")
        lines.append(f"| Суммарно часов аудио | **{total_hours:.2f} ч** |")
        lines.append(f"| Суммарно минут аудио | **{total_minutes:.1f} мин** |")
        lines.append(f"| Среднее слов/запись | **{avg_words:.0f}** |")
        lines.append(f"| Средняя длительность | **{avg_duration_min:.1f} мин** |")
        lines.append(f"| Записей с переводом | **{with_translation}** |")
        lines.append(f"| Обработано LLM | **{with_llm}** |")
        lines.append(f"| В избранном | **{favorites}** |")
        lines.append("")
        return "\n".join(lines)

    def _section_daily_activity(
        self, items: list, days: int, cutoff: datetime
    ) -> str:
        """Секция «Дневная активность» с ASCII bar chart."""
        # Считаем количество записей по дням
        daily_count: dict[str, int] = {}
        for item in items:
            ts = _get_ts(item)
            if ts is None:
                continue
            d = ts.date().isoformat()
            daily_count[d] = daily_count.get(d, 0) + 1

        # Создаём непрерывный список дат за период
        today = date.today()
        start_date = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
        all_dates: list[str] = []
        cur = start_date
        while cur <= today:
            all_dates.append(cur.isoformat())
            cur += timedelta(days=1)

        if not daily_count:
            lines = ["## 2. Дневная активность", "", "_Нет данных за период._", ""]
            return "\n".join(lines)

        max_count = max(daily_count.values()) if daily_count else 1

        lines: list[str] = []
        lines.append("## 2. Дневная активность")
        lines.append("")
        lines.append("```")

        # Показываем только последние 30 дней (не больше), для читаемости
        display_dates = all_dates[-30:]
        for d in display_dates:
            count = daily_count.get(d, 0)
            bar = _ascii_bar(count, max_count, width=20)
            # Краткая дата MM-DD
            short_d = d[5:]  # YYYY-MM-DD → MM-DD
            lines.append(f"{short_d} | {bar:<20} | {count:3d}")

        lines.append("```")
        lines.append("")

        # Сводка по активности
        active_days = len([d for d in display_dates if daily_count.get(d, 0) > 0])
        busiest_day = max(daily_count, key=lambda d: daily_count[d]) if daily_count else ""
        lines.append(
            f"- **Активных дней:** {active_days} из {len(display_dates)}"
        )
        if busiest_day:
            lines.append(
                f"- **Самый активный день:** {busiest_day} ({daily_count[busiest_day]} записей)"
            )
        lines.append("")
        return "\n".join(lines)

    def _section_language_distribution(self, items: list) -> str:
        """Секция «Распределение языков»."""
        lang_counter: Counter = Counter()
        for item in items:
            lang = (_get_attr(item, "source_lang") or "").strip()
            if lang:
                lang_counter[lang] += 1
            else:
                lang_counter["unknown"] += 1

        lines: list[str] = []
        lines.append("## 3. Распределение языков")
        lines.append("")

        if not lang_counter or all(k == "unknown" for k in lang_counter):
            lines.append("_Данные о языках недоступны._")
            lines.append("")
            return "\n".join(lines)

        total = sum(lang_counter.values())
        max_count = max(lang_counter.values()) if lang_counter else 1

        lines.append("```")
        for lang, count in lang_counter.most_common():
            if lang == "unknown":
                continue
            # Валидируем lang как ISO-639-ish токен перед вставкой внутрь fenced
            # code block.  Значение source_lang поступает из NDJSON-истории без
            # дополнительной санитизации: вредоносная строка вида "\n```\n## X"
            # закрыла бы fence и инжектировала заголовок/HTML (MED, wave-19).
            if not re.fullmatch(r"[A-Za-z]{2,8}(-[A-Za-z0-9]+)?", lang):
                lang = "unknown"
            if lang == "unknown":
                continue
            pct = count / total * 100
            bar = _ascii_bar(count, max_count, width=15)
            lines.append(f"{lang:>8} | {bar:<15} | {count:4d} ({pct:.1f}%)")
        lines.append("```")
        lines.append("")
        return "\n".join(lines)

    def _section_quality_metrics(self, items: list) -> str:
        """Секция «Метрики качества» — тренд confidence по неделям."""
        confidences = [
            float(c) for item in items
            if (c := _get_attr(item, "confidence")) is not None
        ]

        lines: list[str] = []
        lines.append("## 4. Метрики качества")
        lines.append("")

        if not confidences:
            lines.append("_Данные о confidence отсутствуют._")
            lines.append("")
            return "\n".join(lines)

        avg_conf = sum(confidences) / len(confidences)
        min_conf = min(confidences)
        max_conf = max(confidences)

        # Распределение по бакетам
        buckets = [
            ("Отлично (0.9–1.0)", 0.9, 1.01),
            ("Хорошо  (0.8–0.9)", 0.8, 0.9),
            ("Средне  (0.7–0.8)", 0.7, 0.8),
            ("Плохо   (0.6–0.7)", 0.6, 0.7),
            ("Низкое  (0.0–0.6)", 0.0, 0.6),
        ]
        bucket_counts: dict[str, int] = {label: 0 for label, _, _ in buckets}
        for c in confidences:
            for label, lo, hi in buckets:
                if lo <= c < hi:
                    bucket_counts[label] += 1
                    break

        total = len(confidences)
        max_b = max(bucket_counts.values()) if bucket_counts else 1

        lines.append(f"- **Средний confidence:** {avg_conf:.1%}")
        lines.append(f"- **Мин / Макс:** {min_conf:.1%} / {max_conf:.1%}")
        lines.append(f"- **Записей с оценкой:** {total}")
        lines.append("")
        lines.append("**Распределение качества:**")
        lines.append("")
        lines.append("```")
        for label, count in bucket_counts.items():
            bar = _ascii_bar(count, max_b, width=15)
            pct = count / total * 100 if total else 0
            lines.append(f"{label} | {bar:<15} | {count:4d} ({pct:.1f}%)")
        lines.append("```")
        lines.append("")

        # Недельный тренд confidence
        weekly: dict[str, list[float]] = defaultdict(list)
        for item in items:
            c = _get_attr(item, "confidence")
            if c is None:
                continue
            ts = _get_ts(item)
            if ts is None:
                continue
            week_key = ts.strftime("%Y-W%W")
            weekly[week_key].append(float(c))

        if len(weekly) >= 2:
            lines.append("**Недельный тренд:**")
            lines.append("")
            lines.append("```")
            sorted_weeks = sorted(weekly.keys())
            for wk in sorted_weeks:
                vals = weekly[wk]
                avg = sum(vals) / len(vals)
                bar = _ascii_bar(avg, 1.0, width=15)
                lines.append(f"{wk} | {bar:<15} | avg={avg:.1%} n={len(vals)}")
            lines.append("```")
            lines.append("")

        return "\n".join(lines)

    def _section_top_speakers(self, items: list) -> str:
        """Секция «Топ спикеров» (только при наличии диаризации)."""
        speaker_duration: dict[str, float] = defaultdict(float)
        speaker_turns: dict[str, int] = defaultdict(int)

        for item in items:
            diarization = _get_attr(item, "diarization")
            if not isinstance(diarization, dict):
                continue
            segments = diarization.get("segments") or diarization.get("annotated_segments") or []
            if not isinstance(segments, list):
                continue
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                speaker = seg.get("speaker") or seg.get("label") or ""
                if not speaker:
                    continue
                start = float(seg.get("start", 0.0))
                end = float(seg.get("end", 0.0))
                dur = max(0.0, end - start)
                speaker_duration[speaker] += dur
                speaker_turns[speaker] += 1

        lines: list[str] = []
        lines.append("## 5. Топ спикеров (диаризация)")
        lines.append("")

        if not speaker_duration:
            lines.append("_Данные диаризации отсутствуют._")
            lines.append("")
            return "\n".join(lines)

        sorted_speakers = sorted(speaker_duration.items(), key=lambda x: -x[1])
        total_dur = sum(speaker_duration.values())
        sorted_speakers[0][1] if sorted_speakers else 1.0

        lines.append("| Спикер | Время (мин) | Доля | Реплик |")
        lines.append("|--------|-------------|------|--------|")
        for speaker, dur_sec in sorted_speakers[:10]:
            dur_min = dur_sec / 60.0
            pct = dur_sec / total_dur * 100 if total_dur > 0 else 0
            turns = speaker_turns[speaker]
            # Метка спикера — пользовательского/диаризационного происхождения,
            # экранируем перед вставкой в Markdown-таблицу.
            lines.append(f"| {_md_cell(speaker)} | {dur_min:.1f} | {pct:.1f}% | {turns} |")
        lines.append("")
        return "\n".join(lines)

    def _section_tags_collections(self, items: list, store: Any) -> str:
        """Секция «Теги и коллекции»."""
        tag_counter: Counter = Counter()
        for item in items:
            tags = _get_attr(item, "tags") or []
            if isinstance(tags, list):
                for tag in tags:
                    if tag:
                        tag_counter[str(tag)] += 1

        lines: list[str] = []
        lines.append("## 6. Теги и коллекции")
        lines.append("")

        # Теги
        if tag_counter:
            lines.append("**Топ тегов:**")
            lines.append("")
            for tag, count in tag_counter.most_common(10):
                # Тег задаётся пользователем: экранируем разделители/переводы строк
                # и нейтрализуем формульный префикс перед вставкой в Markdown.
                # Не оборачиваем в `...` — бэктик в теге закрыл бы code span
                # и позволил бы вставить сырой Markdown/HTML (MED injection, wave-19).
                lines.append(f"- {_md_cell(tag)} — {count}")
            lines.append("")
        else:
            lines.append("_Теги не использовались._")
            lines.append("")

        # Коллекции (если доступны через store)
        collections_path = self._get_collections_path(store)
        if collections_path is not None and collections_path.exists():
            try:
                import json as _json
                raw = _json.loads(collections_path.read_text(encoding="utf-8"))
                colls = raw.get("collections", {})
                if colls:
                    lines.append("**Коллекции:**")
                    lines.append("")
                    lines.append("| Название | Записей |")
                    lines.append("|----------|---------|")
                    for cname, cdata in colls.items():
                        item_count = len(cdata.get("item_ids", []))
                        # Имя коллекции задаётся пользователем: экранируем перед
                        # вставкой в Markdown-таблицу (защита от слома таблицы /
                        # формульной инъекции в .md, открытом как таблица).
                        lines.append(f"| {_md_cell(cname)} | {item_count} |")
                    lines.append("")
            except Exception:
                pass

        return "\n".join(lines)

    def _section_storage(self, store: Any) -> str:
        """Секция «Использование хранилища»."""
        lines: list[str] = []
        lines.append("## 7. Хранилище")
        lines.append("")

        try:
            data_dir = Path(getattr(store, "data_dir", "."))
            files_info: list[tuple[str, float]] = []

            tracked_files = [
                ("history.ndjson", getattr(store, "history_path", None)),
                ("tombstones", getattr(store, "tombstones_path", None)),
                ("status", getattr(store, "status_path", None)),
                ("tags", getattr(store, "tags_path", None)),
                ("settings.json", getattr(store, "settings_path", None)),
            ]
            for label, path in tracked_files:
                if path is not None and Path(path).exists():
                    size_kb = Path(path).stat().st_size / 1024
                    files_info.append((label, size_kb))

            # JSON-файлы данных (коллекции, алиасы и т.д.)
            for f in data_dir.glob("*.json"):
                if f.is_file() and f.name != "settings.json":
                    size_kb = f.stat().st_size / 1024
                    files_info.append((f.name, size_kb))

            total_kb = sum(sz for _, sz in files_info)

            backups_dir = data_dir / "backups"
            backups_count = len(list(backups_dir.glob("*.ndjson"))) if backups_dir.exists() else 0
            backups_size_kb = sum(
                f.stat().st_size / 1024
                for f in backups_dir.glob("*")
                if f.is_file()
            ) if backups_dir.exists() else 0.0

            lines.append("| Файл | Размер |")
            lines.append("|------|--------|")
            for label, size_kb in sorted(files_info, key=lambda x: -x[1])[:10]:
                lines.append(f"| {label} | {size_kb:.1f} KB |")
            lines.append(f"| **Итого (основные файлы)** | **{total_kb:.1f} KB** |")
            lines.append(f"| Бэкапов | {backups_count} файлов ({backups_size_kb:.1f} KB) |")
            lines.append("")
        except Exception as exc:
            logger.warning("Не удалось собрать storage info: %s", exc)
            lines.append("_Информация о хранилище недоступна._")
            lines.append("")

        return "\n".join(lines)

    def _section_system_health(
        self,
        store: Any,
        all_items: list,
        period_items: list,
        days: int,
    ) -> str:
        """Секция «Системное здоровье» — краткое резюме."""
        lines: list[str] = []
        lines.append("## 8. Системное здоровье")
        lines.append("")

        # Записи с успешной вставкой
        total = len(period_items)
        pasted_ok = sum(
            1 for item in period_items
            if _get_attr(item, "paste_status", "") == "ok"
        )
        paste_rate = pasted_ok / total * 100 if total else 0

        # Записи с LLM
        llm_count = sum(1 for item in period_items if _get_attr(item, "llm_applied", False))
        llm_rate = llm_count / total * 100 if total else 0

        # Среднее число слов
        words_per_item = [
            len((_get_attr(item, "text") or "").split()) for item in period_items
        ]
        avg_words = sum(words_per_item) / len(words_per_item) if words_per_item else 0
        max_words = max(words_per_item) if words_per_item else 0

        # Записи за последние 7 дней
        cutoff_7 = datetime.now(timezone.utc) - timedelta(days=7)
        last_7 = sum(
            1 for item in period_items
            if (ts := _get_ts(item)) is not None and ts >= cutoff_7
        )

        # Confidences
        confidences = [
            float(c) for item in period_items
            if (c := _get_attr(item, "confidence")) is not None
        ]
        high_quality = sum(1 for c in confidences if c >= 0.9)
        hq_rate = high_quality / len(confidences) * 100 if confidences else 0

        lines.append("| Показатель | Значение |")
        lines.append("|------------|----------|")
        lines.append(f"| Успешных вставок | {pasted_ok}/{total} ({paste_rate:.0f}%) |")
        lines.append(f"| Обработано LLM | {llm_count}/{total} ({llm_rate:.0f}%) |")
        lines.append(f"| Среднее слов/запись | {avg_words:.0f} |")
        lines.append(f"| Максимум слов | {max_words} |")
        lines.append(f"| Записей за последние 7 дней | {last_7} |")
        lines.append(f"| Высокое качество (conf ≥0.9) | {high_quality} ({hq_rate:.0f}%) |")
        lines.append(f"| Всего записей в истории | {len(all_items)} |")
        lines.append("")

        # Общий вердикт
        if total == 0:
            verdict = "Нет данных за период."
        elif paste_rate >= 80 and (not confidences or hq_rate >= 60):
            verdict = "Система работает в норме."
        elif paste_rate >= 50:
            verdict = "Возможны проблемы с вставкой или качеством распознавания."
        else:
            verdict = "Требует внимания: низкий процент успешных вставок."

        lines.append(f"> **Итог:** {verdict}")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _load_items(store: Any) -> list:
        """Загружает активные записи из store."""
        if store is None:
            return []
        try:
            with store._lock():
                return store._load_active_items_unlocked()
        except Exception:
            logger.exception("Не удалось загрузить историю из store")
            return []

    @staticmethod
    def _get_storage_mb(store: Any) -> float:
        """Возвращает размер history.ndjson в MB."""
        try:
            path = getattr(store, "history_path", None)
            if path is None:
                return 0.0
            p = Path(path)
            return p.stat().st_size / (1024 * 1024) if p.exists() else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _get_collections_path(store: Any) -> Path | None:
        """Возвращает путь к collections.json если доступен."""
        try:
            data_dir = Path(getattr(store, "data_dir", "."))
            return data_dir / "collections.json"
        except Exception:
            return None
