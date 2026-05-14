"""auto_title.py — автоматическая генерация заголовков для записей Krab Ear.

Использует эвристики для извлечения первой значимой фразы из транскрибации
и формирует короткий заголовок для отображения в истории.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


# ── Стоп-слова и приветствия ─────────────────────────────────────────────────

# Слова-заполнители в начале речи, которые не несут смысловой нагрузки
_FILLER_WORDS_RU: frozenset = frozenset([
    "ну", "так", "короче", "вот", "значит", "это", "собственно",
    "типа", "ладно", "хорошо", "окей", "ок", "да", "нет", "ага",
    "угу", "эм", "эхм", "мм", "слушай", "слушайте", "знаешь",
    "знаете", "понимаешь", "понимаете", "алло", "привет", "здравствуйте",
    "добрый", "здравствуй", "хм", "ах", "ой", "эй", "ну-ка",
])

_FILLER_WORDS_ES: frozenset = frozenset([
    "bueno", "pues", "oye", "mira", "hola", "eh", "ah", "um",
    "entonces", "o sea", "venga", "vale", "claro", "sí",
])

_FILLER_WORDS_EN: frozenset = frozenset([
    "well", "so", "um", "uh", "like", "okay", "ok", "hey",
    "hi", "hello", "right", "yeah", "yep", "hmm",
])

_ALL_FILLER_WORDS = _FILLER_WORDS_RU | _FILLER_WORDS_ES | _FILLER_WORDS_EN

# Паттерн диаризации: "Speaker 0:", "SPEAKER_01:", "[Speaker 1]:" и т.п.
_DIARIZATION_RE = re.compile(
    r"(?:^|\n)\s*(?:Speaker\s+\d+|SPEAKER[_\s]\d+|\[Speaker\s+\d+\])"
    r"\s*[:\-]\s*",
    re.IGNORECASE,
)

# Разбивка на предложения
_SENTENCE_END_RE = re.compile(r"[.!?…]+\s*")

# Очистка от служебных символов в начале строки
_LEADING_PUNCT_RE = re.compile(r"^[\s\-—–_*•·:,;]+")
# Очистка слова от не-буквенных символов при поиске значимого начала
_RE_WORD_PUNCT = re.compile(r"[^\wА-Яа-яёЁ]")


class AutoTitleGenerator:
    """Генератор автоматических заголовков для записей транскрибации.

    Использует эвристики:
    - Извлекает первую значимую фразу (пропускает слова-заполнители)
    - Обрезает по границе слова с добавлением «...»
    - Капитализирует первую букву
    - При наличии диаризации берёт первую фразу первого спикера
    - Для очень коротких текстов (<5 слов) использует текст as-is
    """

    def __init__(self) -> None:
        pass

    # ── Основной публичный API ────────────────────────────────────────────────

    def generate_title(self, text: str, max_length: int = 50) -> str:
        """Генерирует заголовок для записи транскрибации.

        Args:
            text: исходный текст транскрибации (может содержать диаризацию).
            max_length: максимальная длина заголовка в символах.

        Returns:
            Строка-заголовок, не длиннее max_length символов.
        """
        if not text or not text.strip():
            return "Запись"

        # Извлекаем первую значимую фразу из текста
        candidate = self._extract_first_meaningful_phrase(text)

        if not candidate:
            return "Запись"

        # Обрезаем по границе слова
        title = self._truncate_at_word_boundary(candidate, max_length)

        # Капитализируем первую букву
        title = self._capitalize_first(title)

        return title or "Запись"

    def generate_title_with_date(self, text: str, timestamp: str) -> str:
        """Генерирует заголовок вида «YYYY-MM-DD — Первая фраза...».

        Args:
            text: исходный текст транскрибации.
            timestamp: строка с датой/временем (ISO 8601 или YYYY-MM-DD).

        Returns:
            Строка вида «2026-04-12 — Первая фраза текста...»
        """
        date_prefix = self._format_date(timestamp)
        title = self.generate_title(text, max_length=50)
        return f"{date_prefix} — {title}"

    def batch_generate(self, items: list) -> list[dict]:
        """Генерирует заголовки для списка записей истории.

        Args:
            items: список словарей с ключами «text» (обязательный),
                   «id» (опциональный), «timestamp» (опциональный).

        Returns:
            Список словарей {id, title, generated_at}.
        """
        results = []
        for item in items:
            item_id = item.get("id", "")
            text = str(item.get("text", "") or "")
            timestamp = item.get("timestamp", "")

            if timestamp:
                title = self.generate_title_with_date(text, timestamp)
            else:
                title = self.generate_title(text)

            results.append({
                "id": item_id,
                "title": title,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

        return results

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _extract_first_meaningful_phrase(self, text: str) -> str:
        """Возвращает первую значимую фразу из текста.

        Обрабатывает диаризацию: если текст содержит метки спикеров,
        берёт фразу первого спикера.
        """
        # Проверяем наличие диаризации
        if _DIARIZATION_RE.search(text):
            return self._extract_from_diarized(text)

        # Обычный текст: берём первое непустое предложение
        return self._extract_first_sentence(text.strip())

    def _extract_from_diarized(self, text: str) -> str:
        """Извлекает первую фразу первого спикера из диаризованного текста."""
        # Разбиваем по меткам спикеров
        parts = _DIARIZATION_RE.split(text)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            phrase = self._extract_first_sentence(part)
            if phrase:
                return phrase
        return ""

    def _extract_first_sentence(self, text: str) -> str:
        """Возвращает первое значимое предложение из текста.

        Пропускает слова-заполнители в начале фразы.
        Если текст очень короткий (<5 слов) — возвращает as-is.
        """
        if not text:
            return ""

        # Очищаем от ведущей пунктуации
        text = _LEADING_PUNCT_RE.sub("", text).strip()
        if not text:
            return ""

        # Считаем слова
        words = text.split()
        if len(words) < 5:
            # Очень короткий текст — используем as-is
            return text

        # Попытка извлечь первое законченное предложение
        sentences = _SENTENCE_END_RE.split(text)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            cleaned = self._skip_filler_words(sentence)
            if cleaned:
                return cleaned

        # Если предложений нет — используем весь текст после пропуска заполнителей
        return self._skip_filler_words(text) or text

    def _skip_filler_words(self, text: str) -> str:
        """Пропускает слова-заполнители в начале фразы.

        Возвращает текст, начиная с первого значимого слова.
        Если все слова — заполнители, возвращает исходный текст.
        """
        words = text.split()
        if not words:
            return text

        start_idx = 0
        for i, word in enumerate(words):
            # Очищаем слово от пунктуации для проверки
            clean_word = _RE_WORD_PUNCT.sub("", word).lower()
            if clean_word and clean_word not in _ALL_FILLER_WORDS:
                start_idx = i
                break
        else:
            # Все слова — заполнители, возвращаем исходный текст
            return text

        return " ".join(words[start_idx:])

    def _truncate_at_word_boundary(self, text: str, max_length: int) -> str:
        """Обрезает текст по границе слова, добавляя «...» если нужно."""
        if len(text) <= max_length:
            return text

        # Ищем последний пробел в пределах max_length - 3 (место для «...»)
        truncated = text[:max_length - 3]
        last_space = truncated.rfind(" ")

        if last_space > 0:
            truncated = truncated[:last_space]

        return truncated + "..."

    def _capitalize_first(self, text: str) -> str:
        """Капитализирует первую букву строки."""
        if not text:
            return text
        return text[0].upper() + text[1:]

    def _format_date(self, timestamp: str) -> str:
        """Форматирует timestamp в строку YYYY-MM-DD."""
        if not timestamp:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Пробуем распарсить ISO 8601 (с или без время)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(timestamp, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # Если парсинг не удался — возвращаем первые 10 символов как есть
        return timestamp[:10] if len(timestamp) >= 10 else timestamp
