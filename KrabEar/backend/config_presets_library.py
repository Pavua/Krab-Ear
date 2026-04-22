"""ConfigPresetsLibrary — библиотека шаблонов конфигурации Krab Ear.

Позволяет сохранять, экспортировать и применять именованные пресеты настроек.
Встроенные пресеты покрывают типовые сценарии использования.
Пользовательские пресеты сохраняются в {data_dir}/config_presets.json.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.ConfigPresetsLibrary")

_PRESETS_FILE = "config_presets.json"

# Формат версии пресета — для совместимости при будущих изменениях схемы
_PRESET_FORMAT_VERSION = "1"


# ---------------------------------------------------------------------------
# Встроенные пресеты
# ---------------------------------------------------------------------------

_BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "interview": {
        "name": "interview",
        "description": (
            "Интервью: максимальное качество, диаризация, формальная нормализация, авто-заголовок"
        ),
        "builtin": True,
        "settings_patch": {
            "quality_profile": "max",
            "cleanup_profile": "strict",
            "translation_mode": "off",
            "realtime_preview_enabled": True,
            "auto_paste": False,
            "diarization_enabled": True,
            "auto_title_enabled": True,
            "translation_style": "formal",
            "summary_style": "brief",
        },
    },
    "meeting": {
        "name": "meeting",
        "description": (
            "Митинг: сбалансированное качество, диаризация, формат bullet_points"
        ),
        "builtin": True,
        "settings_patch": {
            "quality_profile": "balanced",
            "cleanup_profile": "strict",
            "translation_mode": "off",
            "realtime_preview_enabled": True,
            "auto_paste": False,
            "diarization_enabled": True,
            "summary_style": "bullet_points",
        },
    },
    "voice_memo": {
        "name": "voice_memo",
        "description": (
            "Голосовая заметка: максимальное качество, без диаризации, краткое резюме"
        ),
        "builtin": True,
        "settings_patch": {
            "quality_profile": "max",
            "cleanup_profile": "soft",
            "translation_mode": "off",
            "realtime_preview_enabled": True,
            "auto_paste": True,
            "diarization_enabled": False,
            "summary_style": "brief",
        },
    },
    "language_practice": {
        "name": "language_practice",
        "description": (
            "Изучение языка: перевод включён, режим обучения, двуязычный экспорт"
        ),
        "builtin": True,
        "settings_patch": {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_mode": "bilingual_ru_es",
            "translate_and_paste": True,
            "realtime_preview_enabled": True,
            "auto_paste": True,
            "diarization_enabled": False,
            "language_learning_mode": True,
            "export_format": "bilingual",
        },
    },
    "podcast": {
        "name": "podcast",
        "description": (
            "Подкаст: максимальное качество, диаризация, подробное резюме, экспорт в Obsidian"
        ),
        "builtin": True,
        "settings_patch": {
            "quality_profile": "max",
            "cleanup_profile": "strict",
            "translation_mode": "off",
            "realtime_preview_enabled": False,
            "auto_paste": False,
            "diarization_enabled": True,
            "summary_style": "detailed",
            "obsidian_export_enabled": True,
        },
    },
}


class ConfigPresetsLibrary:
    """Библиотека конфигурационных пресетов с поддержкой кастомных пресетов.

    Встроенные пресеты: interview, meeting, voice_memo, language_practice, podcast.
    Кастомные пресеты сохраняются в {data_dir}/config_presets.json.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._presets_path = self._data_dir / _PRESETS_FILE
        self._lock = threading.Lock()
        self._custom: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает кастомные пресеты из файла."""
        try:
            if self._presets_path.exists():
                raw = self._presets_path.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                if isinstance(loaded, dict) and "presets" in loaded:
                    self._custom = {
                        name: preset
                        for name, preset in loaded["presets"].items()
                        if isinstance(preset, dict)
                    }
        except Exception as exc:
            logger.warning("Не удалось загрузить кастомные пресеты: %s", exc)

    def _save(self) -> None:
        """Сохраняет кастомные пресеты в файл."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _PRESET_FORMAT_VERSION,
            "presets": self._custom,
        }
        self._presets_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    @staticmethod
    def get_built_in_presets() -> dict[str, dict[str, Any]]:
        """Возвращает словарь встроенных пресетов (копия)."""
        return {name: dict(preset) for name, preset in _BUILTIN_PRESETS.items()}

    def list_presets(self) -> list[dict[str, Any]]:
        """Возвращает список всех пресетов (встроенные + кастомные).

        Каждый элемент: {name, description, builtin, settings_patch}.
        """
        with self._lock:
            result: list[dict[str, Any]] = []
            for name, preset in _BUILTIN_PRESETS.items():
                result.append(dict(preset))
            for name, preset in self._custom.items():
                result.append(dict(preset))
            return result

    def apply_preset(self, name: str) -> dict[str, Any]:
        """Возвращает settings_patch для именованного пресета.

        Raises:
            KeyError: если пресет не найден.
        """
        with self._lock:
            if name in _BUILTIN_PRESETS:
                return dict(_BUILTIN_PRESETS[name]["settings_patch"])
            if name in self._custom:
                return dict(self._custom[name].get("settings_patch", {}))
            available = list(_BUILTIN_PRESETS.keys()) + list(self._custom.keys())
            raise KeyError(
                f"Пресет '{name}' не найден. Доступные: {', '.join(available)}"
            )

    def create_preset(
        self,
        name: str,
        description: str,
        settings_patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Создаёт или обновляет кастомный пресет.

        Args:
            name: уникальное имя пресета (не может совпадать с именами встроенных).
            description: описание пресета.
            settings_patch: словарь настроек для применения.

        Returns:
            Созданный пресет.

        Raises:
            ValueError: если имя пустое, конфликтует с встроенным пресетом
                        или settings_patch не является словарём.
        """
        name = str(name).strip()
        if not name:
            raise ValueError("Имя пресета не может быть пустым")
        if name in _BUILTIN_PRESETS:
            raise ValueError(
                f"Имя '{name}' зарезервировано встроенным пресетом. Выберите другое имя."
            )
        if not isinstance(settings_patch, dict):
            raise ValueError("settings_patch должен быть словарём")

        preset: dict[str, Any] = {
            "name": name,
            "description": str(description).strip(),
            "builtin": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "settings_patch": dict(settings_patch),
        }

        with self._lock:
            self._custom[name] = preset
            self._save()

        logger.info("Пресет '%s' создан/обновлён", name)
        return dict(preset)

    def export_preset(self, name: str) -> str:
        """Экспортирует пресет в JSON-строку для передачи/сохранения.

        Raises:
            KeyError: если пресет не найден.
        """
        with self._lock:
            if name in _BUILTIN_PRESETS:
                preset = dict(_BUILTIN_PRESETS[name])
            elif name in self._custom:
                preset = dict(self._custom[name])
            else:
                available = list(_BUILTIN_PRESETS.keys()) + list(self._custom.keys())
                raise KeyError(
                    f"Пресет '{name}' не найден. Доступные: {', '.join(available)}"
                )

        envelope = {
            "format_version": _PRESET_FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "preset": preset,
        }
        return json.dumps(envelope, ensure_ascii=False, indent=2)

    def import_preset(self, json_str: str) -> dict[str, Any]:
        """Импортирует пресет из JSON-строки.

        Валидирует структуру и сохраняет как кастомный пресет.

        Returns:
            Импортированный пресет.

        Raises:
            ValueError: если JSON невалиден или структура некорректна.
        """
        try:
            envelope = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Невалидный JSON: {exc}") from exc

        if not isinstance(envelope, dict):
            raise ValueError("JSON должен содержать объект верхнего уровня")

        # Поддерживаем как envelope-формат ({format_version, preset}), так и прямой пресет
        if "preset" in envelope:
            preset_data = envelope["preset"]
        else:
            preset_data = envelope

        if not isinstance(preset_data, dict):
            raise ValueError("Данные пресета должны быть объектом")

        name = preset_data.get("name")
        if not name or not str(name).strip():
            raise ValueError("Поле 'name' обязательно и не может быть пустым")

        settings_patch = preset_data.get("settings_patch")
        if not isinstance(settings_patch, dict):
            raise ValueError("Поле 'settings_patch' обязательно и должно быть словарём")

        description = str(preset_data.get("description", "")).strip()

        return self.create_preset(
            name=str(name).strip(),
            description=description,
            settings_patch=settings_patch,
        )

    def delete_preset(self, name: str) -> bool:
        """Удаляет кастомный пресет.

        Returns:
            True если пресет был удалён, False если не найден.

        Raises:
            ValueError: если попытка удалить встроенный пресет.
        """
        if name in _BUILTIN_PRESETS:
            raise ValueError(f"Встроенный пресет '{name}' нельзя удалить")

        with self._lock:
            if name not in self._custom:
                return False
            del self._custom[name]
            self._save()

        logger.info("Пресет '%s' удалён", name)
        return True

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_list_config_presets(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: список всех конфигурационных пресетов."""
        return {"presets": self.list_presets()}

    def handle_apply_config_preset(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: применить пресет — вернуть settings_patch.

        Params:
            name (str): имя пресета.
        """
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("Параметр 'name' обязателен для apply_config_preset")
        patch = self.apply_preset(name)
        return {"name": name, "settings_patch": patch}

    def handle_create_config_preset(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: создать кастомный пресет.

        Params:
            name (str): имя пресета.
            description (str): описание.
            settings_patch (dict): патч настроек.
        """
        name = str(params.get("name", "")).strip()
        description = str(params.get("description", "")).strip()
        settings_patch = params.get("settings_patch")

        if not name:
            raise ValueError("Параметр 'name' обязателен для create_config_preset")
        if not isinstance(settings_patch, dict):
            raise ValueError("Параметр 'settings_patch' обязателен и должен быть объектом")

        preset = self.create_preset(name=name, description=description, settings_patch=settings_patch)
        return {"preset": preset}
