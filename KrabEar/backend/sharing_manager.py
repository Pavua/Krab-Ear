"""SharingManager — подготовка пакетов для шаринга транскрипций Krab Ear.

Позволяет упаковывать одну или несколько записей истории в текстовый пакет
(markdown / text / json) и сохранять их в {data_dir}/shares/.
"""

from __future__ import annotations

import json
import logging
import random
import string
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.SharingManager")

_BASE62_CHARS = string.ascii_letters + string.digits  # 62 символа
_SHARE_ID_LEN = 8
_SHARES_DIR = "shares"
_SHARES_INDEX_FILE = "shares_index.json"

SUPPORTED_FORMATS = ("markdown", "text", "json")


@dataclass
class SharePackage:
    """Пакет для шаринга транскрипций."""

    share_id: str
    content: str
    filename: str
    size_bytes: int
    created_at: str  # ISO-8601

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SharingManager:
    """Управляет подготовкой и хранением пакетов для шаринга транскрипций.

    Пакеты хранятся в {data_dir}/shares/ как текстовые файлы.
    Индекс пакетов — {data_dir}/shares/shares_index.json.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._data_dir = Path(getattr(store, "data_dir", "."))
        self._shares_dir = self._data_dir / _SHARES_DIR
        self._index_path = self._shares_dir / _SHARES_INDEX_FILE
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        self._shares_dir.mkdir(parents=True, exist_ok=True)
        self._load_index()

    # ------------------------------------------------------------------
    # Персистентность индекса
    # ------------------------------------------------------------------

    def _load_index(self) -> None:
        """Загружает индекс пакетов из файла."""
        try:
            if self._index_path.exists():
                raw = self._index_path.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    self._index = loaded
        except Exception as exc:
            logger.warning("Не удалось загрузить индекс shares: %s", exc)

    def _save_index(self) -> None:
        """Сохраняет индекс пакетов атомарно."""
        try:
            tmp = self._index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._index_path)
        except Exception as exc:
            logger.error("Не удалось сохранить индекс shares: %s", exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def generate_share_id(self) -> str:
        """Генерирует короткий уникальный ID (8 символов, base62)."""
        return "".join(random.choices(_BASE62_CHARS, k=_SHARE_ID_LEN))

    def prepare_share(
        self,
        item_ids: list[str],
        format: str = "markdown",
        include_translation: bool = True,
    ) -> SharePackage:
        """Упаковывает записи истории в SharePackage.

        Args:
            item_ids: список ID записей истории для включения в пакет.
            format: формат пакета — "markdown", "text" или "json".
            include_translation: включать ли поля перевода.

        Returns:
            SharePackage с готовым контентом и метаданными.

        Raises:
            ValueError: если format не поддерживается или item_ids пустой.
        """
        if not item_ids:
            raise ValueError("item_ids не может быть пустым")
        fmt = format.strip().lower()
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Неподдерживаемый формат: {format!r}. Допустимые: {SUPPORTED_FORMATS}"
            )

        items = self._fetch_items(item_ids)
        content = self._render(items, fmt, include_translation)

        share_id = self._unique_share_id()
        ext = {"markdown": "md", "text": "txt", "json": "json"}[fmt]
        filename = f"krabear_share_{share_id}.{ext}"
        created_at = datetime.now(tz=timezone.utc).isoformat()
        size_bytes = len(content.encode("utf-8"))

        package = SharePackage(
            share_id=share_id,
            content=content,
            filename=filename,
            size_bytes=size_bytes,
            created_at=created_at,
        )

        self._persist_package(package)
        return package

    def list_shared(self) -> list[dict[str, Any]]:
        """Возвращает список всех сохранённых пакетов (без content)."""
        with self._lock:
            result = []
            for entry in self._index.values():
                # Возвращаем метаданные без тяжёлого content
                result.append({k: v for k, v in entry.items() if k != "content"})
            result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return result

    def get_shared(self, share_id: str) -> SharePackage | None:
        """Возвращает SharePackage по ID или None, если не найден."""
        with self._lock:
            entry = self._index.get(share_id)
            if entry is None:
                return None
            return SharePackage(**entry)

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_prepare_share(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: подготовить пакет для шаринга."""
        item_ids = params.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise RuntimeError("Параметр 'item_ids' должен быть непустым списком")
        fmt = str(params.get("format", "markdown")).strip()
        include_translation = bool(params.get("include_translation", True))
        package = self.prepare_share(item_ids, format=fmt, include_translation=include_translation)
        return package.to_dict()

    def handle_list_shared(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: список сохранённых пакетов."""
        return {"shares": self.list_shared()}

    def handle_get_shared(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: получить пакет по share_id."""
        share_id = str(params.get("share_id", "")).strip()
        if not share_id:
            raise RuntimeError("Параметр 'share_id' обязателен")
        package = self.get_shared(share_id)
        if package is None:
            raise RuntimeError(f"Пакет не найден: {share_id!r}")
        return package.to_dict()

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _fetch_items(self, item_ids: list[str]) -> list[Any]:
        """Получает записи истории из store по списку ID.

        Записи, не найденные в store, пропускаются с предупреждением.
        """
        items = []
        for item_id in item_ids:
            item = None
            # StateStore предоставляет get_history_item_by_id (или аналог)
            if hasattr(self._store, "get_history_item_by_id"):
                item = self._store.get_history_item_by_id(item_id)
            if item is None:
                logger.warning("Запись не найдена при формировании пакета: %s", item_id)
            else:
                items.append(item)
        return items

    def _render(self, items: list[Any], fmt: str, include_translation: bool) -> str:
        """Рендерит список записей в строку нужного формата."""
        if fmt == "json":
            return self._render_json(items, include_translation)
        elif fmt == "markdown":
            return self._render_markdown(items, include_translation)
        else:
            return self._render_text(items, include_translation)

    def _render_json(self, items: list[Any], include_translation: bool) -> str:
        rows = []
        for item in items:
            d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            if not include_translation:
                d.pop("translated_text", None)
                d.pop("translation_mode", None)
                d.pop("source_lang", None)
                d.pop("target_lang", None)
                d.pop("translation_status", None)
                d.pop("translation_engine", None)
            rows.append(d)
        return json.dumps(rows, ensure_ascii=False, indent=2)

    def _render_markdown(self, items: list[Any], include_translation: bool) -> str:
        lines = ["# Krab Ear — экспорт транскрипций\n"]
        for idx, item in enumerate(items, 1):
            d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            ts = d.get("ts", "")
            text = d.get("text", "")
            lines.append(f"## {idx}. {ts}")
            lines.append(f"\n{text}\n")
            if include_translation:
                translated = d.get("translated_text", "")
                if translated:
                    src_lang = d.get("source_lang", "")
                    tgt_lang = d.get("target_lang", "")
                    lines.append(f"> **Перевод** ({src_lang}→{tgt_lang}): {translated}\n")
        return "\n".join(lines)

    def _render_text(self, items: list[Any], include_translation: bool) -> str:
        parts = []
        for idx, item in enumerate(items, 1):
            d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            ts = d.get("ts", "")
            text = d.get("text", "")
            parts.append(f"[{idx}] {ts}\n{text}")
            if include_translation:
                translated = d.get("translated_text", "")
                if translated:
                    parts.append(f"  Перевод: {translated}")
        return "\n\n".join(parts)

    def _persist_package(self, package: SharePackage) -> None:
        """Сохраняет пакет на диск и обновляет индекс."""
        # Сохраняем текстовый файл пакета
        file_path = self._shares_dir / package.filename
        try:
            file_path.write_text(package.content, encoding="utf-8")
        except Exception as exc:
            logger.error("Не удалось сохранить файл пакета %s: %s", file_path, exc)

        with self._lock:
            self._index[package.share_id] = package.to_dict()
            self._save_index()

    def _unique_share_id(self) -> str:
        """Генерирует share_id, гарантированно отсутствующий в индексе."""
        for _ in range(20):
            sid = self.generate_share_id()
            if sid not in self._index:
                return sid
        # Крайне маловероятно, но добавляем timestamp-суффикс для надёжности
        return self.generate_share_id() + str(int(datetime.now().timestamp()))[-4:]
