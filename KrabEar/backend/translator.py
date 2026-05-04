"""Offline-first переводчик для backend-сервиса Krab Ear.

Модуль покрывает режимы:
1) RU -> ES
2) ES -> RU
3) EN -> RU
4) Auto (эвристический выбор пары)
5) Bilingual RU<->ES (два языка в одном сообщении)

Ключевые принципы:
- по умолчанию данные не уходят в сеть;
- при недоступности модели возвращаем статус, не ломая основной STT pipeline;
- результаты кэшируются в памяти для realtime/повторных запросов.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
import re
from typing import Any

# Profiler singleton — защищаемся от ImportError чтобы translator оставался standalone.
try:
    from backend.performance_profiler import profiler as _profiler
except Exception:  # pragma: no cover — defensive
    class _NoOpSpan:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _NoOpProfiler:
        def start_span(self, name: str):
            return _NoOpSpan()

    _profiler = _NoOpProfiler()  # type: ignore[assignment]

logger = logging.getLogger("KrabEar.Backend.Translator")


@dataclass(slots=True)
class TranslationResult:
    """Результат перевода для дальнейшей вставки и истории."""

    text: str
    status: str
    source_lang: str
    target_lang: str
    mode: str
    engine: str

    @property
    def ok(self) -> bool:
        """Успешен ли перевод."""
        return self.status == "ok" and bool(self.text.strip())


class Translator:
    """Offline-first адаптер перевода с ленивой загрузкой моделей."""

    _MODEL_BY_MODE = {
        "ru_to_es": "Helsinki-NLP/opus-mt-ru-es",
        "es_to_ru": "Helsinki-NLP/opus-mt-es-ru",
        "en_to_ru": "Helsinki-NLP/opus-mt-en-ru",
    }
    _SUPPORTED_MODES = {"off", "ru_to_es", "es_to_ru", "en_to_ru", "auto", "auto_to_ru", "bilingual_ru_es"}

    def __init__(self) -> None:
        self._pipelines: dict[tuple[str, bool], Any] = {}
        self._unavailable: set[tuple[str, bool]] = set()
        self._cache: OrderedDict[tuple[str, str, str, str], TranslationResult] = OrderedDict()
        self._cache_capacity = 500
        # Phase B.2 — error_bus late-injection (same pattern as LLMRewriter / AudioEngine)

    def _push_error(self, code: str, message_debug: str, severity: str | None = None) -> None:
        """Push KrabError to attached ErrorBus if available. Late-injected attribute."""
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get(code, {})
            err = KrabError(
                severity=severity or entry.get("severity", "warn"),
                component="translation",
                code=code,
                message_user=entry.get("user_msg_ru", "Ошибка перевода"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception("error_bus.push failed for code=%s", code)

    def translate(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict[str, str] | None = None,
    ) -> TranslationResult:
        """Переводит текст согласно режиму и сетевой политике."""
        # Профилируем весь translate()-pipeline по режиму. Имя span'а нормализуется
        # до входа чтобы даже mode=""/неизвестный mode попадали в согласованную метку.
        normalized_mode = self._normalize_mode(mode)
        with _profiler.start_span(f"translate_{normalized_mode}"):
            return self._translate_impl(
                text=text,
                normalized_mode=normalized_mode,
                network_mode=network_mode,
                translation_style=translation_style,
                glossary=glossary,
            )

    def _translate_impl(
        self,
        text: str,
        normalized_mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict[str, str] | None = None,
    ) -> TranslationResult:
        """Внутренняя реализация translate(). Вынесена чтобы обернуть span'ом только
        наблюдаемую часть без дублирования normalize/cache логики."""
        clean_text = text.strip()
        normalized_style = self._normalize_style(translation_style)
        normalized_network_mode = self._normalize_network_mode(network_mode)
        safe_glossary = self._normalize_glossary(glossary)
        if not clean_text:
            return TranslationResult(
                text="",
                status="empty_text",
                source_lang="",
                target_lang="",
                mode=normalized_mode,
                engine="none",
            )

        cache_key = (
            normalized_mode,
            normalized_style,
            normalized_network_mode,
            clean_text,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            return self._apply_glossary_to_result(cached, safe_glossary)

        if normalized_mode == "off":
            result = TranslationResult(
                text="",
                status="not_requested",
                source_lang="",
                target_lang="",
                mode=normalized_mode,
                engine="none",
            )
            self._cache_set(cache_key, result)
            return result

        if normalized_mode == "bilingual_ru_es":
            result = self._translate_bilingual_ru_es(
                text=clean_text,
                network_mode=normalized_network_mode,
                translation_style=normalized_style,
            )
            self._cache_set(cache_key, result)
            return self._apply_glossary_to_result(result, safe_glossary)

        result = self._translate_single_mode(
            text=clean_text,
            mode=normalized_mode,
            network_mode=normalized_network_mode,
            translation_style=normalized_style,
        )
        self._cache_set(cache_key, result)
        return self._apply_glossary_to_result(result, safe_glossary)

    def _translate_single_mode(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str,
    ) -> TranslationResult:
        """Переводит текст по одиночной языковой паре."""
        if mode == "auto_to_ru":
            detected = self._detect_source_language(text)
            if detected == "ru":
                return TranslationResult(
                    text="",
                    status="already_target_language",
                    source_lang="ru",
                    target_lang="ru",
                    mode="auto_to_ru",
                    engine="none",
                )
        resolved_mode = self._resolve_mode(mode, text)
        if resolved_mode == "off":
            return TranslationResult(
                text="",
                status="cannot_detect_language",
                source_lang=self._detect_source_language(text),
                target_lang="",
                mode=mode,
                engine="none",
            )
        return self._translate_with_model(
            text=text,
            resolved_mode=resolved_mode,
            network_mode=network_mode,
            translation_style=translation_style,
            return_mode=resolved_mode,
        )

    def _translate_bilingual_ru_es(
        self,
        text: str,
        network_mode: str,
        translation_style: str,
    ) -> TranslationResult:
        """Формирует двуязычный ответ вида `RU: ...` + `ES: ...`."""
        detected = self._detect_source_language(text)
        if detected == "ru":
            base_mode = "ru_to_es"
            first_label = "RU"
            second_label = "ES"
        elif detected == "es":
            base_mode = "es_to_ru"
            first_label = "ES"
            second_label = "RU"
        else:
            return TranslationResult(
                text="",
                status="cannot_detect_language",
                source_lang=detected,
                target_lang="ru+es",
                mode="bilingual_ru_es",
                engine="none",
            )

        translated = self._translate_with_model(
            text=text,
            resolved_mode=base_mode,
            network_mode=network_mode,
            translation_style=translation_style,
            return_mode=base_mode,
        )
        if not translated.ok:
            return TranslationResult(
                text="",
                status=translated.status,
                source_lang=detected,
                target_lang="ru+es",
                mode="bilingual_ru_es",
                engine=translated.engine,
            )

        bilingual_text = f"{first_label}: {text.strip()}\n{second_label}: {translated.text.strip()}"
        return TranslationResult(
            text=bilingual_text,
            status="ok",
            source_lang=detected,
            target_lang="ru+es",
            mode="bilingual_ru_es",
            engine=translated.engine,
        )

    def _translate_with_model(
        self,
        text: str,
        resolved_mode: str,
        network_mode: str,
        translation_style: str,
        return_mode: str,
    ) -> TranslationResult:
        """Выполняет фактический перевод через модель выбранной пары."""
        source_lang, target_lang = self._langs_from_mode(resolved_mode)
        allow_network = network_mode == "online_opt_in"
        model_name = self._MODEL_BY_MODE[resolved_mode]
        pipeline_key = (model_name, allow_network)

        if pipeline_key in self._unavailable:
            return TranslationResult(
                text="",
                status="model_unavailable_cached",
                source_lang=source_lang,
                target_lang=target_lang,
                mode=return_mode,
                engine="hf_marian",
            )

        pipeline = self._pipelines.get(pipeline_key)
        if pipeline is None:
            pipeline = self._build_pipeline(model_name=model_name, allow_network=allow_network)
            if pipeline is None:
                self._unavailable.add(pipeline_key)
                return TranslationResult(
                    text="",
                    status="model_unavailable_offline" if not allow_network else "model_unavailable_online",
                    source_lang=source_lang,
                    target_lang=target_lang,
                    mode=return_mode,
                    engine="hf_marian",
                )
            self._pipelines[pipeline_key] = pipeline

        try:
            translated = self._translate_text_chunks(pipeline=pipeline, text=text)
            translated = self._apply_style(translated, translation_style)
            if not translated:
                return TranslationResult(
                    text="",
                    status="empty_translation",
                    source_lang=source_lang,
                    target_lang=target_lang,
                    mode=return_mode,
                    engine="hf_marian",
                )
            return TranslationResult(
                text=translated,
                status="ok",
                source_lang=source_lang,
                target_lang=target_lang,
                mode=return_mode,
                engine="hf_marian",
            )
        except Exception as exc:
            logger.warning("Ошибка перевода (%s): %s", resolved_mode, exc)
            # Phase B.2: translation.timeout — any translation failure
            self._push_error(
                "translation.timeout",
                f"{type(exc).__name__}: {exc} (mode={resolved_mode})",
                severity="warn",
            )
            return TranslationResult(
                text="",
                status="translate_error",
                source_lang=source_lang,
                target_lang=target_lang,
                mode=return_mode,
                engine="hf_marian",
            )

    def _translate_text_chunks(self, pipeline: Any, text: str) -> str:
        """Переводит длинный текст безопасными чанками и склеивает результат."""
        chunks = self._split_text_chunks(text=text, max_chars=450)
        translated_parts: list[str] = []

        for chunk in chunks:
            result = pipeline(chunk)
            translated = ""
            if isinstance(result, list) and result and isinstance(result[0], dict):
                translated = str(result[0].get("translation_text", "")).strip()
            if not translated:
                continue
            translated_parts.append(translated)

        return " ".join(part.strip() for part in translated_parts if part.strip()).strip()

    @classmethod
    def _normalize_mode(cls, mode: str) -> str:
        """Нормализует режим перевода."""
        clean = mode.strip().lower()
        if clean not in cls._SUPPORTED_MODES:
            return "off"
        return clean

    @staticmethod
    def _normalize_style(style: str) -> str:
        """Нормализует стиль перевода."""
        clean = style.strip().lower()
        if clean not in {"neutral", "chat", "formal"}:
            return "neutral"
        return clean

    @staticmethod
    def _normalize_network_mode(network_mode: str) -> str:
        """Нормализует сетевую политику."""
        clean = network_mode.strip().lower()
        if clean not in {"offline_default", "offline_strict", "online_opt_in"}:
            return "offline_default"
        return clean

    @staticmethod
    def _normalize_glossary(glossary: dict[str, str] | None) -> dict[str, str]:
        """Приводит пользовательский глоссарий к валидному виду."""
        if not isinstance(glossary, dict):
            return {}
        result: dict[str, str] = {}
        for raw_key, raw_value in glossary.items():
            key = str(raw_key).strip()
            value = str(raw_value).strip()
            if key and value:
                result[key] = value
        return result

    def _resolve_mode(self, mode: str, text: str) -> str:
        """Разворачивает auto в конкретную языковую пару."""
        if mode == "auto_to_ru":
            detected = self._detect_source_language(text)
            if detected == "es":
                return "es_to_ru"
            if detected == "en":
                return "en_to_ru"
            return "off"

        if mode != "auto":
            return mode

        detected = self._detect_source_language(text)
        if detected == "ru":
            return "ru_to_es"
        if detected == "es":
            return "es_to_ru"
        if detected == "en":
            return "en_to_ru"
        return "off"

    @staticmethod
    def _langs_from_mode(mode: str) -> tuple[str, str]:
        """Возвращает (source_lang, target_lang) для пары перевода."""
        if mode == "ru_to_es":
            return "ru", "es"
        if mode == "es_to_ru":
            return "es", "ru"
        if mode == "en_to_ru":
            return "en", "ru"
        return "", ""

    @staticmethod
    def _detect_source_language(text: str) -> str:
        """Грубая, но устойчивая эвристика языка для режима auto/bilingual."""
        sample = text.strip()
        if not sample:
            return ""
        if re.search(r"[а-яА-ЯёЁ]", sample):
            return "ru"

        lowered = sample.lower()
        spanish_markers = {
            " el ", " la ", " los ", " las ", " de ", " que ", " en ", " por ", " para ", " una ", " un ",
            " y ", " no ", " si ", " con ", " del ", " al ", " hola ", " gracias ", " buenos ", " buenas ",
        }
        english_markers = {
            " the ", " and ", " is ", " are ", " to ", " of ", " in ", " for ", " with ", " this ", " that ",
            " i ", " you ", " we ", " they ", " hello ", " thanks ", " please ",
        }

        padded = f" {lowered} "
        es_score = sum(1 for marker in spanish_markers if marker in padded)
        en_score = sum(1 for marker in english_markers if marker in padded)

        if any(ch in lowered for ch in "¿¡ñáéíóú"):
            es_score += 3

        # Короткие фразы без служебных слов.
        if re.search(r"\b(hola|gracias|adios|vale|buenas?)\b", lowered):
            es_score += 2
        if re.search(r"\b(hello|thanks|goodbye|okay|please)\b", lowered):
            en_score += 2

        if es_score == 0 and en_score == 0:
            # Для латиницы по умолчанию отдаём EN, чтобы не ломать EN->RU сценарий.
            return "en"
        return "es" if es_score >= en_score else "en"

    @staticmethod
    def _build_pipeline(model_name: str, allow_network: bool) -> Any | None:
        """Создаёт pipeline перевода с запретом сети по умолчанию."""
        try:
            from transformers import pipeline  # type: ignore
        except Exception as exc:
            logger.info("Transformers недоступен, перевод выключен: %s", exc)
            return None

        try:
            if allow_network:
                return pipeline(task="translation", model=model_name)
            return pipeline(task="translation", model=model_name, local_files_only=True)
        except TypeError:
            # Совместимость со старыми версиями transformers.
            if allow_network:
                try:
                    return pipeline("translation", model=model_name)
                except Exception as exc:
                    logger.info("Не удалось создать translation pipeline (%s): %s", model_name, exc)
                    return None
            logger.info(
                "Текущая версия transformers не поддерживает local_files_only для %s",
                model_name,
            )
            return None
        except Exception as exc:
            logger.info("Модель перевода %s недоступна: %s", model_name, exc)
            return None

    @staticmethod
    def _apply_style(text: str, style: str) -> str:
        """Лёгкая пост-обработка стиля перевода без сетевых вызовов."""
        clean = text.strip()
        if not clean:
            return clean
        if style == "formal":
            if clean[-1] not in ".!?…":
                clean = f"{clean}."
            return clean
        if style == "chat":
            if len(clean.split()) <= 10 and clean.endswith("."):
                clean = clean[:-1]
            return clean
        return clean

    @staticmethod
    def _apply_glossary(text: str, glossary: dict[str, str]) -> str:
        """Применяет пользовательские замены терминов к переводу."""
        result = text
        for source, target in glossary.items():
            result = result.replace(source, target)
        return result

    def _apply_glossary_to_result(self, result: TranslationResult, glossary: dict[str, str]) -> TranslationResult:
        """Возвращает копию результата с применённым глоссарием."""
        if not glossary or not result.text:
            return result
        replaced = self._apply_glossary(result.text, glossary)
        return TranslationResult(
            text=replaced,
            status=result.status,
            source_lang=result.source_lang,
            target_lang=result.target_lang,
            mode=result.mode,
            engine=result.engine,
        )

    def _cache_get(self, key: tuple[str, str, str, str]) -> TranslationResult | None:
        """Берёт результат из LRU-кэша перевода."""
        value = self._cache.get(key)
        if value is None:
            return None
        self._cache.move_to_end(key)
        return value

    def _cache_set(self, key: tuple[str, str, str, str], value: TranslationResult) -> None:
        """Сохраняет результат в LRU-кэш."""
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_capacity:
            self._cache.popitem(last=False)

    @staticmethod
    def _split_text_chunks(text: str, max_chars: int) -> list[str]:
        """Разбивает текст на чанки с сохранением порядка и пунктуации."""
        clean = text.strip()
        if not clean:
            return []
        if len(clean) <= max_chars:
            return [clean]

        sentences = re.split(r"(?<=[.!?…])\s+", clean)
        chunks: list[str] = []
        current = ""

        for sentence in sentences:
            part = sentence.strip()
            if not part:
                continue
            candidate = f"{current} {part}".strip() if current else part
            if len(candidate) <= max_chars:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            if len(part) <= max_chars:
                current = part
                continue

            words = part.split()
            buffer = ""
            for word in words:
                candidate_word = f"{buffer} {word}".strip() if buffer else word
                if len(candidate_word) <= max_chars:
                    buffer = candidate_word
                else:
                    if buffer:
                        chunks.append(buffer)
                    buffer = word
            if buffer:
                chunks.append(buffer)

        if current:
            chunks.append(current)

        return chunks or [clean]
