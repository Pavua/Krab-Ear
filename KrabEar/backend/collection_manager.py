"""CollectionManager — управление коллекциями/папками для организации истории Krab Ear.

Коллекции позволяют группировать записи истории в именованные папки.
Данные сохраняются в {data_dir}/collections.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.CollectionManager")

_COLLECTIONS_FILE = "collections.json"

# DoS caps (MED wave-25)
MAX_COLLECTIONS = 500
MAX_ITEMS_PER_COLLECTION = 10_000
MAX_COLLECTION_NAME_LEN = 200
MAX_ITEM_ID_LEN = 200

# Basic item_id format guard (A4 wave-34): no path separators or null bytes.
_ITEM_ID_UNSAFE_RE = re.compile(r'[/\\.\x00]')


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

    def __init__(
        self,
        store: Any,
        settings_fn: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self._store = store
        self._settings_fn = settings_fn
        self._data_dir = Path(getattr(store, "data_dir", "."))
        self._collections_path = self._data_dir / _COLLECTIONS_FILE
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"collections": {}}
        self._load()

    def _is_privacy_mode(self) -> bool:
        """Returns True when privacy_mode_enabled is set in cached settings."""
        if self._settings_fn is None:
            return False
        try:
            return bool(self._settings_fn().get("privacy_mode_enabled", False))
        except Exception:
            return False

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
            logger.warning(
                "Не удалось загрузить коллекции — начинаем с пустого состояния: %s",
                exc,
                exc_info=True,
            )

    def _save(self) -> None:
        """Атомарно сохраняет коллекции в файл (tmp + fsync + rename).

        Запись через временный файл рядом с целевым предотвращает повреждение
        collections.json при сбое в середине записи.

        FINDING 1 (MED W1769 — silent failure): раньше любое исключение записи
        (диск полон / EACCES / read-only FS) проглатывалось — метод логировал
        ошибку и возвращался нормально. Из-за этого create/delete/add/remove/rename
        мутировали состояние в памяти, «успешно» возвращали {ok/deleted: true},
        но НИЧЕГО не писалось на диск → ложный успех; после рестарта изменение
        терялось (удалённая коллекция возвращалась). Теперь исключение логируется
        (структурно, без PII — только тип ошибки) и пробрасывается дальше, чтобы
        вызвавший IPC-метод вернул error-конверт (ok:false), а не ok:true.
        """
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._collections_path.with_suffix(
                self._collections_path.suffix + ".tmp"
            )
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._collections_path)
        except Exception as exc:
            # Структурный лог без PII: пишем только тип исключения, никогда —
            # имена/описания коллекций (free-text PII) или item_ids.
            logger.error(
                "Не удалось сохранить коллекции (изменение НЕ записано на диск)",
                extra={"error": type(exc).__name__},
            )
            # Пробрасываем, чтобы мутирующие методы не вернули ложный успех.
            raise

    def purge_all(self) -> None:
        """Полная очистка всех коллекций (privacy-wipe).

        FINDING 2 (MED W1769 — purge-gap): collections.json хранит
        пользовательские имена и описания коллекций (free-text PII) вместе со
        ссылками на item_ids истории и переживал purge_all_data. Метод закрывает
        этот пробел:
          1. Захватывает _lock.
          2. Сбрасывает in-memory реестр в пустое состояние.
          3. Удаляет collections.json с диска (missing_ok=True).
          4. Удаляет .tmp-сосед (мог остаться от прерванной атомарной записи).

        Гарантирует отсутствие PII после возврата. Идемпотентен: повторный вызов
        при отсутствии файлов не бросает исключений.
        """
        with self._lock:
            self._data = {"collections": {}}
            tmp_path = self._collections_path.with_suffix(
                self._collections_path.suffix + ".tmp"
            )
            for path in (self._collections_path, tmp_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    # Структурный лог без PII — только тип ошибки.
                    logger.warning(
                        "CollectionManager.purge_all: не удалось удалить файл",
                        extra={"error": type(exc).__name__},
                    )
            logger.info("CollectionManager: коллекции очищены (purge_all)")

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def create_collection(self, name: str, description: str = "") -> dict[str, Any]:
        """Создаёт новую коллекцию.

        Args:
            name: Уникальное имя коллекции (непустое, не длиннее MAX_COLLECTION_NAME_LEN).
            description: Опциональное описание.

        Returns:
            dict с полями name, description, created_at, item_count.

        Raises:
            ValueError: если имя пустое, слишком длинное, или коллекция уже существует.
        """
        name = name.strip()
        if not name:
            raise ValueError("Имя коллекции не может быть пустым")
        if len(name) > MAX_COLLECTION_NAME_LEN:
            raise ValueError(
                f"Имя коллекции слишком длинное (максимум {MAX_COLLECTION_NAME_LEN} символов)"
            )

        with self._lock:
            if len(self._data["collections"]) >= MAX_COLLECTIONS:
                return {
                    "ok": False,
                    "reason": "limit_exceeded",
                    "detail": f"Достигнут лимит коллекций ({MAX_COLLECTIONS})",
                }
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
            item_id: ID записи истории (непустой str, не длиннее MAX_ITEM_ID_LEN).

        Returns:
            dict с именем коллекции и обновлённым item_count.
            Возвращает {"ok": False, "reason": "limit_exceeded", ...} при превышении
            лимита элементов коллекции, не бросает исключение.

        Raises:
            KeyError: если коллекция не найдена.
            ValueError: если item_id не является непустой строкой до MAX_ITEM_ID_LEN символов.
        """
        collection_name = collection_name.strip()
        # Input validation: must be a non-empty string within length limit (A4 wave-34)
        if not isinstance(item_id, str):
            raise ValueError("item_id должен быть строкой")
        item_id = item_id.strip()
        if not item_id:
            raise ValueError("item_id не может быть пустым")
        if len(item_id) > MAX_ITEM_ID_LEN:
            raise ValueError(
                f"item_id слишком длинный (максимум {MAX_ITEM_ID_LEN} символов)"
            )
        if _ITEM_ID_UNSAFE_RE.search(item_id):
            raise ValueError(
                f"item_id содержит недопустимые символы: {item_id!r}"
            )

        with self._lock:
            if collection_name not in self._data["collections"]:
                raise KeyError(f"Коллекция '{collection_name}' не найдена")
            col = self._data["collections"][collection_name]
            if item_id not in col["item_ids"]:
                if len(col["item_ids"]) >= MAX_ITEMS_PER_COLLECTION:
                    return {
                        "ok": False,
                        "reason": "limit_exceeded",
                        "detail": (
                            f"Коллекция достигла лимита элементов ({MAX_ITEMS_PER_COLLECTION})"
                        ),
                    }
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
            ValueError: если new_name пустой, слишком длинный или уже занят.
        """
        old_name = old_name.strip()
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("Новое имя коллекции не может быть пустым")
        # Симметрия с create_collection: rename не должен обходить лимит длины
        # (иначе через переименование можно записать на диск имя любой длины).
        if len(new_name) > MAX_COLLECTION_NAME_LEN:
            raise ValueError(
                f"Имя коллекции слишком длинное (максимум {MAX_COLLECTION_NAME_LEN} символов)"
            )

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

        Privacy gate (A2 wave-34): когда privacy_mode_enabled=True, возвращает
        пустой список вместо transcript cleartext.

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
            if self._is_privacy_mode():
                return []
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
        # wave-1770 MED: collection names/descriptions are user-defined free-text PII.
        # Gate consistently with handle_get_collection_items (line ~365).
        if self._is_privacy_mode():
            return {"collections": [], "reason": "privacy_mode_active"}
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
        privacy_active = self._is_privacy_mode()
        result: dict[str, Any] = {
            "collection_name": collection_name,
            "items": items,
            "count": len(items),
        }
        if privacy_active:
            result["ok"] = True
            result["reason"] = "privacy_mode_active"
        return result

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
