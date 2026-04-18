"""transcription_scorer.py — оценка качества транскрибации Krab Ear.

Рассчитывает итоговый балл (0–100) на основе нескольких факторов:
- уверенность модели STT (40%)
- полнота текста (20%)
- соответствие продолжительности (20%)
- бонус за диаризацию (10%)
- бонус за LLM-обработку (10%)

Не требует внешних зависимостей.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger(__name__)


# ── Регулярные выражения ─────────────────────────────────────────────────────

_RE_WORD = re.compile(
    r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+(?:[-'][А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+)*"
)

# ── Диапазоны «нормальной» продолжительности ─────────────────────────────────

# Средний темп речи: ~100–180 слов/мин → ~1.7–3 слова/сек
_MIN_WORDS_PER_SEC = 0.5   # очень медленная речь / длинные паузы
_MAX_WORDS_PER_SEC = 5.0   # очень быстрая речь


@dataclass
class QualityScore:
    """Результат оценки качества транскрибации."""

    overall_score: float                          # итоговый балл 0–100
    grade: str                                    # "A", "B", "C", "D" или "F"
    factors: Dict[str, float] = field(default_factory=dict)  # вклад каждого фактора
    recommendations: List[str] = field(default_factory=list)  # советы по улучшению


def _grade(score: float) -> str:
    """Возвращает буквенную оценку по итоговому баллу."""
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def _word_count(text: str) -> int:
    """Считает число слов в тексте."""
    return len(_RE_WORD.findall(text))


class TranscriptionScorer:
    """Оценивает качество транскрибации по нескольким факторам.

    Факторы и их веса:
    - confidence (40%) — уверенность модели STT, значение 0.0–1.0.
    - text_completeness (20%) — насколько «полным» выглядит текст
      (ненулевое количество слов и разумная длина).
    - duration_appropriateness (20%) — соответствие числа слов
      ожидаемому диапазону для данной длительности аудио.
    - diarization_bonus (10%) — был ли применён диаризатор.
    - llm_enhancement_bonus (10%) — был ли применён LLM-рерайтер.

    Пример::

        scorer = TranscriptionScorer()
        result = scorer.score(
            text="Привет, это тестовая транскрипция.",
            confidence=0.92,
            duration_sec=3.5,
        )
        print(result.overall_score, result.grade)
    """

    # ── Веса факторов (должны суммироваться в 100) ───────────────────────────
    _W_CONFIDENCE = 40.0
    _W_COMPLETENESS = 20.0
    _W_DURATION = 20.0
    _W_DIARIZATION = 10.0
    _W_LLM = 10.0

    def score(
        self,
        text: str,
        confidence: float,
        duration_sec: float,
        has_diarization: bool = False,
        has_llm_enhancement: bool = False,
    ) -> QualityScore:
        """Оценивает качество транскрибации.

        Args:
            text: транскрибированный текст.
            confidence: уверенность STT-модели, 0.0–1.0.
            duration_sec: длительность аудио в секундах.
            has_diarization: True если применялась диаризация спикеров.
            has_llm_enhancement: True если применялся LLM-рерайтер.

        Returns:
            QualityScore с итоговым баллом, оценкой, факторами и рекомендациями.
        """
        confidence = max(0.0, min(1.0, confidence))
        duration_sec = max(0.0, duration_sec)

        # ── Фактор 1: уверенность STT (0–40) ────────────────────────────────
        confidence_score = confidence * self._W_CONFIDENCE

        # ── Фактор 2: полнота текста (0–20) ─────────────────────────────────
        completeness_score = self._calc_completeness(text, duration_sec)

        # ── Фактор 3: соответствие продолжительности (0–20) ─────────────────
        duration_score = self._calc_duration_appropriateness(text, duration_sec)

        # ── Фактор 4: бонус за диаризацию (0–10) ────────────────────────────
        diarization_score = self._W_DIARIZATION if has_diarization else 0.0

        # ── Фактор 5: бонус за LLM (0–10) ───────────────────────────────────
        llm_score = self._W_LLM if has_llm_enhancement else 0.0

        overall = min(
            100.0,
            confidence_score + completeness_score + duration_score
            + diarization_score + llm_score,
        )
        overall = round(overall, 2)

        factors = {
            "confidence": round(confidence_score, 2),
            "text_completeness": round(completeness_score, 2),
            "duration_appropriateness": round(duration_score, 2),
            "diarization_bonus": round(diarization_score, 2),
            "llm_enhancement_bonus": round(llm_score, 2),
        }

        recommendations = self._build_recommendations(
            text=text,
            confidence=confidence,
            confidence_score=confidence_score,
            completeness_score=completeness_score,
            duration_score=duration_score,
            duration_sec=duration_sec,
            has_diarization=has_diarization,
            has_llm_enhancement=has_llm_enhancement,
        )

        return QualityScore(
            overall_score=overall,
            grade=_grade(overall),
            factors=factors,
            recommendations=recommendations,
        )

    # ── Вспомогательные методы ───────────────────────────────────────────────

    def _calc_completeness(self, text: str, duration_sec: float) -> float:
        """Оценивает полноту текста (0–20).

        - Пустой или очень короткий текст (< 2 слов) → 0.
        - Для коротких записей (< 3 с) 1 слово уже считается полным.
        - Чем больше слов — тем лучше, но с насыщением.
        """
        if not text or not text.strip():
            return 0.0

        words = _word_count(text)

        if words == 0:
            return 0.0

        # Для очень коротких фрагментов (< 3 с) один токен уже хорошо
        if duration_sec < 3.0:
            return self._W_COMPLETENESS if words >= 1 else 0.0

        # Ожидаем минимум ~2 слова на 3-5 секунд
        min_expected = max(2, int(duration_sec * _MIN_WORDS_PER_SEC * 0.5))
        if words < min_expected:
            ratio = words / min_expected
            return round(ratio * self._W_COMPLETENESS, 2)

        return self._W_COMPLETENESS

    def _calc_duration_appropriateness(self, text: str, duration_sec: float) -> float:
        """Оценивает соответствие числа слов ожидаемому темпу речи (0–20).

        Если duration_sec == 0 — возвращаем нейтральный балл (10.0).
        """
        if duration_sec <= 0.0:
            return self._W_DURATION / 2.0

        words = _word_count(text)
        if words == 0:
            return 0.0

        wps = words / duration_sec  # слов в секунду

        if _MIN_WORDS_PER_SEC <= wps <= _MAX_WORDS_PER_SEC:
            # Оптимальная зона — полный балл
            return self._W_DURATION

        if wps < _MIN_WORDS_PER_SEC:
            # Слишком мало слов для данной длительности
            ratio = wps / _MIN_WORDS_PER_SEC
            return round(max(0.0, ratio) * self._W_DURATION, 2)

        # wps > _MAX_WORDS_PER_SEC — аномально много слов (галлюцинации?)
        # Плавно штрафуем: вдвое выше максимума → 50% баллов
        excess_ratio = _MAX_WORDS_PER_SEC / wps
        return round(max(0.0, excess_ratio) * self._W_DURATION, 2)

    @staticmethod
    def _build_recommendations(
        *,
        text: str,
        confidence: float,
        confidence_score: float,
        completeness_score: float,
        duration_score: float,
        duration_sec: float,
        has_diarization: bool,
        has_llm_enhancement: bool,
    ) -> List[str]:
        """Формирует список советов по улучшению качества транскрибации."""
        recs: List[str] = []

        # Низкая уверенность STT
        if confidence < 0.6:
            recs.append("Попробуйте говорить ближе к микрофону для повышения качества звука.")
        elif confidence < 0.8:
            recs.append("Уменьшите фоновый шум для лучшей распознаваемости речи.")

        # Слишком мало текста
        words = _word_count(text)
        if completeness_score < 10.0 and words < 5:
            recs.append("Запись очень короткая — убедитесь, что микрофон захватывает всю речь.")

        # Несоответствие темпу речи
        if duration_sec > 0:
            wps = words / duration_sec if words > 0 else 0.0
            if wps < _MIN_WORDS_PER_SEC and duration_sec >= 5.0:
                recs.append(
                    "Обнаружено мало слов для данной длительности — "
                    "проверьте настройки микрофона или уровень тишины."
                )
            elif wps > _MAX_WORDS_PER_SEC * 1.5:
                recs.append(
                    "Аномально высокая плотность слов — возможны артефакты распознавания. "
                    "Попробуйте уменьшить скорость речи."
                )

        # Советы по включению функций
        if not has_diarization:
            recs.append(
                "Включите диаризацию спикеров для повышения оценки качества при многоголосых записях."
            )
        if not has_llm_enhancement:
            recs.append(
                "Включите LLM-обработку для автоматического улучшения текста транскрибации."
            )

        return recs
