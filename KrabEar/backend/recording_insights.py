"""RecordingInsightsGenerator — эвристические инсайты по записям Krab Ear.

Анализирует историю транскрипций и генерирует умные наблюдения без LLM.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import logging

logger = logging.getLogger("KrabEar.Backend.RecordingInsights")


# ---------------------------------------------------------------------------
# Стоп-слова (RU + ES + EN) — для анализа тем
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

# Тематические кластеры ключевых слов для поиска «самой частой темы»
_TOPIC_CLUSTERS: list[tuple[str, list[str]]] = [
    ("технологии", ["программа", "код", "сервер", "база", "данные", "система", "приложение",
                    "python", "swift", "api", "функция", "модель", "сеть", "компьютер",
                    "código", "sistema", "servidor", "datos", "programa", "software",
                    "program", "code", "server", "database", "system", "app", "function",
                    "model", "network", "computer"]),
    ("работа", ["задача", "проект", "встреча", "коллега", "дедлайн", "план", "отчёт",
                "задание", "команда", "офис", "презентация", "совещание",
                "tarea", "proyecto", "reunión", "colega", "plazo", "equipo", "oficina",
                "task", "project", "meeting", "colleague", "deadline", "team", "office"]),
    ("здоровье", ["здоровье", "врач", "лечение", "болезнь", "таблетки", "больница",
                  "симптом", "анализ", "диета", "спорт", "тренировка",
                  "salud", "médico", "tratamiento", "enfermedad", "pastillas",
                  "health", "doctor", "treatment", "illness", "pills", "hospital",
                  "symptom", "diet", "sport", "training"]),
    ("образование", ["учёба", "курс", "урок", "студент", "экзамен", "лекция",
                     "знания", "навык", "обучение", "тема",
                     "estudio", "curso", "lección", "estudiante", "examen", "clase",
                     "study", "course", "lesson", "student", "exam", "lecture",
                     "knowledge", "skill", "learning", "topic"]),
    ("финансы", ["деньги", "бюджет", "зарплата", "расход", "инвестиция", "банк",
                 "цена", "стоимость", "платёж", "счёт",
                 "dinero", "presupuesto", "salario", "gasto", "inversión", "banco",
                 "money", "budget", "salary", "expense", "investment", "bank",
                 "price", "cost", "payment", "account"]),
]


def _tokenize(text: str) -> list[str]:
    """Разбивает текст на слова (нижний регистр, только буквы)."""
    return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)


def _get_ts(item: Any) -> datetime | None:
    """Возвращает timestamp элемента истории в UTC или None."""
    ts_raw = getattr(item, "ts", None) or (item.get("ts") if isinstance(item, dict) else None)
    if not ts_raw:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _get_confidence(item: Any) -> float | None:
    """Возвращает confidence элемента истории или None."""
    val = getattr(item, "confidence", None) if not isinstance(item, dict) else item.get("confidence")
    if val is None:
        return None
    try:
        f = float(val)
        return f if 0.0 <= f <= 1.0 else None
    except (ValueError, TypeError):
        return None


def _get_source_lang(item: Any) -> str:
    """Возвращает source_lang элемента истории."""
    val = getattr(item, "source_lang", "") if not isinstance(item, dict) else item.get("source_lang", "")
    return (val or "").strip().lower()


def _get_text(item: Any) -> str:
    """Возвращает text элемента истории."""
    val = getattr(item, "text", "") if not isinstance(item, dict) else item.get("text", "")
    return (val or "").strip()


def _get_audio_duration(item: Any) -> float:
    """Возвращает audio_duration_sec элемента истории (0.0 если нет)."""
    val = getattr(item, "audio_duration_sec", None) if not isinstance(item, dict) else item.get("audio_duration_sec")
    if val is None:
        return 0.0
    try:
        return max(0.0, float(val))
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Dataclass Insight
# ---------------------------------------------------------------------------

@dataclass
class Insight:
    """Одно умное наблюдение о записях."""

    type: str
    """Тип инсайта: 'peak_productivity', 'language_shift', 'quality_improvement',
    'recording_streak', 'most_discussed_topic', 'speaking_pace_change'."""

    title: str
    """Краткий заголовок инсайта."""

    description: str
    """Детальное описание инсайта."""

    confidence: float
    """Уверенность в инсайте от 0.0 до 1.0."""

    data: dict = field(default_factory=dict)
    """Дополнительные данные (числа, используемые при генерации)."""

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "confidence": self.confidence,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Генератор инсайтов
# ---------------------------------------------------------------------------

class RecordingInsightsGenerator:
    """Генерирует эвристические инсайты по истории записей без LLM."""

    # Минимальное количество записей для генерации инсайтов
    _MIN_ITEMS = 3

    def generate_insights(self, items: list, days: int = 7) -> list[Insight]:
        """Генерирует список инсайтов за последние N дней.

        Args:
            items: список объектов истории (HistoryItem или dict).
            days: окно анализа в днях.

        Returns:
            Список ``Insight`` — пустой если данных недостаточно.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = [
            item for item in items
            if (ts := _get_ts(item)) is not None and ts >= cutoff
        ]

        if len(recent) < self._MIN_ITEMS:
            return []

        insights: list[Insight] = []

        peak = self._compute_peak_productivity(recent)
        if peak is not None:
            insights.append(peak)

        lang_shift = self._compute_language_shift(items, days=days)
        if lang_shift is not None:
            insights.append(lang_shift)

        quality = self._compute_quality_improvement(items, days=days)
        if quality is not None:
            insights.append(quality)

        streak = self._compute_recording_streak(items)
        if streak is not None:
            insights.append(streak)

        topic = self._compute_most_discussed_topic(recent)
        if topic is not None:
            insights.append(topic)

        pace = self._compute_speaking_pace_change(items, days=days)
        if pace is not None:
            insights.append(pace)

        return insights

    def get_daily_insight(self, items: list) -> Insight | None:
        """Возвращает один наиболее релевантный инсайт за сегодня.

        Выбирает инсайт с наибольшей уверенностью из тех, которые
        можно сгенерировать по данным за последние 7 дней.
        Returns None если данных недостаточно.
        """
        insights = self.generate_insights(items, days=7)
        if not insights:
            return None
        return max(insights, key=lambda i: i.confidence)

    # ------------------------------------------------------------------
    # Peak productivity
    # ------------------------------------------------------------------

    def _compute_peak_productivity(self, items: list) -> Insight | None:
        """Определяет наиболее продуктивный час дня."""
        hour_counts: dict[int, int] = {}
        for item in items:
            ts = _get_ts(item)
            if ts is None:
                continue
            hour = ts.hour
            hour_counts[hour] = hour_counts.get(hour, 0) + 1

        if not hour_counts:
            return None

        # Находим час-пик
        peak_hour = max(hour_counts, key=lambda h: hour_counts[h])
        peak_count = hour_counts[peak_hour]
        total_count = sum(hour_counts.values())

        if total_count == 0:
            return None

        # Уверенность пропорциональна доле записей в пиковый час
        peak_ratio = peak_count / total_count
        # Нормализуем: ratio 0.3 → high confidence, ниже → lower
        conf = min(1.0, round(peak_ratio * 2.5, 2))

        end_hour = (peak_hour + 2) % 24
        title = f"Вы наиболее продуктивны в {peak_hour:02d}:00–{end_hour:02d}:00"
        description = (
            f"В период {peak_hour:02d}:00–{end_hour:02d}:00 было сделано "
            f"{peak_count} из {total_count} записей — это {peak_ratio * 100:.0f}% от общего числа."
        )

        return Insight(
            type="peak_productivity",
            title=title,
            description=description,
            confidence=conf,
            data={
                "peak_hour": peak_hour,
                "end_hour": end_hour,
                "peak_count": peak_count,
                "total_count": total_count,
                "peak_ratio": round(peak_ratio, 3),
                "hour_distribution": hour_counts,
            },
        )

    # ------------------------------------------------------------------
    # Language shift
    # ------------------------------------------------------------------

    def _compute_language_shift(self, items: list, days: int = 7) -> Insight | None:
        """Обнаруживает смещение в использовании языков."""
        now = datetime.now(timezone.utc)
        cutoff_recent = now - timedelta(days=days)
        cutoff_prev = now - timedelta(days=days * 2)

        prev_items = []
        recent_items = []
        for item in items:
            ts = _get_ts(item)
            if ts is None:
                continue
            if cutoff_prev <= ts < cutoff_recent:
                prev_items.append(item)
            elif ts >= cutoff_recent:
                recent_items.append(item)

        if not prev_items or not recent_items:
            return None

        def _lang_freq(lst: list) -> dict[str, float]:
            counts: dict[str, int] = {}
            for it in lst:
                lang = _get_source_lang(it)
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
            total = sum(counts.values())
            if total == 0:
                return {}
            return {lang: cnt / total for lang, cnt in counts.items()}

        prev_freq = _lang_freq(prev_items)
        recent_freq = _lang_freq(recent_items)

        if not prev_freq or not recent_freq:
            return None

        # Находим язык с наибольшим ростом
        best_lang = None
        best_change_pct = 0.0
        for lang in recent_freq:
            prev = prev_freq.get(lang, 0.0)
            recent = recent_freq[lang]
            if prev == 0.0:
                continue
            change_pct = ((recent - prev) / prev) * 100.0
            if abs(change_pct) > abs(best_change_pct):
                best_change_pct = change_pct
                best_lang = lang

        # Минимальный порог изменения — 10%
        if best_lang is None or abs(best_change_pct) < 10.0:
            return None

        direction = "выросло" if best_change_pct > 0 else "снизилось"
        lang_display = best_lang.upper()
        abs_pct = abs(round(best_change_pct, 0))
        title = f"Использование {lang_display} {direction} на {abs_pct:.0f}% за неделю"
        description = (
            f"За последние {days} дней доля {lang_display} в записях "
            f"{direction} с {prev_freq.get(best_lang, 0.0) * 100:.0f}% "
            f"до {recent_freq[best_lang] * 100:.0f}%."
        )

        conf = min(1.0, round(abs(best_change_pct) / 100.0 * 1.5, 2))

        return Insight(
            type="language_shift",
            title=title,
            description=description,
            confidence=conf,
            data={
                "language": best_lang,
                "change_pct": round(best_change_pct, 1),
                "prev_ratio": round(prev_freq.get(best_lang, 0.0), 3),
                "recent_ratio": round(recent_freq.get(best_lang, 0.0), 3),
            },
        )

    # ------------------------------------------------------------------
    # Quality improvement
    # ------------------------------------------------------------------

    def _compute_quality_improvement(self, items: list, days: int = 7) -> Insight | None:
        """Обнаруживает улучшение средней confidence между двумя периодами."""
        now = datetime.now(timezone.utc)
        cutoff_recent = now - timedelta(days=days)
        cutoff_prev = now - timedelta(days=days * 2)

        prev_conf: list[float] = []
        recent_conf: list[float] = []
        for item in items:
            ts = _get_ts(item)
            conf = _get_confidence(item)
            if ts is None or conf is None:
                continue
            if cutoff_prev <= ts < cutoff_recent:
                prev_conf.append(conf)
            elif ts >= cutoff_recent:
                recent_conf.append(conf)

        if len(prev_conf) < 2 or len(recent_conf) < 2:
            return None

        prev_avg = sum(prev_conf) / len(prev_conf)
        recent_avg = sum(recent_conf) / len(recent_conf)

        change = recent_avg - prev_avg
        # Минимальный порог изменения — 0.01
        if abs(change) < 0.01:
            return None

        direction = "выросла" if change > 0 else "снизилась"
        title = f"Средняя confidence {direction} с {prev_avg:.2f} до {recent_avg:.2f}"
        description = (
            f"За последние {days} дней средняя уверенность распознавания "
            f"{direction} с {prev_avg:.2f} до {recent_avg:.2f} "
            f"(изменение: {change:+.3f})."
        )

        # Уверенность в инсайте зависит от размера выборки и величины изменения
        sample_factor = min(1.0, (len(prev_conf) + len(recent_conf)) / 20.0)
        change_factor = min(1.0, abs(change) * 10.0)
        conf = round(sample_factor * change_factor * 0.9, 2)

        return Insight(
            type="quality_improvement",
            title=title,
            description=description,
            confidence=conf,
            data={
                "prev_avg_confidence": round(prev_avg, 4),
                "recent_avg_confidence": round(recent_avg, 4),
                "change": round(change, 4),
                "prev_sample_size": len(prev_conf),
                "recent_sample_size": len(recent_conf),
            },
        )

    # ------------------------------------------------------------------
    # Recording streak
    # ------------------------------------------------------------------

    def _compute_recording_streak(self, items: list) -> Insight | None:
        """Считает текущую серию дней с записями подряд."""
        dates: set[str] = set()
        for item in items:
            ts = _get_ts(item)
            if ts is None:
                continue
            dates.add(ts.date().isoformat())

        if not dates:
            return None

        # Ищем серию начиная со вчера/сегодня
        today = datetime.now(timezone.utc).date()
        streak = 0
        current_day = today
        while current_day.isoformat() in dates:
            streak += 1
            current_day = current_day - timedelta(days=1)

        # Если сегодня нет записи, проверяем начиная со вчера
        if streak == 0:
            current_day = today - timedelta(days=1)
            while current_day.isoformat() in dates:
                streak += 1
                current_day = current_day - timedelta(days=1)

        if streak < 2:
            return None

        title = f"{streak} {'дней' if streak >= 5 else 'дня' if streak >= 2 else 'день'} подряд с записями!"
        description = (
            f"Вы делали записи {streak} дней подряд. "
            f"Отличная привычка — продолжайте в том же духе!"
        )

        # Уверенность растёт с длиной серии
        conf = min(1.0, round(0.5 + streak * 0.05, 2))

        return Insight(
            type="recording_streak",
            title=title,
            description=description,
            confidence=conf,
            data={
                "streak_days": streak,
                "total_days_with_recordings": len(dates),
            },
        )

    # ------------------------------------------------------------------
    # Most discussed topic
    # ------------------------------------------------------------------

    def _compute_most_discussed_topic(self, items: list) -> Insight | None:
        """Определяет самую частую тему в записях."""
        all_tokens: list[str] = []
        for item in items:
            text = _get_text(item)
            if not text:
                continue
            tokens = [
                w for w in _tokenize(text)
                if w not in _STOP_WORDS and len(w) > 3
            ]
            all_tokens.extend(tokens)

        if not all_tokens:
            return None

        # Считаем совпадения с кластерами тем
        topic_scores: dict[str, int] = {}
        token_set = Counter(all_tokens)
        for topic_name, keywords in _TOPIC_CLUSTERS:
            score = sum(token_set.get(kw, 0) for kw in keywords)
            if score > 0:
                topic_scores[topic_name] = score

        if not topic_scores:
            # Fallback: наиболее частое слово
            most_common = token_set.most_common(1)
            if most_common:
                word, cnt = most_common[0]
                title = f"Самая частая тема: «{word}»"
                description = f"Слово «{word}» встречалось {cnt} раз в ваших записях."
                return Insight(
                    type="most_discussed_topic",
                    title=title,
                    description=description,
                    confidence=0.4,
                    data={"topic": word, "score": cnt},
                )
            return None

        best_topic = max(topic_scores, key=lambda t: topic_scores[t])
        best_score = topic_scores[best_topic]
        total_score = sum(topic_scores.values())
        ratio = best_score / total_score if total_score > 0 else 0.0

        title = f"Самая частая тема: {best_topic}"
        description = (
            f"Тема «{best_topic}» доминирует в ваших записях "
            f"({best_score} совпадений из {total_score} всего)."
        )

        conf = min(1.0, round(ratio * 1.5 + 0.2, 2))

        return Insight(
            type="most_discussed_topic",
            title=title,
            description=description,
            confidence=conf,
            data={
                "topic": best_topic,
                "score": best_score,
                "total_score": total_score,
                "topic_scores": topic_scores,
            },
        )

    # ------------------------------------------------------------------
    # Speaking pace change
    # ------------------------------------------------------------------

    def _compute_speaking_pace_change(self, items: list, days: int = 7) -> Insight | None:
        """Обнаруживает изменение темпа речи (слов в секунду) между двумя периодами."""
        now = datetime.now(timezone.utc)
        cutoff_recent = now - timedelta(days=days)
        cutoff_prev = now - timedelta(days=days * 2)

        def _wps(lst: list) -> list[float]:
            """Слов в секунду для каждого элемента списка."""
            result = []
            for it in lst:
                text = _get_text(it)
                dur = _get_audio_duration(it)
                if not text or dur < 1.0:
                    continue
                words = len(text.split())
                result.append(words / dur)
            return result

        prev_items = []
        recent_items = []
        for item in items:
            ts = _get_ts(item)
            if ts is None:
                continue
            if cutoff_prev <= ts < cutoff_recent:
                prev_items.append(item)
            elif ts >= cutoff_recent:
                recent_items.append(item)

        prev_wps = _wps(prev_items)
        recent_wps = _wps(recent_items)

        if len(prev_wps) < 2 or len(recent_wps) < 2:
            return None

        prev_avg = sum(prev_wps) / len(prev_wps)
        recent_avg = sum(recent_wps) / len(recent_wps)

        if prev_avg == 0.0:
            return None

        change_pct = ((recent_avg - prev_avg) / prev_avg) * 100.0

        # Минимальный порог — 5%
        if abs(change_pct) < 5.0:
            return None

        direction = "ускорился" if change_pct > 0 else "замедлился"
        abs_pct = abs(round(change_pct, 0))
        title = f"Темп речи {direction} на {abs_pct:.0f}%"
        description = (
            f"Средний темп речи {direction} на {abs_pct:.0f}% "
            f"по сравнению с предыдущим периодом "
            f"({prev_avg:.2f} → {recent_avg:.2f} слов/с)."
        )

        conf = min(1.0, round(abs(change_pct) / 50.0, 2))

        return Insight(
            type="speaking_pace_change",
            title=title,
            description=description,
            confidence=conf,
            data={
                "prev_avg_wps": round(prev_avg, 3),
                "recent_avg_wps": round(recent_avg, 3),
                "change_pct": round(change_pct, 1),
                "prev_sample_size": len(prev_wps),
                "recent_sample_size": len(recent_wps),
            },
        )
