"""FeatureFlags — управление feature-флагами Krab Ear.

Позволяет включать/отключать функциональность без изменения кода.
Флаги персистируются в {data_dir}/feature_flags.json.

Встроенные флаги:
- pipeline_v2 (по умолчанию False) — новый движок обработки
- auto_backup (по умолчанию True) — автоматическое резервное копирование
- llm_rewrite (по умолчанию True) — LLM постобработка
- confidence_calibration (по умолчанию True) — калибровка confidence score
- search_index (по умолчанию True) — инвертированный индекс поиска
- webhook_notifications (по умолчанию False) — доставка через webhook
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.FeatureFlags")

_FLAGS_FILE = "feature_flags.json"


# Описание встроенных флагов: имя → (default, description, since_version)
_BUILTIN_FLAGS: dict[str, tuple[bool, str, str]] = {
    "pipeline_v2": (
        False,
        "Новый движок обработки транскрипций (экспериментальный)",
        "1.1.0",
    ),
    "auto_backup": (
        True,
        "Автоматическое резервное копирование истории транскрипций",
        "1.0.0",
    ),
    "llm_rewrite": (
        True,
        "LLM постобработка транскрипций через LM Studio",
        "1.0.0",
    ),
    "confidence_calibration": (
        True,
        "Калибровка STT confidence score на основе статистики",
        "1.0.0",
    ),
    "search_index": (
        True,
        "Инвертированный индекс для ускорения полнотекстового поиска",
        "1.0.0",
    ),
    "webhook_notifications": (
        False,
        "Доставка событий транскрипции на внешние webhook-URL",
        "1.1.0",
    ),
}


class FeatureFlags:
    """Управляет feature-флагами с персистентностью в JSON-файл.

    Структура feature_flags.json:
    {
        "<flag_name>": true/false,
        ...
    }
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._flags_path = self._data_dir / _FLAGS_FILE
        self._lock = threading.Lock()
        # Текущее состояние флагов (merged: builtin defaults + persisted overrides)
        self._flags: dict[str, bool] = {}
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает переопределения флагов из файла."""
        # Начинаем с дефолтов встроенных флагов
        self._flags = {name: default for name, (default, _, _) in _BUILTIN_FLAGS.items()}

        if not self._flags_path.exists():
            return
        try:
            raw = self._flags_path.read_text(encoding="utf-8").strip()
            if not raw:
                return
            stored: dict[str, Any] = json.loads(raw)
            # Применяем только известные boolean-значения
            for name, value in stored.items():
                if isinstance(value, bool):
                    self._flags[name] = value
                else:
                    logger.warning("FeatureFlags: нестандартное значение флага %s=%r, игнорируется", name, value)
        except Exception as exc:
            logger.warning("FeatureFlags: не удалось загрузить %s: %s", self._flags_path, exc)

    def _save(self) -> None:
        """Сохраняет текущие значения флагов в файл."""
        try:
            self._flags_path.write_text(
                json.dumps(self._flags, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("FeatureFlags: не удалось сохранить %s: %s", self._flags_path, exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def is_enabled(self, flag_name: str) -> bool:
        """Возвращает True, если флаг включён.

        Для неизвестных флагов возвращает False.
        """
        with self._lock:
            return bool(self._flags.get(flag_name, False))

    def set_flag(self, flag_name: str, enabled: bool) -> None:
        """Устанавливает значение флага и сохраняет в файл.

        Args:
            flag_name: Имя флага (строка без пробелов).
            enabled: True — включить, False — отключить.
        """
        if not flag_name or not isinstance(flag_name, str):
            raise ValueError("Имя флага должно быть непустой строкой")
        with self._lock:
            self._flags[flag_name] = bool(enabled)
            self._save()
        logger.info("FeatureFlags: флаг %s = %s", flag_name, enabled)

    def list_flags(self) -> dict[str, bool]:
        """Возвращает словарь {flag_name: enabled} для всех известных флагов."""
        with self._lock:
            return dict(self._flags)

    def get_flag_info(self, flag_name: str) -> dict[str, Any]:
        """Возвращает подробную информацию о флаге.

        Returns:
            {
                "name": str,
                "enabled": bool,
                "description": str,
                "since_version": str,
                "is_builtin": bool,
            }

        Raises:
            KeyError: если флаг не существует (ни встроенный, ни пользовательский).
        """
        with self._lock:
            if flag_name not in self._flags:
                raise KeyError(f"Флаг не найден: {flag_name}")
            enabled = self._flags[flag_name]

        if flag_name in _BUILTIN_FLAGS:
            _, description, since_version = _BUILTIN_FLAGS[flag_name]
            is_builtin = True
        else:
            description = "Пользовательский флаг"
            since_version = "unknown"
            is_builtin = False

        return {
            "name": flag_name,
            "enabled": enabled,
            "description": description,
            "since_version": since_version,
            "is_builtin": is_builtin,
        }

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_get_feature_flags(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_feature_flags → список всех флагов с подробностями."""
        flags_list = []
        for name in self._flags:
            try:
                info = self.get_flag_info(name)
            except KeyError:
                info = {"name": name, "enabled": self._flags[name], "description": "", "since_version": "unknown", "is_builtin": False}
            flags_list.append(info)
        return {
            "flags": flags_list,
            "count": len(flags_list),
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    def handle_set_feature_flag(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: set_feature_flag {flag_name, enabled} → обновляет флаг."""
        flag_name = str(params.get("flag_name", "")).strip()
        if not flag_name:
            raise ValueError("Требуется параметр flag_name")
        enabled_raw = params.get("enabled")
        if not isinstance(enabled_raw, bool):
            raise ValueError("Параметр enabled должен быть boolean")
        self.set_flag(flag_name, enabled_raw)
        return {
            "flag_name": flag_name,
            "enabled": enabled_raw,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
