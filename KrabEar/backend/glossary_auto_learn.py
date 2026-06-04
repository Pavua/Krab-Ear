"""glossary_auto_learn.py — авто-обучение глоссария медицинских терминов.

Сканирует историю переводов, выявляет повторяющиеся термино-пары
(source_text ↔ translated_text) и классифицирует домен (medical / general).

Основной use-case: пользователь ведёт переписку с врачом на ES↔RU —
термины повторяются, сервис предлагает добавить их в translation_glossary.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("KrabEar.Backend.GlossaryAutoLearn")

# ── Ограничения глоссария (W1772) ──────────────────────────────────────────────
# Зеркало констант TranslationService.MAX_GLOSSARY_ENTRIES / MAX_TERM_BYTES.
# Обновлять синхронно при изменении translation_service.py.
MAX_GLOSSARY_ENTRIES: int = 500   # макс. число пар в translation_glossary
MAX_TERM_BYTES: int = 200         # макс. длина source/target в байтах UTF-8

# Мьютекс для сериализации read-modify-write глоссария внутри модуля (W1772).
# Best-effort: предотвращает TOCTOU при конкурентных вызовах apply_glossary_suggestions
# в пределах одного процесса.  Полный фикс требует инъекции SettingsService._save_lock
# (deferred: нужно изменить service.py — другой агент владеет им).
_apply_lock: threading.Lock = threading.Lock()

# ── Медицинские ключевые слова ─────────────────────────────────────────────────
# Используются для эвристики: если хотя бы одно из них встречается в тексте
# записи — считаем запись относящейся к «medical» домену.

_MEDICAL_KEYWORDS_RU: frozenset = frozenset([
    "боль", "болезнь", "диагноз", "симптом", "лечение", "лекарство",
    "препарат", "таблетка", "доза", "дозировка", "врач", "доктор",
    "пациент", "клиника", "больница", "анализ", "рецепт", "аллергия",
    "операция", "хирург", "процедура", "обследование", "давление",
    "температура", "инфекция", "вирус", "бактерия", "антибиотик",
    "вакцина", "прививка", "реабилитация", "диета", "витамин",
    "гормон", "стресс", "депрессия", "тревога", "мигрень", "судороги",
    "тошнота", "рвота", "кашель", "насморк", "бронхит", "астма",
    "сахарный", "диабет", "инсулин", "сердце", "давление", "сосуды",
    "почки", "печень", "желудок", "кишечник", "позвоночник", "сустав",
    "мышца", "нерв", "мозг", "онкология", "опухоль", "биопсия",
])

_MEDICAL_KEYWORDS_ES: frozenset = frozenset([
    "dolor", "enfermedad", "diagnóstico", "sintoma", "síntoma",
    "tratamiento", "medicamento", "pastilla", "dosis", "dosificación",
    "médico", "doctor", "paciente", "clínica", "hospital", "análisis",
    "receta", "alergia", "operación", "cirujano", "procedimiento",
    "examen", "presión", "temperatura", "infección", "virus",
    "bacteria", "antibiótico", "vacuna", "rehabilitación", "dieta",
    "vitamina", "hormona", "estrés", "depresión", "ansiedad",
    "migraña", "convulsión", "náusea", "vómito", "tos", "bronquitis",
    "asma", "diabetes", "insulina", "corazón", "vasos", "riñón",
    "hígado", "estómago", "intestino", "columna", "articulación",
    "músculo", "nervio", "cerebro", "oncología", "tumor", "biopsia",
])

_MEDICAL_KEYWORDS: frozenset = _MEDICAL_KEYWORDS_RU | _MEDICAL_KEYWORDS_ES

# Минимальная длина термина для предложения в глоссарий
_MIN_TERM_LENGTH = 6

# Regex: слово из кириллицы или латиницы (с акцентами)
_RE_WORD = re.compile(
    r"[А-Яа-яёЁA-Za-zÁÉÍÓÚáéíóúÑñÜü]{%d,}" % _MIN_TERM_LENGTH
)

# Общие стоп-слова, не интересные для глоссария
_STOP_WORDS: frozenset = frozenset([
    # RU
    "который", "которая", "которые", "которого", "которой",
    "очень", "более", "менее", "также", "тоже", "через", "между",
    "нужно", "нужна", "надо", "просто", "здесь", "сейчас", "тогда",
    "всегда", "никогда", "ничего", "некоторые", "каждый", "другой",
    "такой", "такая", "такие", "весь", "вся", "всё", "одна", "одно",
    "наш", "наша", "наши", "ваш", "ваша", "ваши",
    "хорошо", "ладно", "давай", "давайте",
    # ES
    "porque", "aunque", "también", "mucho", "mucha", "muchos", "muchas",
    "otro", "otra", "otros", "otras", "todo", "toda", "todos", "todas",
    "cada", "mismo", "misma", "algo", "nada", "siempre", "nunca",
    "aquí", "ahora", "entonces", "después", "antes", "entre", "sobre",
    "contra", "hacia", "solo", "bueno", "bien",
    # EN
    "that", "this", "with", "from", "have", "will", "would", "could",
    "should", "which", "their", "they", "them", "then", "than", "when",
    "what", "where", "but", "not", "are", "was", "were", "has", "had",
])


# ── Датаклассы ─────────────────────────────────────────────────────────────────


@dataclass
class GlossarySuggestion:
    """Кандидат на добавление в глоссарий перевода."""

    id: str                     # уникальный ключ (source_term)
    source_term: str            # термин в исходном языке
    target_term: str            # термин в целевом языке
    frequency: int              # сколько раз встретилась пара
    domain: str                 # "medical" или "general"
    confidence: float           # эвристическая уверенность 0–1


# ── Основной класс ─────────────────────────────────────────────────────────────


class GlossaryAutoLearn:
    """Авто-обучение глоссария из истории переводов.

    Алгоритм:
    1. Для каждой записи истории с non-empty source_text и translated_text
       извлекаем слова (>=6 символов, не стоп-слово).
    2. Подбираем «параллельные» слова: слово из source_text ↔ слово из
       translated_text с одинаковой позицией (с погрешностью).
    3. Считаем частоту каждой пары (source → target).
    4. Классифицируем домен: «medical» если в контексте записи есть хотя бы
       одно медицинское ключевое слово.
    5. Возвращаем пары с frequency >= 2, не входящие в уже известный глоссарий.
    """

    def suggest(
        self,
        items: List[Dict[str, Any]],
        existing_glossary: Optional[Dict[str, str]] = None,
        limit: int = 20,
    ) -> List[GlossarySuggestion]:
        """Предлагает новые пары для глоссария.

        Args:
            items: записи истории — list[dict] с полями source_text,
                   translated_text (опционально text как fallback).
            existing_glossary: текущий глоссарий {source: target};
                               уже существующие пары пропускаются.
            limit: максимальное количество предложений.

        Returns:
            list[GlossarySuggestion], отсортированный по frequency desc.
        """
        if not items:
            return []

        existing = {
            k.lower(): v.lower()
            for k, v in (existing_glossary or {}).items()
        }

        # Счётчики пар и доменов
        pair_freq: Counter = Counter()
        pair_domain_medical: Counter = Counter()

        for item in items:
            src_text = str(item.get("source_text") or "").strip()
            tgt_text = str(item.get("translated_text") or "").strip()

            if not src_text or not tgt_text:
                continue

            src_words = self._extract_terms(src_text)
            tgt_words = self._extract_terms(tgt_text)

            if not src_words or not tgt_words:
                continue

            # Определяем домен по контексту всей записи
            is_medical = self._is_medical_context(src_text + " " + tgt_text)

            # Строим пары по позициям (min-length alignment)
            pairs = self._align_pairs(src_words, tgt_words)

            for s, t in pairs:
                key = (s, t)
                pair_freq[key] += 1
                if is_medical:
                    pair_domain_medical[key] += 1

        # Фильтрация и сборка результата
        suggestions: List[GlossarySuggestion] = []
        for (src, tgt), freq in pair_freq.items():
            if freq < 2:
                continue
            if src in existing:
                continue
            if src == tgt:
                continue

            domain = "medical" if pair_domain_medical[(src, tgt)] > 0 else "general"

            # Confidence: базовая 0.5 + бонус за частоту + бонус за мед. домен
            conf = min(1.0, 0.5 + freq * 0.1 + (0.15 if domain == "medical" else 0.0))

            suggestions.append(GlossarySuggestion(
                id=src,
                source_term=src,
                target_term=tgt,
                frequency=freq,
                domain=domain,
                confidence=round(conf, 3),
            ))

        # Сортируем: medical выше, потом по frequency
        suggestions.sort(key=lambda x: (x.domain != "medical", -x.frequency, x.source_term))
        return suggestions[:limit]

    # ── Приватные методы ───────────────────────────────────────────────────────

    def _extract_terms(self, text: str) -> List[str]:
        """Извлекает уникальные термины (>= _MIN_TERM_LENGTH, не стоп-слово)."""
        words = _RE_WORD.findall(text)
        seen: set = set()
        result: List[str] = []
        for w in words:
            lo = w.lower()
            if lo in _STOP_WORDS:
                continue
            if lo not in seen:
                seen.add(lo)
                result.append(lo)
        return result

    @staticmethod
    def _is_medical_context(text: str) -> bool:
        """Возвращает True если текст содержит хотя бы одно мед. ключевое слово."""
        text_lo = text.lower()
        for kw in _MEDICAL_KEYWORDS:
            if kw in text_lo:
                return True
        return False

    @staticmethod
    def _align_pairs(
        src_words: List[str],
        tgt_words: List[str],
    ) -> List[tuple]:
        """Возвращает «параллельные» пары слов по позициям с выравниванием.

        Использует zip — совмещает слова по позиции. Это работает для
        коротких синтаксических конструкций (термины обычно в начале или
        середине фразы и сохраняют порядок при ES↔RU переводе).
        """
        return list(zip(src_words, tgt_words))


# ── IPC-обработчики ────────────────────────────────────────────────────────────


class GlossaryAutoLearnService:
    """IPC-обёртка для GlossaryAutoLearn, интегрированная в BackendService."""

    def __init__(
        self,
        store: Any,
        cached_settings: Any,
        invalidate_settings_cache: Any,
    ) -> None:
        self._store = store
        self._cached_settings = cached_settings
        self._invalidate_settings_cache = invalidate_settings_cache
        self._learner = GlossaryAutoLearn()

    def handle_suggest_medical_glossary_terms(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """IPC: предлагает термино-пары из истории переводов.

        Params:
            limit (int, default 20): максимум предложений.

        Returns:
            {"suggestions": [{id, source_term, target_term, frequency,
                               domain, confidence}]}

        Privacy gate (wave-29): когда privacy_mode_enabled=True возвращает пустые предложения
        без обращения к истории переводов — утечка medical domain term pairs нарушает
        режим конфиденциальности.
        """
        if (self._cached_settings() or {}).get("privacy_mode_enabled"):
            return {"suggestions": []}

        limit = int(params.get("limit", 20))
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        try:
            items_raw, _ = self._store.get_history_page(cursor=None, limit=500)
        except Exception as exc:
            logger.error("suggest_medical_glossary_terms: history load error: %s", exc)
            return {"suggestions": []}

        items = [
            i.to_dict() if hasattr(i, "to_dict") else dict(i)
            for i in (items_raw or [])
        ]

        settings = self._cached_settings()
        existing_glossary: Dict[str, str] = settings.get("translation_glossary") or {}

        suggestions = self._learner.suggest(
            items=items,
            existing_glossary=existing_glossary,
            limit=limit,
        )

        return {
            "suggestions": [
                {
                    "id": s.id,
                    "source_term": s.source_term,
                    "target_term": s.target_term,
                    "frequency": s.frequency,
                    "domain": s.domain,
                    "confidence": s.confidence,
                }
                for s in suggestions
            ]
        }

    def handle_apply_glossary_suggestions(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """IPC: добавляет выбранные предложения в translation_glossary.

        Params:
            selected_ids (list[str]): список source_term для добавления.
            suggestions (list[dict]): полный список предложений от
                suggest_medical_glossary_terms (нужен для поиска target_term).

        Returns:
            {"applied": int, "skipped": int, "total_glossary": int}
        """
        selected_ids: List[str] = [
            str(x) for x in (params.get("selected_ids") or [])
        ]
        raw_suggestions: List[Dict[str, Any]] = list(
            params.get("suggestions") or []
        )

        if not selected_ids:
            return {"applied": 0, "skipped": 0, "total_glossary": 0}

        # Строим lookup source_term → target_term из переданных suggestions
        term_map: Dict[str, str] = {
            str(s.get("source_term", "")).lower(): str(s.get("target_term", ""))
            for s in raw_suggestions
            if s.get("source_term") and s.get("target_term")
        }

        # W1772: best-effort сериализация read-modify-write в пределах процесса.
        # Предотвращает TOCTOU lost-update при конкурентных вызовах apply_glossary_suggestions.
        # Полный fix требует инъекции SettingsService._save_lock в __init__
        # (deferred: service.py владеет другой агент).
        with _apply_lock:
            settings = self._cached_settings()
            glossary: Dict[str, str] = dict(settings.get("translation_glossary") or {})
            existing_lower: frozenset = frozenset(k.lower() for k in glossary)

            applied = 0
            skipped = 0
            for sid in selected_ids:
                sid_lo = sid.lower()
                target = term_map.get(sid_lo)
                if not target:
                    skipped += 1
                    continue
                if sid_lo in existing_lower:
                    skipped += 1
                    continue

                # W1772 Fix 1: проверка лимита числа записей
                if len(glossary) >= MAX_GLOSSARY_ENTRIES:
                    logger.warning(
                        "apply_glossary_suggestions: entry limit reached, skipping term",
                        extra={
                            "term": sid_lo,
                            "glossary_size": len(glossary),
                            "limit": MAX_GLOSSARY_ENTRIES,
                        },
                    )
                    skipped += 1
                    continue

                # W1772 Fix 1: проверка длины термина в байтах
                src_bytes = len(sid_lo.encode("utf-8"))
                tgt_bytes = len(target.encode("utf-8"))
                if src_bytes > MAX_TERM_BYTES:
                    logger.warning(
                        "apply_glossary_suggestions: source term too long, skipping",
                        extra={
                            "term": sid_lo,
                            "bytes": src_bytes,
                            "limit": MAX_TERM_BYTES,
                        },
                    )
                    skipped += 1
                    continue
                if tgt_bytes > MAX_TERM_BYTES:
                    logger.warning(
                        "apply_glossary_suggestions: target term too long, skipping",
                        extra={
                            "term": sid_lo,
                            "target_bytes": tgt_bytes,
                            "limit": MAX_TERM_BYTES,
                        },
                    )
                    skipped += 1
                    continue

                glossary[sid_lo] = target
                existing_lower = existing_lower | {sid_lo}
                applied += 1

            if applied > 0:
                settings["translation_glossary"] = glossary
                try:
                    saved = self._store.save_settings(settings)
                    self._invalidate_settings_cache()
                    total = len(saved.get("translation_glossary") or glossary)
                except Exception as exc:
                    logger.error(
                        "apply_glossary_suggestions: save error: %s", exc,
                        extra={"applied": applied},
                    )
                    total = len(glossary)
            else:
                total = len(glossary)

        return {"applied": applied, "skipped": skipped, "total_glossary": total}
