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

from core.parsing_utils import safe_json_loads

from .models import DEFAULT_SETTINGS, HistoryItem
from core.search_index import SearchIndex

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
        self.tags_path = self.data_dir / "history_tags.ndjson"
        self.favorites_path = self.data_dir / "history_favorites.ndjson"
        self.annotations_path = self.data_dir / "history_annotations.ndjson"
        self.vocabulary_path = self.data_dir / "vocabulary.txt"
        self.text_updates_path = self.data_dir / "history_text_updates.ndjson"
        self.action_items_path = self.data_dir / "history_action_items.ndjson"
        self.calendar_links_path = self.data_dir / "history_calendar_links.ndjson"
        self.lock_path = self.data_dir / "history.lock"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in (
                self.history_path,
                self.tombstones_path,
                self.status_path,
                self.tags_path,
                self.favorites_path,
                self.annotations_path,
                self.vocabulary_path,
                self.text_updates_path,
                self.action_items_path,
                self.calendar_links_path):
            path.touch(exist_ok=True)

        # Кэш ускоренного поиска по последним N активным записям.
        # Важно: это только read-through оптимизация, источник истины остаётся NDJSON.
        self._recent_search_index_signature: tuple[int, ...] | None = None
        self._recent_search_index: list[tuple[HistoryItem, str]] = []
        self._recent_search_index_limit = 4000

        # Инвертированный индекс для быстрого полнотекстового поиска.
        self._search_index: SearchIndex = SearchIndex()
        # Phase B.2 — error_bus late-injection

    def _push_error(self, code: str, message_debug: str, severity: str | None = None) -> None:
        """Push KrabError to attached ErrorBus if available. Late-injected attribute."""
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get(code, {})
            err = KrabError(
                severity=severity or entry.get("severity", "warn"),
                component="history",
                code=code,
                message_user=entry.get("user_msg_ru", "Ошибка хранилища"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context={"data_dir": str(self.data_dir)},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception as e:  # noqa: BLE001
            # Wave 222: surface push failures to Sentry instead of silent swallow
            try:
                from backend.observability import capture_exception
                capture_exception(e, "_push_error_internal")
            except Exception:
                pass  # Sentry itself failing — stay silent
            logger.exception("error_bus.push failed for code=%s", code)

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

            payload = safe_json_loads(
                self.settings_path.read_text(encoding="utf-8"),
                default=None,
                context="settings.json",
            )
            if payload is None:
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
        confidence: float | None = None,
        emotion: str | None = None,
        word_timestamps: list | None = None,
        speaker_turns: list | None = None,
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
            confidence=confidence,
            emotion=emotion,
            word_timestamps=word_timestamps,
            speaker_turns=speaker_turns,
        )
        try:
            with self._lock():
                self._append_ndjson(self.history_path, item.to_dict())
        except Exception as exc:
            logger.error("Ошибка записи в history.ndjson: %s", exc)
            # Phase B.2: history.write_fail — disk full, permission denied, lock timeout
            self._push_error(
                "history.write_fail",
                f"{type(exc).__name__}: {exc}",
                severity="critical",
            )
            raise
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

        # Быстрый путь через инвертированный индекс (без фильтров по дате/статусу).
        no_extra_filters = (
            filter_paste is None
            and filter_mode is None
            and filter_translation_status is None
            and filter_from_ts is None
            and filter_to_ts is None
        )
        if needle and no_extra_filters:
            self._search_index.build_index([item.to_dict() for item in active])
            idx_results = self._search_index.search(needle, limit=safe_cursor + safe_limit)
            if idx_results is not None:
                # idx_results — неупорядоченные по времени; восстанавливаем порядок.
                matched_ids = {r.item_id for r in idx_results}
                filtered_by_index = [
                    item for item in reversed(active) if item.id in matched_ids
                ]
                start = safe_cursor
                end = safe_cursor + safe_limit
                page = filtered_by_index[start:end]
                next_cursor = str(end) if end < len(filtered_by_index) else None
                return [item.to_dict() for item in page], next_cursor

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

    def _history_signature_unlocked(self) -> tuple[int, ...]:
        """Возвращает сигнатуру журналов для валидации кэша поиска."""
        return (
            self._safe_file_size(self.history_path),
            self._safe_file_size(self.tombstones_path),
            self._safe_file_size(self.status_path),
            self._safe_file_size(self.tags_path),
            self._safe_mtime_ns(self.history_path),
            self._safe_mtime_ns(self.tombstones_path),
            self._safe_mtime_ns(self.status_path),
            self._safe_mtime_ns(self.tags_path),
        )

    def _get_recent_search_index_unlocked(
        self,
        active: list[HistoryItem],
    ) -> list[tuple[HistoryItem, str]]:
        """Возвращает индекс последних N записей для ускоренного текстового поиска."""
        signature = (*self._history_signature_unlocked(), len(active))
        if signature == self._recent_search_index_signature:
            return self._recent_search_index

        window = active[-self._recent_search_index_limit:]
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
        from backend.observability import add_breadcrumb as _add_bc  # lazy — avoid circular

        with self._lock():
            before = self._history_stats_unlocked()
            _add_bc(
                category="history",
                message="compact_start",
                level="info",
                data={
                    "active_count": int(before["active_count"]),
                    "history_lines": int(before["history_lines"]),
                    "tombstones_lines": int(before["tombstones_lines"]),
                    "total_bytes": int(before["total_bytes"]),
                },
            )
            self._compact_unlocked()
            after = self._history_stats_unlocked()

        stats = {
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
        _add_bc(
            category="history",
            message="compact_finish",
            level="info",
            data={
                "items_compacted": stats["before_active_count"],
                "reclaimed_bytes": stats["reclaimed_bytes"],
                "after_active_count": stats["after_active_count"],
            },
        )
        return stats

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

    # ------------------------------------------------------------------
    # Disk / storage utilities
    # ------------------------------------------------------------------

    def auto_cleanup_old(
        self, days: int = 365, dry_run: bool = False
    ) -> dict[str, Any]:
        """Удаляет записи истории старше days дней (tombstone-удаление).

        Args:
            days: Записи старше этого числа дней будут удалены (>= 1).
            dry_run: Если True - возвращает количество, но не удаляет.

        Returns:
            deleted_count, remaining, dry_run, threshold_days, oldest_item_age_days
        """
        if days < 1:
            raise ValueError("days must be >= 1")

        threshold_dt = datetime.now() - timedelta(days=days)

        with self._lock():
            active = self._load_active_items_unlocked()

        to_delete = [
            item
            for item in active
            if item.ts and datetime.fromisoformat(item.ts) < threshold_dt
        ]

        oldest_age_days = None
        if active:
            oldest_ts_str = min(
                (item.ts for item in active if item.ts), default=None
            )
            if oldest_ts_str:
                oldest_dt = datetime.fromisoformat(oldest_ts_str)
                oldest_age_days = (datetime.now() - oldest_dt).days

        if not dry_run:
            for item in to_delete:
                if item.id:
                    self.delete_history_item(item.id)

        return {
            "deleted_count": len(to_delete),
            "remaining": len(active) - len(to_delete),
            "dry_run": dry_run,
            "threshold_days": days,
            "oldest_item_age_days": oldest_age_days,
        }

    def get_storage_breakdown(self) -> dict[str, Any]:
        """Возвращает разбивку использования диска по компонентам (в MB).

        Returns:
            ndjson_mb, transcripts_mb, audio_mb, total_mb, oldest_item_age_days
        """
        ndjson_mb = sum(
            self._safe_size_mb(p)
            for p in [
                self.history_path,
                self.tombstones_path,
                self.status_path,
                self.tags_path,
                self.favorites_path,
                self.annotations_path,
                self.text_updates_path,
                self.settings_path,
            ]
        )

        transcripts_dir = self.data_dir / "transcripts"
        transcripts_mb = self._dir_size_mb(transcripts_dir)

        audio_dir = self.data_dir / "audio"
        audio_mb = self._dir_size_mb(audio_dir)

        total_mb = ndjson_mb + transcripts_mb + audio_mb

        oldest_age_days = None
        try:
            with self._lock():
                active = self._load_active_items_unlocked()
            if active:
                oldest_ts_str = min(
                    (item.ts for item in active if item.ts), default=None
                )
                if oldest_ts_str:
                    oldest_dt = datetime.fromisoformat(oldest_ts_str)
                    oldest_age_days = (datetime.now() - oldest_dt).days
        except Exception:
            pass

        return {
            "ndjson_mb": round(ndjson_mb, 3),
            "transcripts_mb": round(transcripts_mb, 3),
            "audio_mb": round(audio_mb, 3),
            "total_mb": round(total_mb, 3),
            "oldest_item_age_days": oldest_age_days,
        }

    @staticmethod
    def _safe_size_mb(path) -> float:
        """Возвращает размер файла в MB или 0.0 если файл не найден."""
        try:
            return path.stat().st_size / (1024 * 1024)
        except (FileNotFoundError, OSError):
            return 0.0

    @staticmethod
    def _dir_size_mb(directory) -> float:
        """Суммарный размер всех файлов в директории (рекурсивно), MB."""
        if not directory.exists():
            return 0.0
        total = 0
        try:
            for f in directory.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except (OSError, FileNotFoundError):
                        pass
        except (OSError, PermissionError):
            pass
        return total / (1024 * 1024)

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

    def _load_active_items_with_lock(self) -> list[HistoryItem]:
        """Возвращает все активные записи с захватом lock (публичный API для агрегации)."""
        with self._lock():
            return self._load_active_items_unlocked()

    def count_active_items(self) -> int:
        """Возвращает количество активных (не удаленных) записей."""
        with self._lock():
            return len(self._load_active_items_unlocked())

    def _compact_unlocked(self) -> None:
        """Собирает активные записи в новый основной журнал и очищает дельты."""
        # W1254 F1: capture deleted IDs before clearing tombstones
        deleted_ids = self._load_deleted_ids_unlocked()

        active = self._load_active_items_unlocked()
        tmp_history = self.history_path.with_suffix(".ndjson.tmp")

        with tmp_history.open("w", encoding="utf-8") as fh:
            for item in active:
                fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()

        tmp_history.replace(self.history_path)
        self.tombstones_path.write_text("", encoding="utf-8")
        self.status_path.write_text("", encoding="utf-8")
        self.tags_path.write_text("", encoding="utf-8")
        self.favorites_path.write_text("", encoding="utf-8")
        self.text_updates_path.write_text("", encoding="utf-8")
        self.action_items_path.write_text("", encoding="utf-8")

        # W1254 F1: purge version cascade for all compacted-away (tombstoned) items
        _versioner = getattr(self, "_transcript_versioner", None)
        if _versioner is not None and deleted_ids:
            for _item_id in deleted_ids:
                try:
                    _versioner.purge_versions_for_item(_item_id)
                except Exception:
                    logger.exception(
                        "_compact_unlocked: не удалось удалить версии для id=%s", _item_id
                    )

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
        """Читает активные записи с применением tombstone, status-override, tags-override, favorites-override, text-override и action_items-override."""
        deleted = self._load_deleted_ids_unlocked()
        statuses = self._load_status_overrides_unlocked()
        tags_overrides = self._load_tags_overrides_unlocked()
        favorites_overrides = self._load_favorites_overrides_unlocked()
        text_overrides = self._load_text_overrides_unlocked()
        action_items_overrides = self._load_action_items_overrides_unlocked()

        items: list[HistoryItem] = []
        for item in self._iter_history_items_unlocked():
            if item.id in deleted:
                continue
            if item.id in statuses:
                item.paste_status = statuses[item.id]
            if item.id in tags_overrides:
                item.tags = tags_overrides[item.id]
            if item.id in favorites_overrides:
                item.favorite = favorites_overrides[item.id]
            if item.id in text_overrides:
                override = text_overrides[item.id]
                item.text = override["text"]
                if override.get("confidence") is not None:
                    item.confidence = float(override["confidence"])
            if item.id in action_items_overrides:
                override = action_items_overrides[item.id]
                item.action_items = override.get("action_items")
                item.decisions = override.get("decisions")
                item.questions = override.get("questions")
            items.append(item)
        return items

    def _load_text_overrides_unlocked(self) -> dict[str, dict]:
        """Собирает последние text/confidence overrides из журнала bulk-reprocess."""
        result: dict[str, dict] = {}
        if not self.text_updates_path.exists():
            return result
        for payload in self._read_ndjson_unlocked(self.text_updates_path):
            item_id = str(payload.get("id", "")).strip()
            if item_id and "text" in payload:
                result[item_id] = {
                    "text": str(payload["text"]),
                    "confidence": payload.get("confidence"),
                }
        return result

    def _load_action_items_overrides_unlocked(self) -> dict[str, dict]:
        """Собирает последние action_items/decisions/questions overrides из журнала."""
        result: dict[str, dict] = {}
        if not self.action_items_path.exists():
            return result
        for payload in self._read_ndjson_unlocked(self.action_items_path):
            item_id = str(payload.get("id", "")).strip()
            if item_id:
                result[item_id] = {
                    "action_items": payload.get("action_items"),
                    "decisions": payload.get("decisions"),
                    "questions": payload.get("questions"),
                }
        return result

    def update_history_item_action_items(
        self,
        item_id: str,
        action_items: list,
        decisions: list,
        questions: list,
    ) -> bool:
        """Сохраняет action_items/decisions/questions через delta-журнал (last-write-wins).

        Returns True если запись с таким id существует, False иначе.
        """
        import json as _json
        clean_id = (item_id or "").strip()
        if not clean_id:
            return False
        with self._lock():
            active_ids = {item.id for item in self._load_active_items_unlocked()}
            if clean_id not in active_ids:
                return False
            entry = {
                "id": clean_id,
                "action_items": list(action_items) if action_items is not None else [],
                "decisions": list(decisions) if decisions is not None else [],
                "questions": list(questions) if questions is not None else [],
            }
            with self.action_items_path.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        return True

    def update_history_item_text(self, item_id: str, text: str, confidence: float | None = None) -> bool:
        """Сохраняет text/confidence override для записи через delta-журнал bulk-reprocess.

        Returns True если запись с таким id существует, False иначе.
        """
        clean_id = item_id.strip()
        if not clean_id:
            return False
        with self._lock():
            # Verify item exists
            active_ids = {item.id for item in self._load_active_items_unlocked()}
            if clean_id not in active_ids:
                return False
            entry = {"id": clean_id, "text": text}
            if confidence is not None:
                entry["confidence"] = round(float(confidence), 4)
            with self.text_updates_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True

    def _load_tags_overrides_unlocked(self) -> dict[str, list[str]]:
        """Собирает последние значения tags по id из журнала тегов."""
        result: dict[str, list[str]] = {}
        for payload in self._read_ndjson_unlocked(self.tags_path):
            item_id = str(payload.get("id", "")).strip()
            tags = payload.get("tags")
            if item_id and isinstance(tags, list):
                result[item_id] = [str(t) for t in tags]
        return result

    def _load_favorites_overrides_unlocked(self) -> dict[str, bool]:
        """Собирает последние значения favorite по id из журнала избранного."""
        result: dict[str, bool] = {}
        for payload in self._read_ndjson_unlocked(self.favorites_path):
            item_id = str(payload.get("id", "")).strip()
            if item_id and "favorite" in payload:
                result[item_id] = bool(payload["favorite"])
        return result

    def set_annotation(self, item_id: str, note: str) -> bool:
        """Сохраняет пользовательскую заметку для записи (last-write-wins по id)."""
        clean_id = item_id.strip()
        if not clean_id:
            return False
        with self._lock():
            active = self._load_active_items_unlocked()
            if not any(item.id == clean_id for item in active):
                return False
            self._append_ndjson(self.annotations_path, {"id": clean_id, "note": str(note)})
        return True

    def get_annotation(self, item_id: str) -> str | None:
        """Возвращает заметку для записи или None, если заметки нет."""
        clean_id = item_id.strip()
        if not clean_id:
            return None
        with self._lock():
            active = self._load_active_items_unlocked()
            if not any(item.id == clean_id for item in active):
                return None
            overrides = self._load_annotation_overrides_unlocked()
        return overrides.get(clean_id)

    def delete_annotation(self, item_id: str) -> bool:
        """Удаляет заметку записи (записывает пустую строку — tombstone)."""
        clean_id = item_id.strip()
        if not clean_id:
            return False
        with self._lock():
            active = self._load_active_items_unlocked()
            if not any(item.id == clean_id for item in active):
                return False
            self._append_ndjson(self.annotations_path, {"id": clean_id, "note": ""})
        return True

    def search_annotations(self, query: str) -> list[dict[str, Any]]:
        """Полнотекстовый поиск по заметкам. Возвращает список {id, note}."""
        needle = query.strip().lower()
        with self._lock():
            overrides = self._load_annotation_overrides_unlocked()
        if not needle:
            return [{"id": k, "note": v} for k, v in overrides.items() if v]
        return [
            {"id": k, "note": v}
            for k, v in overrides.items()
            if v and needle in v.lower()
        ]

    def _load_annotation_overrides_unlocked(self) -> dict[str, str]:
        """Собирает последние заметки по id из журнала аннотаций (last-write-wins)."""
        result: dict[str, str] = {}
        for payload in self._read_ndjson_unlocked(self.annotations_path):
            item_id = str(payload.get("id", "")).strip()
            note = payload.get("note")
            if item_id and note is not None:
                result[item_id] = str(note)
        # Отфильтровываем пустые (удалённые) заметки из результата
        return {k: v for k, v in result.items() if v}

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
                payload = safe_json_loads(raw)
                if payload is None:
                    continue
                if isinstance(payload, dict):
                    yield payload

    def update_history_item_tags(self, item_id: str, tags: list[str]) -> bool:
        """Записывает теги для записи в отдельный журнал (last-write-wins по id)."""
        clean_id = item_id.strip()
        if not clean_id:
            return False
        with self._lock():
            active = self._load_active_items_unlocked()
            if not any(item.id == clean_id for item in active):
                return False
            self._append_ndjson(self.tags_path, {"id": clean_id, "tags": list(tags)})
        return True

    def update_history_item_favorite(self, item_id: str, favorite: bool) -> bool:
        """Записывает флаг избранного для записи в отдельный журнал (last-write-wins по id)."""
        clean_id = item_id.strip()
        if not clean_id:
            return False
        with self._lock():
            active = self._load_active_items_unlocked()
            if not any(item.id == clean_id for item in active):
                return False
            self._append_ndjson(self.favorites_path, {"id": clean_id, "favorite": bool(favorite)})
        return True

    def get_history_item_by_id(self, item_id: str) -> "HistoryItem | None":
        """Возвращает активную запись по ID или None."""
        clean_id = item_id.strip()
        with self._lock():
            active = self._load_active_items_unlocked()
        for item in active:
            if item.id == clean_id:
                return item
        return None

    def get_history_item_action_items(self, item_id):
        clean_id = item_id.strip()
        if not clean_id:
            return None
        with self._lock():
            overrides = self._load_action_items_overrides_unlocked()
        return overrides.get(clean_id)

    def get_all_pending_action_items(self):
        """Возвращает все незакрытые action items из журнала."""
        with self._lock():
            overrides = self._load_action_items_overrides_unlocked()
        return [a for a in overrides.values() if not a.get("done", False)]

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

    # ------------------------------------------------------------------ #
    # Calendar auto-link delta journal                                     #
    # ------------------------------------------------------------------ #

    def update_history_item_calendar(self, item_id: str, event: "dict[str, Any]") -> bool:
        """Сохраняет ссылку на событие Calendar для записи (last-write-wins по id)."""
        clean_id = item_id.strip()
        if not clean_id:
            return False
        if not isinstance(event, dict) or not event.get("title"):
            return False
        with self._lock():
            active = self._load_active_items_unlocked()
            if not any(item.id == clean_id for item in active):
                return False
            self._append_ndjson(
                self.calendar_links_path,
                {"id": clean_id, "calendar_event": event},
            )
        return True

    def get_history_item_calendar(self, item_id: str) -> "dict[str, Any] | None":
        """Возвращает словарь события Calendar для записи или None."""
        clean_id = item_id.strip()
        if not clean_id:
            return None
        with self._lock():
            active = self._load_active_items_unlocked()
            if not any(item.id == clean_id for item in active):
                return None
            overrides = self._load_calendar_overrides_unlocked()
        return overrides.get(clean_id)

    def search_by_calendar_event(self, event_title: str) -> "list[dict[str, Any]]":
        """Ищет записи, связанные с событием Calendar по подстроке в названии."""
        needle = event_title.strip().lower()
        with self._lock():
            overrides = self._load_calendar_overrides_unlocked()
        results = []
        for item_id, cal_event in overrides.items():
            title = str(cal_event.get("title", "")).lower()
            if not needle or needle in title:
                results.append({"item_id": item_id, "calendar_event": cal_event})
        return results

    def _load_calendar_overrides_unlocked(self) -> "dict[str, dict[str, Any]]":
        """Собирает последние ссылки на события Calendar (last-write-wins по id)."""
        result: "dict[str, dict[str, Any]]" = {}
        for payload in self._read_ndjson_unlocked(self.calendar_links_path):
            item_id = str(payload.get("id", "")).strip()
            cal_event = payload.get("calendar_event")
            if item_id and isinstance(cal_event, dict) and cal_event.get("title"):
                result[item_id] = cal_event
        return result
