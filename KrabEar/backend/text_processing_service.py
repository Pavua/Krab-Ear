"""TextProcessingService — text analysis + post-processing IPC handlers.

Выделено из BackendService Wave 173.
Охватывает 11 обработчиков, связанных с анализом и обработкой текста транскрипций.

Связи модуля:
1) ReadabilityScorer    — оценка читабельности (Flesch score).
2) TranscriptionScorer  — composite quality score 0–100 (A–F).
3) EmotionDetector      — эвристическое определение эмоции.
4) TextComparator       — структурный diff/similarity между двумя текстами.
5) AbbreviationExpander — раскрытие аббревиатур RU/ES/EN.
6) TextPostProcessor    — конвейер пост-обработки текста.
7) llm_rewriter         — LLM-генерация summary (опционально, может быть None).
8) store                — StateStore для _handle_summarize_item (поиск по id).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("KrabEar.Backend.TextProcessing")


class TextProcessingService:
    """Обработчики IPC для анализа и пост-обработки текста транскрипций."""

    def __init__(
        self,
        *,
        readability_scorer: Any,
        transcription_scorer: Any,
        emotion_detector: Any,
        text_comparator: Any,
        abbreviation_expander: Any,
        text_postprocessor: Any,
        store: Any,
        llm_rewriter: Optional[Any] = None,
    ) -> None:
        """
        Args:
            readability_scorer:   ReadabilityScorer — Flesch score и сложность.
            transcription_scorer: TranscriptionScorer — composite quality 0–100.
            emotion_detector:     EmotionDetector — эвристическое определение эмоции.
            text_comparator:      TextComparator — diff/similarity между текстами.
            abbreviation_expander: AbbreviationExpander — раскрытие аббревиатур.
            text_postprocessor:   TextPostProcessor — конвейер пост-обработки.
            store:                StateStore — хранилище истории (для summarize_item).
            llm_rewriter:         LLMRewriter | None — LLM для summary; None = fallback.
        """
        self._readability_scorer = readability_scorer
        self._transcription_scorer = transcription_scorer
        self._emotion_detector = emotion_detector
        self._text_comparator = text_comparator
        self._abbreviation_expander = abbreviation_expander
        self._text_postprocessor = text_postprocessor
        self._store = store
        self._llm_rewriter = llm_rewriter

    # ------------------------------------------------------------------ #
    # summarize_text / summarize_item                                      #
    # ------------------------------------------------------------------ #

    def handle_summarize_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: summarize_text — локальный lightweight-summary для длинных транскриптов."""
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("text обязателен")
        mode = str(params.get("mode", "summary_short")).strip() or "summary_short"
        max_points = int(params.get("max_points", 3) or 3)
        max_points = max(1, min(max_points, 12))
        summary = self._summarize_text_locally(text=text, mode=mode, max_points=max_points)
        return {
            "mode": summary["mode"],
            "summary": summary["summary"],
            "bullets": summary["bullets"],
            "source_chars": len(text),
        }

    @staticmethod
    def _summarize_text_locally(text: str, mode: str, max_points: int) -> dict[str, Any]:
        """Простая эвристика summary без внешних зависимостей."""
        normalized = " ".join(text.replace("\r", "\n").split())
        if not normalized:
            return {"mode": mode, "summary": "", "bullets": []}

        chunks = []
        for raw in re.split(r"(?<=[.!?])\s+", normalized):
            sentence = raw.strip()
            if sentence:
                chunks.append(sentence)
        if not chunks:
            chunks = [normalized]

        if mode == "summary_detailed":
            bullets = chunks[:max_points]
            summary = " ".join(chunks[: min(len(chunks), max_points + 1)])
        else:
            # Короткий summary: первая смысловая фраза + маркеры.
            head = chunks[0]
            bullets = chunks[1: 1 + max_points]
            if not bullets:
                bullets = chunks[:max_points]
            summary = head
        return {"mode": mode, "summary": summary, "bullets": bullets}

    def _generate_summary(self, text: str) -> Optional[str]:
        """Генерирует краткое LLM-summary. Возвращает None если LLM недоступен."""
        if self._llm_rewriter is None:
            return None
        try:
            result = self._llm_rewriter.summarize(text, max_sentences=3)
            if result.ok and result.text:
                logger.info("LLM summary сгенерировано (%d мс)", result.latency_ms or 0)
                return result.text
            logger.debug("LLM summary не удалось: %s", result.fallback_reason)
            return None
        except Exception as exc:
            logger.warning("Ошибка генерации LLM summary: %s", exc)
            return None

    def handle_summarize_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: summarize_item — LLM-summary для элемента истории по ID."""
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("Параметр id обязателен")

        with self._store._lock():
            items = self._store._load_active_items_unlocked()
        target = None
        for item in items:
            if item.id == item_id:
                target = item
                break
        if target is None:
            raise RuntimeError(f"Элемент не найден: {item_id}")

        text = target.text or ""
        if len(text) < 50:
            raise RuntimeError("Текст слишком короткий для summary")

        summary = self._generate_summary(text)
        if summary is None:
            # Fallback на локальный summary
            local = self._summarize_text_locally(text, mode="summary_short", max_points=3)
            return {
                "id": item_id,
                "summary": local["summary"],
                "llm": False,
                "source_chars": len(text),
            }

        return {
            "id": item_id,
            "summary": summary,
            "llm": True,
            "source_chars": len(text),
        }

    # ------------------------------------------------------------------ #
    # compare_texts                                                        #
    # ------------------------------------------------------------------ #

    def handle_compare_texts(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: compare_texts — сравнивает два текста или две записи истории по ID."""
        item_id_1 = params.get("item_id_1")
        item_id_2 = params.get("item_id_2")
        text1 = params.get("text1", "")
        text2 = params.get("text2", "")

        if item_id_1 and item_id_2:
            result = self._text_comparator.compare_items(item_id_1, item_id_2, self._store)
        else:
            result = self._text_comparator.compare_texts(text1, text2)

        return {
            "similarity": result.similarity,
            "text_1": result.text_1,
            "text_2": result.text_2,
            "common_phrases": result.common_phrases,
            "unique_to_1": result.unique_to_1,
            "unique_to_2": result.unique_to_2,
            "word_count_diff": result.word_count_diff,
            "summary": result.summary,
        }

    # ------------------------------------------------------------------ #
    # score_readability / score_transcription                              #
    # ------------------------------------------------------------------ #

    def handle_score_readability(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: score_readability — оценивает читабельность текста транскрибации."""
        text = params.get("text", "")
        if not text:
            return {
                "flesch_score": 0.0,
                "avg_sentence_length": 0.0,
                "avg_word_length": 0.0,
                "vocabulary_level": "simple",
                "sentence_count": 0,
                "word_count": 0,
                "longest_sentence": "",
                "shortest_sentence": "",
            }
        report = self._readability_scorer.score(text)
        return {
            "flesch_score": report.flesch_score,
            "avg_sentence_length": report.avg_sentence_length,
            "avg_word_length": report.avg_word_length,
            "vocabulary_level": report.vocabulary_level,
            "sentence_count": report.sentence_count,
            "word_count": report.word_count,
            "longest_sentence": report.longest_sentence,
            "shortest_sentence": report.shortest_sentence,
        }

    def handle_score_transcription(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: score_transcription — оценивает качество транскрибации, балл 0–100 (A–F).

        Params:
            text (str): транскрибированный текст.
            confidence (float): уверенность STT-модели, 0.0–1.0.
            duration_sec (float): длительность аудио в секундах.
            has_diarization (bool, optional): была ли применена диаризация. Default False.
            has_llm_enhancement (bool, optional): был ли применён LLM-рерайтер. Default False.

        Returns:
            Словарь с полями QualityScore: overall_score, grade, factors, recommendations.
        """
        text = params.get("text", "")
        confidence = float(params.get("confidence", 0.0))
        duration_sec = float(params.get("duration_sec", 0.0))
        has_diarization = bool(params.get("has_diarization", False))
        has_llm_enhancement = bool(params.get("has_llm_enhancement", False))

        result = self._transcription_scorer.score(
            text=text,
            confidence=confidence,
            duration_sec=duration_sec,
            has_diarization=has_diarization,
            has_llm_enhancement=has_llm_enhancement,
        )
        return {
            "overall_score": result.overall_score,
            "grade": result.grade,
            "factors": result.factors,
            "recommendations": result.recommendations,
        }

    # ------------------------------------------------------------------ #
    # detect_emotion                                                       #
    # ------------------------------------------------------------------ #

    def handle_detect_emotion(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: detect_emotion — эвристическое определение эмоции в тексте транскрипции.

        Параметры:
            text     (str) — исходный текст для анализа.
            language (str) — язык текста ("ru", "es", "en"). По умолчанию "ru".

        Возвращает:
            primary_emotion, confidence, indicators, exclamation_count,
            question_count, caps_ratio
        """
        text = str(params.get("text", ""))
        language = str(params.get("language", "ru"))
        result = self._emotion_detector.detect(text, language=language)
        return {
            "primary_emotion": result.primary_emotion,
            "confidence": result.confidence,
            "indicators": result.indicators,
            "exclamation_count": result.exclamation_count,
            "question_count": result.question_count,
            "caps_ratio": result.caps_ratio,
        }

    # ------------------------------------------------------------------ #
    # expand_abbreviations / remove_abbreviation / list_abbreviations     #
    # ------------------------------------------------------------------ #

    def handle_expand_abbreviations(self, params: dict) -> dict:
        """IPC: expand_abbreviations — раскрыть аббревиатуры в тексте транскрипции.

        Params:
            text (str): Исходный текст.
            language (str, optional): Код языка (по умолчанию "ru").

        Returns:
            {"expanded": str, "changed": bool}
        """
        text = str(params.get("text", ""))
        language = str(params.get("language", "ru"))
        expanded = self._abbreviation_expander.expand(text, language=language)
        return {"expanded": expanded, "changed": expanded != text}

    def handle_add_abbreviation(self, params: dict) -> dict:
        """IPC: add_abbreviation — добавить пользовательскую аббревиатуру.

        Params:
            abbreviation (str): Аббревиатура (например, "т.н.").
            expansion    (str): Полная форма (например, "так называемый").
            language     (str, optional): Код языка (по умолчанию "ru").
            flags        (str, optional): Дополнительные флаги, например "no_after_digit".

        Returns:
            {"added": True, "abbreviation": str, "expansion": str, "language": str}

        Raises:
            ValueError: если ``abbreviation`` или ``expansion`` пустые.
        """
        abbr = str(params.get("abbreviation", "")).strip()
        expansion = str(params.get("expansion", "")).strip()
        language = str(params.get("language", "ru"))
        flags = str(params.get("flags", ""))
        if not abbr:
            raise ValueError("Параметр 'abbreviation' не может быть пустым")
        if not expansion:
            raise ValueError("Параметр 'expansion' не может быть пустым")
        self._abbreviation_expander.add_abbreviation(
            abbr, expansion, language=language, flags=flags
        )
        return {"added": True, "abbreviation": abbr, "expansion": expansion, "language": language}

    def handle_remove_abbreviation(self, params: dict) -> dict:
        """IPC: remove_abbreviation — удалить аббревиатуру.

        Params:
            abbr (str): Аббревиатура.
            language (str, optional): Код языка (по умолчанию "ru").

        Returns:
            {"removed": bool}
        """
        abbr = str(params.get("abbr", "")).strip()
        language = str(params.get("language", "ru"))
        removed = self._abbreviation_expander.remove_abbreviation(abbr, language=language)
        return {"removed": removed}

    def handle_list_abbreviations(self, params: dict) -> dict:
        """IPC: list_abbreviations — список аббревиатур для языка.

        Params:
            language (str, optional): Код языка (по умолчанию "ru").

        Returns:
            {"abbreviations": list[dict], "language": str, "count": int}
        """
        language = str(params.get("language", "ru"))
        abbreviations = self._abbreviation_expander.list_abbreviations(language=language)
        return {"abbreviations": abbreviations, "language": language, "count": len(abbreviations)}

    def handle_add_abbreviation(self, params: dict) -> dict:
        """IPC: add_abbreviation — добавить пользовательскую аббревиатуру.

        Params:
            abbr (str): Аббревиатура (например, "т.н.").
            expansion (str): Полная форма (например, "так называемый").
            language (str, optional): Код языка (по умолчанию "ru").
            flags (str, optional): Дополнительные флаги (например, "no_after_digit").

        Returns:
            {"added": bool, "abbr": str, "language": str}
        """
        abbr = str(params.get("abbr", "")).strip()
        expansion = str(params.get("expansion", "")).strip()
        language = str(params.get("language", "ru"))
        flags = str(params.get("flags", ""))
        if not abbr or not expansion:
            raise ValueError("abbr и expansion обязательны")
        self._abbreviation_expander.add_abbreviation(abbr, expansion, language=language, flags=flags)
        return {"added": True, "abbr": abbr, "language": language}

    # ------------------------------------------------------------------ #
    # post_process_text / list_post_process_steps                         #
    # ------------------------------------------------------------------ #

    def handle_post_process_text(self, params: dict) -> dict:
        """IPC: post_process_text — прогнать текст через конвейер пост-обработки.

        Params:
            text  (str)       — исходный текст для обработки.
            steps (list[str]) — список имён шагов в нужном порядке.
                                Если не указан, применяется цепочка по умолчанию:
                                [strip_whitespace, fix_punctuation, normalize_entities].

        Возвращает:
            text           — обработанный текст.
            steps_applied  — список имён выполненных шагов.
            changes_count  — число шагов, изменивших текст.
        """
        text = str(params.get("text", ""))
        steps = params.get("steps")  # None → цепочка по умолчанию
        if steps is not None and not isinstance(steps, list):
            raise ValueError("Параметр 'steps' должен быть списком строк или null")
        if steps is not None:
            steps = [str(s) for s in steps]

        result = self._text_postprocessor.process(text, steps=steps)
        return {
            "text": result.text,
            "steps_applied": result.steps_applied,
            "changes_count": result.changes_count,
        }

    def handle_list_post_process_steps(self, params: dict) -> dict:
        """IPC: list_post_process_steps — список доступных шагов пост-обработки.

        Возвращает:
            steps — список имён доступных шагов.
        """
        return {"steps": self._text_postprocessor.list_steps()}
