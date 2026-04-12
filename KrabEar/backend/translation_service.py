"""TranslationService — обработчики IPC-методов перевода.

Извлечено из service.py (BackendService) для уменьшения размера монолита.
Методы: translate_text, set/remove_translation_glossary_item,
get_glossary_suggestions, get_vocabulary_suggestions.
"""

from __future__ import annotations

import re
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.state_store import StateStore
    from backend.translator import Translator
    from backend.transcriber import Transcriber
    from backend.vocabulary_store import VocabularyStore


class TranslationService:
    """Обработчики IPC-команд, связанных с переводом и глоссарием."""

    # ── Стоп-слова для фильтрации vocabulary suggestions ──────────────
    _STOP_WORDS_RU = frozenset([
        "быть", "было", "была", "были", "буду", "будет", "будут",
        "этот", "этой", "этом", "этих", "этого", "этому",
        "который", "которая", "которое", "которые", "которого", "которой",
        "может", "можно", "могут", "можем",
        "если", "когда", "потом", "потому", "после", "перед",
        "очень", "более", "менее", "также", "тоже",
        "через", "между", "около", "вокруг",
        "нужно", "нужна", "надо", "просто",
        "здесь", "сейчас", "тогда", "всегда", "никогда",
        "ничего", "некоторые", "каждый", "другой", "другие",
        "такой", "такая", "такие", "такое",
        "свой", "свою", "свои", "своей", "своего",
        "весь", "вся", "всё", "все", "всех", "всем",
        "один", "одна", "одно", "одни",
        "наш", "наша", "наши", "ваш", "ваша", "ваши",
        "есть", "нет", "там", "тут", "еще", "ещё", "уже",
        "только", "самый", "самая", "самое",
        "хорошо", "ладно", "давай", "давайте",
    ])

    _STOP_WORDS_ES = frozenset([
        "pero", "para", "como", "desde", "este", "esta", "esto",
        "estos", "estas", "donde", "cuando", "porque", "aunque",
        "puede", "pueden", "podemos", "tiene", "tienen",
        "hace", "hacen", "está", "están", "sido", "haber",
        "también", "mucho", "mucha", "muchos", "muchas",
        "otro", "otra", "otros", "otras",
        "todo", "toda", "todos", "todas",
        "cada", "mismo", "misma", "mismos",
        "algo", "nada", "siempre", "nunca",
        "aquí", "ahora", "entonces", "después", "antes",
        "entre", "sobre", "contra", "hacia",
        "solo", "bueno", "bien", "vale",
    ])

    def __init__(
        self,
        translator: "Translator",
        store: "StateStore",
        cached_settings: Callable[[], dict[str, Any]],
        invalidate_settings_cache: Callable[[], None],
        vocabulary_store: "VocabularyStore | None" = None,
    ) -> None:
        self.translator = translator
        self.store = store
        self._cached_settings = cached_settings
        self._invalidate_settings_cache = invalidate_settings_cache
        self._vocabulary_store = vocabulary_store

    def handle_translate_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отдельная IPC-команда перевода текста для UI и будущих workflow."""
        text = str(params.get("text", "")).strip()
        mode = str(params.get("translation_mode", "off"))
        translation_style = str(params.get("translation_style", "neutral"))
        settings = self._cached_settings()
        network_mode = str(params.get("network_mode") or settings.get("network_mode", "offline_default"))
        glossary = settings.get("translation_glossary", {})
        result = self.translator.translate(
            text=text,
            mode=mode,
            network_mode=network_mode,
            translation_style=translation_style,
            glossary=glossary,
        )
        return {
            "text": result.text,
            "status": result.status,
            "source_lang": result.source_lang,
            "target_lang": result.target_lang,
            "translation_mode": result.mode,
            "translation_style": translation_style,
            "engine": result.engine,
        }

    def handle_set_translation_glossary_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет/обновляет одну пару глоссария перевода."""
        source = str(params.get("source", "")).strip()
        target = str(params.get("target", "")).strip()
        if not source or not target:
            raise RuntimeError("source и target обязательны")
        settings = self._cached_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary[source] = target
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return {"updated": True, "count": len(saved.get("translation_glossary", {}))}

    def handle_remove_translation_glossary_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет одну пару из глоссария перевода."""
        source = str(params.get("source", "")).strip()
        if not source:
            raise RuntimeError("source обязателен")
        settings = self._cached_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary.pop(source, None)
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return {"removed": True, "count": len(saved.get("translation_glossary", {}))}

    def handle_get_glossary_suggestions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует историю переводов и предлагает пары source→target для глоссария.

        Сканирует записи истории с source_text и translated_text, извлекает:
        - заглавные слова/фразы (имена собственные, бренды)
        - термины из BRAND_REPLACEMENTS
        - повторяющиеся слова в парах оригинал→перевод

        Возвращает кандидатов, которых ещё нет в текущем глоссарии.
        """
        from core.utils import _BRAND_REPLACEMENTS_RAW

        scan_limit = max(10, min(int(params.get("scan_limit", 200) or 200), 1000))
        min_count = max(2, min(int(params.get("min_count", 2) or 2), 20))
        top_k = max(5, min(int(params.get("top_k", 30) or 30), 100))

        items, _ = self.store.get_history_page(cursor=None, limit=scan_limit)

        # Загружаем текущий глоссарий — фильтруем уже добавленные пары
        settings = self._cached_settings()
        current_glossary: dict[str, str] = settings.get("translation_glossary", {}) or {}

        # Бренды из utils.py — канонические замены, которые стоит добавить в глоссарий
        brand_canonicals: list[str] = [canonical for _pat, canonical in _BRAND_REPLACEMENTS_RAW]

        # Собираем частоту заглавных слов и пары source→translated из истории
        pair_counts: dict[str, dict[str, int]] = {}  # source_word → {translated_word: count}
        capitalized_freq: dict[str, int] = {}

        for item in items:
            source_text = str(item.get("source_text", "") or "").strip()
            translated_text = str(item.get("translated_text", "") or "").strip()
            if not source_text or not translated_text:
                continue

            cap_words = re.findall(r"\b[A-ZА-Я][A-Za-zА-Яа-я]{2,}\b", source_text)
            for w in cap_words:
                capitalized_freq[w] = capitalized_freq.get(w, 0) + 1

            for src_word in set(cap_words):
                pattern = re.compile(r"\b" + re.escape(src_word) + r"\b", re.IGNORECASE)
                match = pattern.search(translated_text)
                if match:
                    found = match.group(0)
                    if src_word not in pair_counts:
                        pair_counts[src_word] = {}
                    pair_counts[src_word][found] = pair_counts[src_word].get(found, 0) + 1

        suggestions: list[dict] = []

        # 1. Пары из истории (src != target — реальный перевод)
        for src_word, trans_counts in pair_counts.items():
            if capitalized_freq.get(src_word, 0) < min_count:
                continue
            if src_word in current_glossary:
                continue
            best_target = max(trans_counts, key=lambda k: trans_counts[k])
            if src_word.lower() != best_target.lower():
                suggestions.append({
                    "source": src_word,
                    "target": best_target,
                    "count": capitalized_freq[src_word],
                    "origin": "history_pair",
                })

        # 2. Заглавные слова без явного перевода — пользователь уточнит target
        for word, count in capitalized_freq.items():
            if count < min_count:
                continue
            if word in current_glossary:
                continue
            if any(s["source"] == word for s in suggestions):
                continue
            suggestions.append({
                "source": word,
                "target": word,
                "count": count,
                "origin": "capitalized_term",
            })

        # 3. Бренды из BRAND_REPLACEMENTS — предлагаем зафиксировать в глоссарии
        for canonical in brand_canonicals:
            if canonical in current_glossary:
                continue
            if any(s["source"] == canonical for s in suggestions):
                continue
            suggestions.append({
                "source": canonical,
                "target": canonical,
                "count": 0,
                "origin": "brand_replacement",
            })

        # Сначала history_pair/capitalized_term по count desc, бренды в конце
        suggestions.sort(key=lambda s: (s["origin"] == "brand_replacement", -s["count"], s["source"]))
        top = suggestions[:top_k]

        return {
            "suggestions": top,
            "total_candidates": len(suggestions),
            "scanned_items": len(items),
            "current_glossary_size": len(current_glossary),
        }

    def handle_get_vocabulary_suggestions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует историю транскрибаций и предлагает слова для vocabulary.

        Сканирует последние N записей истории, находит слова с частотой >= min_count,
        фильтрует стоп-слова и короткие слова, возвращает top-K кандидатов.
        """
        scan_limit = max(10, min(int(params.get("scan_limit", 100) or 100), 500))
        min_count = max(2, min(int(params.get("min_count", 3) or 3), 20))
        top_k = max(5, min(int(params.get("top_k", 20) or 20), 50))
        min_word_len = max(2, min(int(params.get("min_word_len", 4) or 4), 10))

        # Собираем тексты из последних записей истории
        items, _ = self.store.get_history_page(cursor=None, limit=scan_limit)

        # Подсчёт частоты слов
        word_freq: dict[str, int] = {}
        for item in items:
            text = str(item.get("text", "") or "")
            source_text = str(item.get("source_text", "") or "")
            # Используем source_text (до перевода) если есть, иначе text
            raw = source_text if source_text else text
            words = re.findall(r"[A-Za-zА-Яа-яÁÉÍÓÚáéíóúÑñÜü0-9_-]{2,}", raw)
            for w in words:
                key = w.strip()
                if len(key) >= min_word_len:
                    word_freq[key] = word_freq.get(key, 0) + 1

        # Фильтрация стоп-слов и уже известных vocabulary
        if self._vocabulary_store is not None:
            current_vocab = set(self._vocabulary_store.load())
        else:
            current_vocab = set(self.store.load_vocabulary())
        stop_words = self._STOP_WORDS_RU | self._STOP_WORDS_ES
        candidates: list[tuple[str, int]] = []
        for word, count in word_freq.items():
            if count < min_count:
                continue
            lower = word.lower()
            if lower in stop_words:
                continue
            if word in current_vocab:
                continue
            candidates.append((word, count))

        # Сортируем по частоте (desc), потом по длине (desc) для стабильности
        candidates.sort(key=lambda x: (-x[1], -len(x[0]), x[0]))
        top = candidates[:top_k]

        return {
            "suggestions": [{"word": w, "count": c} for w, c in top],
            "total_candidates": len(candidates),
            "scanned_items": len(items),
            "current_vocabulary_size": len(current_vocab),
        }
