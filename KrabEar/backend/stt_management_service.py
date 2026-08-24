"""STTManagementService — обработчики IPC-методов управления STT в Krab Ear.

Выделен из backend/service.py для снижения размера монолитного модуля.
Содержит 7 IPC-обработчиков: STT hotwords CRUD, warmup_stt,
scored routing decision, select_model, list_voice_commands.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import time as _time
from typing import Any, TYPE_CHECKING

from backend.observability import add_breadcrumb
from core.voice_commands import _RU_COMMANDS, _ES_COMMANDS, _EN_COMMANDS

if TYPE_CHECKING:
    from backend.settings_service import SettingsService
    from backend.transcriber import Transcriber

logger = logging.getLogger("KrabEar.Backend.STTManagementService")

# Whisper initial_prompt hard limit: ~224 tokens ≈ ~170 avg words.
# We cap hotwords at 100 entries (≈ safe budget) to avoid prompt overflow.
# When the list exceeds this limit, oldest entries are dropped (FIFO).
_STT_HOTWORDS_MAX: int = 100


class STTManagementService:
    """Обработчики IPC-команд управления STT (hotwords, warmup, routing, model selection)."""

    def __init__(
        self,
        settings_svc: "SettingsService",
        transcriber: "Transcriber | None" = None,
    ) -> None:
        self._settings_svc = settings_svc
        self._transcriber = transcriber

    # ------------------------------------------------------------------
    # STT hotwords (initial_prompt boost)
    # ------------------------------------------------------------------

    def handle_add_stt_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет термин в список STT hotwords.

        Параметры:
          - word: str — термин для добавления (имя, бренд, технический термин).

        Возвращает: {hotwords: list[str], truncated: bool} — обновлённый список.
          truncated=True если список обрезан до _STT_HOTWORDS_MAX.
        """
        word = str(params.get("word") or "").strip()
        if not word:
            raise ValueError("Параметр 'word' обязателен и не может быть пустым")
        current: list[str] = self._settings_svc.cached_settings().get("stt_hotwords", [])
        if not isinstance(current, list):
            current = []
        truncated = False
        if word not in current:
            current = current + [word]
            # Enforce per-IPC budget: drop oldest entries when limit exceeded.
            if len(current) > _STT_HOTWORDS_MAX:
                excess = len(current) - _STT_HOTWORDS_MAX
                logger.warning(
                    "stt_hotwords: список превышает лимит %d — удаляем %d старых записей",
                    _STT_HOTWORDS_MAX, excess,
                )
                current = current[excess:]
                truncated = True
            self._settings_svc.handle_set_settings({"stt_hotwords": current})
        add_breadcrumb(
            category="stt",
            message="add_stt_hotword",
            data={"total_hotwords": len(current), "truncated": truncated},
        )
        return {"hotwords": current, "truncated": truncated}

    def handle_remove_stt_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет термин из списка STT hotwords.

        Параметры:
          - word: str — термин для удаления.

        Возвращает: {hotwords: list[str]} — обновлённый список.
        """
        word = str(params.get("word") or "").strip()
        if not word:
            raise ValueError("Параметр 'word' обязателен и не может быть пустым")
        current: list[str] = self._settings_svc.cached_settings().get("stt_hotwords", [])
        if not isinstance(current, list):
            current = []
        updated = [w for w in current if w != word]
        if len(updated) != len(current):
            self._settings_svc.handle_set_settings({"stt_hotwords": updated})
        return {"hotwords": updated}

    def handle_list_stt_hotwords(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущий список STT hotwords.

        Учитывает флаг stt_hotwords_enabled: если False — возвращает пустой список.

        Возвращает: {hotwords: list[str], enabled: bool}
        """
        s = self._settings_svc.cached_settings()
        enabled = bool(s.get("stt_hotwords_enabled", True))
        if not enabled:
            return {"hotwords": [], "enabled": False, "truncated": False}
        current: list[str] = s.get("stt_hotwords", [])
        if not isinstance(current, list):
            current = []
        truncated = len(current) >= _STT_HOTWORDS_MAX
        return {"hotwords": sorted(current), "enabled": True, "truncated": truncated}

    # ------------------------------------------------------------------
    # STT warmup
    # ------------------------------------------------------------------

    def handle_warmup_stt(self, params: dict) -> dict:
        """Ручной запуск STT warmup — полезен после смены профиля или модели.

        Загружает текущую активную Whisper-модель через tiny (1s silent) inference.
        Блокирующий вызов — выполняется в потоке IPC handler'а, возвращает
        результат только после завершения warmup (или ошибки).

        Returns:
            {
              "loaded": bool,      # True если warmup inference прошёл без ошибок
              "latency_ms": int,   # время inference в мс
              "model_name": str,   # имя прогретой модели
              "error": str | None  # сообщение об ошибке (None если loaded=True)
            }
        """
        if self._transcriber is None or not hasattr(self._transcriber, "engine"):
            add_breadcrumb(
                category="stt",
                message="warmup_stt",
                level="warning",
                data={"loaded": False, "error": "engine not available"},
            )
            return {"loaded": False, "latency_ms": 0, "model_name": "", "error": "engine not available"}
        engine = self._transcriber.engine
        if engine is None or not hasattr(engine, "warmup") or not callable(engine.warmup):
            add_breadcrumb(
                category="stt",
                message="warmup_stt",
                level="warning",
                data={"loaded": False, "error": "engine not available"},
            )
            return {"loaded": False, "latency_ms": 0, "model_name": "", "error": "engine not available"}
        _t0 = _time.monotonic()
        result = engine.warmup()
        add_breadcrumb(
            category="stt",
            message="warmup_stt",
            data={
                "loaded": result.get("loaded", False),
                "model_name": result.get("model_name", ""),
                "duration_ms": result.get("latency_ms", round((_time.monotonic() - _t0) * 1000)),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Scored STT routing decision (debug)
    # ------------------------------------------------------------------

    def handle_get_stt_routing_decision(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает результат scored STT adapter selection для отладки.

        Параметры:
            language         — ISO 639-1 код языка (например «ru», «en», «zh»).
            audio_duration_s — длительность аудио в секундах (float, опционально).

        Возвращает:
            selected_engine — имя выбранного адаптера или null.
            scores          — dict {engine_name: score} для всех доступных адаптеров.
            language        — нормализованный код языка.
            audio_duration_s — длительность из params или null.
        """
        from core.stt_router import score_adapters, select_adapter_scored

        language = str(params.get("language", "")).strip().lower() or "und"
        raw_dur = params.get("audio_duration_s")
        audio_duration_s: float | None = float(raw_dur) if raw_dur is not None else None

        adapters = self._build_virtual_adapters_for_routing()
        scores = score_adapters(adapters, language, audio_duration_s)
        best = select_adapter_scored(language, audio_duration_s, adapters)
        selected_name: str | None = getattr(best, "name", None) if best is not None else None

        return {
            "selected_engine": selected_name,
            "scores": scores,
            "language": language,
            "audio_duration_s": audio_duration_s,
        }

    def _build_virtual_adapters_for_routing(self) -> "list[Any]":
        """Создаёт список виртуальных адаптеров для scored selection.

        Не загружает реальные модели — только описывает возможности каждого
        адаптера на основе настроек. Используется в IPC для отладки routing.
        """
        from types import SimpleNamespace
        from core.config import settings

        def _make(name: str, languages: "set[str]", enabled: bool) -> "Any":
            ns = SimpleNamespace(
                name=name,
                supported_languages=languages,
            )
            ns.is_available = lambda: enabled  # type: ignore[attr-defined]
            return ns

        adapters = []

        # GigaAM — RU-only specialist
        gigaam_enabled = getattr(settings, "STT_GIGAAM_ENABLED", False)
        adapters.append(_make("gigaam", {"ru", "uk"}, bool(gigaam_enabled)))

        # Parakeet — EN-only specialist
        parakeet_enabled = getattr(settings, "PARAKEET_ENABLED", False)
        adapters.append(_make("parakeet", {"en"}, bool(parakeet_enabled)))

        # SenseVoice — ZH/JA/KO/YUE specialist + EN. RU is NOT supported by the
        # SenseVoiceSmall model; passing language="ru" silently falls back to
        # language="auto" inside funasr. Advertising RU here misleads the STT
        # router into selecting SenseVoice for Russian audio (W1218 F1 fix).
        sensevoice_enabled = getattr(settings, "SENSEVOICE_ENABLED", False)
        adapters.append(_make("sensevoice", {"zh", "ja", "ko", "yue", "en"}, bool(sensevoice_enabled)))

        # Whisper-MLX — multilingual generalist (empty set = multilingual)
        adapters.append(_make("whisper-mlx", set(), True))

        return adapters

    # ------------------------------------------------------------------
    # Smart model selection
    # ------------------------------------------------------------------

    def handle_select_model(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: select_model — умный выбор STT-модели на основе условий.

        Параметры:
            duration_sec  — длительность аудио в секундах (float, обязательный).
            quality       — "balanced" | "max" (строка, опциональный, по умолчанию "balanced").
            is_preview    — True если это превью-транскрибация (bool, опциональный).
            system_load   — нагрузка CPU 0.0–1.0 (float, опциональный, по умолчанию 0).

        Возвращает:
            {model_name, reason, estimated_latency_ms, quality_tier}
        """
        from core.model_selector import SmartModelSelector

        try:
            duration_sec = float(params.get("duration_sec", 0.0))
        except (TypeError, ValueError):
            raise ValueError("Параметр 'duration_sec' должен быть числом")

        quality = str(params.get("quality", "balanced")).strip()
        is_preview = bool(params.get("is_preview", False))

        try:
            system_load = float(params.get("system_load", 0.0))
        except (TypeError, ValueError):
            system_load = 0.0

        selector = SmartModelSelector()
        sel = selector.select_model(
            duration_sec=duration_sec,
            quality=quality,
            is_preview=is_preview,
            system_load=system_load,
        )
        add_breadcrumb(
            category="stt",
            message="select_model",
            data={
                "model_name": sel.model_name,
                "quality_tier": sel.quality_tier,
                "duration_sec": duration_sec,
            },
        )
        return {
            "model_name": sel.model_name,
            "reason": sel.reason,
            "estimated_latency_ms": sel.estimated_latency_ms,
            "quality_tier": sel.quality_tier,
        }

    # ------------------------------------------------------------------
    # List all known STT engines (model-picker IPC for GUI)
    # ------------------------------------------------------------------

    def handle_list_stt_engines(self, params: dict[str, Any]) -> dict[str, Any]:
        """Перечисляет ВСЕ известные STT-движки, включая отключённые-но-установленные.

        Используется GUI для построения model-picker'а: показывает все движки,
        позволяет включить/отключить каждый через set_settings(toggle_key, bool).

        Параметры: {} (игнорирует лишние параметры).

        Возвращает:
            {
              "ok": true,
              "engines": [
                {
                  "name": str,          # стабильный ID движка
                  "display_name": str,  # человекочитаемое название
                  "available": bool,    # best-effort is_available(), False при любой ошибке
                  "enabled": bool,      # текущее значение флага в settings
                  "toggle_key": str|null,  # ключ settings.json для включения/отключения
                  "note": str,          # краткая подсказка на RU (ОЗУ, применение)
                  "type": "local"       # тип движка
                }
              ],
              "default": "whisper_mlx"  # всегда включённый движок по умолчанию
            }

        Privacy: возвращает ТОЛЬКО метаданные движков, НЕ транскрипты/историю.
        Privacy gate: не нужен.
        """
        s = self._settings_svc.cached_settings()

        # Canonical engine descriptors.  Order mirrors build_router() priority.
        # Each entry defines the stable metadata; is_available() called in try/except.
        _ENGINE_META = [
            {
                "name": "gigaam",
                "display_name": "GigaAM v3 (RU)",
                "toggle_key": "stt_gigaam_enabled",
                "note": "Лучший для RU, subprocess ~1.5 ГБ",
                "adapter_class": "core.pipeline.stt_gigaam_adapter.GigaAMSTTAdapter",
                "checks_mlx_availability": True,
            },
            {
                "name": "parakeet",
                "display_name": "Parakeet MLX (EN)",
                "toggle_key": "stt_parakeet_enabled",
                "note": "Быстрый EN-only, MLX Apple Silicon, ~0.5 ГБ",
                "adapter_class": "core.pipeline.stt_parakeet.ParakeetSTTAdapter",
            },
            {
                "name": "sensevoice",
                "display_name": "SenseVoice (ZH/JA/KO/YUE)",
                "toggle_key": "stt_sensevoice_enabled",
                "note": "Специалист по азиатским языкам (zh/ja/ko/yue), PyTorch MPS",
                "adapter_class": "core.pipeline.stt_sensevoice.SenseVoiceSTTAdapter",
            },
            {
                "name": "sherpa",
                "display_name": "Sherpa-ONNX (Paraformer)",
                "toggle_key": "stt_sherpa_enabled",
                "note": "Ultra-low latency для звонков, ONNX, требует sherpa-onnx",
                "adapter_class": "core.pipeline.stt_sherpa.SherpaOnnxSTTAdapter",
            },
            {
                "name": "whisper_mlx",
                "display_name": "Whisper MLX (multilingual)",
                "toggle_key": None,
                "note": "Многоязычный движок по умолчанию, MLX Apple Silicon",
                "adapter_class": "core.pipeline.stt_whisper_mlx_adapter.WhisperMLXAdapter",
            },
        ]

        engines = []
        for meta in _ENGINE_META:
            toggle_key = meta["toggle_key"]

            # whisper_mlx is always enabled (no toggle)
            if toggle_key is None:
                enabled = True
            else:
                enabled = bool(s.get(toggle_key, False))

            # Best-effort availability probe — never raise
            available = False
            try:
                module_path, class_name = meta["adapter_class"].rsplit(".", 1)
                import importlib
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                inst = cls()
                available = bool(inst.is_available())
            except Exception:
                available = False

            entry = {
                "name": meta["name"],
                "display_name": meta["display_name"],
                "available": available,
                "enabled": enabled,
                "toggle_key": toggle_key,
                "note": meta["note"],
                "type": "local",
            }
            if meta.get("checks_mlx_availability"):
                # find_spec, а НЕ импорт core.pipeline.stt_gigaam_mlx: тот модуль
                # импортируется успешно и без библиотеки gigaam_mlx (ленивый
                # импорт внутри методов адаптера) — импорт был бы
                # ложноположительной проверкой.
                entry["mlx_available"] = importlib.util.find_spec("gigaam_mlx") is not None
            engines.append(entry)

        add_breadcrumb(
            category="stt",
            message="list_stt_engines",
            data={"count": len(engines)},
        )
        return {"ok": True, "engines": engines, "default": "whisper_mlx"}

    # ------------------------------------------------------------------
    # Voice commands reference list
    # ------------------------------------------------------------------

    def handle_list_voice_commands(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статический справочник голосовых команд диктовки.

        Параметры (опциональные):
          - language: str — фильтр по языку ("ru", "es", "en").
            Если не задан — возвращаются все языки.

        Возвращает:
          {
            "ok": True,
            "languages": ["ru", "es", "en"],
            "commands": [
              {
                "language": "ru",
                "phrase": "удалить последнее слово",
                "action": "delete_last",
                "description": "Удаляет последнее слово"
              },
              ...
            ]
          }

        Данные полностью статические (словари паттернов) — privacy gate не нужен.
        Никогда не выбрасывает исключение — при ошибке деградирует до пустого ответа.
        """
        try:
            lang_filter = str(params.get("language") or "").strip().lower() or None

            _lang_tables: list[tuple[str, list[tuple[str, str, str]]]] = [
                ("ru", _RU_COMMANDS),
                ("es", _ES_COMMANDS),
                ("en", _EN_COMMANDS),
            ]

            all_langs = [lang for lang, _ in _lang_tables]
            if lang_filter and lang_filter not in all_langs:
                return {"ok": True, "languages": all_langs, "commands": []}

            commands: list[dict[str, Any]] = []
            for lang, table in _lang_tables:
                if lang_filter and lang != lang_filter:
                    continue
                for raw_pattern, action, arg in table:
                    phrase = _clean_pattern(raw_pattern)
                    description = _describe_command(action, arg)
                    commands.append({
                        "language": lang,
                        "phrase": phrase,
                        "action": action,
                        "description": description,
                    })

            return {"ok": True, "languages": all_langs, "commands": commands}
        except Exception:
            logger.exception("handle_list_voice_commands: неожиданная ошибка")
            return {"ok": False, "languages": [], "commands": []}


# ---------------------------------------------------------------------------
# Вспомогательные функции для handle_list_voice_commands
# ---------------------------------------------------------------------------

def _clean_pattern(raw: str) -> str:
    """Преобразует regex-паттерн в читаемую человеком фразу.

    Убирает regex-спецсимволы (lookaround-границы, экранирование),
    нормализует пробелы.
    """
    # Убираем lookaround-обёртки (?<!\w) и (?!\w)
    cleaned = re.sub(r'\(\?[<>!][^)]+\)', '', raw)
    # Убираем экранирование — re.escape-дёрнутые символы → обычные
    cleaned = re.sub(r'\\(.)', r'\1', cleaned)
    # \s+ → пробел
    cleaned = re.sub(r'\\s\+', ' ', cleaned)
    # Нормализуем пробелы
    return ' '.join(cleaned.split()).strip() or raw


def _describe_command(action: str, arg: str) -> str:
    """Формирует краткое описание команды на русском языке."""
    if action == "delete_last":
        targets = {
            "word": "Удаляет последнее слово",
            "sentence": "Удаляет последнее предложение",
            "paragraph": "Удаляет последний абзац",
        }
        return targets.get(arg, "Удаляет последний элемент")
    if action == "capitalize_next":
        return "Следующее слово с заглавной буквы"
    if action == "uppercase_sent":
        return "Следующая фраза заглавными буквами"
    if action == "insert":
        # Специальные символы → понятное описание
        special: dict[str, str] = {
            "\n": "Вставляет перевод строки",
            "\n\n": "Вставляет новый абзац",
            "\t": "Вставляет табуляцию",
            " ": "Вставляет пробел",
            " — ": "Вставляет тире «—»",
        }
        if arg in special:
            return special[arg]
        # Знак препинания: «,», «.» и т.д.
        return f"Вставляет «{arg}»"
    return action
