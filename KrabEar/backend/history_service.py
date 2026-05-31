"""HistoryService — обработчики IPC-методов управления историей Krab Ear.

Выделен из backend/service.py для снижения размера монолитного модуля.
Содержит 13 IPC-обработчиков + форматирующие хелперы.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from backend.observability import add_breadcrumb
from core.fuzzy_search import FuzzySearcher
from core.parsing_utils import safe_json_loads
from core.search_highlighter import SearchHighlighter

from core.duplicate_detector import DuplicateDetector
from backend.summary_profiles import SummaryProfileManager

# Typed imports — only loaded during static analysis, avoid runtime circular imports
if TYPE_CHECKING:
    from backend.state_store import StateStore
    from backend.llm_rewriter import LLMRewriter
    from backend.playback_tracker import PlaybackTracker

logger = logging.getLogger("KrabEar.Backend.HistoryService")

# ---------------------------------------------------------------------------
# Export path allowlist (W1432 / W1532 security guard)
# ---------------------------------------------------------------------------
# Directories where export handlers are permitted to write files.
# All paths are expanded and resolved at check time to defeat symlink tricks.
_EXPORT_ALLOWED_ROOTS: tuple[str, ...] = (
    "~/Library/Application Support/KrabEar",
    "~/.krab_ear_data",
    "~/Documents",
    "~/Desktop",
    "~/Downloads",
    "/tmp",            # W1432: allowed for scripts/tests; restored W1707
    "/private/tmp",    # macOS: /tmp symlinks to /private/tmp
)


def _is_safe_export_dir(output_dir: str) -> bool:
    """Return True only if *output_dir* is inside one of the allowed roots.

    Defends against:
    - Absolute paths outside allowed roots (e.g. /etc, /tmp, /)
    - Parent traversal (../../sensitive)
    - Symlinks that escape allowed roots (resolved via Path.resolve())
    """
    try:
        target = Path(output_dir).expanduser().resolve()
    except (ValueError, OSError):
        return False

    for root_str in _EXPORT_ALLOWED_ROOTS:
        allowed = Path(root_str).expanduser().resolve()
        try:
            target.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


class HistoryService:
    """Обработчики IPC-команд для истории транскрипций."""

    def __init__(
        self,
        store: "StateStore",
        clipboard_history: list[dict] | None = None,
        llm_rewriter: "LLMRewriter | None" = None,
        cached_settings: "Callable[[], dict[str, Any]] | None" = None,
        semantic_searcher: Any | None = None,
        auto_glossary_builder: Any | None = None,
        playback_tracker: "PlaybackTracker | None" = None,
        transcript_versions: Any | None = None,
    ) -> None:
        self.store = store
        # Разделяемый список clipboard_history из BackendService (передаётся по ссылке).
        # Если не передан — создаём изолированный список (для тестов).
        self._clipboard_history: list[dict] = clipboard_history if clipboard_history is not None else []
        # LLMRewriter для авто-резюмирования пакетов транскрипций (опционально).
        self._llm_rewriter = llm_rewriter
        # SpeakerManager для резолва псевдонимов спикеров в экспортах (опционально).
        self._speaker_manager = None
        # Callable для получения текущих настроек (для privacy mode guard и др.).
        self._cached_settings = cached_settings
        # SemanticSearcher для синхронизации удаления эмбеддингов (W1426 F2).
        self._semantic_searcher = semantic_searcher
        # AutoGlossaryBuilder для инвалидации кэша после добавления записи (опционально).
        # Late-injection: передаётся из BackendService после создания AutoGlossaryBuilder.
        self._auto_glossary = auto_glossary_builder
        # F4 W1343: cascade-delete orphan playback stats.
        self._playback_tracker = playback_tracker
        # W1045 F2: TranscriptVersionManager для каскадного удаления версий (опционально).
        self._transcript_versions = transcript_versions
        # Менеджер профилей резюмирования (персистентность в data_dir).
        _data_dir = getattr(store, "data_dir", None)
        self._summary_profiles = SummaryProfileManager(data_dir=_data_dir)
        # Late-injection: RecordingChainManager для каскадной очистки ghost item_ids (W1253 RC-3).
        self._recording_chain_mgr = None
        # Late-injection: W1734 — collaborators for full privacy purge.
        # Wired by BackendService.__init__ after these objects are constructed.
        self._archive_manager: Any = None    # ArchiveManager — archive.ndjson
        self._bookmarks: Any = None          # BookmarkManager — bookmarks.ndjson
        self._call_session_store: Any = None  # CallSessionStore — call_sessions.ndjson

    # ------------------------------------------------------------------
    # Privacy helpers
    # ------------------------------------------------------------------

    def _is_privacy_mode(self) -> bool:
        """Возвращает True если privacy_mode_enabled активен в текущих настройках."""
        try:
            if self._cached_settings is not None:
                settings = self._cached_settings()
            else:
                settings = self.store.load_settings()
            return bool(settings.get("privacy_mode_enabled", False))
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Export path allowlist helper
    # ------------------------------------------------------------------

    def _resolve_export_dir(self, output_dir: str | None) -> Path | None:
        """Validates *output_dir* against the export allowlist.

        Returns the resolved absolute Path when *output_dir* is provided and
        allowed, or ``None`` when *output_dir* is ``None`` / empty (callers
        should fall back to their default directory).

        Raises ``ValueError`` if the resolved path is not permitted.
        Additionally allows self.store.data_dir (instance-level root).
        """
        import tempfile as _tempfile

        if not output_dir:
            return None

        resolved = Path(output_dir).expanduser().resolve()

        data_dir_root = Path(self.store.data_dir).resolve()
        home = Path.home()
        allowed_roots: list[Path] = [
            data_dir_root,
            (home / "Documents").resolve(),
            (home / "Downloads").resolve(),
            (home / "Desktop").resolve(),
            Path("/tmp").resolve(),
            Path(_tempfile.gettempdir()).resolve(),
        ]

        for root in allowed_roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue

        # Also try module-level allowed roots for cross-user paths.
        if _is_safe_export_dir(str(resolved)):
            return resolved

        raise ValueError(
            f"export output_dir is outside allowed directories: {resolved!s}. "
            f"Allowed roots: {[str(r) for r in allowed_roots]}"
        )

    # ------------------------------------------------------------------
    # История
    # ------------------------------------------------------------------

    def handle_add_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("Пустой текст нельзя добавить в историю")
        paste_status = str(params.get("paste_status", "failed"))
        item = self.store.add_history_item(
            text=text,
            paste_status=paste_status,
            source_text=str(params.get("source_text", "")).strip(),
            translated_text=str(params.get("translated_text", "")).strip(),
            translation_mode=str(params.get("translation_mode", "off")).strip() or "off",
            source_lang=str(params.get("source_lang", "")).strip(),
            target_lang=str(params.get("target_lang", "")).strip(),
            translation_status=str(params.get("translation_status", "not_requested")).strip() or "not_requested",
            translation_engine=str(params.get("translation_engine", "")).strip(),
        )
        # W1292: invalidate auto-glossary cache after new recording added (restored W1659)
        if self._auto_glossary is not None:
            try:
                self._auto_glossary.invalidate()
            except Exception as _ag_exc:
                logger.warning("auto_glossary invalidate error after add_history_item: %s", _ag_exc)
        return item.to_dict()

    def handle_get_history_page(self, params: dict[str, Any]) -> dict[str, Any]:
        cursor = params.get("cursor")
        cursor_str = None if cursor is None else str(cursor)
        limit = int(params.get("limit", 50))
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)
        translation_mode = params.get("translation_mode")
        translation_mode_str = None if translation_mode is None else str(translation_mode)
        translation_status = params.get("translation_status")
        translation_status_str = None if translation_status is None else str(translation_status)
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        items, next_cursor = self.store.get_history_page_filtered(
            cursor=cursor_str,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=translation_mode_str,
            translation_status=translation_status_str,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )
        return {"items": items, "next_cursor": next_cursor}

    def handle_search_history(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", "")).strip()
        cursor = params.get("cursor")
        cursor_str = None if cursor is None else str(cursor)
        limit = int(params.get("limit", 50))
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)
        translation_mode = params.get("translation_mode")
        translation_mode_str = None if translation_mode is None else str(translation_mode)
        translation_status = params.get("translation_status")
        translation_status_str = None if translation_status is None else str(translation_status)
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        items, next_cursor = self.store.search_history(
            query=query,
            cursor=cursor_str,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=translation_mode_str,
            translation_status=translation_status_str,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )
        return {"items": items, "next_cursor": next_cursor}

    def handle_fuzzy_search(self, params: dict[str, Any]) -> dict[str, Any]:
        """Нечёткий поиск по истории транскрипций.

        Params:
            query (str): поисковый запрос.
            threshold (float): минимальный порог сходства [0.0, 1.0], по умолчанию 0.6.
            limit (int): максимальное кол-во результатов, по умолчанию 50.

        Returns:
            {"matches": [{"id": ..., "text": ..., "score": ...}, ...]}
        """
        # Privacy mode guard (W1007): не раскрывать текст в режиме приватности.
        if self._is_privacy_mode():
            return {"ok": True, "results": [], "reason": "privacy_mode_active"}

        query = str(params.get("query", "")).strip()
        threshold = float(params.get("threshold", 0.6))
        limit = int(params.get("limit", 50))

        threshold = max(0.0, min(1.0, threshold))

        if not query:
            return {"matches": []}

        # Получаем все записи истории без пагинации (большой лимит)
        all_items, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=10_000,
            paste_status=None,
            translation_mode=None,
            translation_status=None,
            from_ts=None,
            to_ts=None,
        )

        # Собираем тексты для поиска (основной текст + source_text)
        texts: list[str] = []
        for item in all_items:
            text = item.get("text", "") or ""
            source = item.get("source_text", "") or ""
            # Объединяем оба поля для поиска
            combined = f"{text} {source}".strip() if source else text
            texts.append(combined)

        searcher = FuzzySearcher()
        matches = searcher.search(query, texts, threshold=threshold)

        results = []
        for match in matches[:limit]:
            item = all_items[match.index]
            results.append({
                "id": item.get("id", ""),
                "text": item.get("text", ""),
                "source_text": item.get("source_text", ""),
                "score": round(match.score, 4),
                "ts": item.get("ts", ""),
            })

        return {"matches": results}

    def handle_search_with_highlights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Поиск по истории с подсветкой совпадений в результатах.

        Выполняет обычный поиск через store.search_history и дополнительно
        добавляет поля highlighted_text и snippets с подсвеченными совпадениями.

        Params:
            query (str): поисковый запрос.
            marker (str): маркер для подсветки, по умолчанию '**'.
            context_chars (int): кол-во символов контекста в сниппетах, по умолчанию 50.
            max_snippets (int): максимальное кол-во сниппетов на запись, по умолчанию 3.
            limit (int): максимальное кол-во результатов, по умолчанию 50.
            cursor (str | None): курсор пагинации.

        Returns:
            {"items": [...], "next_cursor": ...}
            Каждый элемент содержит дополнительные поля:
              - highlighted_text (str): текст с маркерами вокруг совпадений
              - snippets (list[str]): контекстные сниппеты
        """
        # Privacy mode guard: не раскрывать текст транскрипций в режиме приватности.
        if self._is_privacy_mode():
            return {"ok": True, "results": [], "reason": "privacy_mode_active"}

        query = str(params.get("query", "")).strip()
        marker = str(params.get("marker", "**"))
        context_chars = int(params.get("context_chars", 50))
        max_snippets = int(params.get("max_snippets", 3))
        cursor = params.get("cursor")
        cursor_str = None if cursor is None else str(cursor)
        limit = int(params.get("limit", 50))

        if not query:
            return {"items": [], "next_cursor": None}

        items, next_cursor = self.store.search_history(
            query=query,
            cursor=cursor_str,
            limit=limit,
            paste_status=None,
            translation_mode=None,
            translation_status=None,
            from_ts=None,
            to_ts=None,
        )

        highlighter = SearchHighlighter()
        results = []
        for item in items:
            text = item.get("text", "") or ""
            enriched = dict(item)
            enriched["highlighted_text"] = highlighter.highlight(text, query, marker=marker)
            enriched["snippets"] = highlighter.extract_snippets(
                text, query,
                context_chars=context_chars,
                max_snippets=max_snippets,
            )
            results.append(enriched)

        return {"items": results, "next_cursor": next_cursor}

    def handle_delete_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        import time as _time
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise ValueError("id обязателен для удаления")
        _t0 = _time.monotonic()
        ok = self.store.delete_history_item(item_id)
        if not ok:
            raise ValueError(f"Запись не найдена: {item_id}")
        # Удаляем эмбеддинг из семантического индекса, если он подключён (W1426 F2).
        if self._semantic_searcher is not None:
            try:
                self._semantic_searcher.remove_item(item_id)
            except Exception:
                logger.warning(
                    "semantic_search remove failed for %s", item_id, exc_info=True
                )
        # Каскадное удаление ghost item_id из всех цепочек (W1253 RC-3).
        if self._recording_chain_mgr is not None:
            self._recording_chain_mgr.remove_item_from_all_chains(item_id)
        # F4 W1343: cascade-delete orphan playback stats.
        if self._playback_tracker is not None:
            self._playback_tracker.remove_stats(item_id)
        # W1045 F2: cascade-delete transcript versions to prevent privacy bypass.
        if self._transcript_versions is not None:
            try:
                self._transcript_versions.delete_versions_for(item_id)
            except Exception:
                logger.warning(
                    "transcript_versions cascade delete failed for %s", item_id, exc_info=True
                )
        add_breadcrumb(
            category="history",
            message="delete_history_item",
            data={"ok": True, "duration_ms": round((_time.monotonic() - _t0) * 1000)},
        )
        return {"deleted": True}

    def handle_compact_history(self, params: dict[str, Any]) -> dict[str, Any]:
        stats = self.store.compact_with_stats()
        return {"compacted": True, **stats}

    def handle_import_history_ndjson(self, params: dict[str, Any]) -> dict[str, Any]:
        """Импортирует историю из внешнего NDJSON-файла."""
        import time as _time
        raw_path = str(params.get("path", "")).strip()
        if not raw_path:
            raise RuntimeError("path обязателен")
        resolved = Path(raw_path).expanduser().resolve()
        allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]
        if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
            return {"error": {"message": f"Path outside allowed directories: {resolved}"}}
        _t0 = _time.monotonic()
        result = self.store.import_history_ndjson(resolved)
        imported = int(result.get("imported", 0))
        errors = int(result.get("errors", 0))
        add_breadcrumb(
            category="history",
            message="import_history_ndjson",
            level="warning" if errors else "info",
            data={
                "imported": imported,
                "skipped": int(result.get("skipped", 0)),
                "errors": errors,
                "duration_ms": round((_time.monotonic() - _t0) * 1000),
            },
        )
        return {
            "path": raw_path,
            "imported": imported,
            "skipped": int(result.get("skipped", 0)),
            "errors": errors,
        }

    def handle_get_history_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает состояние журналов истории и оценку размера."""
        return self.store.get_history_stats()

    def handle_get_history_overview(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает обзорный срез истории для панели управления."""
        return self.store.get_history_overview()

    def handle_search_by_speaker(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает записи истории, в которых участвует указанный спикер.

        Params:
            speaker (str): идентификатор спикера, например "SPEAKER_00".
            limit (int, optional): максимальное количество результатов (1–500, default 100).

        Returns:
            {"items": [...], "count": N}
        """
        speaker = str(params.get("speaker", "")).strip()
        if not speaker:
            raise RuntimeError("speaker обязателен")
        safe_limit = max(1, min(int(params.get("limit", 100)), 500))

        with self.store._lock():
            active = self.store._load_active_items_unlocked()

        matched = []
        for item in reversed(active):
            if item.diarization is None:
                continue
            segments = item.diarization.get("speaker_segments", [])
            if any(str(seg.get("speaker", "")) == speaker for seg in segments):
                matched.append(item.to_dict())
                if len(matched) >= safe_limit:
                    break

        return {"items": matched, "count": len(matched)}

    def handle_get_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает полные детали одной записи истории по ID."""
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        for item in items:
            if item.id == item_id:
                result = item.to_dict()
                result["text_length"] = len(item.text)
                result["word_count"] = len(item.text.split()) if item.text else 0
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                matching = list(transcripts_dir.glob(f"*{item_id[:8]}*")) if transcripts_dir.exists() else []
                result["transcript_file"] = str(matching[0]) if matching else None
                return result

        raise RuntimeError(f"Запись {item_id} не найдена")

    # ------------------------------------------------------------------
    # Теги (tagging / labelling)
    # ------------------------------------------------------------------

    def handle_add_tag(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет тег к записи истории.

        Params: id (str), tag (str)
        Returns: {"id": ..., "tags": [...]}
        """
        item_id = str(params.get("id", "")).strip()
        tag = str(params.get("tag", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")
        if not tag:
            raise RuntimeError("tag обязателен")

        item = self.store.get_history_item_by_id(item_id)
        if item is None:
            raise RuntimeError(f"Запись {item_id} не найдена")

        current_tags: list[str] = list(item.tags or [])
        if tag not in current_tags:
            current_tags.append(tag)
            self.store.update_history_item_tags(item_id, current_tags)

        return {"id": item_id, "tags": current_tags}

    def handle_remove_tag(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет тег из записи истории.

        Params: id (str), tag (str)
        Returns: {"id": ..., "tags": [...]}
        """
        item_id = str(params.get("id", "")).strip()
        tag = str(params.get("tag", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")
        if not tag:
            raise RuntimeError("tag обязателен")

        item = self.store.get_history_item_by_id(item_id)
        if item is None:
            raise RuntimeError(f"Запись {item_id} не найдена")

        current_tags: list[str] = [t for t in (item.tags or []) if t != tag]
        self.store.update_history_item_tags(item_id, current_tags)

        return {"id": item_id, "tags": current_tags}

    def handle_get_tags(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает все теги для конкретной записи.

        Params: id (str)
        Returns: {"id": ..., "tags": [...]}
        """
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        item = self.store.get_history_item_by_id(item_id)
        if item is None:
            raise RuntimeError(f"Запись {item_id} не найдена")

        return {"id": item_id, "tags": list(item.tags or [])}

    def handle_search_by_tag(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает записи истории с указанным тегом.

        Params: tag (str), limit (int, optional, default 100)
        Returns: {"items": [...], "count": N}
        """
        tag = str(params.get("tag", "")).strip()
        if not tag:
            raise RuntimeError("tag обязателен")
        safe_limit = max(1, min(int(params.get("limit", 100)), 500))

        with self.store._lock():
            active = self.store._load_active_items_unlocked()

        matched = []
        for item in reversed(active):
            if tag in (item.tags or []):
                matched.append(item.to_dict())
                if len(matched) >= safe_limit:
                    break

        return {"items": matched, "count": len(matched)}

    def handle_list_all_tags(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает все уникальные теги с количеством использований.

        Returns: {"tags": [{"tag": "...", "count": N}, ...]}
        """
        with self.store._lock():
            active = self.store._load_active_items_unlocked()

        counts: dict[str, int] = {}
        for item in active:
            for t in (item.tags or []):
                counts[t] = counts.get(t, 0) + 1

        sorted_tags = sorted(counts.items(), key=lambda pair: pair[1], reverse=True)
        return {"tags": [{"tag": t, "count": c} for t, c in sorted_tags]}

    # ------------------------------------------------------------------
    # Избранное (favorites / bookmarks)
    # ------------------------------------------------------------------

    def handle_toggle_favorite(self, params: dict[str, Any]) -> dict[str, Any]:
        """Переключает флаг избранного для записи истории.

        Params: id (str)
        Returns: {"id": ..., "favorite": bool}
        """
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        item = self.store.get_history_item_by_id(item_id)
        if item is None:
            raise RuntimeError(f"Запись {item_id} не найдена")

        new_value = not bool(item.favorite)
        ok = self.store.update_history_item_favorite(item_id, new_value)
        if not ok:
            raise RuntimeError(f"Не удалось обновить избранное для {item_id}")

        return {"id": item_id, "favorite": new_value}

    def handle_get_favorites(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает все избранные записи, отсортированные по времени (новые первыми).

        Returns: {"items": [...], "count": N}
        """
        with self.store._lock():
            active = self.store._load_active_items_unlocked()

        favorites = [item.to_dict() for item in reversed(active) if item.favorite]
        return {"items": favorites, "count": len(favorites)}

    def handle_is_favorite(self, params: dict[str, Any]) -> dict[str, Any]:
        """Проверяет, находится ли запись в избранном.

        Params: id (str)
        Returns: {"id": ..., "favorite": bool}
        """
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        item = self.store.get_history_item_by_id(item_id)
        if item is None:
            raise RuntimeError(f"Запись {item_id} не найдена")

        return {"id": item_id, "favorite": bool(item.favorite)}

    # ------------------------------------------------------------------
    # Аннотации / пользовательские заметки
    # ------------------------------------------------------------------

    def handle_set_annotation(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет текстовую заметку к записи истории.

        Params: id (str), note (str)
        Returns: {"id": ..., "note": "..."}
        """
        item_id = str(params.get("id", "")).strip()
        note = str(params.get("note", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        # Пустая заметка — допустима (удаляет предыдущую аннотацию).
        ok = self.store.set_annotation(item_id, note)
        if not ok:
            raise RuntimeError(f"Запись {item_id} не найдена")
        return {"id": item_id, "note": note}

    def handle_get_annotation(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает заметку для записи истории.

        Params: id (str)
        Returns: {"id": ..., "note": str | None}
        """
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        # Убедимся, что запись существует.
        item = self.store.get_history_item_by_id(item_id)
        if item is None:
            raise RuntimeError(f"Запись {item_id} не найдена")

        note = self.store.get_annotation(item_id)
        return {"id": item_id, "note": note}

    def handle_search_annotations(self, params: dict[str, Any]) -> dict[str, Any]:
        """Полнотекстовый поиск по пользовательским заметкам.

        Params: query (str)
        Returns: {"results": [{"id": ..., "note": ...}, ...], "count": N}
        """
        query = str(params.get("query", "")).strip()
        results = self.store.search_annotations(query)
        return {"results": results, "count": len(results)}

    # ------------------------------------------------------------------
    # Экспорт истории (markdown / SRT)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration_human(seconds: float | None) -> str:
        """Форматирует длительность аудио в читаемый вид: '5м 23с'."""
        if seconds is None or seconds <= 0:
            return ""
        total = int(seconds)
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}ч {m}м {s}с"
        if m > 0:
            return f"{m}м {s}с"
        return f"{s}с"

    @staticmethod
    def _format_ts_human(iso_ts: str) -> str:
        """Преобразует ISO timestamp в читаемый формат: '2026-04-11 22:46'."""
        try:
            dt = datetime.fromisoformat(iso_ts)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return iso_ts

    def handle_export_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует всю историю в формате Markdown с метаданными и диаризацией.

        Параметры:
            limit (int): максимальное количество записей (по умолчанию 500)
            save_to_file (bool): если True, сохраняет файл в transcripts/

        Возвращает:
            content (str): markdown-текст
            total_items (int): количество экспортированных записей
            path (str|None): путь к файлу, если save_to_file=True
        """
        import time as _time
        _t0 = _time.monotonic()
        limit = max(1, min(int(params.get("limit", 500) or 500), 5000))

        items_dicts, _ = self.store.get_history_page_filtered(
            cursor=None, limit=limit,
            paste_status=None, translation_mode=None,
        )
        if not items_dicts:
            return {"content": "# Krab Ear — Экспорт истории\n\nИстория пуста.\n", "total_items": 0, "path": None}

        from backend.models import HistoryItem as _HI
        items = [_HI.from_dict(d) for d in items_dicts]

        ts_list = [it.ts for it in items if it.ts]
        earliest_ts = self._format_ts_human(ts_list[-1]) if ts_list else "?"
        latest_ts = self._format_ts_human(ts_list[0]) if ts_list else "?"
        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        header_lines = [
            "# Krab Ear — Экспорт истории",
            f"- Записей: {len(items)}",
            f"- Период: {earliest_ts} — {latest_ts}",
            f"- Экспорт: {export_ts}",
            "",
            "---",
            "",
        ]

        sections: list[str] = []
        for idx, item in enumerate(items, start=1):
            ts_human = self._format_ts_human(item.ts)
            duration_str = self._format_duration_human(item.audio_duration_sec)
            title_parts = [f"## {idx}. [{ts_human}]"]
            if duration_str:
                title_parts.append(f"({duration_str})")
            sections.append(" ".join(title_parts))

            meta_parts: list[str] = []
            if item.source_lang:
                meta_parts.append(f"**Язык:** {item.source_lang}")
            diar = item.diarization
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers) >= 2:
                    meta_parts.append(f"**Спикеры:** {len(speakers)}")
            if meta_parts:
                sections.append(" | ".join(meta_parts))
                sections.append("")

            include_labels = self._should_include_speaker_labels(params)
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers) >= 2 and turns:
                    for turn in turns:
                        sid = turn.get("speaker", "SPEAKER_00")
                        turn_text = str(turn.get("text", "")).strip()
                        if turn_text:
                            if include_labels:
                                lbl = self._resolve_speaker_name(sid, lang=item.source_lang)
                                sections.append(f"**{lbl}:** {turn_text}")
                            else:
                                sections.append(f"[{sid}]: {turn_text}")
                else:
                    sections.append(item.text)
            else:
                sections.append(item.text)

            if item.translated_text and item.translation_status == "ok":
                mode_label = item.translation_mode or ""
                sections.append("")
                sections.append(f"**Перевод** ({mode_label}):")
                sections.append(item.translated_text)

            sections.append("")

        content = "\n".join(header_lines) + "\n".join(sections)

        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
                file_path = transcripts_dir / filename
                file_path.write_text(content, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("Не удалось сохранить экспорт в файл: %s", exc)

        add_breadcrumb(
            category="history",
            message="export_history",
            data={
                "total_items": len(items),
                "save_to_file": save_path is not None,
                "duration_ms": round((_time.monotonic() - _t0) * 1000),
            },
        )
        return {"content": content, "total_items": len(items), "path": save_path}

    def handle_export_history_srt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует запись истории в формате SRT-субтитров (по speaker_turns).

        Параметры:
            id (str): идентификатор записи в истории
            save_to_file (bool): если True, сохраняет файл в transcripts/

        Возвращает:
            content (str): SRT-текст
            item_id (str): ID записи
            speakers (int): количество спикеров
            segments (int): количество сегментов
            path (str|None): путь к файлу, если save_to_file=True
        """
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        from backend.models import HistoryItem as _HI
        target_item: _HI | None = None
        cursor: str | None = None
        for _ in range(200):
            page_dicts, next_cursor = self.store.get_history_page_filtered(
                cursor=cursor, limit=100,
                paste_status=None, translation_mode=None,
            )
            if not page_dicts:
                break
            for d in page_dicts:
                if d.get("id") == item_id:
                    target_item = _HI.from_dict(d)
                    break
            if target_item is not None:
                break
            if next_cursor is None:
                break
            cursor = next_cursor

        if target_item is None:
            raise RuntimeError(f"Запись не найдена: {item_id}")

        diar = target_item.diarization
        if not diar or not isinstance(diar, dict) or not diar.get("enabled"):
            duration = target_item.audio_duration_sec or 0.0
            srt_content = self._build_srt_single(target_item.text, duration)
            return self._finalize_srt_export(
                params, srt_content, item_id, speakers=1, segments=1,
            )

        turns = diar.get("speaker_turns", [])
        if not turns:
            duration = target_item.audio_duration_sec or 0.0
            srt_content = self._build_srt_single(target_item.text, duration)
            return self._finalize_srt_export(
                params, srt_content, item_id, speakers=1, segments=1,
            )

        speakers = {t.get("speaker") for t in turns if t.get("speaker")}
        srt_lines: list[str] = []
        seq = 0
        for turn in turns:
            speaker = turn.get("speaker", "SPEAKER_00")
            turn_text = str(turn.get("text", "")).strip()
            if not turn_text:
                continue
            seq += 1
            start_sec = float(turn.get("start", 0.0) or 0.0)
            end_sec = float(turn.get("end", start_sec + 1.0) or start_sec + 1.0)
            srt_lines.append(str(seq))
            srt_lines.append(f"{self._srt_timestamp(start_sec)} --> {self._srt_timestamp(end_sec)}")
            if self._should_include_speaker_labels(params):
                lbl = self._resolve_speaker_name(speaker, lang=getattr(target_item, "source_lang", None))
                srt_lines.append(f"{lbl}: {turn_text}")
            else:
                srt_lines.append(f"[{speaker}]: {turn_text}")
            srt_lines.append("")

        srt_content = "\n".join(srt_lines)
        return self._finalize_srt_export(
            params, srt_content, item_id,
            speakers=len(speakers), segments=len(turns),
        )

    def handle_export_history_markdown(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует историю транскрипций в формате Markdown.

        Параметры:
            limit (int): максимальное количество записей (по умолчанию 500)
            from_ts (str|None): начало диапазона (ISO timestamp или YYYY-MM-DD)
            to_ts (str|None): конец диапазона (ISO timestamp или YYYY-MM-DD)
            paste_status (str|None): фильтр по статусу вставки
            copy_to_clipboard (bool): если True, копирует результат в буфер обмена

        Возвращает:
            ok (bool): True при успехе
            entries (int): количество экспортированных записей
            chars (int): размер результата в символах
        """
        limit = max(1, min(int(params.get("limit", 500) or 500), 5000))
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)

        items_dicts, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=None,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )

        if not items_dicts:
            md = "# Krab Ear — Экспорт транскрипций\n\nИстория пуста.\n"
            return {"ok": True, "entries": 0, "chars": len(md)}

        from backend.models import HistoryItem as _HI
        items = [_HI.from_dict(d) for d in items_dicts]

        # Определяем диапазон дат для заголовка
        ts_list = [it.ts for it in items if it.ts]
        earliest_ts = self._format_ts_human(ts_list[-1]) if ts_list else "?"
        latest_ts = self._format_ts_human(ts_list[0]) if ts_list else "?"
        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Собираем статистику для сводки
        languages_used: set[str] = set()

        # Заголовок документа
        lines: list[str] = [
            "# Krab Ear — Экспорт транскрипций",
            "",
            f"**Период:** {earliest_ts} — {latest_ts}  ",
            f"**Записей:** {len(items)}  ",
            f"**Экспорт:** {export_ts}",
            "",
            "---",
            "",
        ]

        for idx, item in enumerate(items, start=1):
            ts_human = self._format_ts_human(item.ts)
            duration_str = self._format_duration_human(item.audio_duration_sec)

            # Заголовок раздела
            section_title = f"## {idx}. {ts_human}"
            if duration_str:
                section_title += f" ({duration_str})"
            lines.append(section_title)
            lines.append("")

            # Метаданные записи
            meta: list[str] = []
            if item.source_lang:
                meta.append(f"**Язык:** {item.source_lang}")
                languages_used.add(item.source_lang)
            if item.target_lang and item.translation_status == "ok":
                languages_used.add(item.target_lang)

            # Диаризация: подсчёт спикеров
            diar = item.diarization
            diar_turns: list[dict] = []
            has_diarization = False
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                diar_turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in diar_turns if t.get("speaker")}
                if len(speakers) >= 2:
                    has_diarization = True
                    meta.append(f"**Спикеры:** {len(speakers)}")

            if meta:
                lines.append(" | ".join(meta))
                lines.append("")

            # Основной текст или реплики по спикерам
            if has_diarization and diar_turns:
                for turn in diar_turns:
                    speaker = turn.get("speaker", "?")
                    turn_text = str(turn.get("text", "")).strip()
                    if not turn_text:
                        continue
                    start_sec = turn.get("start")
                    if start_sec is not None:
                        ts_mark = f"`{self._srt_timestamp(float(start_sec))[:8]}`"
                        lines.append(f"**[{speaker}]** {ts_mark}: {turn_text}")
                    else:
                        lines.append(f"**[{speaker}]**: {turn_text}")
            else:
                lines.append(item.text)

            # Перевод (если есть)
            if item.translated_text and item.translation_status == "ok":
                mode_label = item.translation_mode or "перевод"
                lines.append("")
                lines.append(f"> **Перевод** ({mode_label}): {item.translated_text}")

            lines.append("")

        # Итоговая статистика
        lines.append("---")
        lines.append("")
        lines.append("## Сводная статистика")
        lines.append("")
        lines.append(f"- **Всего записей:** {len(items)}")
        if languages_used:
            lines.append(f"- **Языки:** {', '.join(sorted(languages_used))}")
        lines.append(f"- **Экспортировано:** {export_ts}")
        lines.append("")

        md_content = "\n".join(lines)

        # Копирование в буфер обмена (по запросу)
        if self._coerce_bool(params.get("copy_to_clipboard", False), default=False):
            try:
                import subprocess
                proc = subprocess.run(
                    ["pbcopy"],
                    input=md_content.encode("utf-8"),
                    check=False,
                )
                if proc.returncode != 0:
                    logger.warning("pbcopy завершился с кодом %d", proc.returncode)
            except Exception as exc:
                logger.warning("Не удалось скопировать Markdown в буфер обмена: %s", exc)

        return {"ok": True, "entries": len(items), "chars": len(md_content)}

    def _finalize_srt_export(
        self,
        params: dict[str, Any],
        srt_content: str,
        item_id: str,
        speakers: int,
        segments: int,
    ) -> dict[str, Any]:
        """Общая финализация SRT-экспорта: опциональное сохранение в файл."""
        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"srt_{item_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.srt"
                file_path = transcripts_dir / filename
                file_path.write_text(srt_content, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("Не удалось сохранить SRT в файл: %s", exc)
        return {
            "content": srt_content,
            "item_id": item_id,
            "speakers": speakers,
            "segments": segments,
            "path": save_path,
        }

    @staticmethod
    def _build_srt_single(text: str, duration: float) -> str:
        """Строит SRT с одним сегментом (без диаризации)."""
        end_ts = HistoryService._srt_timestamp(duration) if duration > 0 else "00:00:01,000"
        return f"1\n00:00:00,000 --> {end_ts}\n{text}\n"

    @staticmethod
    def _srt_timestamp(seconds: float) -> str:
        """Конвертирует секунды в SRT-формат: HH:MM:SS,mmm."""
        if seconds < 0:
            seconds = 0.0
        total_ms = int(round(seconds * 1000))
        h, remainder = divmod(total_ms, 3600000)
        m, remainder = divmod(remainder, 60000)
        s, ms = divmod(remainder, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    # ------------------------------------------------------------------
    # JSON export (structured, machine-readable)
    # ------------------------------------------------------------------

    def handle_export_history_json(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует историю транскрипций в структурированный JSON.

        Параметры:
            limit (int): максимальное количество записей (по умолчанию 500, макс 5000)
            from_ts (str|None): начало диапазона (ISO timestamp или YYYY-MM-DD)
            to_ts (str|None): конец диапазона (ISO timestamp или YYYY-MM-DD)
            paste_status (str|None): фильтр по статусу вставки
            pretty (bool): форматировать JSON с отступами (по умолчанию True)
            save_to_file (bool): если True, сохранить файл в transcripts/
            copy_to_clipboard (bool): если True, скопировать JSON в буфер обмена

        Возвращает:
            ok (bool): True при успехе
            entries (int): количество экспортированных записей
            chars (int): размер JSON в символах
            path (str|None): путь к файлу, если save_to_file=True
        """
        import json as _json
        import subprocess

        limit = max(1, min(int(params.get("limit", 500) or 500), 5000))
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)
        pretty = self._coerce_bool(params.get("pretty", True), default=True)

        items_dicts, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=None,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )

        from backend.models import HistoryItem as _HI

        # Загружаем все аннотации одним вызовом для эффективности (без N+1)
        with self.store._lock():
            annotation_map: dict[str, str] = self.store._load_annotation_overrides_unlocked()

        entries: list[dict] = []
        for d in items_dicts:
            item = _HI.from_dict(d)

            # Блок перевода
            translation_block: dict | None = None
            if item.translated_text or item.translation_status not in (None, "", "not_requested"):
                translation_block = {
                    "text": item.translated_text or None,
                    "engine": item.translation_engine or None,
                    "status": item.translation_status or None,
                    "mode": item.translation_mode or None,
                    "source_lang": item.source_lang or None,
                    "target_lang": item.target_lang or None,
                }

            # Блок диаризации
            diarization_block: dict | None = None
            diar = item.diarization
            if diar and isinstance(diar, dict):
                turns = diar.get("speaker_turns", [])
                segments = diar.get("speaker_segments", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                include_labels = self._should_include_speaker_labels(params)
                if include_labels and (turns or segments):
                    seg_list = turns or segments
                    resolved_segs = []
                    for seg in seg_list:
                        seg_copy = dict(seg)
                        sid = seg_copy.get("speaker", "")
                        if sid:
                            seg_copy["speaker_name"] = self._resolve_speaker_name(
                                sid, lang=None
                            )
                        resolved_segs.append(seg_copy)
                else:
                    resolved_segs = turns or segments
                diarization_block = {
                    "enabled": bool(diar.get("enabled", False)),
                    "speakers": len(speakers),
                    "segments": resolved_segs,
                }

            entry: dict = {
                "id": item.id,
                "timestamp": item.ts,
                "text": item.text,
                "language": item.source_lang or None,
                "confidence": item.confidence if item.confidence is not None else None,
                "duration_sec": item.audio_duration_sec if item.audio_duration_sec is not None else None,
                "paste_status": item.paste_status or None,
                "translation": translation_block,
                "diarization": diarization_block,
                "tags": list(item.tags) if item.tags else [],
                "favorite": bool(item.favorite) if item.favorite is not None else False,
                "annotation": annotation_map.get(item.id) or None,
            }
            entries.append(entry)

        export_ts = datetime.now(timezone.utc).isoformat()
        payload = {
            "export_info": {
                "version": "2.0",
                "exported_at": export_ts,
                "total_entries": len(entries),
                "filters": {
                    "from_ts": from_ts_str,
                    "to_ts": to_ts_str,
                    "paste_status": paste_status_str,
                    "limit": limit,
                },
            },
            "entries": entries,
        }

        indent = 2 if pretty else None
        json_text = _json.dumps(payload, ensure_ascii=False, indent=indent)

        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                file_path = transcripts_dir / filename
                file_path.write_text(json_text, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("Не удалось сохранить JSON-экспорт в файл: %s", exc)

        if self._coerce_bool(params.get("copy_to_clipboard", False), default=False):
            try:
                subprocess.run(
                    ["pbcopy"],
                    input=json_text.encode("utf-8"),
                    check=False,
                    timeout=5,
                )
            except Exception as exc:
                logger.warning("Не удалось скопировать JSON в буфер обмена: %s", exc)

        return {
            "ok": True,
            "entries": len(entries),
            "chars": len(json_text),
            "path": save_path,
        }

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def handle_export_history_csv(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспорт истории в CSV формат."""
        import csv
        import io
        import subprocess

        delimiter = params.get("delimiter", ",")
        if len(delimiter) != 1:
            delimiter = ","
        include_header = params.get("include_header", True)
        limit = params.get("limit")
        from_ts = params.get("from_ts")
        to_ts = params.get("to_ts")
        copy_to_clipboard = params.get("copy_to_clipboard", True)
        save_path = params.get("save_to_file")

        items = [i.to_dict() if hasattr(i, 'to_dict') else i
                 for i in self.store._load_active_items_with_lock()]
        if from_ts:
            items = [i for i in items if i.get("ts", "") >= from_ts]
        if to_ts:
            items = [i for i in items if i.get("ts", "") <= to_ts]
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        if limit:
            items = items[:int(limit)]

        output = io.StringIO()
        writer = csv.writer(output, delimiter=delimiter)
        include_labels = self._should_include_speaker_labels(params)
        columns = ["timestamp", "text", "translation", "language", "confidence",
                   "duration_sec", "paste_status", "speakers"]
        if include_header:
            writer.writerow(columns)

        for item in items:
            translation = ""
            if item.get("translation_status") == "ok":
                translation = item.get("translated_text") or item.get("translation", "")
            speaker_val = ""
            diar = item.get("diarization")
            if diar and isinstance(diar, dict):
                turns = diar.get("speaker_turns", diar.get("speaker_segments", []))
                speaker_ids = sorted({
                    s.get("speaker", "") for s in turns
                    if isinstance(s, dict) and s.get("speaker")
                })
                if include_labels:
                    item_lang = item.get("source_lang") or item.get("lang") or None
                    speaker_val = ", ".join(
                        self._resolve_speaker_name(sid, lang=item_lang)
                        for sid in speaker_ids
                    )
                else:
                    speaker_val = ", ".join(speaker_ids)
            writer.writerow([
                item.get("ts", ""),
                item.get("text", ""),
                translation,
                item.get("lang", ""),
                item.get("confidence", ""),
                item.get("duration", ""),
                item.get("paste_status", ""),
                speaker_val,
            ])

        csv_text = output.getvalue()
        file_path = None

        if save_path or save_path is True:
            from datetime import datetime
            transcripts_dir = self.store.data_dir / "transcripts"
            transcripts_dir.mkdir(parents=True, exist_ok=True)
            fname = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            file_path = transcripts_dir / fname
            file_path.write_text(csv_text, encoding="utf-8")
            file_path = str(file_path)

        if copy_to_clipboard:
            try:
                subprocess.run(["pbcopy"], input=csv_text.encode(), check=True, timeout=5)
            except Exception:
                pass

        return {"ok": True, "entries": len(items), "file": file_path}

    # ------------------------------------------------------------------
    # Clipboard history
    # ------------------------------------------------------------------

    def handle_get_clipboard_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последние N вставленных транскрипций из clipboard_history.

        Параметры:
            limit (int): максимальное количество элементов (по умолчанию 10, макс 20)

        Возвращает:
            items (list): список записей {text, ts, history_id}
            count (int): общее количество элементов в истории
        """
        limit = self._coerce_bounded(
            value=params.get("limit", 10),
            default=10,
            min_value=1,
            max_value=20,
        )
        return {
            "items": self._clipboard_history[-limit:],
            "count": len(self._clipboard_history),
        }

    def handle_repaste_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Находит текст по history_id в clipboard_history и возвращает его для повторной вставки.

        Параметры:
            history_id (str): идентификатор записи из clipboard_history

        Возвращает:
            text (str): текст для вставки
            history_id (str): подтверждённый идентификатор
            found (bool): True если запись найдена
        """
        history_id = str(params.get("history_id", "")).strip()
        if not history_id:
            raise RuntimeError("history_id обязателен")
        for entry in reversed(self._clipboard_history):
            if entry.get("history_id") == history_id:
                return {
                    "text": entry["text"],
                    "history_id": history_id,
                    "found": True,
                }
        raise RuntimeError(f"Запись не найдена в clipboard_history: {history_id}")

    # ------------------------------------------------------------------
    # Очистка и хранилище
    # ------------------------------------------------------------------

    def handle_cleanup_old_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет записи истории старше N дней (по умолчанию 90).

        Параметры:
            older_than_days (int): порог возраста в днях (по умолчанию 90)

        Возвращает:
            deleted (int): количество удалённых записей
            remaining (int): количество оставшихся активных записей
        """
        older_than_days = int(params.get("older_than_days", 90))
        if older_than_days <= 0:
            raise RuntimeError("older_than_days должен быть положительным числом")

        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        cutoff_iso = cutoff.isoformat()

        with self.store._lock():
            active = self.store._load_active_items_unlocked()
            to_delete = [item for item in active if item.ts < cutoff_iso]
            for item in to_delete:
                self.store._append_ndjson(self.store.tombstones_path, {"id": item.id})
            remaining = len(active) - len(to_delete)

        # W1045 F2: cascade-delete transcript versions for bulk-deleted items.
        if to_delete and self._transcript_versions is not None:
            deleted_ids = [item.id for item in to_delete]
            try:
                self._transcript_versions.cleanup_for_ids(deleted_ids)
            except Exception:
                logger.warning(
                    "transcript_versions bulk cleanup failed for %d items", len(deleted_ids), exc_info=True
                )

        add_breadcrumb(
            category="history",
            message="cleanup_old_history",
            level="info",
            data={"deleted_count": len(to_delete), "remaining": remaining, "older_than_days": older_than_days},
        )
        return {"deleted_count": len(to_delete), "remaining": remaining}

    def handle_purge_all_data(self, params: dict[str, Any]) -> dict[str, Any]:
        """Полная очистка всех пользовательских данных (privacy-purge / wipe-all).

        Удаляет ВСЕ записи истории (без временного порога), а также:
          - архив записей (archive/archive.ndjson) — W1734 FIX-A
          - закладки (bookmarks.ndjson) — W1734 FIX-B
          - сессии звонков (call_sessions.ndjson) — W1734 FIX-B
          - все цепочки записей (recording_chains.json) — W1730
          - семантический индекс (embeddings), если подключён
          - версии транскрипций, если подключён менеджер версий

        **Требует подтверждения (W1734 FIX-D)**: параметр ``confirm`` должен быть
        равен ``True`` (bool) или строке ``"PURGE_ALL"``. Без него возвращает ошибку
        ``confirmation_required`` и НЕ удаляет никаких данных.

        Каждый дополнительный шаг защищён try/except — ошибка в одном шаге
        не прерывает остальные (privacy-purge должен быть атомарным для истории).

        Возвращает:
            history_deleted (int): количество удалённых записей истории
            chains_deleted (int): количество удалённых цепочек (0 если N/A)
            archive_deleted (int): количество удалённых архивных записей (0 если N/A)
            bookmarks_deleted (int): количество удалённых закладок (0 если N/A)
            call_sessions_deleted (int): количество удалённых сессий звонков (0 если N/A)
            semantic_purged (bool): True если семантический индекс очищен
            complete (bool): True если все вторичные шаги завершились без ошибок
            errors (list[str]): имена шагов, завершившихся с ошибкой (без PII)
        """
        # --- D. Confirm guard — prevent accidental wipe ---
        confirm = params.get("confirm")
        if confirm is not True and confirm != "PURGE_ALL":
            return {
                "ok": False,
                "error": "confirmation_required",
                "message": (
                    "purge_all_data requires explicit confirmation. "
                    "Pass confirm=True or confirm='PURGE_ALL' to proceed."
                ),
            }

        # --- 1. Удалить все записи истории (primary — must succeed) ---
        with self.store._lock():
            active = self.store._load_active_items_unlocked()
            for item in active:
                self.store._append_ndjson(self.store.tombstones_path, {"id": item.id})
            history_deleted = len(active)

        secondary_errors: list[str] = []

        # --- 2. Каскадная очистка версий транскрипций ---
        if active and self._transcript_versions is not None:
            deleted_ids = [item.id for item in active]
            try:
                self._transcript_versions.cleanup_for_ids(deleted_ids)
            except Exception:
                logger.warning(
                    "purge_all_data: transcript_versions cleanup failed", exc_info=True
                )
                secondary_errors.append("transcript_versions")

        # --- 3. W1730: очистить все цепочки записей ---
        chains_deleted = 0
        if self._recording_chain_mgr is not None:
            try:
                chains_deleted = self._recording_chain_mgr.delete_all_chains()
            except Exception:
                logger.warning(
                    "purge_all_data: delete_all_chains failed", exc_info=True
                )
                secondary_errors.append("chains")

        # --- 4. Очистить семантический индекс ---
        semantic_purged = False
        if self._semantic_searcher is not None:
            try:
                self._semantic_searcher.purge_all()
                semantic_purged = True
            except Exception:
                logger.warning(
                    "purge_all_data: semantic_searcher.purge_all failed", exc_info=True
                )
                secondary_errors.append("semantic_search")

        # --- 5. W1734 FIX-A: очистить архив (archive.ndjson) ---
        archive_deleted = 0
        if self._archive_manager is not None:
            try:
                archive_deleted = self._archive_manager.clear_all()
            except Exception:
                logger.warning(
                    "purge_all_data: archive clear_all failed", exc_info=True
                )
                secondary_errors.append("archive")

        # --- 6. W1734 FIX-B: очистить закладки (bookmarks.ndjson) ---
        bookmarks_deleted = 0
        if self._bookmarks is not None:
            try:
                bookmarks_deleted = self._bookmarks.delete_all()
            except Exception:
                logger.warning(
                    "purge_all_data: bookmarks delete_all failed", exc_info=True
                )
                secondary_errors.append("bookmarks")

        # --- 7. W1734 FIX-B: очистить сессии звонков (call_sessions.ndjson) ---
        call_sessions_deleted = 0
        if self._call_session_store is not None:
            try:
                call_sessions_deleted = self._call_session_store.delete_all()
            except Exception:
                logger.warning(
                    "purge_all_data: call_session_store delete_all failed", exc_info=True
                )
                secondary_errors.append("call_sessions")

        # --- C. W1734: Audit log entry ---
        try:
            from backend.privacy_audit import get_privacy_audit_logger
            audit = get_privacy_audit_logger()
            audit.log_event(
                category="privacy",
                action="purge_all_data",
                details={
                    "history_deleted": history_deleted,
                    "chains_deleted": chains_deleted,
                    "archive_deleted": archive_deleted,
                    "bookmarks_deleted": bookmarks_deleted,
                    "call_sessions_deleted": call_sessions_deleted,
                    "secondary_errors": secondary_errors,
                },
            )
        except Exception:
            logger.warning("purge_all_data: privacy audit log failed", exc_info=True)

        add_breadcrumb(
            category="history",
            message="purge_all_data",
            level="info",
            data={
                "history_deleted": history_deleted,
                "chains_deleted": chains_deleted,
                "archive_deleted": archive_deleted,
                "bookmarks_deleted": bookmarks_deleted,
                "call_sessions_deleted": call_sessions_deleted,
                "semantic_purged": semantic_purged,
            },
        )
        logger.info(
            "purge_all_data: history=%d chains=%d archive=%d bookmarks=%d calls=%d "
            "semantic_purged=%s errors=%s",
            history_deleted,
            chains_deleted,
            archive_deleted,
            bookmarks_deleted,
            call_sessions_deleted,
            semantic_purged,
            secondary_errors,
        )
        return {
            "ok": True,
            "history_deleted": history_deleted,
            "chains_deleted": chains_deleted,
            "archive_deleted": archive_deleted,
            "bookmarks_deleted": bookmarks_deleted,
            "call_sessions_deleted": call_sessions_deleted,
            "semantic_purged": semantic_purged,
            "complete": len(secondary_errors) == 0,
            "errors": secondary_errors,
        }

    def handle_get_storage_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает информацию о размере файлов данных Krab Ear.

        Возвращает:
            history_bytes (int): размер history.ndjson в байтах
            history_file_size_mb (float): размер history.ndjson в МБ
            transcripts_count (int): количество .md файлов в transcripts/
            transcripts_size_mb (float): суммарный размер transcripts/ в МБ
            reports_count (int): количество файлов-отчётов в data_dir
            total_bytes (int): суммарный размер директории данных в байтах
            total_data_mb (float): суммарный размер директории данных в МБ
        """
        data_dir = Path(self.store.data_dir)

        history_path = self.store.history_path
        history_bytes = history_path.stat().st_size if history_path.exists() else 0
        history_size_mb = history_bytes / (1024 * 1024)

        transcripts_dir = data_dir / "transcripts"
        md_files = list(transcripts_dir.glob("*.md")) if transcripts_dir.exists() else []
        transcripts_count = len(md_files)
        transcripts_size_mb = sum(f.stat().st_size for f in md_files) / (1024 * 1024)

        reports_count = len(list(data_dir.glob("*.report")) + list(data_dir.glob("report_*")))

        total_bytes = sum(
            f.stat().st_size
            for f in data_dir.rglob("*")
            if f.is_file()
        )
        total_data_mb = total_bytes / (1024 * 1024)

        return {
            "history_bytes": history_bytes,
            "history_file_size_mb": round(history_size_mb, 3),
            "transcripts_count": transcripts_count,
            "transcripts_size_mb": round(transcripts_size_mb, 3),
            "reports_count": reports_count,
            "total_bytes": total_bytes,
            "total_data_mb": round(total_data_mb, 3),
        }

    def handle_get_transcripts_path(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает путь к папке транскриптов и создаёт её при необходимости.

        Возвращает:
            path (str): абсолютный путь к директории transcripts/
            exists (bool): True если директория уже существовала
        """
        transcripts_dir = Path(self.store.data_dir) / "transcripts"
        existed = transcripts_dir.exists()
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        return {"path": str(transcripts_dir), "exists": existed}

    # ------------------------------------------------------------------
    # Авто-резюмирование пакета транскрипций через LLM
    # ------------------------------------------------------------------

    def handle_auto_summarize_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует сводное LLM-резюме для нескольких транскрипций.

        Параметры (взаимно-исключающие — приоритет: ids > date range):
            ids (list[str]): список ID записей истории для резюмирования
            from_ts (str|None): ISO-timestamp начала диапазона (включительно)
            to_ts (str|None): ISO-timestamp конца диапазона (включительно)
            limit (int): макс. кол-во записей при выборке по диапазону (по умолчанию 50)

        Возвращает:
            summary (str): связный текст резюме
            key_points (list[str]): список ключевых тезисов
            items_processed (int): кол-во обработанных записей
            total_words (int): суммарное кол-во слов в исходных текстах
            llm (bool): True если резюме сгенерировано через LLM
            fallback (bool): True если LLM был недоступен (circuit open / отключён)
            error (str|None): описание ошибки при fallback=True
        """
        ids: list[str] | None = params.get("ids")
        from_ts: str | None = params.get("from_ts")
        to_ts: str | None = params.get("to_ts")
        limit = max(1, min(int(params.get("limit", 50) or 50), 200))
        profile_name: str | None = params.get("profile")
        # Загружаем профиль — если не указан или не найден, используем "brief"
        _active_profile = None
        if profile_name:
            try:
                _active_profile = self._summary_profiles.get_profile(profile_name)
            except KeyError:
                logger.warning("auto_summarize_batch: профиль %r не найден, используем 'brief'", profile_name)
        if _active_profile is None:
            _active_profile = self._summary_profiles.get_profile("brief")

        # --- Загружаем записи ---
        if ids is not None:
            # По списку ID
            if not isinstance(ids, list) or len(ids) == 0:
                raise RuntimeError("ids должен быть непустым списком строк")
            id_set = {str(i).strip() for i in ids if str(i).strip()}
            with self.store._lock():
                all_items = self.store._load_active_items_unlocked()
            items = [it for it in all_items if it.id in id_set]
            if not items:
                raise RuntimeError("Ни одна из указанных записей не найдена")
        else:
            # По временному диапазону
            page_dicts, _ = self.store.get_history_page_filtered(
                cursor=None,
                limit=limit,
                paste_status=None,
                translation_mode=None,
                from_ts=str(from_ts) if from_ts else None,
                to_ts=str(to_ts) if to_ts else None,
            )
            if not page_dicts:
                raise RuntimeError("Записи в указанном диапазоне не найдены")
            from backend.models import HistoryItem as _HI
            items = [_HI.from_dict(d) for d in page_dicts]

        # --- Собираем единый текст ---
        texts: list[str] = []
        for it in items:
            t = (it.text or "").strip()
            if t:
                texts.append(t)

        if not texts:
            raise RuntimeError("Все выбранные записи имеют пустой текст")

        total_words = sum(len(t.split()) for t in texts)

        # --- LLM-резюме ---
        if self._llm_rewriter is None:
            # LLM не сконфигурирован — базовая статистика
            fallback_summary = self._build_fallback_summary(texts)
            return {
                "summary": fallback_summary["summary"],
                "key_points": fallback_summary["key_points"],
                "items_processed": len(texts),
                "total_words": total_words,
                "llm": False,
                "fallback": True,
                "error": "LLM unavailable",
            }

        # Проверяем circuit breaker через публичный метод rewriter'а
        # (allow_request — внутренний, проверяем косвенно через status)
        circuit_state = self._llm_rewriter._circuit.state if hasattr(self._llm_rewriter, "_circuit") else None
        if circuit_state == "open":
            logger.warning("auto_summarize_batch: circuit breaker открыт, LLM недоступен")
            fallback_summary = self._build_fallback_summary(texts)
            return {
                "summary": fallback_summary["summary"],
                "key_points": fallback_summary["key_points"],
                "items_processed": len(texts),
                "total_words": total_words,
                "llm": False,
                "fallback": True,
                "error": "LLM unavailable",
            }

        # Формируем промпт для пакетного резюме с учётом профиля
        prompt = self._build_batch_summary_prompt(texts, profile=_active_profile)
        logger.info(
            "auto_summarize_batch: профиль=%r, отправляем %d записей в LLM (итого %d слов)",
            _active_profile.name, len(texts), total_words,
        )

        # Используем метод summarize — он обёрнут в circuit breaker и never-raises контракт
        _max_sentences = max(1, _active_profile.max_tokens // 50)
        result = self._llm_rewriter.summarize(prompt, max_sentences=_max_sentences)

        if not result.ok or not result.text:
            logger.warning(
                "auto_summarize_batch: LLM вернул ошибку — %s, fallback на эвристику",
                result.fallback_reason,
            )
            fallback_summary = self._build_fallback_summary(texts)
            return {
                "summary": fallback_summary["summary"],
                "key_points": fallback_summary["key_points"],
                "items_processed": len(texts),
                "total_words": total_words,
                "llm": False,
                "fallback": True,
                "error": result.fallback_reason or "LLM unavailable",
            }

        # Парсим структурированный ответ LLM
        parsed = self._parse_llm_batch_response(result.text)
        return {
            "summary": parsed["summary"],
            "key_points": parsed["key_points"],
            "items_processed": len(texts),
            "total_words": total_words,
            "llm": True,
            "fallback": False,
            "error": None,
            "profile": _active_profile.name,
        }

    @staticmethod
    def _build_batch_summary_prompt(texts: list[str], profile: Any = None) -> str:
        """Формирует промпт для пакетного LLM-резюмирования транскрипций.

        Если передан профиль — использует его system_prompt и format_instructions.
        Без профиля — стандартный промпт (режим совместимости с тестами).
        """
        joined = "\n\n---\n\n".join(texts)
        if profile is not None:
            header = profile.system_prompt
            fmt = profile.format_instructions
            instructions = f"\n\nФормат ответа: {fmt}" if fmt else ""
            return (
                f"{header}{instructions}\n\n"
                "Текст для резюмирования:\n"
                f"{joined}"
            )
        # Стандартный (обратно-совместимый) промпт
        return (
            "Ниже представлены несколько транскрипций переговоров/диктовок.\n"
            "Сделай краткое сводное резюме на русском языке.\n"
            "Структура ответа:\n"
            "РЕЗЮМЕ: <одно-два предложения — главная суть>\n"
            "ТЕЗИСЫ:\n"
            "- <тезис 1>\n"
            "- <тезис 2>\n"
            "- <тезис 3>\n\n"
            f"{joined}"
        )

    @staticmethod
    def _parse_llm_batch_response(text: str) -> dict[str, Any]:
        """Парсит структурированный ответ LLM в summary + key_points.

        Ожидаемый формат:
            РЕЗЮМЕ: <текст>
            ТЕЗИСЫ:
            - <тезис>
            - <тезис>

        Если формат не распознан — весь текст идёт в summary, key_points=[].
        """
        summary = ""
        key_points: list[str] = []

        lines = text.strip().splitlines()
        mode = "scan"
        summary_parts: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            lower = stripped.lower()
            if lower.startswith("резюме:"):
                mode = "summary"
                tail = stripped[len("резюме:"):].strip()
                if tail:
                    summary_parts.append(tail)
                continue
            if lower.startswith("тезисы:") or lower.startswith("ключевые тезисы:"):
                mode = "bullets"
                continue

            if mode == "summary":
                if stripped.startswith("-"):
                    mode = "bullets"
                    point = stripped.lstrip("- ").strip()
                    if point:
                        key_points.append(point)
                else:
                    summary_parts.append(stripped)
            elif mode == "bullets":
                if stripped.startswith("-"):
                    point = stripped.lstrip("- ").strip()
                    if point:
                        key_points.append(point)
            else:
                # Нераспознанный формат — весь текст в резюме
                summary_parts.append(stripped)

        summary = " ".join(summary_parts).strip()
        if not summary:
            # Fallback: первое предложение из исходного текста
            summary = text.strip().split("\n")[0][:300]

        return {"summary": summary, "key_points": key_points}

    @staticmethod
    def _build_fallback_summary(texts: list[str]) -> dict[str, Any]:
        """Минимальное эвристическое резюме без LLM (первые предложения каждой записи)."""
        import re as _re
        key_points: list[str] = []
        for t in texts[:10]:  # ограничиваем кол-во тезисов
            sentences = _re.split(r"(?<=[.!?])\s+", t.strip())
            first = sentences[0].strip() if sentences else t[:150].strip()
            if first:
                key_points.append(first[:200])

        summary = key_points[0] if key_points else ""
        return {"summary": summary, "key_points": key_points}

    # ------------------------------------------------------------------
    # Фильтрация по уровню уверенности STT
    # ------------------------------------------------------------------

    def handle_filter_by_confidence(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает записи истории, отфильтрованные по STT confidence score.

        Параметры:
            min_confidence (float): нижняя граница confidence (0.0–1.0), обязательна
            max_confidence (float|None): верхняя граница confidence (0.0–1.0), опционально

        Возвращает:
            items (list): записи истории, у которых confidence в заданном диапазоне
            count (int): количество найденных записей
            avg_confidence (float): среднее значение confidence среди найденных записей
        """
        raw_min = params.get("min_confidence")
        if raw_min is None:
            raise RuntimeError("Параметр min_confidence обязателен")
        try:
            min_confidence = float(raw_min)
        except (TypeError, ValueError):
            raise RuntimeError("min_confidence должен быть числом от 0.0 до 1.0")
        if not (0.0 <= min_confidence <= 1.0):
            raise RuntimeError("min_confidence должен быть в диапазоне 0.0–1.0")

        raw_max = params.get("max_confidence")
        max_confidence: float | None = None
        if raw_max is not None:
            try:
                max_confidence = float(raw_max)
            except (TypeError, ValueError):
                raise RuntimeError("max_confidence должен быть числом от 0.0 до 1.0")
            if not (0.0 <= max_confidence <= 1.0):
                raise RuntimeError("max_confidence должен быть в диапазоне 0.0–1.0")
            if max_confidence < min_confidence:
                raise RuntimeError("max_confidence не может быть меньше min_confidence")

        with self.store._lock():
            active = self.store._load_active_items_unlocked()

        matched = []
        for item in reversed(active):
            c = item.confidence
            if c is None:
                continue
            if c < min_confidence:
                continue
            if max_confidence is not None and c > max_confidence:
                continue
            matched.append(item)

        avg_confidence = (
            round(sum(it.confidence for it in matched) / len(matched), 4)  # type: ignore[arg-type]
            if matched
            else 0.0
        )
        return {
            "items": [it.to_dict() for it in matched],
            "count": len(matched),
            "avg_confidence": avg_confidence,
        }

    # ------------------------------------------------------------------
    # Агрегированная статистика по истории
    # ------------------------------------------------------------------

    def handle_get_history_statistics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Агрегирует статистику по всем активным записям истории за один проход.

        Возвращает:
            total_items (int): общее количество записей
            total_duration_sec (float): суммарная длительность аудио в секундах
            total_words (int): суммарное количество слов
            avg_confidence (float): средняя уверенность STT (0.0–1.0), 0.0 если нет данных
            languages (dict): {lang_code: count} — частота языков по source_lang
            date_range (dict): {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} или None если нет записей
            items_with_translation (int): количество записей с переводом (translation_status == "ok")
            items_with_diarization (int): количество записей с диаризацией (enabled=True, ≥2 спикеров)
            avg_speakers (float): среднее количество спикеров в записях с диаризацией
            top_speakers (dict): {speaker_id: count} — топ спикеров по частоте встреч
            daily_counts (dict): {"YYYY-MM-DD": count} — количество записей за последние 30 дней
        """
        with self.store._lock():
            active = self.store._load_active_items_unlocked()

        if not active:
            return {
                "total_items": 0,
                "total_duration_sec": 0.0,
                "total_words": 0,
                "avg_confidence": 0.0,
                "languages": {},
                "date_range": None,
                "items_with_translation": 0,
                "items_with_diarization": 0,
                "avg_speakers": 0.0,
                "top_speakers": {},
                "daily_counts": {},
            }

        total_items = len(active)
        total_duration_sec = 0.0
        total_words = 0
        confidence_sum = 0.0
        confidence_count = 0
        languages: dict[str, int] = {}
        items_with_translation = 0
        items_with_diarization = 0
        speakers_per_item: list[int] = []
        all_speakers: dict[str, int] = {}
        min_date: str | None = None
        max_date: str | None = None

        # Вычисляем порог для daily_counts (последние 30 дней)
        now = datetime.now(timezone.utc)
        thirty_days_ago = (now - timedelta(days=30)).date()
        daily_counts: dict[str, int] = {}

        for item in active:
            # Длительность
            if item.audio_duration_sec is not None:
                total_duration_sec += item.audio_duration_sec

            # Слова
            if item.text:
                total_words += len(item.text.split())

            # Уверенность
            if item.confidence is not None:
                confidence_sum += item.confidence
                confidence_count += 1

            # Язык
            if item.source_lang:
                languages[item.source_lang] = languages.get(item.source_lang, 0) + 1

            # Перевод
            if item.translated_text and item.translation_status == "ok":
                items_with_translation += 1

            # Диаризация
            diar = item.diarization
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {str(t.get("speaker")) for t in turns if t.get("speaker")}
                if len(speakers) >= 2:
                    items_with_diarization += 1
                    speakers_per_item.append(len(speakers))
                    for spk in speakers:
                        all_speakers[spk] = all_speakers.get(spk, 0) + 1

            # Диапазон дат
            if item.ts:
                try:
                    item_date = item.ts[:10]  # "YYYY-MM-DD"
                    if min_date is None or item_date < min_date:
                        min_date = item_date
                    if max_date is None or item_date > max_date:
                        max_date = item_date

                    # daily_counts за последние 30 дней
                    from datetime import date as _date
                    parsed_date = _date.fromisoformat(item_date)
                    if parsed_date >= thirty_days_ago:
                        daily_counts[item_date] = daily_counts.get(item_date, 0) + 1
                except (ValueError, TypeError):
                    pass

        avg_confidence = round(confidence_sum / confidence_count, 4) if confidence_count > 0 else 0.0
        avg_speakers = round(sum(speakers_per_item) / len(speakers_per_item), 2) if speakers_per_item else 0.0

        # Топ-10 спикеров по частоте
        top_speakers = dict(
            sorted(all_speakers.items(), key=lambda kv: kv[1], reverse=True)[:10]
        )

        date_range = {"from": min_date, "to": max_date} if min_date and max_date else None

        return {
            "total_items": total_items,
            "total_duration_sec": round(total_duration_sec, 3),
            "total_words": total_words,
            "avg_confidence": avg_confidence,
            "languages": languages,
            "date_range": date_range,
            "items_with_translation": items_with_translation,
            "items_with_diarization": items_with_diarization,
            "avg_speakers": avg_speakers,
            "top_speakers": top_speakers,
            "daily_counts": daily_counts,
        }

    # ------------------------------------------------------------------
    # Статические хелперы (копированы из BackendService для автономности)
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        """Нормализует bool-поля из UI/JSON с поддержкой строковых значений."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "on", "yes"}:
                return True
            if normalized in {"0", "false", "off", "no"}:
                return False
        return default

    @staticmethod
    def _coerce_bounded(
        value: Any,
        default: int | float,
        min_value: int | float,
        max_value: int | float,
    ) -> int | float:
        """Нормализует числовое значение в допустимый диапазон."""
        coerce = int if isinstance(default, int) else float
        try:
            parsed = coerce(value)
        except (TypeError, ValueError):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))

    # ------------------------------------------------------------------
    # Speaker label helpers
    # ------------------------------------------------------------------

    _SPEAKER_LABEL_PREFIXES = {"ru": "Спикер", "es": "Hablante", "en": "Speaker"}
    _SPEAKER_LABEL_DEFAULT = "Спикер"

    def _resolve_speaker_name(self, speaker_id: str, lang=None) -> str:
        """Возвращает читаемое имя спикера (псевдоним или 'Спикер N')."""
        import re as _re
        if self._speaker_manager is not None:
            try:
                alias = self._speaker_manager.get_alias(speaker_id)
                if alias:
                    return alias
            except Exception:
                pass
        m = _re.search(r"(\d+)$", speaker_id)
        n = (int(m.group(1)) + 1) if m else 1
        prefix = self._SPEAKER_LABEL_PREFIXES.get(
            (lang or "").lower()[:2], self._SPEAKER_LABEL_DEFAULT
        )
        return f"{prefix} {n}"

    def _should_include_speaker_labels(self, params: dict) -> bool:
        """True если нужно включать метки спикеров в экспорт."""
        param_val = params.get("include_speaker_labels")
        if param_val is not None:
            return self._coerce_bool(param_val, default=False)
        from core.config import settings
        return settings.EXPORT_INCLUDE_SPEAKER_LABELS

    # ------------------------------------------------------------------
    # Экспорт в формат Obsidian
    # ------------------------------------------------------------------

    def handle_export_obsidian(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует транскрипции в формат Obsidian-совместимого Markdown.

        Параметры (взаимно-исключающие — приоритет: ids > date range):
            ids (list[str]|None): список ID записей для экспорта
            from_ts (str|None): ISO-timestamp начала диапазона (включительно)
            to_ts (str|None): ISO-timestamp конца диапазона (включительно)
            limit (int): макс. количество записей при выборке по диапазону (по умолчанию 100)
            title (str|None): заголовок документа (по умолчанию генерируется из дат)
            output_dir (str|None): директория для сохранения (по умолчанию transcripts/)
            tags (list[str]|None): дополнительные Obsidian-теги (кроме #transcription #krab-ear)

        Возвращает:
            file (str): путь к созданному .md файлу
            entries (int): количество экспортированных записей
            content (str): содержимое файла
        """
        ids: list[str] | None = params.get("ids")
        from_ts: str | None = params.get("from_ts")
        to_ts: str | None = params.get("to_ts")
        limit = max(1, min(int(params.get("limit", 100) or 100), 2000))
        custom_title: str | None = params.get("title")
        output_dir_param: str | None = params.get("output_dir")
        extra_tags: list[str] = list(params.get("tags") or [])

        # --- Загружаем записи ---
        from backend.models import HistoryItem as _HI
        if ids is not None:
            if not isinstance(ids, list) or len(ids) == 0:
                raise RuntimeError("ids должен быть непустым списком строк")
            id_set = {str(i).strip() for i in ids if str(i).strip()}
            with self.store._lock():
                all_items = self.store._load_active_items_unlocked()
            items = [it for it in all_items if it.id in id_set]
            if not items:
                raise RuntimeError("Ни одна из указанных записей не найдена")
        else:
            page_dicts, _ = self.store.get_history_page_filtered(
                cursor=None,
                limit=limit,
                paste_status=None,
                translation_mode=None,
                from_ts=str(from_ts) if from_ts else None,
                to_ts=str(to_ts) if to_ts else None,
            )
            if not page_dicts:
                raise RuntimeError("Записи в указанном диапазоне не найдены")
            items = [_HI.from_dict(d) for d in page_dicts]

        # --- Метаданные ---
        ts_list = [it.ts for it in items if it.ts]
        first_ts = ts_list[-1] if ts_list else None   # старейшая (список в порядке убывания)
        last_ts = ts_list[0] if ts_list else None      # новейшая

        # Определяем дату для заголовка и имени файла
        try:
            title_date_dt = datetime.fromisoformat(last_ts) if last_ts else datetime.now()
        except (ValueError, TypeError):
            title_date_dt = datetime.now()
        title_date_str = title_date_dt.strftime("%Y-%m-%d")

        # Человекочитаемая дата для frontmatter
        MONTHS_RU = [
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря",
        ]
        date_human = f"{title_date_dt.day} {MONTHS_RU[title_date_dt.month - 1]} {title_date_dt.year} г."

        # --- Длительность всех записей ---
        total_dur_sec = sum(
            (it.audio_duration_sec or 0.0) for it in items
        )

        # --- Теги ---
        base_tags = ["transcription", "krab-ear"]
        all_tags = base_tags + [t.lstrip("#") for t in extra_tags if t.strip()]
        tags_yaml = ", ".join(f'"{t}"' for t in all_tags)
        tags_inline = " ".join(f"#{t}" for t in all_tags)

        # --- Заголовок ---
        if custom_title:
            doc_title = custom_title
        elif len(items) == 1:
            doc_title = f"Транскрибация ({title_date_str})"
        else:
            doc_title = f"Транскрибации ({title_date_str})"

        include_labels = self._should_include_speaker_labels(params)

        # Collect all speaker IDs across all items for frontmatter when include_labels
        all_speaker_ids: list[str] = []
        if include_labels:
            seen_speakers: dict[str, int] = {}
            for _it in items:
                _diar = _it.diarization
                if _diar and isinstance(_diar, dict) and _diar.get("enabled"):
                    for _t in _diar.get("speaker_turns", []):
                        _sid = _t.get("speaker", "")
                        if _sid and _sid not in seen_speakers:
                            seen_speakers[_sid] = len(seen_speakers)
            all_speaker_ids = sorted(seen_speakers.keys(), key=lambda s: seen_speakers[s])

        # --- Строим YAML frontmatter ---
        fm_lines = [
            "---",
            f"title: \"{doc_title}\"",
            f"date: {title_date_str}",
            f"tags: [{tags_yaml}]",
            f"entries: {len(items)}",
        ]
        if include_labels and all_speaker_ids:
            resolved_names = [
                self._resolve_speaker_name(sid) for sid in all_speaker_ids
            ]
            speakers_yaml = ", ".join(f'\"{n}\"' for n in resolved_names)
            fm_lines.append(f"speakers: [{speakers_yaml}]")
        if total_dur_sec > 0:
            fm_lines.append(
                f"duration: \"{self._format_duration_human(total_dur_sec)}\""
            )
        if first_ts and last_ts and first_ts != last_ts:
            fm_lines.append(f"from: \"{self._format_ts_human(first_ts)}\"")
            fm_lines.append(f"to: \"{self._format_ts_human(last_ts)}\"")
        fm_lines.append("---")

        # --- Тело документа ---
        body_lines: list[str] = [
            "",
            f"# {doc_title}",
            "",
            f"**Дата:** {date_human}  ",
            f"**Теги:** {tags_inline}  ",
            "",
        ]

        # Summary: если LLM доступен — пытаемся авто-резюме
        summary_text: str | None = None
        key_points: list[str] = []

        if self._llm_rewriter is not None:
            circuit_state = (
                self._llm_rewriter._circuit.state
                if hasattr(self._llm_rewriter, "_circuit")
                else None
            )
            if circuit_state != "open":
                texts = [(it.text or "").strip() for it in items if (it.text or "").strip()]
                if texts:
                    try:
                        prompt = self._build_batch_summary_prompt(texts)
                        result = self._llm_rewriter.summarize(prompt, max_sentences=5)
                        if result.ok and result.text:
                            parsed = self._parse_llm_batch_response(result.text)
                            summary_text = parsed.get("summary")
                            key_points = parsed.get("key_points", [])
                    except Exception as exc:
                        logger.warning("export_obsidian: LLM summarize failed: %s", exc)

        if summary_text is None:
            # Базовое резюме: статистика
            duration_str = self._format_duration_human(total_dur_sec)
            summary_text = (
                f"{len(items)} транскрипций"
                + (f", суммарная длительность {duration_str}" if duration_str else "")
                + f", экспортировано {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )

        body_lines += [
            "## Краткое содержание (Summary)",
            "",
            summary_text,
            "",
        ]

        if key_points:
            body_lines += ["**Основные темы:**", ""]
            for i, point in enumerate(key_points, start=1):
                body_lines.append(f"{i}. **Тема:** {point}")
            body_lines.append("")

        body_lines += ["---", "", "## Улучшенная транскрибация", ""]

        # --- Каждая запись ---
        for item in items:
            ts_human = self._format_ts_human(item.ts) if item.ts else "—"

            diar = item.diarization
            diar_turns: list[dict] = []
            has_diarization = False
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                diar_turns = diar.get("speaker_turns", [])
                speakers_set = {t.get("speaker") for t in diar_turns if t.get("speaker")}
                if len(speakers_set) >= 2:
                    has_diarization = True

            if has_diarization and diar_turns:
                for turn in diar_turns:
                    sid = turn.get("speaker", "SPEAKER_00")
                    turn_text = str(turn.get("text", "")).strip()
                    if not turn_text:
                        continue
                    start_sec = turn.get("start")
                    if include_labels:
                        lbl = self._resolve_speaker_name(sid, lang=item.source_lang)
                        if start_sec is not None:
                            ts_mark = self._srt_timestamp(float(start_sec))[:8]
                            body_lines.append(f"**{lbl}** ({ts_mark}):")
                        else:
                            body_lines.append(f"**{lbl}** ({ts_human}):")
                        body_lines.append(turn_text)
                    else:
                        if start_sec is not None:
                            ts_mark = self._srt_timestamp(float(start_sec))[:8]  # HH:MM:SS
                            body_lines.append(f"[{sid} ({ts_mark})]")
                        else:
                            body_lines.append(f"[{sid} ({ts_human})]")
                        body_lines.append(turn_text)
                    body_lines.append("")
            else:
                body_lines.append(f"[Спикер ({ts_human})]")
                body_lines.append(item.text or "")
                body_lines.append("")

            # Перевод
            if item.translated_text and item.translation_status == "ok":
                mode_label = item.translation_mode or "перевод"
                body_lines += [
                    f"> **Перевод** ({mode_label}): {item.translated_text}",
                    "",
                ]

        # --- Собираем файл ---
        content = "\n".join(fm_lines) + "\n" + "\n".join(body_lines)

        # --- Сохраняем ---
        if output_dir_param:
            # W1707: use _resolve_export_dir (includes data_dir + /tmp) instead of
            # _is_safe_export_dir (module-level, doesn't know data_dir).
            resolved = self._resolve_export_dir(output_dir_param)
            if resolved is None:
                raise ValueError(
                    f"output_dir outside allowed roots: {output_dir_param!r}"
                )
            out_dir = resolved
        else:
            out_dir = Path(self.store.data_dir) / "transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)

        safe_title = (
            doc_title
            .replace("/", "-")
            .replace("\\", "-")
            .replace(":", "-")
            .replace("*", "")
            .replace("?", "")
            .replace("\"", "")
            .replace("<", "")
            .replace(">", "")
            .replace("|", "-")
        )
        filename = f"{title_date_str}-{safe_title}.md"
        file_path = out_dir / filename

        # Уникальность имени файла
        if file_path.exists():
            suffix = datetime.now().strftime("%H%M%S")
            filename = f"{title_date_str}-{safe_title}-{suffix}.md"
            file_path = out_dir / filename

        file_path.write_text(content, encoding="utf-8")
        logger.info("Obsidian экспорт сохранён: %s (%d записей)", file_path, len(items))

        return {
            "file": str(file_path),
            "entries": len(items),
            "content": content,
        }

    def _build_obsidian_content_for_items(
        self,
        items: list[Any],
        doc_title: str,
        extra_tags: list[str],
    ) -> str:
        """Строит содержимое Obsidian .md для списка объектов HistoryItem.

        Вспомогательный метод — используется в handle_export_obsidian и тестах.
        """

        ts_list = [it.ts for it in items if it.ts]
        last_ts = ts_list[0] if ts_list else None
        try:
            title_date_dt = datetime.fromisoformat(last_ts) if last_ts else datetime.now()
        except (ValueError, TypeError):
            title_date_dt = datetime.now()
        title_date_str = title_date_dt.strftime("%Y-%m-%d")

        MONTHS_RU = [
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря",
        ]
        date_human = f"{title_date_dt.day} {MONTHS_RU[title_date_dt.month - 1]} {title_date_dt.year} г."

        total_dur_sec = sum((getattr(it, "audio_duration_sec", None) or 0.0) for it in items)

        base_tags = ["transcription", "krab-ear"]
        all_tags = base_tags + [t.lstrip("#") for t in extra_tags if t.strip()]
        tags_yaml = ", ".join(f'"{t}"' for t in all_tags)
        tags_inline = " ".join(f"#{t}" for t in all_tags)

        fm_lines = [
            "---",
            f"title: \"{doc_title}\"",
            f"date: {title_date_str}",
            f"tags: [{tags_yaml}]",
            f"entries: {len(items)}",
        ]
        if total_dur_sec > 0:
            fm_lines.append(
                f"duration: \"{self._format_duration_human(total_dur_sec)}\""
            )
        fm_lines.append("---")

        body_lines: list[str] = [
            "",
            f"# {doc_title}",
            "",
            f"**Дата:** {date_human}  ",
            f"**Теги:** {tags_inline}  ",
            "",
            "## Краткое содержание (Summary)",
            "",
            (
                f"{len(items)} транскрипций"
                + (f", суммарная длительность {self._format_duration_human(total_dur_sec)}" if total_dur_sec > 0 else "")
            ),
            "",
            "---",
            "",
            "## Улучшенная транскрибация",
            "",
        ]

        for item in items:
            ts_human = self._format_ts_human(item.ts) if item.ts else "—"
            diar = getattr(item, "diarization", None)
            diar_turns: list[dict] = []
            has_diarization = False
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                diar_turns = diar.get("speaker_turns", [])
                speakers_set = {t.get("speaker") for t in diar_turns if t.get("speaker")}
                if len(speakers_set) >= 2:
                    has_diarization = True

            if has_diarization and diar_turns:
                for turn in diar_turns:
                    speaker = turn.get("speaker", "SPEAKER_00")
                    turn_text = str(turn.get("text", "")).strip()
                    if not turn_text:
                        continue
                    start_sec = turn.get("start")
                    if start_sec is not None:
                        ts_mark = self._srt_timestamp(float(start_sec))[:8]
                        body_lines.append(f"[{speaker} ({ts_mark})]")
                    else:
                        body_lines.append(f"[{speaker} ({ts_human})]")
                    body_lines.append(turn_text)
                    body_lines.append("")
            else:
                body_lines.append(f"[Спикер ({ts_human})]")
                body_lines.append(getattr(item, "text", "") or "")
                body_lines.append("")

            translated_text = getattr(item, "translated_text", "") or ""
            translation_status = getattr(item, "translation_status", "") or ""
            translation_mode = getattr(item, "translation_mode", "") or "перевод"
            if translated_text and translation_status == "ok":
                body_lines += [
                    f"> **Перевод** ({translation_mode}): {translated_text}",
                    "",
                ]

        return "\n".join(fm_lines) + "\n" + "\n".join(body_lines)

    # ------------------------------------------------------------------
    # Частотный анализ слов
    # ------------------------------------------------------------------

    # Стоп-слова: ru + es + en + uk из core.stop_words
    _STOP_WORDS: frozenset = (
        __import__("core.stop_words", fromlist=["StopWords"]).StopWords.get_stop_words("ru")
        | __import__("core.stop_words", fromlist=["StopWords"]).StopWords.get_stop_words("es")
        | __import__("core.stop_words", fromlist=["StopWords"]).StopWords.get_stop_words("en")
        | __import__("core.stop_words", fromlist=["StopWords"]).StopWords.get_stop_words("uk")
    )

    @staticmethod
    def _tokenize(text: str) -> list:
        """Разбивает текст на слова (нижний регистр, только буквы)."""
        import re
        return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)

    def handle_word_frequency_analysis(self, params: dict) -> dict:
        """Анализирует частоту слов по истории транскрипций.

        Params:
            language (str, optional): фильтрация по языку-источнику ('ru', 'es', 'en', …).
            limit (int, optional): лимит записей для анализа (default 1000).

        Returns:
            top_words        — топ-50 слов [{word, count, percentage}]
            total_words      — суммарное количество токенов
            unique_words     — количество уникальных слов
            vocabulary_richness — unique/total (0.0 если total=0)
            bigrams          — топ-20 биграмм [{phrase, count}]
            by_language      — частоты по языкам {'ru': {'top_words': [...]}, …}
        """
        from collections import Counter

        language_filter = str(params.get("language", "")).strip().lower() or None
        record_limit = max(1, min(int(params.get("limit", 1000)), 10000))

        with self.store._lock():
            items = self.store._load_active_items_unlocked()

        global_words: list = []
        global_bigrams: list = []
        lang_words: dict = {}

        for item in items[:record_limit]:
            lang = (getattr(item, "source_lang", "") or "").strip().lower()
            if language_filter and lang != language_filter:
                continue
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            tokens = [w for w in self._tokenize(text) if w not in self._STOP_WORDS and len(w) > 1]
            global_words.extend(tokens)
            for i in range(len(tokens) - 1):
                global_bigrams.append((tokens[i], tokens[i + 1]))
            if lang:
                lang_words.setdefault(lang, []).extend(tokens)

        total_words = len(global_words)
        word_counter: Counter = Counter(global_words)
        unique_words = len(word_counter)
        vocabulary_richness = round(unique_words / total_words, 4) if total_words else 0.0

        def _top_words(counter: Counter, n: int = 50) -> list:
            total = sum(counter.values())
            return [
                {
                    "word": w,
                    "count": c,
                    "percentage": round(c / total * 100, 2) if total else 0.0,
                }
                for w, c in counter.most_common(n)
            ]

        bigram_counter: Counter = Counter(global_bigrams)
        top_bigrams = [
            {"phrase": f"{a} {b}", "count": c}
            for (a, b), c in bigram_counter.most_common(20)
        ]

        by_language: dict = {}
        for lang, words in lang_words.items():
            lc: Counter = Counter(words)
            by_language[lang] = {"top_words": _top_words(lc, 20)}

        return {
            "top_words": _top_words(word_counter, 50),
            "total_words": total_words,
            "unique_words": unique_words,
            "vocabulary_richness": vocabulary_richness,
            "bigrams": top_bigrams,
            "by_language": by_language,
        }

    # ------------------------------------------------------------------
    # Backup / Restore
    # ------------------------------------------------------------------

    def handle_backup_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Создаёт timestamped-резервную копию history.ndjson и settings.json.

        Возвращает:
            backup_path (str): путь к папке резервной копии
            size_mb (float): суммарный размер файлов в МБ
            entries (int): количество активных записей истории
        """
        import shutil

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backups_dir = Path(self.store.data_dir) / "backups"
        backup_dir = backups_dir / f"backup_{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        files_to_backup = [
            self.store.history_path,
            self.store.tombstones_path,
            self.store.status_path,
            self.store.settings_path,
        ]

        total_bytes = 0
        for src in files_to_backup:
            if src.exists():
                dst = backup_dir / src.name
                shutil.copy2(src, dst)
                total_bytes += dst.stat().st_size

        # Сохраняем метаданные резервной копии
        import json as _json
        entries = self.store.count_active_items()
        meta = {
            "backup_ts": ts,
            "entries": entries,
            "size_bytes": total_bytes,
            "files": [f.name for f in files_to_backup if f.exists()],
        }
        (backup_dir / "backup_meta.json").write_text(
            _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        size_mb = round(total_bytes / (1024 * 1024), 3)
        logger.info("Резервная копия создана: %s (%s МБ, %d записей)", backup_dir, size_mb, entries)
        return {
            "backup_path": str(backup_dir),
            "size_mb": size_mb,
            "entries": entries,
        }

    def handle_restore_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Восстанавливает историю из резервной копии.

        Params:
            backup_path (str): путь к папке резервной копии
            restore_settings (bool): если True — восстанавливает и settings.json (default: False)

        Возвращает:
            restored_entries (int): количество записей после восстановления
            backup_date (str): timestamp резервной копии
        """
        import shutil

        raw_path = str(params.get("backup_path", "")).strip()
        if not raw_path:
            raise RuntimeError("backup_path обязателен")

        backup_dir = Path(raw_path).expanduser().resolve()
        if not backup_dir.exists() or not backup_dir.is_dir():
            raise RuntimeError(f"Папка резервной копии не найдена: {backup_dir}")

        # Проверяем, что это наш backup (должен содержать history.ndjson или backup_meta.json)
        history_backup = backup_dir / "history.ndjson"
        meta_file = backup_dir / "backup_meta.json"
        if not history_backup.exists() and not meta_file.exists():
            raise RuntimeError(
                f"Невалидная резервная копия: нет history.ndjson или backup_meta.json в {backup_dir}"
            )

        # Читаем метаданные
        backup_date = "unknown"
        if meta_file.exists():
            meta = safe_json_loads(
                meta_file.read_text(encoding="utf-8"),
                default=None,
                context="backup_meta.json",
            )
            if meta:
                backup_date = meta.get("backup_ts", "unknown")

        restore_settings = self._coerce_bool(params.get("restore_settings", False), default=False)

        # Восстанавливаем файлы (под lock)
        with self.store._lock():
            if history_backup.exists():
                shutil.copy2(history_backup, self.store.history_path)

            for aux_name in ("history_tombstones.ndjson", "history_status.ndjson"):
                src = backup_dir / aux_name
                if src.exists():
                    dst = self.store.data_dir / aux_name
                    shutil.copy2(src, dst)

            if restore_settings:
                settings_backup = backup_dir / "settings.json"
                if settings_backup.exists():
                    shutil.copy2(settings_backup, self.store.settings_path)

        restored_entries = self.store.count_active_items()
        logger.info("История восстановлена из %s: %d записей", backup_dir, restored_entries)
        return {
            "restored_entries": restored_entries,
            "backup_date": backup_date,
        }

    def handle_list_backups(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных резервных копий с метаданными.

        Возвращает:
            backups (list): список объектов с полями path, backup_date, entries, size_mb
        """
        backups_dir = Path(self.store.data_dir) / "backups"
        if not backups_dir.exists():
            return {"backups": []}

        result = []
        for backup_dir in sorted(backups_dir.iterdir(), reverse=True):
            if not backup_dir.is_dir():
                continue
            meta_file = backup_dir / "backup_meta.json"
            entry: dict[str, Any] = {
                "path": str(backup_dir),
                "backup_date": backup_dir.name,
                "entries": None,
                "size_mb": None,
            }
            if meta_file.exists():
                meta = safe_json_loads(
                    meta_file.read_text(encoding="utf-8"),
                    default=None,
                    context="backup_meta.json",
                )
                if meta:
                    entry["backup_date"] = meta.get("backup_ts", backup_dir.name)
                    entry["entries"] = meta.get("entries")
                    size_bytes = meta.get("size_bytes", 0)
                    entry["size_mb"] = round(size_bytes / (1024 * 1024), 3)
            result.append(entry)

        return {"backups": result}

    def handle_find_duplicates(self, params: dict[str, Any]) -> dict[str, Any]:
        """Находит дублирующиеся транскрипции в истории.

        Параметры:
            similarity_threshold (float): порог сходства [0..1], по умолчанию 0.9.
            limit (int): максимальное количество записей истории для анализа (по умолчанию 500).

        Возвращает:
            groups (list): список групп, каждая содержит items[] и similarity.
            total_duplicates (int): общее количество дублирующихся записей.
        """
        threshold = float(params.get("similarity_threshold", 0.9))
        limit = int(params.get("limit", 500))

        items, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=limit,
            paste_status=None,
            translation_mode=None,
        )
        detector = DuplicateDetector()
        groups = detector.find_duplicates(items, similarity_threshold=threshold)

        total_duplicates = sum(len(g.items) - 1 for g in groups)
        return {
            "groups": [
                {"items": g.items, "similarity": g.similarity}
                for g in groups
            ],
            "total_duplicates": total_duplicates,
        }

    # ------------------------------------------------------------------
    # IPC-обработчики для профилей резюмирования
    # ------------------------------------------------------------------

    def handle_list_summary_profiles(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список всех профилей резюмирования.

        Возвращает:
            profiles (list): список профилей (name, system_prompt, max_tokens,
                             format_instructions, builtin).
        """
        return {"profiles": self._summary_profiles.list_profiles()}

    def handle_add_summary_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет или заменяет кастомный профиль резюмирования.

        Параметры:
            name (str): уникальное имя профиля (snake_case, обязательно)
            prompt (str): системный промпт для LLM (обязательно)
            max_tokens (int): максимальное количество токенов (по умолчанию 300)
            format_instructions (str): описание формата ответа (опционально)

        Возвращает:
            profile (dict): созданный профиль
        """
        name = str(params.get("name", "")).strip()
        prompt = str(params.get("prompt", "")).strip()
        max_tokens = int(params.get("max_tokens", 300) or 300)
        format_instructions = str(params.get("format_instructions", "")).strip()

        if not name:
            raise RuntimeError("Параметр 'name' обязателен")
        if not prompt:
            raise RuntimeError("Параметр 'prompt' обязателен")

        profile = self._summary_profiles.add_custom_profile(
            name=name,
            prompt=prompt,
            max_tokens=max_tokens,
            format_instructions=format_instructions,
        )
        return {"profile": profile.to_dict()}

    # ------------------------------------------------------------------
    # Batch export
    # ------------------------------------------------------------------

    def handle_batch_export(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует историю в нескольких форматах одновременно.

        Параметры:
            formats (list[str]): список форматов — любое подмножество
                ["srt", "csv", "markdown", "obsidian"].
                По умолчанию все четыре.
            from_ts (str|None): начало диапазона (ISO timestamp или YYYY-MM-DD)
            to_ts (str|None): конец диапазона (ISO timestamp или YYYY-MM-DD)
            output_dir (str|None): директория для бандла (по умолчанию {data_dir}/exports/)
            limit (int): максимальное количество записей (по умолчанию 500)

        Возвращает:
            dir (str): путь к директории бандла
            files (dict[str, str]): {format: path} для каждого успешно экспортированного формата
            errors (dict[str, str]): {format: error_message} для неудачных форматов
            total_entries (int): количество записей в экспорте
        """
        all_formats = {"srt", "csv", "markdown", "obsidian"}
        formats_raw = params.get("formats")
        if formats_raw is None:
            requested = list(all_formats)
        else:
            if not isinstance(formats_raw, list) or len(formats_raw) == 0:
                raise RuntimeError("formats должен быть непустым списком строк")
            requested = [str(f).lower().strip() for f in formats_raw]
            unknown = set(requested) - all_formats
            if unknown:
                raise RuntimeError(
                    f"Неизвестные форматы: {sorted(unknown)}. Допустимые: {sorted(all_formats)}"
                )

        from_ts: str | None = params.get("from_ts")
        to_ts: str | None = params.get("to_ts")
        limit = max(1, min(int(params.get("limit", 500) or 500), 5000))
        output_dir_param: str | None = params.get("output_dir")

        # Создаём директорию бандла
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        if output_dir_param:
            # W1707: use _resolve_export_dir (includes data_dir + /tmp) instead of
            # _is_safe_export_dir (module-level, doesn't know data_dir).
            resolved = self._resolve_export_dir(output_dir_param)
            if resolved is None:
                raise ValueError(
                    f"output_dir outside allowed roots: {output_dir_param!r}"
                )
            base_dir = resolved
        else:
            base_dir = Path(self.store.data_dir) / "exports"
        bundle_dir = base_dir / f"export_{timestamp_str}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        # Получаем записи один раз для подсчёта общего числа
        items_dicts, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=limit,
            paste_status=None,
            translation_mode=None,
            from_ts=str(from_ts) if from_ts else None,
            to_ts=str(to_ts) if to_ts else None,
        )
        total_entries = len(items_dicts)

        files: dict[str, str] = {}
        errors: dict[str, str] = {}

        for fmt in requested:
            try:
                if fmt == "csv":
                    csv_params: dict[str, Any] = {"limit": limit}
                    if from_ts is not None:
                        csv_params["from_ts"] = from_ts
                    if to_ts is not None:
                        csv_params["to_ts"] = to_ts
                    files["csv"] = self._export_csv_to_dir(csv_params, bundle_dir, timestamp_str)

                elif fmt == "markdown":
                    md_content = self._build_markdown_content(items_dicts)
                    md_path = bundle_dir / f"export_{timestamp_str}.md"
                    md_path.write_text(md_content, encoding="utf-8")
                    files["markdown"] = str(md_path)

                elif fmt == "srt":
                    srt_content = self._build_bulk_srt(items_dicts)
                    srt_path = bundle_dir / f"export_{timestamp_str}.srt"
                    srt_path.write_text(srt_content, encoding="utf-8")
                    files["srt"] = str(srt_path)

                elif fmt == "obsidian":
                    obs_params: dict[str, Any] = {
                        "limit": limit,
                        "output_dir": str(bundle_dir),
                    }
                    if from_ts is not None:
                        obs_params["from_ts"] = from_ts
                    if to_ts is not None:
                        obs_params["to_ts"] = to_ts
                    obs_result = self.handle_export_obsidian(obs_params)
                    files["obsidian"] = obs_result["file"]

            except Exception as exc:
                logger.warning("batch_export: ошибка формата %s: %s", fmt, exc)
                errors[fmt] = str(exc)

        logger.info(
            "batch_export завершён: %d форматов, %d ошибок, dir=%s",
            len(files), len(errors), bundle_dir,
        )
        return {
            "dir": str(bundle_dir),
            "files": files,
            "errors": errors,
            "total_entries": total_entries,
        }

    # ------------------------------------------------------------------
    # Batch export helpers
    # ------------------------------------------------------------------

    def _export_csv_to_dir(
        self,
        params: dict[str, Any],
        target_dir: Path,
        timestamp_str: str,
    ) -> str:
        """Экспортирует CSV в указанную директорию и возвращает путь к файлу."""
        import csv
        import io

        delimiter = params.get("delimiter", ",")
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            delimiter = ","
        include_header = params.get("include_header", True)
        limit = params.get("limit")
        from_ts = params.get("from_ts")
        to_ts = params.get("to_ts")

        items = [i.to_dict() if hasattr(i, "to_dict") else i
                 for i in self.store._load_active_items_with_lock()]
        if from_ts:
            items = [i for i in items if i.get("ts", "") >= str(from_ts)]
        if to_ts:
            items = [i for i in items if i.get("ts", "") <= str(to_ts)]
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        if limit:
            items = items[: int(limit)]

        output = io.StringIO()
        writer = csv.writer(output, delimiter=delimiter)
        columns = ["timestamp", "text", "translation", "language", "confidence",
                   "duration_sec", "paste_status", "speakers"]
        if include_header:
            writer.writerow(columns)

        for item in items:
            translation = ""
            if item.get("translation_status") == "ok":
                translation = item.get("translated_text") or item.get("translation", "")
            speakers = ""
            diar = item.get("diarization")
            if diar and isinstance(diar, dict):
                segs = diar.get("speaker_segments", [])
                speaker_set = {s.get("speaker", "") for s in segs if isinstance(s, dict)}
                speakers = ", ".join(sorted(speaker_set))
            writer.writerow([
                item.get("ts", ""),
                item.get("text", ""),
                translation,
                item.get("lang", ""),
                item.get("confidence", ""),
                item.get("duration", ""),
                item.get("paste_status", ""),
                speakers,
            ])

        csv_text = output.getvalue()
        file_path = target_dir / f"export_{timestamp_str}.csv"
        file_path.write_text(csv_text, encoding="utf-8")
        return str(file_path)

    def _build_markdown_content(self, items_dicts: list[dict]) -> str:
        """Строит Markdown-содержимое для пакетного экспорта."""
        from backend.models import HistoryItem as _HI
        items = [_HI.from_dict(d) for d in items_dicts]

        if not items:
            return "# Krab Ear — Экспорт транскрипций\n\nИстория пуста.\n"

        ts_list = [it.ts for it in items if it.ts]
        earliest_ts = self._format_ts_human(ts_list[-1]) if ts_list else "?"
        latest_ts = self._format_ts_human(ts_list[0]) if ts_list else "?"
        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines: list[str] = [
            "# Krab Ear — Экспорт транскрипций",
            "",
            f"**Период:** {earliest_ts} — {latest_ts}  ",
            f"**Записей:** {len(items)}  ",
            f"**Экспорт:** {export_ts}",
            "",
            "---",
            "",
        ]

        for idx, item in enumerate(items, start=1):
            ts_human = self._format_ts_human(item.ts)
            duration_str = self._format_duration_human(item.audio_duration_sec)
            section_title = f"## {idx}. {ts_human}"
            if duration_str:
                section_title += f" ({duration_str})"
            lines.append(section_title)
            lines.append("")
            lines.append(item.text or "")
            if item.translated_text and item.translation_status == "ok":
                lines.append("")
                lines.append(f"> **Перевод:** {item.translated_text}")
            lines.append("")

        return "\n".join(lines)

    def _build_bulk_srt(self, items_dicts: list[dict]) -> str:
        """Строит единый SRT-файл для набора записей истории."""
        from backend.models import HistoryItem as _HI
        lines: list[str] = []
        seq = 1
        offset_sec = 0.0

        for d in reversed(items_dicts):  # хронологический порядок
            item = _HI.from_dict(d)
            duration = item.audio_duration_sec or 3.0
            diar = item.diarization

            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers) >= 2 and turns:
                    for turn in turns:
                        speaker = turn.get("speaker", "SPEAKER_00")
                        turn_text = str(turn.get("text", "")).strip()
                        if not turn_text:
                            continue
                        start_sec = offset_sec + float(turn.get("start", 0.0) or 0.0)
                        end_sec = offset_sec + float(
                            turn.get("end", start_sec + 1.0) or start_sec + 1.0
                        )
                        lines.append(str(seq))
                        lines.append(
                            f"{self._srt_timestamp(start_sec)} --> {self._srt_timestamp(end_sec)}"
                        )
                        lines.append(f"[{speaker}]: {turn_text}")
                        lines.append("")
                        seq += 1
                    offset_sec += duration
                    continue

            # Нет диаризации — весь текст как один сегмент
            text = (item.text or "").strip()
            if text:
                lines.append(str(seq))
                lines.append(
                    f"{self._srt_timestamp(offset_sec)} --> "
                    f"{self._srt_timestamp(offset_sec + duration)}"
                )
                lines.append(text)
                lines.append("")
                seq += 1
            offset_sec += duration

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # HTML report export
    # ------------------------------------------------------------------

    def handle_export_html_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует историю транскрипций в автономный HTML-отчёт.

        Параметры:
            title (str): заголовок отчёта (по умолчанию «Krab Ear Report»)
            limit (int): максимальное количество записей (по умолчанию 500, макс 5000)
            from_ts (str|None): начало диапазона (ISO timestamp или YYYY-MM-DD)
            to_ts (str|None): конец диапазона (ISO timestamp или YYYY-MM-DD)
            paste_status (str|None): фильтр по статусу вставки
            save_to_file (bool): если True, сохраняет файл в transcripts/

        Возвращает:
            ok (bool): True при успехе
            html (str): полный HTML-документ
            entries (int): количество записей в отчёте
            chars (int): размер HTML в символах
            path (str|None): путь к сохранённому файлу, если save_to_file=True
        """
        from backend.html_report import HTMLReportGenerator

        title = str(params.get("title", "Krab Ear Report")).strip() or "Krab Ear Report"
        limit = max(1, min(int(params.get("limit", 500) or 500), 5000))
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)

        items_dicts, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=None,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )

        generator = HTMLReportGenerator()
        html_content = generator.generate_report(items=items_dicts, title=title)

        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
                file_path = transcripts_dir / filename
                file_path.write_text(html_content, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("Не удалось сохранить HTML-отчёт в файл: %s", exc)

        return {
            "ok": True,
            "html": html_content,
            "entries": len(items_dicts),
            "chars": len(html_content),
            "path": save_path,
        }
