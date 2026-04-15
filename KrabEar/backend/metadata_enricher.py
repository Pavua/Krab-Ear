"""metadata_enricher.py — автоматическое обогащение метаданных записей Krab Ear.

Обогащает записи истории вычисляемыми метаданными:
  - word_count, sentence_count, avg_word_length
  - language_detected (LanguageDetector)
  - emotion (EmotionDetector)
  - speech_pace_wpm (SpeechPaceAnalyzer)
  - quality_grade (TranscriptionScorer)
  - auto_title (AutoTitleGenerator)
  - topics (TopicTracker)

Не требует внешних зависимостей: делегирует к уже существующим модулям core/.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from core.auto_title import AutoTitleGenerator
from core.emotion_detector import EmotionDetector
from core.language_detector import LanguageDetector
from core.speech_pace import SpeechPaceAnalyzer
from core.topic_tracker import TopicTracker
from core.transcription_scorer import TranscriptionScorer

logger = logging.getLogger("KrabEar.Backend.MetadataEnricher")

# Паттерн для разбивки на предложения (учитывает .!? с пробелами)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+\s+")

# Паттерн для токенизации слов (кириллица + латиница)
_WORD_RE = re.compile(
    r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+(?:[-'][А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+)*"
)


def _count_sentences(text: str) -> int:
    """Считает число предложений в тексте по знакам . ! ? …"""
    if not text or not text.strip():
        return 0
    # Разбиваем по терминаторам предложений; число сегментов = число предложений
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return max(1, len(parts)) if text.strip() else 0


def _avg_word_length(words: list[str]) -> float:
    """Средняя длина слова в символах."""
    if not words:
        return 0.0
    return round(sum(len(w) for w in words) / len(words), 2)


class MetadataEnricher:
    """Автоматически обогащает записи истории вычисляемыми метаданными.

    Пример использования::

        enricher = MetadataEnricher()
        enriched_item = enricher.enrich(history_item)
        stats = enricher.get_enrichment_stats()
    """

    def __init__(self) -> None:
        self._language_detector = LanguageDetector()
        self._emotion_detector = EmotionDetector()
        self._pace_analyzer = SpeechPaceAnalyzer()
        self._scorer = TranscriptionScorer()
        self._title_generator = AutoTitleGenerator()
        self._topic_tracker = TopicTracker()

        # Статистика обогащения
        self._enriched_count: int = 0
        self._total_enrichment_time_sec: float = 0.0

    # ── Основной API ──────────────────────────────────────────────────────────

    def enrich(self, item: dict[str, Any]) -> dict[str, Any]:
        """Обогащает одну запись истории вычисляемыми метаданными.

        Добавляет ключ ``metadata`` в копию *item* (исходный словарь
        не изменяется).

        Args:
            item: запись истории. Ожидаемые поля:
                  - ``text`` (str) — транскрибированный текст.
                  - ``duration_sec`` (float, опционально) — длительность аудио.
                  - ``confidence`` (float, опционально) — STT-уверенность 0–1.
                  - ``has_diarization`` (bool, опционально).
                  - ``has_llm_enhancement`` (bool, опционально).
                  - ``timestamp`` (str, опционально) — ISO 8601.

        Returns:
            Новый словарь с добавленным ключом ``metadata``.
        """
        t0 = time.monotonic()

        text: str = str(item.get("text") or "")
        duration_sec: float = float(item.get("duration_sec") or 0.0)
        confidence: float = float(item.get("confidence") or 0.0)
        has_diarization: bool = bool(item.get("has_diarization", False))
        has_llm: bool = bool(item.get("has_llm_enhancement", False))

        # ── Базовые текстовые метрики ────────────────────────────────────────
        words = _WORD_RE.findall(text)
        word_count = len(words)
        sentence_count = _count_sentences(text)
        avg_wl = _avg_word_length(words)

        # ── Определение языка ────────────────────────────────────────────────
        lang_result = self._language_detector.detect(text)
        language_detected = lang_result.language

        # ── Определение эмоции ───────────────────────────────────────────────
        emotion_result = self._emotion_detector.detect(text, language=language_detected)
        emotion = emotion_result.primary_emotion

        # ── Анализ темпа речи ────────────────────────────────────────────────
        pace_report = self._pace_analyzer.analyze(text, duration_sec=duration_sec)
        speech_pace_wpm = pace_report.words_per_minute

        # ── Оценка качества транскрибации ────────────────────────────────────
        quality_score = self._scorer.score(
            text=text,
            confidence=confidence,
            duration_sec=duration_sec,
            has_diarization=has_diarization,
            has_llm_enhancement=has_llm,
        )
        quality_grade = quality_score.grade

        # ── Авто-заголовок ───────────────────────────────────────────────────
        timestamp = str(item.get("timestamp") or "")
        if timestamp:
            auto_title = self._title_generator.generate_title_with_date(text, timestamp)
        else:
            auto_title = self._title_generator.generate_title(text)

        # ── Темы (извлекаем ключевые слова текущей записи) ───────────────────
        # TopicTracker работает со списком элементов; передаём одну запись
        # и получаем её ключевые слова как topics.
        topics = self._extract_topics_for_item(item)

        elapsed = time.monotonic() - t0
        self._enriched_count += 1
        self._total_enrichment_time_sec += elapsed

        enriched = dict(item)
        enriched["metadata"] = {
            "word_count": word_count,
            "sentence_count": sentence_count,
            "avg_word_length": avg_wl,
            "language_detected": language_detected,
            "emotion": emotion,
            "speech_pace_wpm": speech_pace_wpm,
            "quality_grade": quality_grade,
            "auto_title": auto_title,
            "topics": topics,
            "enriched_at": _utc_now_iso(),
        }
        return enriched

    def enrich_batch(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Обогащает список записей истории.

        Args:
            items: список записей истории.

        Returns:
            Список обогащённых записей (порядок сохранён).
        """
        return [self.enrich(item) for item in items]

    def get_enrichment_stats(self) -> dict[str, Any]:
        """Возвращает статистику обогащения.

        Returns:
            Словарь::

                {
                    "items_enriched": int,
                    "avg_enrichment_time_sec": float,
                    "total_enrichment_time_sec": float,
                }
        """
        avg = (
            round(self._total_enrichment_time_sec / self._enriched_count, 6)
            if self._enriched_count > 0
            else 0.0
        )
        return {
            "items_enriched": self._enriched_count,
            "avg_enrichment_time_sec": avg,
            "total_enrichment_time_sec": round(self._total_enrichment_time_sec, 6),
        }

    # ── IPC-обработчик ────────────────────────────────────────────────────────

    def handle_enrich_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик метода ``enrich_recording``.

        Принимает params с ключом ``item`` (dict) или отдельные поля
        ``text``, ``duration_sec``, ``confidence`` и т.д.

        Returns:
            Словарь с ключами ``enriched_item`` и ``stats``.
        """
        item = params.get("item")
        if item is None:
            # Строим item из плоских params (совместимость с прямыми запросами)
            item = {
                "text": params.get("text", ""),
                "duration_sec": params.get("duration_sec", 0.0),
                "confidence": params.get("confidence", 0.0),
                "has_diarization": params.get("has_diarization", False),
                "has_llm_enhancement": params.get("has_llm_enhancement", False),
                "timestamp": params.get("timestamp", ""),
            }

        if not isinstance(item, dict):
            raise RuntimeError("Параметр item должен быть объектом")

        enriched = self.enrich(item)
        return {
            "enriched_item": enriched,
            "stats": self.get_enrichment_stats(),
        }

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _extract_topics_for_item(self, item: dict[str, Any]) -> list[str]:
        """Извлекает ключевые слова-темы из одной записи."""
        current = self._topic_tracker.get_current_topic([item], last_n=1)
        return current.get("topic_words", [])


# ── Вспомогательная функция ──────────────────────────────────────────────────

def _utc_now_iso() -> str:
    """Возвращает текущее время UTC в ISO 8601 (без внешних зависимостей)."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
