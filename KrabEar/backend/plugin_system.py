"""Система плагинов Krab Ear — обнаружение и загрузка плагинов.

Плагины размещаются в {data_dir}/plugins/, каждый в своей поддиректории
с файлом-манифестом plugin.json.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger("KrabEar.Backend.PluginSystem")


@runtime_checkable
class Plugin(Protocol):
    """Протокол плагина Krab Ear."""

    name: str
    version: str

    def initialize(self, service: Any) -> None:
        """Инициализирует плагин, передавая ссылку на BackendService."""
        ...

    def get_ipc_methods(self) -> dict[str, Callable]:
        """Возвращает словарь IPC-методов, добавляемых плагином."""
        ...


@dataclass
class PluginInfo:
    """Метаданные плагина из plugin.json."""

    name: str
    version: str
    description: str
    author: str
    entry_point: str
    plugin_dir: Path = field(default_factory=Path)


_REQUIRED_MANIFEST_FIELDS = ("name", "version", "entry_point")

# Возможные статусы плагина.
_STATUS_DISCOVERED = "discovered"
_STATUS_LOADED = "loaded"
_STATUS_ERROR = "error"
_STATUS_DISABLED = "disabled"

# Имена поддерживаемых хуков.
HOOK_ON_TRANSCRIBE = "on_transcribe"
HOOK_ON_PASTE = "on_paste"
_SUPPORTED_HOOKS = (HOOK_ON_TRANSCRIBE, HOOK_ON_PASTE)


class PluginManager:
    """Менеджер плагинов: обнаружение, загрузка и перечисление."""

    def __init__(self, data_dir: Path | None = None) -> None:
        # Базовая директория для плагинов.
        self._data_dir: Path | None = data_dir
        # PluginInfo по имени плагина.
        self._discovered: dict[str, PluginInfo] = {}
        # Загруженные экземпляры плагинов.
        self._loaded: dict[str, Plugin] = {}
        # Статусы (discovered / loaded / error / disabled).
        self._statuses: dict[str, str] = {}
        # Тексты ошибок загрузки.
        self._errors: dict[str, str] = {}
        # Отключённые вручную плагины.
        self._disabled: set[str] = set()

    # ------------------------------------------------------------------
    # Обнаружение
    # ------------------------------------------------------------------

    def discover_plugins(self, plugins_dir: Path | None = None) -> list[PluginInfo]:
        """Сканирует директорию plugins_dir в поисках plugin.json манифестов.

        Если plugins_dir не передан, используется {data_dir}/plugins/.
        """
        if plugins_dir is None:
            if self._data_dir is None:
                return []
            plugins_dir = self._data_dir / "plugins"

        if not plugins_dir.is_dir():
            return []

        found: list[PluginInfo] = []
        for entry in sorted(plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / "plugin.json"
            if not manifest_path.is_file():
                continue
            info = self._parse_manifest(manifest_path, entry)
            if info is None:
                continue
            self._discovered[info.name] = info
            if info.name not in self._statuses:
                self._statuses[info.name] = _STATUS_DISCOVERED
            found.append(info)

        return found

    def _parse_manifest(self, manifest_path: Path, plugin_dir: Path) -> PluginInfo | None:
        """Парсит plugin.json и возвращает PluginInfo или None при ошибке."""
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Ошибка чтения манифеста %s: %s", manifest_path, exc)
            return None

        if not isinstance(data, dict):
            logger.warning("Манифест %s не является объектом JSON", manifest_path)
            return None

        for field_name in _REQUIRED_MANIFEST_FIELDS:
            if not data.get(field_name):
                logger.warning(
                    "Манифест %s не содержит обязательного поля '%s'",
                    manifest_path,
                    field_name,
                )
                return None

        return PluginInfo(
            name=str(data["name"]),
            version=str(data["version"]),
            description=str(data.get("description", "")),
            author=str(data.get("author", "")),
            entry_point=str(data["entry_point"]),
            plugin_dir=plugin_dir,
        )

    # ------------------------------------------------------------------
    # Загрузка
    # ------------------------------------------------------------------

    def load_plugin(self, name: str) -> Plugin:
        """Загружает плагин по имени.

        Плагин должен быть предварительно обнаружен через discover_plugins().
        Точка входа — Python-файл entry_point, в котором должен присутствовать
        класс или фабрика create_plugin() возвращающая объект, соответствующий
        протоколу Plugin.
        """
        if name in self._loaded:
            return self._loaded[name]

        if name not in self._discovered:
            raise KeyError(f"Плагин '{name}' не найден. Сначала вызовите discover_plugins().")

        info = self._discovered[name]
        try:
            plugin = self._import_plugin(info)
        except Exception as exc:
            self._statuses[name] = _STATUS_ERROR
            self._errors[name] = str(exc)
            logger.error("Ошибка загрузки плагина '%s': %s", name, exc)
            raise

        self._loaded[name] = plugin
        self._statuses[name] = _STATUS_LOADED
        return plugin

    def _import_plugin(self, info: PluginInfo) -> Plugin:
        """Динамически импортирует модуль плагина и создаёт экземпляр."""
        entry_path = info.plugin_dir / info.entry_point
        if not entry_path.is_file():
            raise FileNotFoundError(f"Файл точки входа не найден: {entry_path}")

        module_name = f"_krabear_plugin_{info.name}"
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Не удалось создать spec для '{entry_path}'")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        # Предпочитаем фабрику create_plugin(), иначе ищем класс Plugin.
        if hasattr(module, "create_plugin"):
            plugin = module.create_plugin()
        elif hasattr(module, "Plugin"):
            plugin = module.Plugin()
        else:
            raise ImportError(
                f"Модуль '{entry_path}' не содержит create_plugin() или класс Plugin"
            )

        # Проверяем соответствие протоколу.
        for attr in ("name", "version", "initialize", "get_ipc_methods"):
            if not hasattr(plugin, attr):
                raise TypeError(
                    f"Плагин '{info.name}' не реализует обязательный атрибут '{attr}'"
                )

        return plugin

    # ------------------------------------------------------------------
    # Перечисление и информация
    # ------------------------------------------------------------------

    def list_plugins(self) -> list[dict[str, Any]]:
        """Возвращает список всех обнаруженных плагинов с их статусом."""
        result: list[dict[str, Any]] = []
        for name, info in self._discovered.items():
            plugin_entry: dict[str, Any] = {
                "name": info.name,
                "version": info.version,
                "description": info.description,
                "author": info.author,
                "entry_point": info.entry_point,
                "status": self._statuses.get(name, _STATUS_DISCOVERED),
                "enabled": name not in self._disabled,
            }
            # Если загружен — добавляем список IPC-методов.
            if name in self._loaded:
                try:
                    plugin_entry["methods"] = list(self._loaded[name].get_ipc_methods().keys())
                except Exception:
                    plugin_entry["methods"] = []
            else:
                plugin_entry["methods"] = []

            if name in self._errors:
                plugin_entry["error"] = self._errors[name]

            result.append(plugin_entry)

        return result

    def get_plugin_info(self, name: str) -> dict[str, Any]:
        """Возвращает подробную информацию о плагине по имени."""
        if name not in self._discovered:
            raise KeyError(f"Плагин '{name}' не найден")

        info = self._discovered[name]
        result: dict[str, Any] = {
            "name": info.name,
            "version": info.version,
            "description": info.description,
            "author": info.author,
            "entry_point": info.entry_point,
            "plugin_dir": str(info.plugin_dir),
            "status": self._statuses.get(name, _STATUS_DISCOVERED),
        }
        if name in self._loaded:
            try:
                result["methods"] = list(self._loaded[name].get_ipc_methods().keys())
            except Exception:
                result["methods"] = []
        else:
            result["methods"] = []

        if name in self._errors:
            result["error"] = self._errors[name]

        return result

    # ------------------------------------------------------------------
    # Включение / отключение плагинов
    # ------------------------------------------------------------------

    def enable_plugin(self, name: str) -> bool:
        """Включает ранее отключённый плагин.

        Returns:
            True если плагин был отключён и теперь включён, False если не был отключён.

        Raises:
            KeyError: если плагин не обнаружен.
        """
        if name not in self._discovered:
            raise KeyError(f"Плагин '{name}' не найден")
        if name not in self._disabled:
            return False
        self._disabled.discard(name)
        # Восстанавливаем статус: loaded если был загружен, иначе discovered.
        if name in self._loaded:
            self._statuses[name] = _STATUS_LOADED
        else:
            self._statuses[name] = _STATUS_DISCOVERED
        logger.info("Плагин '%s' включён", name)
        return True

    def disable_plugin(self, name: str) -> bool:
        """Отключает плагин (не выгружает из памяти, но хуки не вызываются).

        Returns:
            True если плагин был включён и теперь отключён, False если уже был отключён.

        Raises:
            KeyError: если плагин не обнаружен.
        """
        if name not in self._discovered:
            raise KeyError(f"Плагин '{name}' не найден")
        if name in self._disabled:
            return False
        self._disabled.add(name)
        self._statuses[name] = _STATUS_DISABLED
        logger.info("Плагин '%s' отключён", name)
        return True

    # ------------------------------------------------------------------
    # Хуки
    # ------------------------------------------------------------------

    def call_hook(self, hook_name: str, payload: Any) -> list[Any]:
        """Вызывает хук у всех загруженных и включённых плагинов.

        Хук вызывается только если соответствующий метод присутствует у плагина.
        Ошибки в хуке изолированы — не прерывают обработку остальных плагинов.

        Args:
            hook_name: имя хука (HOOK_ON_TRANSCRIBE / HOOK_ON_PASTE).
            payload: данные, передаваемые хуку.

        Returns:
            Список возвращаемых значений от каждого сработавшего хука.
        """
        results: list[Any] = []
        for name, plugin in self._loaded.items():
            if name in self._disabled:
                continue
            hook_fn = getattr(plugin, hook_name, None)
            if hook_fn is None or not callable(hook_fn):
                continue
            try:
                result = hook_fn(payload)
                results.append(result)
            except Exception as exc:
                logger.error("Ошибка в хуке '%s' плагина '%s': %s", hook_name, name, exc)
        return results

    # ------------------------------------------------------------------
    # IPC-обработчики (интегрируются в BackendService.handle_request)
    # ------------------------------------------------------------------

    def handle_list_plugins(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик list_plugins — список плагинов с метаданными."""
        # При первом вызове запускаем обнаружение (ленивое).
        if not self._discovered and self._data_dir is not None:
            self.discover_plugins()
        return {"plugins": self.list_plugins()}

    def handle_get_plugin_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик get_plugin_info — детали одного плагина."""
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("Параметр 'name' обязателен")
        return self.get_plugin_info(name)
