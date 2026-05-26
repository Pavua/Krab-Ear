"""Эвристическое определение эмоций в транскрибированном тексте.

Не использует внешние зависимости — только regex и словари.
Поддерживает русский (ru) и испанский (es) языки с английским fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Словари по языкам ────────────────────────────────────────────────────────

# Multi-word negative phrases per language checked via substring match BEFORE
# tokenization.  Phrases containing spaces cannot be matched by the per-word
# tokenizer, so they live in a separate set.  Each matched phrase contributes
# +1 to the negative count (and the phrase itself is added to indicators).
# Phrases must not also appear in _NEGATIVE_WORDS to avoid double-counting.
_NEGATIVE_PHRASES: dict[str, list[str]] = {
    "ru": [
        "не нравится",
        "не нравятся",
        "не нравилось",
        "не нравилась",
        "не люблю",
        "не хочу",
        "не могу",
    ],
    "es": [
        "no me gusta",
        "no me gustan",
        "no quiero",
    ],
    "en": [
        "don't like",
        "do not like",
        "can't stand",
        "cannot stand",
    ],
}

_NEGATIVE_WORDS: dict[str, list[str]] = {
    "ru": [
        # Чистые отрицательные частицы («не», «нет») удалены — W1009 F1:
        # они служебные слова, а не сентиментальные.
        "плохо", "ужасно", "нельзя", "никогда", "невозможно",
        "провал", "ошибка", "неудача", "плохой", "ужасный", "отстой",
        "катастрофа", "беда", "кошмар", "хуже", "худший", "жуть",
        "ненавижу", "надоело", "бесит", "раздражает", "злой", "злость",
        "злюсь", "отвратительно", "скучно",
    ],
    "es": [
        # «no» удалено как чистая отрицательная частица — W1009 F1.
        "mal", "malo", "terrible", "nunca", "imposible", "error",
        "fallo", "fracaso", "horrible", "pésimo", "desastre", "odio",
        "detesto", "molesta", "fastidio", "peor", "peorísimo",
    ],
    "en": [
        # «no», «never», «not» удалены как чистые отрицательные частицы — W1009 F1.
        "bad", "terrible", "impossible", "error", "fail",
        "failure", "horrible", "awful", "disaster", "hate", "worst",
        "annoying", "frustrated", "angry", "useless",
    ],
}

_POSITIVE_WORDS: dict[str, list[str]] = {
    "ru": [
        # «да» удалено — утвердительная частица, не сентиментальное слово (W1009 F3).
        "отлично", "здорово", "хорошо", "прекрасно", "замечательно",
        "супер", "круто", "восхитительно", "великолепно", "молодец",
        "спасибо", "благодарю", "люблю", "нравится", "радость", "счастье",
        "успех", "победа", "класс", "шикарно", "браво", "отличный",
    ],
    "es": [
        # «sí» удалено — утвердительная частица (W1009 F3).
        "bien", "bueno", "excelente", "maravilloso", "fantástico",
        "genial", "perfecto", "increíble", "gracias", "amor", "feliz",
        "éxito", "bravo", "estupendo",
    ],
    "en": [
        # «yes» удалено — утвердительная частица (W1009 F3).
        "great", "good", "excellent", "wonderful", "fantastic",
        "awesome", "perfect", "amazing", "thanks", "love", "happy",
        "success", "brilliant", "superb",
    ],
}

# Precompiled regex for tokenization — called on every _tokenize() invocation
_RE_WORD_TOKENS = re.compile(r"[А-Яа-яёЁA-Za-zÀ-ÿ]+")

# ── Dataclass результата ──────────────────────────────────────────────────────


@dataclass
class EmotionResult:
    """Результат эмоционального анализа текста."""

    primary_emotion: str
    """Основная эмоция: neutral, positive, negative, excited, frustrated, questioning."""

    confidence: float
    """Уверенность в классификации (0.0–1.0)."""

    indicators: list[str] = field(default_factory=list)
    """Слова/паттерны, которые triggered определение эмоции."""

    exclamation_count: int = 0
    """Количество восклицательных знаков в тексте."""

    question_count: int = 0
    """Количество вопросительных знаков в тексте."""

    caps_ratio: float = 0.0
    """Доля символов в верхнем регистре (среди буквенных символов)."""


# ── Основной класс ────────────────────────────────────────────────────────────

class EmotionDetector:
    """Эвристический детектор эмоций для транскрибированного текста.

    Не требует внешних зависимостей — работает на словарях и regex.
    """

    # Порог CAPS для «кричащего» текста (frustrated/shouting).
    CAPS_THRESHOLD = 0.6
    # Минимальная длина слова, считающегося значимым токеном.
    MIN_WORD_LEN = 2

    def detect(self, text: str, language: str = "ru") -> EmotionResult:
        """Определяет эмоцию в тексте.

        Args:
            text: Транскрибированный текст.
            language: Язык текста («ru», «es», «en»). По умолчанию «ru».

        Returns:
            EmotionResult с основной эмоцией, уверенностью и индикаторами.
        """
        if not text or not text.strip():
            return EmotionResult(
                primary_emotion="neutral",
                confidence=0.0,
                indicators=[],
            )

        lang = language.lower().split("-")[0]  # «ru-RU» → «ru»

        # ── Статистика символов ──────────────────────────────────────────────
        exclamation_count = text.count("!")
        question_count = text.count("?")
        caps_ratio = self._compute_caps_ratio(text)

        # ── Фразовый поиск (многословные паттерны) ───────────────────────────
        # Must run BEFORE tokenization because tokenizer splits on whitespace
        # and multi-word phrases like "не нравится" are lost as separate tokens.
        text_lower = text.lower()
        phrase_hits = self._match_phrases(text_lower, _NEGATIVE_PHRASES, lang)

        # ── Словарный поиск ──────────────────────────────────────────────────
        tokens = self._tokenize(text)
        neg_hits = self._match_words(tokens, _NEGATIVE_WORDS, lang)
        pos_hits = self._match_words(tokens, _POSITIVE_WORDS, lang)

        # Merge phrase hits into neg_hits (deduplication by value)
        for phrase in phrase_hits:
            if phrase not in neg_hits:
                neg_hits.append(phrase)

        # ── Логика классификации ─────────────────────────────────────────────
        indicators: list[str] = []
        scores: dict[str, float] = {
            "neutral": 0.0,
            "positive": 0.0,
            "negative": 0.0,
            "excited": 0.0,
            "frustrated": 0.0,
            "questioning": 0.0,
        }

        # Восклицательные знаки → excited
        if exclamation_count >= 1:
            excitement = min(0.4 + 0.15 * (exclamation_count - 1), 0.85)
            scores["excited"] += excitement
            indicators.append(f"exclamation_marks:{exclamation_count}")

        # Вопросительные знаки → questioning
        if question_count >= 1:
            questioning = min(0.4 + 0.15 * (question_count - 1), 0.85)
            scores["questioning"] += questioning
            indicators.append(f"question_marks:{question_count}")

        # CAPS → frustrated
        if caps_ratio >= self.CAPS_THRESHOLD:
            scores["frustrated"] += min(0.3 + caps_ratio * 0.5, 0.9)
            indicators.append(f"caps_ratio:{caps_ratio:.2f}")

        # Негативные слова → negative
        if neg_hits:
            neg_score = min(0.35 + 0.12 * len(neg_hits), 0.9)
            scores["negative"] += neg_score
            indicators.extend(neg_hits)

        # Позитивные слова → positive
        if pos_hits:
            pos_score = min(0.35 + 0.12 * len(pos_hits), 0.9)
            scores["positive"] += pos_score
            indicators.extend(pos_hits)

        # Комбинированная логика: восклицание + позитив → excited с повышенным confidence
        if exclamation_count >= 1 and pos_hits:
            scores["excited"] = max(scores["excited"], scores["positive"] + 0.1)

        # CAPS + негатив → frustrated
        if caps_ratio >= self.CAPS_THRESHOLD and neg_hits:
            scores["frustrated"] = max(scores["frustrated"], scores["negative"] + 0.15)

        # ── Выбор победителя ─────────────────────────────────────────────────
        best_emotion = max(scores, key=lambda k: scores[k])
        best_score = scores[best_emotion]

        if best_score < 0.1:
            return EmotionResult(
                primary_emotion="neutral",
                confidence=0.5,
                indicators=indicators,
                exclamation_count=exclamation_count,
                question_count=question_count,
                caps_ratio=caps_ratio,
            )

        confidence = min(best_score, 1.0)
        return EmotionResult(
            primary_emotion=best_emotion,
            confidence=round(confidence, 3),
            indicators=indicators,
            exclamation_count=exclamation_count,
            question_count=question_count,
            caps_ratio=round(caps_ratio, 3),
        )

    # ── Вспомогательные методы ───────────────────────────────────────────────

    @staticmethod
    def _compute_caps_ratio(text: str) -> float:
        """Доля заглавных букв среди всех буквенных символов."""
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return 0.0
        upper = sum(1 for c in letters if c.isupper())
        return upper / len(letters)

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        """Разбивает текст на токены (слова), приводит к нижнему регистру."""
        raw = _RE_WORD_TOKENS.findall(text)
        return [w.lower() for w in raw if len(w) >= cls.MIN_WORD_LEN]

    @staticmethod
    def _match_phrases(text_lower: str, phrase_dict: dict[str, list[str]], lang: str) -> list[str]:
        """Ищет многословные фразы через substring-поиск в тексте нижнего регистра.

        Возвращает список найденных фраз-индикаторов (без дубликатов).
        Каждая фраза засчитывается не более одного раза, даже если встречается
        несколько раз в тексте.
        """
        candidates = phrase_dict.get(lang, [])
        hits: list[str] = []
        seen: set[str] = set()
        for phrase in candidates:
            if phrase in text_lower and phrase not in seen:
                hits.append(phrase)
                seen.add(phrase)
        return hits

    @staticmethod
    def _match_words(tokens: list[str], word_dict: dict[str, list[str]], lang: str) -> list[str]:
        """Ищет совпадения токенов со словарём для указанного языка.

        Возвращает список найденных слов-индикаторов (без дубликатов).
        """
        candidates = word_dict.get(lang, word_dict.get("en", []))
        hits: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if token in candidates and token not in seen:
                hits.append(token)
                seen.add(token)
        return hits
