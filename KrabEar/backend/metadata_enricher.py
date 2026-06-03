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

Wave 1765 — ReDoS-backstop + privacy-guard wire
-----------------------------------------------
HIGH ReDoS (backstop):
  Исходный _SENTENCE_SPLIT_RE = r"[.!?…]+\\s+" применялся к тексту транскрипта без
  ограничения длины входа.  Паттерн сам по себе не содержит вложенных квантификаторов
  и не вызывает классического catastrophic backtracking, НО при патологически длинном
  вводе (сотни тысяч символов) он работает O(N) без backstop.  Исправления:
    1. Добавлен _MAX_TEXT_LEN = 200 000 символов — backstop перед ЛЮБЫМИ regex-
       операциями над контролируемым пользователем текстом (_clip_text).
    2. Паттерн переписан с явной non-capturing-group: r"(?:[.!?…]+)\\s+" —
       семантически идентичен, читаемость и аудитабельность улучшены.
    3. Структурированное предупреждение при усечении (без содержимого текста).

MED dead-privacy-guard (wire):
  settings_provider ранее был None (MetadataEnricher() без аргументов в service.py:598),
  из-за чего _get_runtime_setting('privacy_mode_enabled') всегда возвращал default=False.
  Исправлено в service.py: MetadataEnricher(settings_provider=self._get_runtime_setting) —
  зеркалит паттерн AutoDeduplicator (строка 601) и translator (строка 220).
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Callable, Optional

from core.auto_title import AutoTitleGenerator
from core.emotion_detector import EmotionDetector
from core.language_detector import LanguageDetector
from core.speech_pace import SpeechPaceAnalyzer
from core.topic_tracker import TopicTracker
from core.transcription_scorer import TranscriptionScorer

logger = logging.getLogger("KrabEar.Backend.MetadataEnricher")

# Backstop длины текста перед regex-операциями (символов).
# Транскрипты Krab Ear << 200 000 символов; это щедрый предел для
# импортированных файлов и одновременно backstop против O(N) regex на длинном вводе.
_MAX_TEXT_LEN: int = 200_000

