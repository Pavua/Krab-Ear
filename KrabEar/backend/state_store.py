"""Локальное хранилище настроек и безлимитной истории Krab Ear.

Ключевые требования:
1) история хранится в append-only NDJSON;
2) удаление делается tombstone-записями;
3) статусы вставки обновляются отдельным журналом;
4) все операции записи защищены file-lock и атомарными replace.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator

from .models import DEFAULT_SETTINGS, HistoryItem

logger = logging.getLogger("KrabEar.Backend.Store")


class StateStore:
    """Фасад для настроек и истории backend-сервиса."""

    def __init__(
        self,
        data_dir: Path,
        compact_threshold_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.data_dir = data_dir
        self.compact_threshold_bytes = compact_threshold_bytes

        self.settings_path = self.data_dir / "settings.json"
        self.history_path = self.data_dir / "history.ndjson"
        self.tombstones_path = self.data_dir / "history_tombstones.ndjson"
        self.status_path = self.data_dir / "history_status.ndjson"
        self.vocabulary_path = self.data_dir / "vocabulary.txt"
        self.lock_path = self.data_dir / "history.lock"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.history_path, self.tombstones_path, self.status_path, self.vocabulary_path):
            path.touch(exist_ok=True)

        # Кэш ускоренного поиска по последним N активным записям.
        # Важно: это только read-through оптимизация, источник истины остаётся NDJSON.
        self._recent_search_index_signature: tuple[int, int, int, int, int, int, int] | None = None
        self._recent_search_index: list[tuple[HistoryItem, str]] = []
        self._recent_search_index_limit = 4000

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Глобальный lock для журналов истории и настроек."""
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def load_settings(self) -> dict[str, Any]:
        """Читает настройки и дополняет их дефолтами."""
        with self._lock():
            if not self.settings_path.exists():
                return dict(DEFAULT_SETTINGS)

            try:
                payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Файл настроек поврежден, возвращены дефолты")
                return dict(DEFAULT_SETTINGS)

            settings = dict(DEFAULT_SETTINGS)
            if isinstance(payload, dict):
                settings.update(payload)
            return settings

    def save_settings(self, new_settings: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет настройки атомарно и возвращает нормализованный результат."""
        with self._lock():
            settings = dict(DEFAULT_SETTINGS)
            settings.update(new_settings)
            tmp_path = self.settings_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.settings_path)
            return settings

    def load_vocabulary(self) -> list[str]:
        """Загружает список пользовательских слов."""
        with self._lock():
            if not self.vocabulary_path.exists():
                return []
            content = self.vocabulary_path.read_text(encoding="utf-8")
            return [w.strip() for w in content.splitlines() if w.strip()]

    def save_vocabulary(self, words: list[str]) -> None:
        """Сохраняет список пользовательских слов."""
        unique_words = sorted(list(set(w.strip() for w in words if w.strip())))
        with self._lock():
            self.vocabulary_path.write_text("\n".join(unique_words) + "\n", encoding="utf-8")

    def add_history_item(
        self,
        text: str,
        paste_status: str = "failed",
        source_text: str = "",
        translated_text: str = "",
        translation_mode: str = "off",
        source_lang: str = "",
        target_lang: str = "",
        translation_status: str = "not_requested",
        translation_engine: str = "",
        chat_id: str = "",
        message_id: str = "",
        cleaned_text: str = "",
        llm_applied: bool = False,
        llm_latency_ms: int = 0,
        diarization: dict | None = None,
        audio_duration_sec: float | None = None,
    ) -> HistoryItem:
        """Добавляет запись в основной журнал истории."""
        item = HistoryItem.create(
            text=text,
            paste_status=paste_status,
            source_text=source_text,
            translated_text=translated_text,
            translation_mode=translation_mode,
            source_lang=source_lang,
            target_lang=target_lang,
            translation_status=translation_status,
            translation_engine=translation_engine,
            chat_id=chat_id,
            message_id=message_id,
            cleaned_text=cleaned_text,
            llm_applied=llm_applied,
            llm_latency_ms=llm_latency_ms,
            diarization=diarization,
            audio_duration_sec=audio_duration_sec,
        )
        with self._lock():
            self._append_ndjson(self.history_path, item.to_dict())
        return item

    def set_paste_status(self, item_id: str, paste_status: str) -> bool:
        """Записывает обновление статуса вставки отдельным append-журналом."""
        clean_id = item_id.strip()
        if not clean_id:
            return False

        payload = {"id": clean_id, "paste_status": paste_status.strip() or "failed"}
        with self._lock():
            self._append_ndjson(self.status_path, payload)
        return True

    def delete_history_item(self, item_id: str) -> bool:
        """Логически удаляет запись через tombstone."""
        clean_id = item_id.strip()
        if not clean_id:
            return False
        with self._lock():
            self._append_ndjson(self.tombstones_path, {"id": clean_id})
        return True

    def get_history_page(self, cursor: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        """Возвращает страницу истории от новых к старым."""
        return self.get_history_page_filtered(
            cursor=cursor,
            limit=limit,
            paste_status=None,
            translation_mode=None,
        )

    def get_history_page_filtered(
        self,
        cursor: str | None,
        limit: int,
        paste_status: str | None,
        translation_mode: str | None,
        translation_status: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Возвращает страницу истории от новых к старым с опциональными фильтрами.

        Оптимизация: при наличии from_ts итерация прекращается, как только
        записи становятся старше нижней границы диапазона (NDJSON хронологический).
        """
        safe_limit = max(1, min(limit, 500))
        safe_cursor = self._parse_cursor(cursor)
        filter_paste = self._normalize_optional_filter(paste_status)
        filter_mode = self._normalize_optional_filter(translation_mode)
        filter_translation_status = self._normalize_optional_filter(translation_status)
        filter_from_ts = self._normalize_ts_filter(from_ts, is_end=False)
        filter_to_ts = self._normalize_ts_filter(to_ts, is_end=True)

        with self._lock():
            active = self._load_active_items_unlocked()

        newest_first = []
        for item in reversed(active):
            # Early termination: items are chronological, iterating newest-first.
            # Once item.ts < from_ts, all remaining are even older — stop.
            if filter_from_ts is not None and item.ts < filter_from_ts:
                break
            if not self._matches_filters(
                item,
                filter_paste,
                filter_mode,
                filter_translation_status,
                filter_from_ts,
                filter_to_ts,
            ):
                continue
            newest_first.append(item)

        start = safe_cursor
        end = safe_cursor + safe_limit
        page = newest_first[start:end]

        next_cursor = str(end) if end < len(newest_first) else None
        return [item.to_dict() for item in page], next_cursor

    def search_history(
        self,
        query: str,
        cursor: str | None,
        limit: int,
        paste_status: str | None = None,
        translation_mode: str | None = None,
        translation_status: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Поиск по истории с пагинацией, сортировка от новых к старым."""
        needle = query.strip().lower()
        safe_limit = max(1, min(limit, 500))
        safe_cursor = self._parse_cursor(cursor)
        filter_paste = self._normalize_optional_filter(paste_status)
        filter_mode = self._normalize_optional_filter(translation_mode)
        filter_translation_status = self._normalize_optional_filter(translation_status)
        filter_from_ts = self._normalize_ts_filter(from_ts, is_end=False)
        filter_to_ts = self._normalize_ts_filter(to_ts, is_end=True)

        with self._lock():
            active = self._load_active_items_unlocked()
            recent_index = self._get_recent_search_index_unlocked(active)

        # Быстрый путь: проверяем сначала последние N записей.
        # Если найденных результатов достаточно для текущей страницы,
        # возвращаем их без полного прохода по всей истории.
        # Early termination при наличии from_ts (индекс отсортирован newest-first).
        filtered = []
        if needle:
            for item, haystack in recent_index:
                if filter_from_ts is not None and item.ts < filter_from_ts:
                    break
                if not self._matches_filters(
                    item,
                    filter_paste,
                    filter_mode,
                    filter_translation_status,
                    filter_from_ts,
                    filter_to_ts,
                ):
                    continue
                if needle in haystack:
                    filtered.append(item)

            if len(filtered) >= (safe_cursor + safe_limit) or len(recent_index) >= len(active):
                start = safe_cursor
                end = safe_cursor + safe_limit
                page = filtered[start:end]
                next_cursor = str(end) if end < len(filtered) else None
                return [item.to_dict() for item in page], next_cursor

        # Точный fallback: полный проход по всей истории.
        # Early termination при наличии from_ts (хронологический порядок NDJSON).
        filtered = []
        for item in reversed(active):
            if filter_from_ts is not None and item.ts < filter_from_ts:
                break
            if not self._matches_filters(
                item,
                filter_paste,
                filter_mode,
                filter_translation_status,
                filter_from_ts,
                filter_to_ts,
            ):
                continue

            if not needle:
                filtered.append(item)
                continue

            haystack = "\n".join(
                [
                    item.text.lower(),
                    item.source_text.lower(),
                    item.translated_text.lower(),
                ]
            )
            if needle in haystack:
                filtered.append(item)

        start = safe_cursor
        end = safe_cursor + safe_limit
        page = filtered[start:end]
        next_cursor = str(end) if end < len(filtered) else None
        return [item.to_dict() for item in page], next_cursor

    def _history_signature_unlocked(self) -> tuple[int, int, int, int, int, int]:
        """Возвращает сигнатуру журналов для валидации кэша поиска."""
        return (
            self._safe_file_size(self.history_path),
            self._safe_file_size(self.tombstones_path),
            self._safe_file_size(self.status_path),
            self._safe_mtime_ns(self.history_path),
            self._safe_mtime_ns(self.tombstones_path),
            self._safe_mtime_ns(self.status_path),
        )

    def _get_recent_search_index_unlocked(
        self,
        active: list[HistoryItem],
    ) -> list[tuple[HistoryItem, str]]:
        """Возвращает индекс последних N записей для ускоренного текстового поиска."""
        signature = (*self._history_signature_unlocked(), len(active))
        if signature == self._recent_search_index_signature:
            return self._recent_search_index

        window = active[-self._recent_search_index_limit :]
        index: list[tuple[HistoryItem, str]] = []
        for item in reversed(window):
            haystack = "\n".join(
                [
                    item.text.lower(),
                    item.source_text.lower(),
                    item.translated_text.lower(),
                ]
            )
            index.append((item, haystack))

        self._recent_search_index = index
        self._recent_search_index_signature = signature
        return self._recent_search_index

    @staticmethod
    def _normalize_optional_filter(value: str | None) -> str | None:
        """Нормализует фильтр; пустое значение означает отсутствие фильтра."""
        if value is None:
            return None
        clean = value.strip()
        return clean or None

    @staticmethod
    def _normalize_ts_filter(value: str | None, is_end: bool) -> str | None:
        """Нормализует фильтр времени в ISO-формат.

        Поддерживает:
        - YYYY-MM-DD
        - YYYY-MM-DDTHH:MM:SS
        """
        clean = StateStore._normalize_optional_filter(value)
        if clean is None:
            return None

        if len(clean) == 10:
            # Диапазон по дате: начало/конец суток.
            return f"{clean}T23:59:59" if is_end else f"{clean}T00:00:00"

        try:
            parsed = datetime.fromisoformat(clean)
        except ValueError:
            return None
        return parsed.isoformat(timespec="seconds")

    @staticmethod
    def _matches_filters(
        item: HistoryItem,
        paste_status: str | None,
        translation_mode: str | None,
        translation_status: str | None,
        from_ts: str | None,
        to_ts: str | None,
    ) -> bool:
        """Проверяет, подходит ли элемент истории под активные фильтры."""
        if paste_status is not None and item.paste_status != paste_status:
            return False
        if translation_mode is not None and item.translation_mode != translation_mode:
            return False
        if translation_status is not None and item.translation_status != translation_status:
            return False
        if from_ts is not None and item.ts < from_ts:
            return False
        if to_ts is not None and item.ts > to_ts:
            return False
        return True

    def import_history_ndjson(self, path: Path) -> dict[str, int]:
        """Импортирует записи истории из NDJSON без дублей по `id`."""
        source_path = path.expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError("Файл импорта не найден")

        with self._lock():
            known_ids = {item.id for item in self._iter_history_items_unlocked()}
            imported = 0
            skipped = 0
            errors = 0

            for payload in self._read_ndjson_unlocked(source_path):
                try:
                    item = HistoryItem.from_dict(payload)
                except Exception:
                    errors += 1
                    continue

                if not item.id or not item.ts or not item.text:
                    errors += 1
                    continue

                if item.id in known_ids:
                    skipped += 1
                    continue

                self._append_ndjson(self.history_path, item.to_dict())
                known_ids.add(item.id)
                imported += 1

        return {"imported": imported, "skipped": skipped, "errors": errors}

    def maybe_compact(self) -> bool:
        """Запускает компактирование при превышении порога размера файла."""
        with self._lock():
            try:
                current_size = self.history_path.stat().st_size
            except FileNotFoundError:
                return False

            if current_size <= self.compact_threshold_bytes:
                return False

            self._compact_unlocked()
            return True

    def compact(self) -> bool:
        """Явная команда компактирования истории."""
        self.compact_with_stats()
        return True

    def compact_with_stats(self) -> dict[str, int]:
        """Компактирует историю и возвращает детальную статистику."""
        with self._lock():
            before = self._history_stats_unlocked()
            self._compact_unlocked()
            after = self._history_stats_unlocked()

        return {
            "before_active_count": int(before["active_count"]),
            "before_history_lines": int(before["history_lines"]),
            "before_tombstones_lines": int(before["tombstones_lines"]),
            "before_status_lines": int(before["status_lines"]),
            "before_total_bytes": int(before["total_bytes"]),
            "after_active_count": int(after["active_count"]),
            "after_history_lines": int(after["history_lines"]),
            "after_tombstones_lines": int(after["tombstones_lines"]),
            "after_status_lines": int(after["status_lines"]),
            "after_total_bytes": int(after["total_bytes"]),
            "reclaimed_bytes": int(before["total_bytes"]) - int(after["total_bytes"]),
        }

    def get_history_stats(self) -> dict[str, int]:
        """Возвращает сводку состояния журналов истории."""
        with self._lock():
            stats = self._history_stats_unlocked()
        return {
            "active_count": int(stats["active_count"]),
            "history_lines": int(stats["history_lines"]),
            "tombstones_lines": int(stats["tombstones_lines"]),
            "status_lines": int(stats["status_lines"]),
            "history_bytes": int(stats["history_bytes"]),
            "tombstones_bytes": int(stats["tombstones_bytes"]),
            "status_bytes": int(stats["status_bytes"]),
            "total_bytes": int(stats["total_bytes"]),
        }

    def get_history_overview(self) -> dict[str, Any]:
        """Возвращает обзор истории для UI: статусы, перевод, языки, диаризация."""
        with self._lock():
            active = self._load_active_items_unlocked()

        today_iso = datetime.now().date().isoformat()
        last_24h_threshold = datetime.now() - timedelta(hours=24)

        paste_ok = 0
        paste_failed = 0
        translated_ok = 0
        translated_error = 0
        no_translation = 0
        today_count = 0
        last_24h_count = 0
        diarization_count = 0
        llm_applied_count = 0
        total_text_chars = 0
        mode_counts: dict[str, int] = {}
        source_lang_counts: dict[str, int] = {}
        target_lang_counts: dict[str, int] = {}
        today_text_chars = 0

        for item in active:
            text_len = len(item.text)
            total_text_chars += text_len

            if item.paste_status == "ok":
                paste_ok += 1
            else:
                paste_failed += 1

            if item.translation_mode == "off":
                no_translation += 1
            mode_counts[item.translation_mode] = mode_counts.get(item.translation_mode, 0) + 1

            if item.translation_status == "ok":
                translated_ok += 1
            elif item.translation_status == "translate_error":
                translated_error += 1

            if item.source_lang:
                source_lang_counts[item.source_lang] = source_lang_counts.get(item.source_lang, 0) + 1
            if item.target_lang:
                target_lang_counts[item.target_lang] = target_lang_counts.get(item.target_lang, 0) + 1

            if item.diarization is not None:
                diarization_count += 1

            if item.llm_applied:
                llm_applied_count += 1

            is_today = item.ts.startswith(today_iso)
            if is_today:
                today_count += 1
                today_text_chars += text_len
            try:
                item_dt = datetime.fromisoformat(item.ts)
            except ValueError:
                item_dt = None
            if item_dt is not None and item_dt >= last_24h_threshold:
                last_24h_count += 1

        total = len(active)
        top_modes = sorted(mode_counts.items(), key=lambda pair: pair[1], reverse=True)[:3]
        top_modes_payload = [{"mode": mode, "count": count} for mode, count in top_modes]
        top_source_langs = sorted(source_lang_counts.items(), key=lambda pair: pair[1], reverse=True)[:5]
        top_target_langs = sorted(target_lang_counts.items(), key=lambda pair: pair[1], reverse=True)[:5]

        return {
            "active_count": total,
            "paste_ok": paste_ok,
            "paste_failed": paste_failed,
            "translated_ok": translated_ok,
            "translated_error": translated_error,
            "no_translation": no_translation,
            "today_count": today_count,
            "last_24h_count": last_24h_count,
            "top_modes": top_modes_payload,
            # Языковая статистика
            "source_langs": [{"lang": lang, "count": cnt} for lang, cnt in top_source_langs],
            "target_langs": [{"lang": lang, "count": cnt} for lang, cnt in top_target_langs],
            # Диаризация и LLM
            "diarization_count": diarization_count,
            "llm_applied_count": llm_applied_count,
            # Объём текста
            "avg_text_chars": round(total_text_chars / total) if total else 0,
            "today_text_chars": today_text_chars,
        }

    def count_active_items(self) -> int:
        """Возвращает количество активных (не удаленных) записей."""
        with self._lock():
            return len(self._load_active_items_unlocked())

    def _compact_unlocked(self) -> None:
        """Собирает активные записи в новый основной журнал и очищает дельты."""
        active = self._load_active_items_unlocked()
        tmp_history = self.history_path.with_suffix(".ndjson.tmp")

        with tmp_history.open("w", encoding="utf-8") as fh:
            for item in active:
                fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()

        tmp_history.replace(self.history_path)
        self.tombstones_path.write_text("", encoding="utf-8")
        self.status_path.write_text("", encoding="utf-8")

    def _history_stats_unlocked(self) -> dict[str, int]:
        """Собирает метрики журналов истории без повторного захвата lock."""
        history_lines = self._count_ndjson_entries_unlocked(self.history_path)
        tombstones_lines = self._count_ndjson_entries_unlocked(self.tombstones_path)
        status_lines = self._count_ndjson_entries_unlocked(self.status_path)
        history_bytes = self._safe_file_size(self.history_path)
        tombstones_bytes = self._safe_file_size(self.tombstones_path)
        status_bytes = self._safe_file_size(self.status_path)
        active_count = len(self._load_active_items_unlocked())
        return {
            "active_count": active_count,
            "history_lines": history_lines,
            "tombstones_lines": tombstones_lines,
            "status_lines": status_lines,
            "history_bytes": history_bytes,
            "tombstones_bytes": tombstones_bytes,
            "status_bytes": status_bytes,
            "total_bytes": history_bytes + tombstones_bytes + status_bytes,
        }

    def _load_active_items_unlocked(self) -> list[HistoryItem]:
        """Читает активные записи с применением tombstone и status-override."""
        deleted = self._load_deleted_ids_unlocked()
        statuses = self._load_status_overrides_unlocked()

        items: list[HistoryItem] = []
        for item in self._iter_history_items_unlocked():
            if item.id in deleted:
                continue
            if item.id in statuses:
                item.paste_status = statuses[item.id]
            items.append(item)
        return items

    def _load_deleted_ids_unlocked(self) -> set[str]:
        """Собирает множество удаленных идентификаторов."""
        deleted: set[str] = set()
        for payload in self._read_ndjson_unlocked(self.tombstones_path):
            item_id = str(payload.get("id", "")).strip()
            if item_id:
                deleted.add(item_id)
        return deleted

    def _load_status_overrides_unlocked(self) -> dict[str, str]:
        """Собирает последние значения paste_status по id."""
        result: dict[str, str] = {}
        for payload in self._read_ndjson_unlocked(self.status_path):
            item_id = str(payload.get("id", "")).strip()
            status = str(payload.get("paste_status", "")).strip()
            if item_id and status:
                result[item_id] = status
        return result

    def _iter_history_items_unlocked(self) -> Iterator[HistoryItem]:
        """Итератор по основному журналу истории."""
        for payload in self._read_ndjson_unlocked(self.history_path):
            try:
                item = HistoryItem.from_dict(payload)
            except Exception:
                continue
            if item.id and item.ts and item.text:
                yield item

    @staticmethod
    def _parse_cursor(cursor: str | None) -> int:
        """Преобразует курсор пагинации в безопасный integer offset."""
        if cursor is None:
            return 0
        try:
            value = int(cursor)
        except ValueError:
            return 0
        return max(0, value)

    @staticmethod
    def _append_ndjson(path: Path, payload: dict[str, Any]) -> None:
        """Атомарный append JSON-строки с flush/fsync."""
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _count_ndjson_entries_unlocked(path: Path) -> int:
        """Подсчитывает количество валидных JSON-строк в журнале."""
        count = 0
        for _ in StateStore._read_ndjson_unlocked(path):
            count += 1
        return count

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        """Возвращает размер файла или 0, если файл не найден."""
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    @staticmethod
    def _safe_mtime_ns(path: Path) -> int:
        """Возвращает mtime в ns или 0, если файл не найден."""
        try:
            return int(path.stat().st_mtime_ns)
        except FileNotFoundError:
            return 0

    @staticmethod
    def _read_ndjson_unlocked(path: Path) -> Iterator[dict[str, Any]]:
        """Итерация по корректным JSON-строкам файла."""
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
    def is_idempotent(self, chat_id: str | int | None, message_id: str | int | None) -> bool:
        """Проверяет, было ли уже успешно обработано сообщение с такими ID.
        
        Использует внутренний индекс для быстрого поиска по последним записям.
        """
        if chat_id is None or message_id is None:
            return False
            
        cid = str(chat_id).strip()
        mid = str(message_id).strip()
        if not cid or not mid:
            return False
            
        with self._lock():
            active = self._load_active_items_unlocked()
            # Проверяем последние 1000 записей
            for item in reversed(active[-1000:]):
                if item.chat_id == cid and item.message_id == mid:
                    logger.info("Обнаружен дубликат запроса: chat=%s, msg=%s", cid, mid)
                    return True
        return False
