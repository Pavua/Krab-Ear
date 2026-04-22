"""CollectionManager — управление коллекциями/папками для организации истории Krab Ear.

Коллекции позволяют группировать записи истории в именованные папки.
Данные сохраняются в {data_dir}/collections.json.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.CollectionManager")

_COLLECTIONS_FILE = "collections.json"


class CollectionManager:
    """Управление коллекциями/папками для организации записей истории.

    Структура collections.json:
    {
        "collections": {
            "<name>": {
                "name": str,
                "description": str,
                "created_at": ISO8601,
                "item_ids": [str, ...]
            },
            ...
        }
    }
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._data_dir = Path(getattr(store, "data_dir", "."))
        self._collections_path = self._data_dir / _COLLECTIONS_FILE
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"collections": {}}
        self._load()

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает коллекции из файла."""
        try:
            if self._collections_path.exists():
                raw = self._collections_path.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                if isinstance(loaded, dict) and "collections" in loaded:
                    self._data = loaded
        except Exception as exc:
            logger.warning("Не удалось загрузить коллекции: %s", exc)

    def _save(self) -> None:
        """Сохраняет коллекции в файл."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._collections_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Не удалось сохранить коллекции: %s", exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def create_collection(self, name: str, description: str = "") -> dict[str, Any]:
        """Создаёт новую коллекцию.

        Args:
            name: Уникальное имя коллекции (непустое).
            description: Опциональное описание.

        Returns:
            dict с полями name, description, created_at, item_count.

        Raises:
            ValueError: если имя пустое или коллекция с таким именем уже существует.
        """
        name = name.strip()
        if not name:
            raise ValueError("Имя коллекции не может быть пустым")

        with self._lock:
            if name in self._data["collections"]:
                raise ValueError(f"Коллекция '{name}' уже существует")

            now = datetime.now(timezone.utc).isoformat()
            col = {
                "name": name,
                "description": description.strip(),
                "created_at": now,
                "item_ids": [],
            }
            self._data["collections"][name] = col
            self._save()

        return self._collection_to_dict(col)

    def delete_collection(self, name: str) -> bool:
        """Удаляет коллекцию по имени.

        Returns:
            True если коллекция существовала и была удалена, False — если не найдена.
        """
        name = name.strip()
        with self._lock:
            if name not in self._data["collections"]:
                return False
            del self._data["collections"][name]
            self._save()
        return True

    def list_collections(self) -> list[dict[str, Any]]:
        """Возвращает список всех коллекций с количеством элементов.

        Returns:
            Список dict: name, description, created_at, item_count.
        """
        with self._lock:
            cols = list(self._data["collections"].values())
        return [self._collection_to_dict(c) for c in cols]

    def add_to_collection(self, collection_name: str, item_id: str) -> dict[str, Any]:
        """Добавляет запись истории в коллекцию.

        Args:
            collection_name: Имя коллекции.
            item_id: ID записи истории.

        Returns:
            dict с именем коллекции и обновлённым item_count.

        Raises:
            KeyError: если коллекция не найдена.
            ValueError: если item_id пустой.
        """
        collection_name = collection_name.strip()
        item_id = item_id.strip()
        if not item_id:
            raise ValueError("item_id не может быть пустым")

        with self._lock:
            if collection_name not in self._data["collections"]:
                raise KeyError(f"Коллекция '{collection_name}' не найдена")
            col = self._data["collections"][collection_name]
            if item_id not in col["item_ids"]:
                col["item_ids"].append(item_id)
                self._save()
            return self._collection_to_dict(col)

    def remove_from_collection(self, collection_name: str, item_id: str) -> dict[str, Any]:
        """Удаляет запись истории из коллекции.

        Args:
            collection_name: Имя коллекции.
            item_id: ID записи истории.

        Returns:
            dict с именем коллекции и обновлённым item_count.

        Raises:
            KeyError: если коллекция не найдена.
        """
        collection_name = collection_name.strip()
        item_id = item_id.strip()

        with self._lock:
            if collection_name not in self._data["collections"]:
                raise KeyError(f"Коллекция '{collection_name}' не найдена")
            col = self._data["collections"][collection_name]
            col["item_ids"] = [i for i in col["item_ids"] if i != item_id]
            self._save()
            return self._collection_to_dict(col)

    def rename_collection(self, old_name: str, new_name: str) -> dict[str, Any]:
        """Переименовывает коллекцию.

        Args:
            old_name: Текущее имя коллекции.
            new_name: Новое имя (непустое, уникальное).

        Returns:
            dict с новым именем и item_count.

        Raises:
            KeyError: если коллекция с old_name не найдена.
            ValueError: если new_name пустой или уже занят.
        """
        old_name = old_name.strip()
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("Новое имя коллекции не может быть пустым")

        with self._lock:
            if old_name not in self._data["collections"]:
                raise KeyError(f"Коллекция '{old_name}' не найдена")
            if new_name != old_name and new_name in self._data["collections"]:
                raise ValueError(f"Коллекция '{new_name}' уже существует")
            col = self._data["collections"].pop(old_name)
            col["name"] = new_name
            self._data["collections"][new_name] = col
            self._save()

        return self._collection_to_dict(col)

    def handle_rename_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик: rename_collection."""
        old_name = str(params.get("old_name", "")).strip()
        new_name = str(params.get("new_name", "")).strip()
        if not old_name:
            raise RuntimeError("old_name обязателен")
        if not new_name:
            raise RuntimeError("new_name обязателен")
        try:
            return self.rename_collection(old_name, new_name)
        except KeyError as exc:
            raise RuntimeError(str(exc)) from exc

    def get_collection_items(self, collection_name: str) -> list[dict[str, Any]]:
        """Возвращает записи истории, входящие в коллекцию.

        Args:
            collection_name: Имя коллекции.

        Returns:
            Список dict записей истории (через store) в порядке добавления.
            Записи, удалённые из истории, пропускаются.

        Raises:
            KeyError: если коллекция не найдена.
        """
        collection_name = collection_name.strip()

        with self._lock:
            if collection_name not in self._data["collections"]:
                raise KeyError(f"Коллекция '{collection_name}' не найдена")
            item_ids = list(self._data["collections"][collection_name]["item_ids"])

        result = []
        for item_id in item_ids:
            try:
                item = self._store.get_history_item_by_id(item_id)
                if item is not None:
                    result.append(item.to_dict())
            except Exception as exc:
                logger.warning("Не удалось загрузить запись %s: %s", item_id, exc)

        return result

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_create_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик: create_collection."""
        name = str(params.get("name", "")).strip()
        description = str(params.get("description", "")).strip()
        if not name:
            raise RuntimeError("name обязателен")
        return self.create_collection(name=name, description=description)

    def handle_delete_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик: delete_collection."""
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("name обязателен")
        deleted = self.delete_collection(name)
        return {"deleted": deleted, "name": name}

    def handle_list_collections(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик: list_collections."""
        return {"collections": self.list_collections()}

    def handle_add_to_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик: add_to_collection."""
        collection_name = str(params.get("collection_name", "")).strip()
        item_id = str(params.get("item_id", "")).strip()
        if not collection_name:
            raise RuntimeError("collection_name обязателен")
        if not item_id:
            raise RuntimeError("item_id обязателен")
        try:
            return self.add_to_collection(collection_name, item_id)
        except KeyError as exc:
            raise RuntimeError(str(exc)) from exc

    def handle_remove_from_collection(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик: remove_from_collection."""
        collection_name = str(params.get("collection_name", "")).strip()
        item_id = str(params.get("item_id", "")).strip()
        if not collection_name:
            raise RuntimeError("collection_name обязателен")
        if not item_id:
            raise RuntimeError("item_id обязателен")
        try:
            return self.remove_from_collection(collection_name, item_id)
        except KeyError as exc:
            raise RuntimeError(str(exc)) from exc

    def handle_get_collection_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик: get_collection_items."""
        collection_name = str(params.get("collection_name", "")).strip()
        if not collection_name:
            raise RuntimeError("collection_name обязателен")
        try:
            items = self.get_collection_items(collection_name)
        except KeyError as exc:
            raise RuntimeError(str(exc)) from exc
        return {"collection_name": collection_name, "items": items, "count": len(items)}

    # ------------------------------------------------------------------
    # Хелперы
    # ------------------------------------------------------------------

    @staticmethod
    def _collection_to_dict(col: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": col["name"],
            "description": col.get("description", ""),
            "created_at": col.get("created_at", ""),
            "item_count": len(col.get("item_ids", [])),
        }
