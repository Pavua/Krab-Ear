"""TranslationService — обработчики IPC-методов перевода.

Извлечено из service.py (BackendService) для уменьшения размера монолита.
Методы: translate_text, translate_selection,
set/remove_translation_glossary_item,
get_glossary_suggestions, get_vocabulary_suggestions.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, TYPE_CHECKING

from backend.observability import add_breadcrumb
from core.language_detector import LanguageDetector

if TYPE_CHECKING:
    from backend.settings_service import SettingsService
    from backend.state_store import StateStore
    from backend.translator import Translator
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

    # Карта авто-определения направления: detected_lang → translation_mode
    _AUTO_DIRECTION: dict[str, str] = {
        "ru": "ru_to_es",
        "es": "es_to_ru",
        "en": "en_to_ru",
        "uk": "en_to_ru",  # украинский → русский как наиболее близкий
    }

    def __init__(
        self,
        translator: "Translator",
        store: "StateStore",
        cached_settings: Callable[[], dict[str, Any]],
        invalidate_settings_cache: Callable[[], None],
        vocabulary_store: "VocabularyStore | None" = None,
        settings_svc: "SettingsService | None" = None,
    ) -> None:
        self.translator = translator
        self.store = store
        self._cached_settings = cached_settings
        self._invalidate_settings_cache = invalidate_settings_cache
        self._vocabulary_store = vocabulary_store
        # W1767: инъекция SettingsService для атомарного read-modify-write глоссария
        # через _save_lock, предотвращающая TOCTOU lost-update.
        self._settings_svc: "SettingsService | None" = settings_svc
        self._lang_detector = LanguageDetector()

    def handle_translate_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отдельная IPC-команда перевода текста для UI и будущих workflow."""
        text = str(params.get("text", "")).strip()
        mode = str(params.get("translation_mode", "off"))
        translation_style = str(params.get("translation_style", "neutral"))
        settings = self._cached_settings()
        original_network_mode = str(params.get("network_mode") or settings.get("network_mode", "offline_default"))
        network_mode = original_network_mode
        # Privacy mode: force offline translation — no external network requests.
        if settings.get("privacy_mode_enabled"):
            if original_network_mode != "offline_strict":
                try:
                    from backend.privacy_audit import get_privacy_audit_logger  # noqa: PLC0415
                    get_privacy_audit_logger().log_event(
                        category="translation",
                        action="forced_offline",
                        details={
                            "original_mode": original_network_mode,
                            "method": "handle_translate_text",
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
            network_mode = "offline_strict"
        glossary = settings.get("translation_glossary", {})
        result = self.translator.translate(
            text=text,
            mode=mode,
            network_mode=network_mode,
            translation_style=translation_style,
            glossary=glossary,
        )
        add_breadcrumb(
            category="translation",
            message="translate_text",
            level="info",
            data={
                "source_lang": result.source_lang or "auto",
                "target_lang": result.target_lang or "auto",
                "mode": mode,
                "engine": result.engine or "none",
            },
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

    def handle_translate_selection(self, params: dict[str, Any]) -> dict[str, Any]:
        """Переводит выделенный текст из любого приложения (Phase 2A workflow).

        Параметры:
            text        — выделенный текст (обязательно).
            source_lang — ISO-код языка источника (ru/es/en). Опционально;
                          если не задан — определяется автоматически через
                          LanguageDetector.
            target_lang — ISO-код языка перевода (ru/es/en). Опционально;
                          если не задан — определяется по умолчанию:
                          ru→es, es→ru, en→ru, else ru.

        Возвращает:
            translated_text      — результат перевода (пустая строка если text пуст).
            source_lang_detected — определённый или переданный язык источника.
            target_lang          — целевой язык перевода.
            engine               — движок перевода.
            latency_ms           — время обработки в миллисекундах.
        """
        t0 = time.monotonic()
        text = str(params.get("text", "")).strip()

        # Пустой текст — возвращаем быстрый ответ без ошибки
        if not text:
            return {
                "translated_text": "",
                "source_lang_detected": "",
                "target_lang": "",
                "engine": "none",
                "latency_ms": 0,
            }

        # Определяем язык источника
        source_lang = str(params.get("source_lang") or "").strip().lower()
        if not source_lang:
            detected = self._lang_detector.detect(text)
            source_lang = detected.language if detected.language != "und" else "ru"

        # Определяем целевой язык
        target_lang = str(params.get("target_lang") or "").strip().lower()
        if not target_lang:
            # ru→es, es→ru, en→ru, остальное → ru
            target_map = {"ru": "es", "es": "ru", "en": "ru"}
            target_lang = target_map.get(source_lang, "ru")

        # Формируем режим перевода
        _mode_map = {
            ("ru", "es"): "ru_to_es",
            ("es", "ru"): "es_to_ru",
            ("en", "ru"): "en_to_ru",
            ("uk", "ru"): "en_to_ru",
        }
        mode = _mode_map.get((source_lang, target_lang))
        if mode is None:
            # Неизвестная пара → авто-режим с fallback на ru_to_es
            mode = self._AUTO_DIRECTION.get(source_lang, "ru_to_es")

        settings = self._cached_settings()
        original_network_mode = str(settings.get("network_mode", "offline_default"))
        network_mode = original_network_mode
        # Privacy mode: force offline translation — no external network requests.
        if settings.get("privacy_mode_enabled"):
            if original_network_mode != "offline_strict":
                try:
                    from backend.privacy_audit import get_privacy_audit_logger  # noqa: PLC0415
                    get_privacy_audit_logger().log_event(
                        category="translation",
                        action="forced_offline",
                        details={
                            "original_mode": original_network_mode,
                            "method": "handle_translate_selection",
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
            network_mode = "offline_strict"
        translation_style = str(settings.get("translation_style", "neutral"))
        glossary = settings.get("translation_glossary", {})

        result = self.translator.translate(
            text=text,
            mode=mode,
            network_mode=network_mode,
            translation_style=translation_style,
            glossary=glossary,
        )

        latency_ms = int((time.monotonic() - t0) * 1000)
        add_breadcrumb(
            category="translation",
            message="translate_selection",
            level="info",
            data={
                "source_lang": source_lang,
                "target_lang": target_lang,
                "char_count": len(text),
                "engine": result.engine or "none",
                "duration_ms": latency_ms,
            },
        )
        return {
            "translated_text": result.text,
            "source_lang_detected": source_lang,
            "target_lang": target_lang,
            "engine": result.engine,
            "latency_ms": latency_ms,
        }

    # ------------------------------------------------------------------
    # W1767: атомарный read-modify-write глоссария через _save_lock
    # ------------------------------------------------------------------

    def _locked_set_glossary_item(self, source: str, target: str) -> int:
        """Атомарно добавляет/обновляет пару глоссария под _save_lock SettingsService.

        Если _settings_svc инжектирован — операция выполняется под его RLock
        (разделяемым со всеми записями настроек), что предотвращает TOCTOU
        lost-update при конкурентных вызовах set_settings / set_translation_glossary_item.

        Fallback (без settings_svc): прежнее поведение через store.save_settings.
        Возвращает итоговый размер глоссария.
        """
        if self._settings_svc is not None:
            # Атомарный путь: вся операция read→merge→write под одним lock
            with self._settings_svc._save_lock:
                current = self._settings_svc.cached_settings()
                glossary = dict(current.get("translation_glossary", {}) or {})
                glossary[source] = target
                # Обновляем через locked-путь SettingsService напрямую
                result = self._settings_svc._handle_set_settings_locked(
                    {"translation_glossary": glossary}
                )
                saved_glossary = result.get("translation_glossary", glossary)
                if isinstance(saved_glossary, dict):
                    return len(saved_glossary)
                return len(glossary)
        # Fallback: прежнее поведение (без lock-защиты)
        settings = self._cached_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary[source] = target
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return len(saved.get("translation_glossary", {}))

    def _locked_remove_glossary_item(self, source: str) -> int:
        """Атомарно удаляет пару глоссария под _save_lock SettingsService.

        Аналогичная защита от TOCTOU что и _locked_set_glossary_item.
        Возвращает итоговый размер глоссария.
        """
        if self._settings_svc is not None:
            with self._settings_svc._save_lock:
                current = self._settings_svc.cached_settings()
                glossary = dict(current.get("translation_glossary", {}) or {})
                glossary.pop(source, None)
                result = self._settings_svc._handle_set_settings_locked(
                    {"translation_glossary": glossary}
                )
                saved_glossary = result.get("translation_glossary", glossary)
                if isinstance(saved_glossary, dict):
                    return len(saved_glossary)
                return len(glossary)
        # Fallback: прежнее поведение (без lock-защиты)
        settings = self._cached_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary.pop(source, None)
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return len(saved.get("translation_glossary", {}))

    def handle_set_translation_glossary_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет/обновляет одну пару глоссария перевода.

        W1767: запись выполняется через _locked_set_glossary_item, которая
        удерживает SettingsService._save_lock на всё время read-modify-write,
        исключая TOCTOU lost-update при конкурентных записях настроек.
        """
        source = str(params.get("source", "")).strip()
        target = str(params.get("target", "")).strip()
        if not source or not target:
            raise RuntimeError("source и target обязательны")
        glossary_count = self._locked_set_glossary_item(source, target)
        add_breadcrumb(
            category="translation",
            message="set_translation_glossary_item",
            level="info",
            data={
                "source_char_count": len(source),
                "target_char_count": len(target),
                "glossary_size": glossary_count,
            },
        )
        return {"updated": True, "count": glossary_count}

    def handle_remove_translation_glossary_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет одну пару из глоссария перевода.

        W1767: запись выполняется через _locked_remove_glossary_item под
        SettingsService._save_lock — атомарный read-modify-write.
        """
        source = str(params.get("source", "")).strip()
        if not source:
            raise RuntimeError("source обязателен")
        glossary_count = self._locked_remove_glossary_item(source)
        return {"removed": True, "count": glossary_count}

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

        # Бренды из utils.py — канонические замены, которые стоит добавить в глоссарий.
        # Filter to str только: некоторые entries в _BRAND_REPLACEMENTS_RAW используют
        # lambda replacements (для regex backreferences), мы их не предлагаем как канон.
        brand_canonicals: list[str] = [
            canonical
            for _pat, canonical in _BRAND_REPLACEMENTS_RAW
            if isinstance(canonical, str)
        ]

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

        add_breadcrumb(
            category="translation",
            message="get_glossary_suggestions",
            level="info",
            data={
                "scanned_items": len(items),
                "total_candidates": len(suggestions),
                "returned": len(top),
                "current_glossary_size": len(current_glossary),
            },
        )
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
