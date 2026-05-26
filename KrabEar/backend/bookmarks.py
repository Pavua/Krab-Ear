"""Менеджер закладок (bookmarks) для длинных записей Krab Ear.

Позволяет пометить текущую секунду активной записи как закладку с заметкой.
Закладки хранятся в append-only NDJSON-журнале (delta, tombstone-удаление).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.Bookmarks")


class BookmarkManager:
    """Управление закладками для записей Krab Ear.

    Закладки хранятся в {data_dir}/bookmarks.ndjson.
    Удаление — через tombstone-записи ("deleted": true).
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "bookmarks.ndjson"
        self._path.touch(exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> None:
        """Атомарно дописывает одну JSON-запись в журнал."""
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _append_unlocked(self, record: dict[str, Any]) -> None:
        """Дописывает одну JSON-запись в журнал — вызывать только внутри self._lock."""
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _parse_active(self, raw: str) -> list[dict[str, Any]]:
        """Разбирает NDJSON-текст и возвращает активные закладки (без tombstone'ов)."""
        records: dict[str, dict[str, Any]] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            bid = obj.get("id")
            if not bid:
                continue
            if obj.get("deleted"):
                records.pop(bid, None)
            else:
                records[bid] = obj
        return list(records.values())

    def _load_active(self) -> list[dict[str, Any]]:
        """Читает все активные закладки (без tombstone'ов)."""
        with self._lock:
            raw = self._path.read_text(encoding="utf-8")
        return self._parse_active(raw)

    def _load_active_unlocked(self) -> list[dict[str, Any]]:
        """Читает активные закладки — вызывать только внутри self._lock."""
        raw = self._path.read_text(encoding="utf-8")
        return self._parse_active(raw)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, session_id: str, offset_sec: float, note: str = "") -> dict[str, Any]:
        """Создаёт закладку для сессии/item в момент offset_sec.

        Args:
            session_id: ID текущей записи (или "__live__" если ещё нет item_id).
            offset_sec: смещение в секундах от начала записи.
            note: текстовая заметка пользователя (опционально).

        Returns:
            Словарь закладки с полями id / session_id / offset_sec / note / ts.
        """
        import uuid
        from datetime import datetime

        bookmark: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "session_id": str(session_id).strip() or "__live__",
            "offset_sec": round(float(offset_sec), 3),
            "note": str(note).strip(),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "deleted": False,
        }
        self._append(bookmark)
        logger.info(
            "Закладка создана: id=%s session=%s offset=%.1fs",
            bookmark["id"], bookmark["session_id"], bookmark["offset_sec"],
        )
        return bookmark

    def list_for_item(self, item_id: str) -> list[dict[str, Any]]:
        """Возвращает все закладки для указанной записи, отсортированные по offset."""
        clean_id = str(item_id).strip()
        bookmarks = [
            b for b in self._load_active()
            if b.get("session_id") == clean_id
        ]
        bookmarks.sort(key=lambda b: b.get("offset_sec", 0.0))
        return bookmarks

    def list_all(self) -> list[dict[str, Any]]:
        """Возвращает все активные закладки."""
        active = self._load_active()
        active.sort(key=lambda b: b.get("ts", ""))
        return active

    def delete(self, bookmark_id: str) -> bool:
        """Помечает закладку удалённой (tombstone).

        Проверка существования и запись tombstone выполняются под одним lock-ом,
        исключая TOCTOU-гонку (BUG-1, W877).

        Returns:
            True если закладка существовала и была удалена.
        """
        clean_id = str(bookmark_id).strip()
        if not clean_id:
            return False

        with self._lock:
            active = self._load_active_unlocked()
            exists = any(b["id"] == clean_id for b in active)
            if not exists:
                logger.warning("Попытка удалить несуществующую закладку: %s", clean_id)
                return False
            self._append_unlocked({"id": clean_id, "deleted": True})

        logger.info("Закладка удалена: id=%s", clean_id)
        return True

    def get(self, bookmark_id: str) -> dict[str, Any] | None:
        """Возвращает закладку по ID или None."""
        clean_id = str(bookmark_id).strip()
        for b in self._load_active():
            if b.get("id") == clean_id:
                return b
        return None

    def update_session_id(self, old_session_id: str, new_item_id: str) -> int:
        """Переписывает session_id закладок, созданных как '__live__' или с temp-ID.

        Вызывается из BackendService после финализации item в StateStore,
        чтобы привязать live-закладки к реальному item_id.

        Весь цикл (load → tombstone → re-add) выполняется под одним lock-ом,
        исключая TOCTOU-гонку с concurrent add() (BUG-2, W877).

        Returns:
            Количество обновлённых закладок.
        """
        old = str(old_session_id).strip()
        new = str(new_item_id).strip()
        if not old or not new:
            return 0

        with self._lock:
            to_update = [
                b for b in self._load_active_unlocked()
                if b.get("session_id") == old
            ]
            if not to_update:
                return 0

            for bm in to_update:
                # tombstone старой записи + новая с updated session_id
                self._append_unlocked({"id": bm["id"], "deleted": True})
                updated = dict(bm)
                updated["session_id"] = new
                updated["deleted"] = False
                self._append_unlocked(updated)

        logger.info(
            "Обновлены session_id закладок: %s → %s (%d шт.)",
            old, new, len(to_update),
        )
        return len(to_update)

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_add_bookmark(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: add_bookmark — создать закладку для текущей записи.

        Params:
            session_id  — ID сессии/записи (обязательно)
            offset_sec  — смещение в секундах (обязательно)
            note        — текстовая заметка (опционально)
        Returns:
            {"bookmark": <bookmark_dict>}
        """
        session_id = str(params.get("session_id", "__live__")).strip()
        offset_sec_raw = params.get("offset_sec")
        if offset_sec_raw is None:
            raise ValueError("Параметр offset_sec обязателен")
        try:
            offset_sec = float(offset_sec_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"offset_sec должен быть числом: {exc}") from exc
        if offset_sec < 0:
            raise ValueError("offset_sec не может быть отрицательным")

        note = str(params.get("note", "")).strip()
        bm = self.add(session_id=session_id, offset_sec=offset_sec, note=note)
        return {"bookmark": bm}

    def handle_list_bookmarks(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_bookmarks — закладки для конкретного item_id.

        Params:
            item_id — ID записи (обязательно)
        Returns:
            {"bookmarks": [...], "count": N}
        """
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        bookmarks = self.list_for_item(item_id)
        return {"bookmarks": bookmarks, "count": len(bookmarks)}

    def handle_list_all_bookmarks(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_all_bookmarks — все закладки.

        Returns:
            {"bookmarks": [...], "count": N}
        """
        bookmarks = self.list_all()
        return {"bookmarks": bookmarks, "count": len(bookmarks)}

    def handle_delete_bookmark(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: delete_bookmark — удалить закладку по ID.

        Params:
            id — ID закладки
        Returns:
            {"ok": bool}
        """
        bid = str(params.get("id", "")).strip()
        if not bid:
            raise ValueError("Параметр id обязателен")
        ok = self.delete(bid)
        return {"ok": ok}

    def handle_jump_to_bookmark(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: jump_to_bookmark — получить данные закладки для навигации плеера.

        Эмитит событие playback.seek (через event bus) чтобы GUI перешёл
        к offset_sec. Если event bus недоступен — просто возвращает данные закладки.

        Params:
            id — ID закладки
        Returns:
            {"bookmark": <bookmark_dict>, "seek_to_sec": float}
        """
        bid = str(params.get("id", "")).strip()
        if not bid:
            raise ValueError("Параметр id обязателен")

        bm = self.get(bid)
        if bm is None:
            raise ValueError(f"Закладка не найдена: {bid}")

        seek_sec = bm.get("offset_sec", 0.0)

        # Попытка эмитить событие через event bus (опционально)
        try:
            from backend.event_bus import bus as event_bus
            event_bus.emit("playback.seek", {
                "bookmark_id": bid,
                "session_id": bm.get("session_id"),
                "seek_to_sec": seek_sec,
            })
        except Exception:
            pass  # event bus недоступен в тестах — не критично

        return {"bookmark": bm, "seek_to_sec": seek_sec}
