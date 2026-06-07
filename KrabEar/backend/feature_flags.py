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
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.parsing_utils import safe_json_loads

logger = logging.getLogger("KrabEar.Backend.FeatureFlags")

_FLAGS_FILE = "feature_flags.json"

# Защита от DoS через неограниченное количество/длину пользовательских флагов
MAX_FLAGS = 200
MAX_FLAG_NAME_LEN = 100
_FLAG_NAME_RE = re.compile(r'^[a-z0-9_]+$')


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
        except Exception as exc:
            logger.warning("FeatureFlags: не удалось загрузить %s: %s", self._flags_path, exc)
            return
        if not raw:
            return
        stored: dict[str, Any] = safe_json_loads(raw, default={}, context="feature_flags.json")
        # Применяем только известные boolean-значения
        for name, value in stored.items():
            if isinstance(value, bool):
                self._flags[name] = value
            else:
                logger.warning("FeatureFlags: нестандартное значение флага %s=%r, игнорируется", name, value)

    def _save(self) -> None:
        """Сохраняет текущие значения флагов в файл атомарно (tmp + fsync + rename).

        Использует запись во временный файл рядом с целевым, fsync и атомарное
        переименование (os.replace) — гарантирует что читатель видит либо старое,
        либо полное новое состояние, никогда частичную запись.
        """
        tmp_path = self._flags_path.with_suffix(self._flags_path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._flags, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._flags_path)
        except Exception as exc:
            logger.error("FeatureFlags: не удалось сохранить %s: %s", self._flags_path, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def is_enabled(self, flag_name: str) -> bool:
        """Возвращает True, если флаг включён.

        Для неизвестных флагов возвращает False.
        """
        with self._lock:
            return bool(self._flags.get(flag_name, False))

    def set_flag(self, flag_name: str, enabled: bool) -> dict[str, object] | None:
        """Устанавливает значение флага и сохраняет в файл.

        Args:
            flag_name: Имя флага (строчные буквы, цифры и подчёркивание, макс. 100 символов).
            enabled: True — включить, False — отключить.

        Returns:
            None при успехе; dict с 'ok': False и 'reason' при отказе валидации.

        Raises:
            ValueError: если flag_name пустой или не является строкой.
        """
        if not flag_name or not isinstance(flag_name, str):
            raise ValueError("Имя флага должно быть непустой строкой")
        if len(flag_name) > MAX_FLAG_NAME_LEN:
            return {"ok": False, "reason": "flag_name_too_long"}
        if not _FLAG_NAME_RE.match(flag_name):
            return {"ok": False, "reason": "flag_name_invalid_chars"}
        with self._lock:
            if flag_name not in self._flags and len(self._flags) >= MAX_FLAGS:
                return {"ok": False, "reason": "flag_limit_reached"}
            self._flags[flag_name] = bool(enabled)
            self._save()
        logger.info("FeatureFlags: флаг %s = %s", flag_name, enabled)
        return None

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
        # wave-1770 (Antigravity): snapshot under lock — iterating self._flags directly
        # raised RuntimeError "dictionary changed size during iteration" if set_flag
        # added a flag concurrently. list_flags() returns a locked copy.
        flags_snapshot = self.list_flags()
        flags_list = []
        for name in flags_snapshot:
            try:
                info = self.get_flag_info(name)
            except KeyError:
                info = {"name": name, "enabled": flags_snapshot[name], "description": "", "since_version": "unknown", "is_builtin": False}
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
        err = self.set_flag(flag_name, enabled_raw)
        if err is not None:
            return {**err, "ts": datetime.now(timezone.utc).isoformat()}
        return {
            "flag_name": flag_name,
            "enabled": enabled_raw,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