# Паттерн для токенизации слов (кириллица + латиница).
# Не содержит вложенных квантификаторов — безопасен.
_WORD_RE = re.compile(
    r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+(?:[-'][А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]+)*"
)

# Символы-терминаторы предложений и пробельные символы для char-scan.
# Wave 1765: _count_sentences переписан на O(N) char-scan без regex,
# чтобы исключить O(N²) поведение на патологическом вводе (например, "."*50000).
_SENTENCE_TERMINATORS: frozenset[str] = frozenset(".!?…")
_WHITESPACE_CHARS: frozenset[str] = frozenset(" \t\n\r")


# ---------------------------------------------------------------------------
# Numeric safety guard (Wave 23 — MED NaN in IPC JSON + LOW OverflowError)
# ---------------------------------------------------------------------------
# float("nan") and float("inf") are NOT valid JSON (RFC 8259).  Swift's
# JSONDecoder rejects them and the entire IPC response is silently dropped.
# TranscriptionScorer also raises OverflowError on Inf duration_sec.
# Mirror the _safe_float pattern from core/audio_quality.py.

def _safe_float(v: Any, default: float = 0.0) -> float:
    """Coerce NaN / Inf / non-numeric input to a finite default (Wave 23).

    Returns *default* if *v* is:
    - not a numeric type (int/float),
    - NaN  (math.isnan),
    - infinite (math.isinf).

    Always returns a value that round-trips through
    ``json.dumps(..., allow_nan=False)`` without ValueError.
    """
    if not isinstance(v, (int, float)):
        return default
    if not math.isfinite(v):
        return default
    return float(v)


def _clip_text(text: str, caller: str = "") -> str:
    """Обрезает текст до _MAX_TEXT_LEN символов с предупреждением (Wave 1765).

    Backstop против O(N) regex-обработки для патологически длинного ввода.
    Предупреждение НЕ включает содержимое текста (privacy-safe).

    Args:
        text: Входной текст (возможно, контролируемый пользователем).
        caller: Имя вызывающей функции для диагностики.

    Returns:
        text[:_MAX_TEXT_LEN] если text длиннее _MAX_TEXT_LEN, иначе text.
    """
    if len(text) <= _MAX_TEXT_LEN:
        return text
    logger.warning(
        "MetadataEnricher: текст усечён перед regex-операцией",
        extra={"original_len": len(text), "max_len": _MAX_TEXT_LEN, "caller": caller},
    )
    return text[:_MAX_TEXT_LEN]


def _count_sentences(text: str) -> int:
    """Считает число предложений в тексте по знакам . ! ? …

    Wave 1765 ReDoS-fix: реализован как O(N) char-scan без regex.
    До W1765 использовался re.split(r"[.!?…]+\\s+", text), который на вводе
    типа "."*50000 работал O(N²) — CPython re-движок перебирал все позиции.
    Новая реализация семантически идентична, но принципиально безопасна.

    Алгоритм: подсчитываем «границы предложений» — переходы «терминатор(ы)→
    пробельный символ(ы)»; число предложений = границы + 1 (для непустого текста).

    Args:
        text: Транскрибированный текст (возможно, контролируемый пользователем).
              Backstop-усечение выполняется в вызывающем коде (enrich).

    Returns:
        Целое число ≥ 0.  0 для пустой строки, ≥ 1 для непустой.
    """
    if not text or not text.strip():
        return 0
    n = len(text)
    boundaries = 0
    i = 0
    while i < n:
        ch = text[i]
        if ch in _SENTENCE_TERMINATORS:
            # Потребляем непрерывную серию терминаторов (…!!! ??? и т.п.)
            j = i + 1
            while j < n and text[j] in _SENTENCE_TERMINATORS:
                j += 1
            # Если после серии следует пробельный символ — это граница предложения.
            if j < n and text[j] in _WHITESPACE_CHARS:
                boundaries += 1
            i = j
        else:
            i += 1
    return max(1, boundaries + 1)


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

    def __init__(
        self,
        settings_provider: Optional[Callable[[], dict]] = None,
    ) -> None:
        """Инициализация обогатителя метаданных.

        Args:
            settings_provider: опциональный callable, возвращающий текущий dict
                настроек (runtime). Используется для проверки ``privacy_mode_enabled``.
                Если ``None`` — приватный режим считается выключенным (W1277 F5).
        """
        self._settings_provider = settings_provider
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

    def enrich(
        self,
        item: dict[str, Any],
        privacy_mode: bool = False,  # kept for backwards compat; overridden by settings_provider
    ) -> dict[str, Any]:
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
            privacy_mode: если True — поле ``topics`` не заполняется (W1277 F5).

        Returns:
            Новый словарь с добавленным ключом ``metadata``.
        """
        t0 = time.monotonic()

        text: str = str(item.get("text") or "")
        # Wave 23: guard against NaN/Inf before passing to TranscriptionScorer
        # (OverflowError on Inf) and before serialising to IPC JSON (NaN is
        # not RFC 8259-compliant; Swift JSONDecoder rejects the whole response).
        duration_sec: float = _safe_float(
            float(item.get("duration_sec") or 0.0), default=0.0
        )
        confidence: float = _safe_float(
            float(item.get("confidence") or 0.0), default=0.0
        )
        has_diarization: bool = bool(item.get("has_diarization", False))
        has_llm: bool = bool(item.get("has_llm_enhancement", False))

        # Wave 1765: backstop — обрезаем текст ОДИН РАЗ перед всеми regex-операциями.
        # _count_sentences тоже вызывает _clip_text, но лучше одноразовое усечение здесь.
        safe_text: str = _clip_text(text, caller="enrich")

        # ── Базовые текстовые метрики ────────────────────────────────────────
        words = _WORD_RE.findall(safe_text)
        word_count = len(words)
        sentence_count = _count_sentences(safe_text)
        avg_wl = _avg_word_length(words)

        # ── Определение языка ────────────────────────────────────────────────
        lang_result = self._language_detector.detect(safe_text)
        language_detected = lang_result.language

        # ── Определение эмоции ───────────────────────────────────────────────
        emotion_result = self._emotion_detector.detect(safe_text, language=language_detected)
        emotion = emotion_result.primary_emotion

        # ── Анализ темпа речи ────────────────────────────────────────────────
        pace_report = self._pace_analyzer.analyze(safe_text, duration_sec=duration_sec)
        speech_pace_wpm = pace_report.words_per_minute

        # ── Оценка качества транскрибации ────────────────────────────────────
        quality_score = self._scorer.score(
            text=safe_text,
            confidence=confidence,
            duration_sec=duration_sec,
            has_diarization=has_diarization,
            has_llm_enhancement=has_llm,
        )
        quality_grade = quality_score.grade

        # ── Авто-заголовок ───────────────────────────────────────────────────
        timestamp = str(item.get("timestamp") or "")
        if timestamp:
            auto_title = self._title_generator.generate_title_with_date(safe_text, timestamp)
        else:
            auto_title = self._title_generator.generate_title(safe_text)

        # ── Темы (извлекаем ключевые слова текущей записи) ───────────────────
        # TopicTracker работает со списком элементов; передаём одну запись
        # и получаем её ключевые слова как topics.
        # W1277 F5: skip topic extraction in privacy_mode — transcript content
        # must not be processed beyond the minimum required for STT output.
        # Use settings_provider (runtime) if available, else fall back to parameter.
        effective_privacy = bool(self._get_runtime_setting("privacy_mode_enabled", privacy_mode))
        if effective_privacy:
            topics: list[str] = []
            logger.debug("MetadataEnricher: topic enrichment skipped (privacy_mode_enabled=True)")
        else:
            topics = self._extract_topics_for_item(item)

        elapsed = time.monotonic() - t0
        self._enriched_count += 1
        self._total_enrichment_time_sec += elapsed

        enriched = dict(item)
        # Wave 23: sanitise any non-finite float fields copied verbatim from
        # the input item so the full response is JSON-safe (allow_nan=False).
        for _field in ("duration_sec", "confidence"):
            if _field in enriched and isinstance(enriched[_field], (int, float)):
                enriched[_field] = _safe_float(enriched[_field], default=0.0)
        enriched["metadata"] = {
            "word_count": word_count,
            "sentence_count": sentence_count,
            "avg_word_length": avg_wl,
            "language_detected": language_detected,
            "emotion": emotion,
            # Wave 23: guard float metrics so the dict is always JSON-safe.
            "speech_pace_wpm": _safe_float(speech_pace_wpm, default=0.0),
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

        privacy_mode = bool(params.get("privacy_mode", False))
        enriched = self.enrich(item, privacy_mode=privacy_mode)
        return {
            "enriched_item": enriched,
            "stats": self.get_enrichment_stats(),
        }

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _extract_topics_for_item(self, item: dict[str, Any]) -> list[str]:
        """Извлекает ключевые слова-темы из одной записи."""
        current = self._topic_tracker.get_current_topic([item], last_n=1)
        return current.get("topic_words", [])

    def _get_runtime_setting(self, key: str, default: Any = None) -> Any:
        """Читает runtime-настройку через settings_provider.

        Если ``settings_provider`` не задан или выбросил исключение,
        возвращает ``default`` (W1277 F5 — безопасный fallback).
        """
        if self._settings_provider is None:
            return default
        try:
            return self._settings_provider().get(key, default)
        except Exception:
            return default


# ── Вспомогательная функция ──────────────────────────────────────────────────

def _utc_now_iso() -> str:
    """Возвращает текущее время UTC в ISO 8601 (без внешних зависимостей)."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
