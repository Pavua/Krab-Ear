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

# MED DoS cap (wave-28): unbounded add_bookmark filled disk.
# 10 000 active entries ~ 6 MB in the worst case (256-char notes).
MAX_BOOKMARKS = 10_000

# LOW DoS cap (wave-36): per-note length cap to bound NDJSON growth.
# A single malicious note could write arbitrarily large bytes per bookmark.
MAX_NOTE_LEN = 2000

# Compact when tombstones exceed this fraction of total NDJSON lines.
_COMPACT_TOMBSTONE_RATIO = 0.5


class BookmarkManager:
    """Управление закладками для записей Krab Ear.

    Закладки хранятся в {data_dir}/bookmarks.ndjson.
    Удаление — через tombstone-записи ("deleted": true).
    """

    def __init__(self, data_dir: Path, settings_get: Any = None) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "bookmarks.ndjson"
        self._path.touch(exist_ok=True)
        self._lock = threading.Lock()
        self._active_count: int | None = None
        self._active_file_signature: tuple[int, int, int] | None = None
        # wave-1770: privacy gate callable — settings_get("privacy_mode_enabled", False)
        self._settings_get = settings_get or (lambda k, d: d)

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

    def _file_signature_unlocked(self) -> tuple[int, int, int]:
        """Возвращает подпись файла для обнаружения записи другим менеджером."""
        stat = self._path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _remember_active_count_unlocked(self, active_count: int) -> None:
        """Запоминает счётчик; при сбое служебного stat инвалидирует кэш."""
        try:
            signature = self._file_signature_unlocked()
        except OSError:
            self._active_count = None
            self._active_file_signature = None
            return
        self._active_count = active_count
        self._active_file_signature = signature

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

    def _should_compact(self, raw: str) -> bool:
        """True если доля tombstone-строк превышает _COMPACT_TOMBSTONE_RATIO."""
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return False
        deleted = sum(
            1 for ln in lines
            if '"deleted": true' in ln or '"deleted":true' in ln
        )
        return deleted / len(lines) >= _COMPACT_TOMBSTONE_RATIO

    def _compact_unlocked(self, active: list[dict[str, Any]]) -> None:
        """Перезаписывает bookmarks.ndjson только активными записями.

        Вызывать только внутри self._lock.
        Атомарная замена через tmp-файл (rename) в той же директории.
        """
        tmp = self._path.with_suffix(".ndjson.tmp")
        try:
            content = "\n".join(
                json.dumps(bm, ensure_ascii=False) for bm in active
            )
            if content:
                content += "\n"
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        logger.info(
            "bookmarks: compacted → %d активных записей", len(active)
        )

    def _load_active(self) -> list[dict[str, Any]]:
        """Читает все активные закладки (без tombstone'ов).

        Если tombstone-доля превышает _COMPACT_TOMBSTONE_RATIO — автоматически
        выполняет compaction под lock для сдерживания роста файла.
        """
        with self._lock:
            raw = self._path.read_text(encoding="utf-8")
            active = self._parse_active(raw)
            if self._should_compact(raw):
                try:
                    self._compact_unlocked(active)
                except Exception:
                    logger.warning("bookmarks: компакция не удалась", exc_info=True)
            self._remember_active_count_unlocked(len(active))
        return active

    def _load_active_unlocked(self) -> list[dict[str, Any]]:
        """Читает активные закладки — вызывать только внутри self._lock.

        Автоматически выполняет compaction если tombstone-доля превышает порог.
        """
        raw = self._path.read_text(encoding="utf-8")
        active = self._parse_active(raw)
        if self._should_compact(raw):
            try:
                self._compact_unlocked(active)
            except Exception:
                logger.warning("bookmarks: компакция не удалась", exc_info=True)
        self._remember_active_count_unlocked(len(active))
        return active

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, session_id: str, offset_sec: float, note: str = "") -> dict[str, Any]:
        """Создаёт закладку для сессии/item в момент offset_sec.

        MED DoS cap (wave-28): если активных закладок уже MAX_BOOKMARKS —
        возвращает {"ok": False, "reason": "limit_exceeded"} без записи.

        Args:
            session_id: ID текущей записи (или "__live__" если ещё нет item_id).
            offset_sec: смещение в секундах от начала записи.
            note: текстовая заметка пользователя (опционально).

        Returns:
            Словарь закладки с полями id / session_id / offset_sec / note / ts,
            или {"ok": False, "reason": "limit_exceeded"} при превышении лимита.
        """
        import uuid
        from datetime import datetime

        with self._lock:
            # Повторное чтение растущего NDJSON на каждом add давало O(n²) до лимита.
            # Подпись файла сохраняет быстрый путь, но замечает запись другого экземпляра.
            current_signature = self._file_signature_unlocked()
            if (
                self._active_count is None
                or current_signature != self._active_file_signature
            ):
                active_count = len(self._load_active_unlocked())
            else:
                active_count = self._active_count or 0
            if active_count >= MAX_BOOKMARKS:
                logger.warning(
                    "bookmarks: лимит %d превышен — add_bookmark отклонён", MAX_BOOKMARKS
                )
                return {"ok": False, "reason": "limit_exceeded"}

            clean_note = str(note).strip()
            # D2 (wave-36 LOW): per-note length cap — a single oversized note
            # could write an unbounded number of bytes into bookmarks.ndjson.
            if len(clean_note) > MAX_NOTE_LEN:
                clean_note = clean_note[:MAX_NOTE_LEN]
                logger.warning(
                    "bookmarks: заметка обрезана до %d символов", MAX_NOTE_LEN
                )
            bookmark: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "session_id": str(session_id).strip() or "__live__",
                "offset_sec": round(float(offset_sec), 3),
                "note": clean_note,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "deleted": False,
            }
            self._append_unlocked(bookmark)
            self._remember_active_count_unlocked(active_count + 1)

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

    def delete_all(self) -> int:
        """Полностью очищает журнал закладок (privacy-purge / wipe-all).

        Перезаписывает bookmarks.ndjson пустым файлом под lock.
        Не раскрывает никакого пользовательского контента — только item_id'ы.

        Используется ТОЛЬКО из handle_purge_all_data.

        Returns:
            Количество активных закладок до очистки.
        """
        with self._lock:
            try:
                active_before = len(self._load_active_unlocked())
            except Exception:
                logger.warning(
                    "bookmarks delete_all: count load failed — reporting 0 deleted",
                    exc_info=True,
                )
                active_before = 0
            # Atomic truncate via tmp file (same directory for rename atomicity)
            tmp = self._path.with_suffix(".ndjson.tmp")
            try:
                tmp.write_text("", encoding="utf-8")
                tmp.replace(self._path)
                self._remember_active_count_unlocked(0)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        return active_before

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
        # MED DoS cap (wave-28): add() returns {ok:False, reason:...} when limit hit.
        if not bm.get("id"):
            return bm
        return {"bookmark": bm}

    def handle_list_bookmarks(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_bookmarks — закладки для конкретного item_id.

        Privacy gate (wave-1770 HIGH): bookmarks contain session_id + offset_sec +
        user notes linked to transcript recordings. Hidden in privacy mode.

        Params:
            item_id — ID записи (обязательно)
        Returns:
            {"bookmarks": [...], "count": N}
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {"bookmarks": [], "count": 0, "reason": "privacy_mode_active"}
        item_id = str(params.get("item_id", "")).strip()
        if not item_id:
            raise ValueError("Параметр item_id обязателен")
        bookmarks = self.list_for_item(item_id)
        return {"bookmarks": bookmarks, "count": len(bookmarks)}

    def handle_list_all_bookmarks(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_all_bookmarks — все закладки.

        Privacy gate (wave-1770 HIGH): same as handle_list_bookmarks.

        Returns:
            {"bookmarks": [...], "count": N}
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {"bookmarks": [], "count": 0, "reason": "privacy_mode_active"}
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

        Privacy gate (wave-1770 HIGH): returns bookmark data (offset, notes, session_id)
        linked to a transcript recording — hidden in privacy mode.

        Эмитит событие playback.seek (через event bus) чтобы GUI перешёл
        к offset_sec. Если event bus недоступен — просто возвращает данные закладки.

        Params:
            id — ID закладки
        Returns:
            {"bookmark": <bookmark_dict>, "seek_to_sec": float}
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "reason": "privacy_mode_active"}
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
