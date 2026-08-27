"""LLMOpsService — обработчики IPC-методов взаимодействия с LLM.

Извлечено из service.py (BackendService, W783) для уменьшения монолита.

Методы:
    list_llm_models               — список моделей из LM Studio /api/v1/models
    get_last_llm_diff             — последний word-level diff от LLM rewriter'а
    replace_word_in_last_transcript — замена слова в последней транскрипции

Зависимости:
    store         — StateStore (чтение/запись истории)
    settings_svc  — SettingsService (cached_settings для llm_base_url/llm_api_key)
    transcriber   — Transcriber (доступ к engine._last_llm_diff)
"""

from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.state_store import StateStore
    from backend.settings_service import SettingsService
    from backend.transcriber import Transcriber

logger = logging.getLogger(__name__)


class LLMOpsService:
    """Обработчики IPC-команд LLM discovery, result inspection и transcript editing."""

    def __init__(
        self,
        store: "StateStore",
        settings_svc: "SettingsService",
        transcriber: "Transcriber",
    ) -> None:
        self._store = store
        self._settings_svc = settings_svc
        self._transcriber = transcriber

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    # Порядок опроса каталога LM Studio. 🔴 Замер 2026-08-26 (v0.4.21, 105
    # моделей на диске, ни одной загруженной):
    #   /api/v0/models → 200, 105 (нативный каталог: type/state/quantization)
    #   /v1/models     → 200, 105 (OpenAI-compat, только id)
    #   /api/v1/models → 200,   0 ← сюда бил backend после W68 (PR #415)
    # Последний отвечает 200 с пустым телом, поэтому функция молчала месяцами:
    # ошибки нет, данных нет, GUI показывал захардкоженный список.
    _CATALOG_ENDPOINTS = ("/api/v0/models", "/v1/models")

    # Рекомендованные модели рерайтера — ОДИН литерал на весь файл: раньше этот
    # список собирался дважды (успешный путь и except-ветка), и дубль расходился
    # молча — протухшее имя всплывало ровно тогда, когда LM Studio недоступна,
    # то есть когда рекомендации нужнее всего.
    #
    # Порядок — по замеру 2026-08-27 на 12 живых диктовках, прод-промпт и
    # параметры: 14b-abl сохраняет текст дословно (9/9 матерных слов, как 26B)
    # при медиане 8.4 с; e4b быстрее (4.8 с), но срезает слова автора; 26B столь
    # же точна, но 24.7 с медиана и 65 с холодной загрузки.
    # 🔴 Имена сверены с живым каталогом: удалённое `qwen3-8b-abliterated`
    # рекомендовалось до этой волны.
    _RECOMMENDED_REWRITER_MODELS = (
        "huihui-qwen3-14b-abl-v2",
        "gemma-4-e4b-it-mlx",
        "gemma-4-26b-a4b-it@4bit",
        "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
    )

    # Эмбеддинг-модели не умеют chat/completions — в списке рерайта им не место.
    # 🔴 vlm НЕ исключаем: мультимодальные модели отлично переписывают текст, и
    # ровно такой тип у gemma-4-26b-a4b-it@4bit — фильтр "только llm" выкинул бы
    # запрошенную владельцем модель.
    _EXCLUDED_MODEL_TYPES = frozenset({"embeddings"})

    def handle_list_llm_models(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает каталог моделей, ДОСТУПНЫХ в LM Studio (не только загруженных).

        Используется GUI для динамического заполнения dropdown'а выбора LLM-модели.
        При недоступности LM Studio возвращает пустой список с описанием ошибки.
        Таймаут 3 секунды — не блокирует UI.
        """
        try:
            import requests as _requests
            from backend.llm_rewriter import _validate_llm_url
            cached = self._settings_svc.cached_settings()
            base_url = str(cached.get("llm_base_url", "http://127.0.0.1:1234/v1")).rstrip("/")
            api_key = str(cached.get("llm_api_key", ""))
            headers: dict[str, str] = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            # Wave 68 (LM Studio probe fix): /v1/models возвращает 200 но логирует ERROR
            # в LM Studio. /api/v1/models — корректный endpoint. Same pattern as PR #396
            # для llm_rewriter.py:1064 (passive_health_check).
            _host = re.sub(r"/v\d+$", "", base_url)
            recommended_models = list(self._RECOMMENDED_REWRITER_MODELS)

            last_error: str | None = None
            for endpoint in self._CATALOG_ENDPOINTS:
                _url = f"{_host}{endpoint}"
                # Wave 1741: SSRF guard — reject file://, gopher://, etc.
                _validate_llm_url(_url)
                try:
                    resp = _requests.get(
                        _url,
                        headers=headers,
                        timeout=3,
                        allow_redirects=False,  # Wave 1741: no redirect-based SSRF
                    )
                except Exception as exc:      # сеть/таймаут — пробуем следующий
                    last_error = str(exc)
                    continue
                if resp.status_code != 200:
                    last_error = f"http_{resp.status_code}"
                    continue
                try:
                    data = resp.json() or {}
                except Exception as exc:
                    last_error = f"bad_json: {exc}"
                    continue

                details = self._parse_catalog(data)
                if not details:
                    # 200 + пустой каталог — НЕ успех: именно так выглядел
                    # сломанный endpoint. Пробуем следующий источник и, если
                    # все пусты, отвечаем ошибкой, а не молчаливым «моделей нет».
                    last_error = f"empty_catalog:{endpoint}"
                    continue

                return {
                    "models": sorted(d["id"] for d in details),
                    "model_details": details,
                    "recommended_models": recommended_models,
                    "error": None,
                }

            return {
                "models": [],
                "model_details": [],
                "recommended_models": recommended_models,
                "error": last_error or "no_catalog_endpoint",
            }
        except Exception as exc:
            # Рекомендации статичны и не зависят от сети: пустой список здесь
            # оставил бы GUI вообще без вариантов при недоступном LM Studio.
            return {
                "models": [],
                "model_details": [],
                "recommended_models": list(self._RECOMMENDED_REWRITER_MODELS),
                "error": str(exc),
            }

    @classmethod
    def _parse_catalog(cls, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Нормализует ответ каталога в список моделей с метаданными.

        Формат ``/api/v0/models`` богаче (type/state/quantization/arch), у
        OpenAI-совместимого ``/v1/models`` есть только ``id`` — тогда поля
        остаются пустыми, а ``loaded`` неизвестен (False).
        """
        out: list[dict[str, Any]] = []
        for item in data.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not model_id:
                continue
            model_type = str(item.get("type") or "").lower()
            if model_type in cls._EXCLUDED_MODEL_TYPES:
                continue
            out.append({
                "id": model_id,
                "type": model_type or None,
                "loaded": str(item.get("state") or "") == "loaded",
                "arch": item.get("arch"),
                "quantization": item.get("quantization"),
                "max_context_length": item.get("max_context_length"),
            })
        return out

    def handle_get_last_llm_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последний word-level diff от LLM rewriter'а.

        Privacy gate (wave-29): если privacy_mode_enabled → diff не возвращается.
        diff.words_added/words_removed содержат текст транскрипции — утечка PII.
        """
        cached = self._settings_svc.cached_settings() if self._settings_svc is not None else {}
        if cached.get("privacy_mode_enabled"):
            return {"ok": True, "diff": None, "reason": "privacy_mode_active"}

        engine = self._transcriber.engine
        diff = getattr(engine, '_last_llm_diff', None)
        if diff is None:
            return {"available": False, "diff": None}
        return {
            "available": True,
            "diff": {
                "similarity_ratio": diff.similarity_ratio,
                "words_added": diff.words_added,
                "words_removed": diff.words_removed,
                "words_unchanged": diff.words_unchanged,
                "summary": diff.summary,
                "changes": [
                    {"type": c.type, "text": c.text, "position": c.position}
                    for c in diff.changes
                ],
            },
        }

    def handle_replace_word_in_last_transcript(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Заменяет слово в последней (или указанной) записи истории без перезаписи.

        Privacy gate (wave-29): если privacy_mode_enabled → отказ без чтения истории.
        new_text в ответе содержит текст транскрипции — утечка PII в privacy mode.

        Параметры:
          - old_word: str — слово для замены (не пустое).
          - new_word: str — новое слово (не пустое).
          - history_id: str | None — ID записи; если не указан, берётся последняя запись.

        Возвращает:
            {"ok": bool, "replaced_count": int, "history_id": str | None, "new_text": str | None,
             "auto_learned": bool}

            auto_learned=True означает, что new_word реально был добавлен в
            stt_hotwords (closed-loop STT vocabulary). False — auto-learn выключен
            настройкой, слово не прошло sanity-проверки, или уже было в словаре.
            Присутствует только при ok=True (при ok=False auto-learn не запускается).

        Ошибки (ok=False):
          - "privacy_mode_active" — privacy mode включён, операция запрещена.
          - "missing_words"      — old_word или new_word пусты.
          - "no_recent_history"  — история пуста и history_id не указан.
          - "item_not_found"     — запись с history_id не найдена.
          - "word_not_found"     — слово не найдено в тексте (с учётом границ слова).
        """
        cached = self._settings_svc.cached_settings() if self._settings_svc is not None else {}
        if cached.get("privacy_mode_enabled"):
            return {"ok": False, "reason": "privacy_mode_active"}

        old = str(params.get("old_word", "")).strip()
        new = str(params.get("new_word", "")).strip()
        if not old or not new:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "missing_words"}

        history_id = str(params.get("history_id", "")).strip() or None

        if history_id is None:
            # Берём самую последнюю запись
            with self._store._lock():
                active = self._store._load_active_items_unlocked()
            history_id = active[-1].id if active else None

        if history_id is None:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "no_recent_history"}

        item = self._store.get_history_item_by_id(history_id)
        if item is None:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "item_not_found"}

        # Замена с учётом границ слова и без учёта регистра
        pattern = re.compile(r'\b' + re.escape(old) + r'\b', re.IGNORECASE)
        new_text, replaced_count = pattern.subn(new, item.text)

        if replaced_count == 0:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "word_not_found"}

        self._store.update_history_item_text(history_id, new_text)
        logger.info(
            "replace_word_in_last_transcript: history_id=%s old=%r new=%r count=%d",
            history_id,
            old,
            new,
            replaced_count,
        )

        # Closed-loop STT auto-learn: add corrected word to stt_hotwords so
        # Whisper stops mishearing it next time. Non-fatal — vocab add failure
        # must never break the replace itself.
        auto_learned = self._maybe_auto_learn_word(new, old)

        return {
            "ok": True,
            "replaced_count": replaced_count,
            "history_id": history_id,
            "new_text": new_text,
            "auto_learned": auto_learned,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # Maximum token budget for stt_hotwords (mirrors STTManagementService limit).
    _STT_HOTWORDS_MAX: int = 100

    def _maybe_auto_learn_word(self, new_word: str, old_word: str) -> bool:
        """Добавляет исправленное слово в stt_hotwords если auto_learn_corrections_enabled.

        Non-fatal: любая ошибка логируется на уровне debug, replace продолжается.
        Проверки перед добавлением:
          - auto_learn_corrections_enabled == True
          - new_word не совпадает с old_word (нечего учить)
          - new_word не пустой, разумной длины (≤60 символов)
          - new_word — одно слово или короткая фраза (≤4 токена по пробелам)

        Возвращает True только если слово реально было добавлено в stt_hotwords
        (используется для поля "auto_learned" в ответе IPC-обработчика, чтобы
        Swift-сторона могла явно показать пользователю "слово выучено в словарь STT").
        """
        try:
            cached = self._settings_svc.cached_settings() if self._settings_svc is not None else {}
            if not cached.get("auto_learn_corrections_enabled", False):
                return False

            # Sanity checks: non-empty, short, and meaningfully different from old word
            word = new_word.strip()
            if not word:
                return False
            if word.lower() == old_word.strip().lower():
                return False
            if len(word) > 60:
                logger.debug("auto_learn: skipping long token %r (len=%d)", word, len(word))
                return False
            if len(word.split()) > 4:
                logger.debug("auto_learn: skipping multi-token phrase %r", word)
                return False

            current: list = cached.get("stt_hotwords", [])
            if not isinstance(current, list):
                current = []
            if word in current:
                logger.debug("auto_learn: %r already in stt_hotwords, skip", word)
                return False

            updated = current + [word]
            if len(updated) > self._STT_HOTWORDS_MAX:
                excess = len(updated) - self._STT_HOTWORDS_MAX
                updated = updated[excess:]
            self._settings_svc.handle_set_settings({"stt_hotwords": updated})
            logger.debug(
                "auto_learn: added %r to stt_hotwords (total=%d)", word, len(updated)
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto_learn_correction failed (non-fatal): %s", exc)
            return False
