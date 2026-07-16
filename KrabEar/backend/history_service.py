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
    tempfile.gettempdir(),  # macOS $TMPDIR = /private/var/folders/.../T/ (pytest tmp_path)
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
        # W1765: _speaker_manager (строка ~91) и _playback_tracker (строка ~100)
        # используются для privacy-purge биометрики (speaker_fingerprints.json /
        # speaker_aliases.json) и статистики воспроизведения (playback_stats.json).
        # Оба поля уже существуют; service.py заполняет их late-inject после __init__.
        # W1766: webhooks.json хранит HMAC-секреты и переживает purge без этого wire.
        # W1766: Obsidian vault содержит полные транскрипции и переживает purge без wire.
        self._webhook_manager: Any = None   # WebhookManager — webhooks.json
        self._obsidian_sync: Any = None     # ObsidianSyncManager — vault .md files
        # W1767: новые collaborators для privacy-purge.
        # Все поля заполняются late-inject из BackendService.__init__.
        self._translation_cache: Any = None  # TranslationCache — translation_cache.json
        self._vocabulary_store: Any = None   # VocabularyStore — vocabulary.json
        self._settings_svc: Any = None       # SettingsService — для invalidate_cache()
        self._settings_backup: Any = None    # SettingsBackup — settings_backups/
        # W1770: новые collaborators для privacy-purge (late-inject из BackendService).
        self._collection_manager: Any = None  # CollectionManager — collections.json (#1613)
        self._session_tracker: Any = None     # SessionTracker — sessions.ndjson (#1605)
        # W1771: collaborators с in-memory clear-хуками для privacy-purge.
        # Все поля заполняются late-inject из BackendService.__init__.
        self._template_manager: Any = None    # TemplateManager — templates.json (free-text PII)
        self._event_replay: Any = None        # EventReplayManager — event_replay.ndjson + ring
        self._live_subs_service: Any = None   # LiveSubsService — in-memory PCM buffer (raw voice)
        # Wave-18: ContextMemory — RAM-only deque последних 50 сырых транскриптов
        # (полный PII, re-exposable через get_context_memory IPC). Файлового
        # артефакта нет, поэтому очищается ТОЛЬКО через late-injected clear().
        self._context_memory: Any = None      # ContextMemory — in-memory transcript deque (raw PII)
        # Wave-22: JobTracker — in-memory реестр async-задач транскрибации.
        # terminal-задачи хранят items[].text (полный текст транскрипций) и errors.
        # Файлового артефакта нет; clear() сбрасывает _jobs и все сопутствующие dict-ы.
        self._job_tracker: Any = None         # JobTracker — in-memory async-job registry (transcript PII)
        # wave-33 A1: SharingManager — in-memory _index хранит полный текст транскрипций
        # (content/text/translated_text). rmtree(shares/) удаляет файлы, но RAM-копия
        # продолжает отдавать данные через get_shared. clear() сбрасывает _index.
        self._sharing_manager: Any = None     # SharingManager — in-memory share index (transcript PII)
        # wave-1770 HIGH: SearchHistoryManager — in-memory _entries list survives file
        # deletion unless clear_search_history() is explicitly called during purge.
        # Late-injected by BackendService.__init__ after both objects are constructed.
        self._search_history_mgr: Any = None  # SearchHistoryManager — in-memory query list (user search PII)

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
    # Phase B loud-error helper (late-injected _error_bus)
    # ------------------------------------------------------------------

    def _push_error(
        self,
        code: str,
        message_debug: str,
        context: "dict[str, Any] | None" = None,
    ) -> None:
        """Push a KrabError to the attached ErrorBus, if wired.

        ``_error_bus`` is late-injected by BackendService.__init__ after
        HistoryService is constructed (same pattern as StateStore, DiskMonitor,
        StartupDiagnostics).  When not wired the call is a silent no-op so that
        unit tests that don't need a full BackendService still work.
        """
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone

            entry = ERROR_REGISTRY.get(code, {})
            err = KrabError(
                severity=entry.get("severity", "error"),
                component="history",
                code=code,
                message_user=entry.get("user_msg_ru", "Ошибка истории"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context=context or {"data_dir": str(self.store.data_dir)},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:  # noqa: BLE001
            logger.exception("_push_error failed for code=%s", code)

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
        # Privacy mode gate (wave-35, HIGH B1): this is the PRIMARY list-history
        # endpoint — no transcript items may leak over IPC while privacy is active.
        if self._is_privacy_mode():
            return {"items": [], "next_cursor": None, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-31): consistent with handle_search_with_highlights
        # and handle_fuzzy_search — no transcript text in IPC responses when active.
        if self._is_privacy_mode():
            return {"ok": True, "items": [], "total": 0, "reason": "privacy_mode_active"}

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
        # Записываем запрос для автодополнения недавних/частых поисков
        # (get_recent_searches / get_popular_searches). РАСПОЛОЖЕНО ПОСЛЕ privacy-гейта
        # выше → в режиме приватности запросы не персистятся. Best-effort: сбой записи
        # НЕ должен ломать сам поиск.
        if self._search_history_mgr is not None and query:
            try:
                self._search_history_mgr.record_search(query, results_count=len(items))
            except Exception as exc:  # noqa: BLE001
                logger.debug("record_search failed (non-fatal): %s", exc)
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

    # ------------------------------------------------------------------
    # Вспомогательные методы: работа с .md файлами транскриптов
    # ------------------------------------------------------------------

    def _transcript_md_candidates(self, item_ts: str) -> list[Path]:
        """Возвращает список кандидатов .md файлов для записи с заданным ts.

        TranscriptWriter формирует имена файлов по схеме:
          {date_str}-Транскрибация.md
          {date_str}-Транскрибация-{time_str}.md
          {date_str}-Транскрибация-{time_str}-N.md

        Где date_str = YYYY-MM-DD, time_str = HHMMSS из item.ts.
        Метод возвращает только файлы, в имени которых содержится time_str —
        это минимизирует риск стереть соседний файл того же дня при нескольких
        записях.  Если time_str недоступен (невалидный ts) — возвращает пустой
        список (безопасный fallback: лучше не удалить, чем удалить лишнее).
        """
        if not item_ts:
            return []
        transcripts_dir = Path(self.store.data_dir) / "transcripts"
        if not transcripts_dir.is_dir():
            return []
        try:
            dt = datetime.fromisoformat(item_ts)
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H%M%S")
        except (ValueError, TypeError):
            return []
        # Glob по date_str + time_str — не задевает соседние записи того же дня
        pattern = f"{date_str}-*{time_str}*.md"
        return list(transcripts_dir.glob(pattern))

    def _erase_transcript_md(self, item_ts: str, item_id: str) -> None:
        """Стирает .md файлы транскрипта, связанные с записью.

        W1762: приватная операция — ошибка стирания выводится как WARNING,
        но не прерывает удаление записи (tombstone уже записан).
        В лог попадает только имя файла (без содержимого/PII).
        """
        candidates = self._transcript_md_candidates(item_ts)
        for md_path in candidates:
            try:
                md_path.unlink(missing_ok=True)
                logger.info(
                    "W1762: transcript .md удалён при удалении записи: file=%s item_id=%s",
                    md_path.name, item_id,
                )
            except OSError:
                logger.warning(
                    "W1762: не удалось стереть transcript .md: file=%s item_id=%s",
                    md_path.name, item_id, exc_info=True,
                )

    def handle_delete_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        import time as _time
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise ValueError("id обязателен для удаления")
        _t0 = _time.monotonic()

        # W1762: получаем ts записи ДО tombstone — нужен для поиска .md файла.
        # Загружаем только под коротким lock'ом, чтобы не держать его при удалении.
        item_ts: str = ""
        with self.store._lock():
            active = self.store._load_active_items_unlocked()
        for _item in active:
            if _item.id == item_id:
                item_ts = _item.ts
                break

        ok = self.store.delete_history_item(item_id)
        if not ok:
            raise ValueError(f"Запись не найдена: {item_id}")

        # Все каскады, кроме самого tombstone (.md erase, semantic, chains, playback,
        # versions).  Вынесено в cascade_delete_artifacts, чтобы RecordingMerger мог
        # переиспользовать тот же путь после собственного атомарного tombstone'а
        # (wave1776 HIGH 1) — DRY: один источник истины для каскадного удаления.
        self.cascade_delete_artifacts(item_id, item_ts)

        add_breadcrumb(
            category="history",
            message="delete_history_item",
            data={"ok": True, "duration_ms": round((_time.monotonic() - _t0) * 1000)},
        )
        return {"deleted": True}

    def cascade_delete_artifacts(self, item_id: str, item_ts: str) -> None:
        """Выполняет ВСЕ каскады удаления, КРОМE tombstone'а самой записи.

        Tombstone должен быть записан вызывающей стороной ДО этого вызова
        (handle_delete_history_item или RecordingMerger атомарным append'ом).
        ``item_ts`` обязателен и должен быть захвачен ПОКА запись ещё активна —
        после tombstone'а её ts уже не найти через _load_active_items_unlocked,
        и стирание .md (privacy gap) будет молча пропущено.

        Покрывает: .md-транскрипт (W1762), эмбеддинг семантического поиска
        (W1426 F2), ghost-ссылки в цепочках (W1253 RC-3), playback-статистику
        (W1343), версии транскрипта (W1045 F2).  Каждый шаг отказоустойчив.
        """
        # W1762: стираем .md файл транскрипта (privacy gap — файл пережил tombstone).
        self._erase_transcript_md(item_ts, item_id)

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
        _path_allowed = False
        for _root in allowed_roots:
            try:
                resolved.relative_to(_root)
                _path_allowed = True
                break
            except ValueError:
                continue
        if not _path_allowed:
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
        # wave-39 MED: today_count/last_24h_count/source_langs/today_text_chars reveal usage patterns.
        # Gate mirrors get_history_statistics (wave-38) and get_recording_stats (wave-37).
        if self._is_privacy_mode():
            return {
                "active_count": 0, "paste_ok": 0, "paste_failed": 0,
                "translated_ok": 0, "translated_error": 0, "no_translation": 0,
                "today_count": 0, "last_24h_count": 0,
                "diarization_count": 0, "llm_applied_count": 0,
                "total_text_chars": 0, "today_text_chars": 0,
                "source_langs": [], "target_langs": [],
                "reason": "privacy_mode_active",
            }
        return self.store.get_history_overview()

    def handle_search_by_speaker(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает записи истории, в которых участвует указанный спикер.

        Params:
            speaker (str): идентификатор спикера, например "SPEAKER_00".
            limit (int, optional): максимальное количество результатов (1–500, default 100).

        Returns:
            {"items": [...], "count": N}
        """
        # Privacy mode gate (wave-35, HIGH B3): no diarized transcript items over IPC.
        if self._is_privacy_mode():
            return {"items": [], "count": 0, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-35, HIGH B2): no single-item transcript over IPC.
        if self._is_privacy_mode():
            return {"ok": False, "reason": "privacy_mode_active"}

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
        # wave-40 LOW: tag names can be sensitive. Gate for consistency with handle_search_by_tag.
        if self._is_privacy_mode():
            return {"id": params.get("id", ""), "tags": [], "reason": "privacy_mode_active"}
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
        # Privacy mode gate (wave-35, HIGH B4): no tagged transcript items over IPC.
        if self._is_privacy_mode():
            return {"items": [], "count": 0, "reason": "privacy_mode_active"}

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
        # wave-40 MED: user tag names can be sensitive (e.g. "doctor visit", "secret project").
        # Aggregate frequency + corpus reveals usage patterns. Gate mirrors search_by_tag (line 784).
        if self._is_privacy_mode():
            return {"tags": [], "reason": "privacy_mode_active"}
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
        # Privacy mode gate (wave-35, HIGH B4): no favorite transcript items over IPC.
        if self._is_privacy_mode():
            return {"items": [], "count": 0, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-35, MED B6): annotations may quote transcript PII.
        if self._is_privacy_mode():
            return {"id": "", "note": None, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-35, MED B6): annotations may quote transcript PII.
        if self._is_privacy_mode():
            return {"results": [], "count": 0, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-36, HIGH B1): this export writes the FULL transcript
        # corpus to the IPC response (and optionally to a file on disk). Privacy mode
        # means no transcript data leaves the store — even to a local file.
        if self._is_privacy_mode():
            return {"content": "", "total_items": 0, "path": None, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-35, MED B7): this export returns SRT transcript
        # content directly in the IPC response — withhold while privacy is active.
        if self._is_privacy_mode():
            return {
                "content": "",
                "item_id": "",
                "speakers": 0,
                "segments": 0,
                "path": None,
                "reason": "privacy_mode_active",
            }

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
        # Privacy mode gate (wave-36, HIGH B1): writes the full transcript corpus to
        # the IPC response / clipboard — withhold entirely while privacy is active.
        if self._is_privacy_mode():
            return {"ok": False, "entries": 0, "chars": 0, "reason": "privacy_mode_active"}

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

    # ------------------------------------------------------------------
    # Экспорт ВЫБРАННЫХ записей (multi-select → Markdown/SRT)
    # ------------------------------------------------------------------

    def handle_export_selected_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует только указанные записи истории (multi-select экспорт).

        Параметры:
            item_ids (list[str]): список идентификаторов записей для экспорта
            format (str): формат — «markdown» (по умолчанию) или «srt»
            save_to_file (bool): если True, сохраняет файл в data_dir/transcripts/

        Возвращает:
            ok (bool): True при успехе
            content (str): текст экспорта
            entries (int): количество экспортированных записей
            path (str|None): путь к файлу, если save_to_file=True
            reason (str): причина ошибки (при ok=False)

        Связи: переиспользует render-логику из handle_export_history_markdown
        и _finalize_srt_export. Вызывается из Swift-расширения
        HistoryPanelController+ExportSelection.swift через IPC метод
        «export_selected_items».
        """
        # Privacy mode gate — экспорт транскрипций в режиме приватности запрещён
        if self._is_privacy_mode():
            return {
                "ok": False,
                "content": "",
                "entries": 0,
                "path": None,
                "reason": "privacy_mode_active",
            }

        # Валидация item_ids
        raw_ids = params.get("item_ids")
        if not raw_ids or not isinstance(raw_ids, list):
            return {
                "ok": False,
                "content": "",
                "entries": 0,
                "path": None,
                "reason": "item_ids обязателен и не должен быть пустым",
            }
        requested_ids: set[str] = {str(i) for i in raw_ids if i}
        if not requested_ids:
            return {
                "ok": False,
                "content": "",
                "entries": 0,
                "path": None,
                "reason": "item_ids обязателен и не должен быть пустым",
            }

        export_format = str(params.get("format", "markdown")).lower().strip()
        if export_format not in ("markdown", "srt"):
            # Неизвестный формат — используем markdown как fallback (не ломаем UX)
            logger.warning(
                "handle_export_selected_items: неизвестный формат '%s', "
                "используется markdown", export_format,
            )
            export_format = "markdown"

        # Собираем все страницы истории и фильтруем по requested_ids
        from backend.models import HistoryItem as _HI
        selected_items: list[_HI] = []
        cursor: str | None = None
        for _ in range(500):  # защита от бесконечного цикла
            page_dicts, next_cursor = self.store.get_history_page_filtered(
                cursor=cursor, limit=100,
                paste_status=None, translation_mode=None,
            )
            if not page_dicts:
                break
            for d in page_dicts:
                if d.get("id") in requested_ids:
                    selected_items.append(_HI.from_dict(d))
            if next_cursor is None:
                break
            cursor = next_cursor
            # Ранний выход: нашли все запрошенные элементы
            if len(selected_items) >= len(requested_ids):
                break

        if not selected_items:
            # Все запрошенные ID не найдены — возвращаем пустой, но ok=True
            # (совместимо с паттерном handle_export_history на пустую историю)
            content = "# Krab Ear — Экспорт выбранных записей\n\nЗаписей не найдено.\n"
            return {"ok": True, "content": content, "entries": 0, "path": None}

        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        if export_format == "srt":
            content = self._render_selected_items_srt(selected_items, params)
        else:
            content = self._render_selected_items_markdown(selected_items, export_ts)

        # Path containment: сохраняем только внутри data_dir (аналогично
        # существующим handle_export_history / handle_export_history_json)
        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                base = Path(self.store.data_dir)
                transcripts_dir = base / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                ext = "srt" if export_format == "srt" else "md"
                filename = (
                    f"selected_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    f"_{len(selected_items)}items.{ext}"
                )
                file_path = transcripts_dir / filename
                # Защита от path traversal (аналог audit_path_containment guard)
                resolved = file_path.resolve()
                if not resolved.is_relative_to(base.resolve()):
                    logger.error(
                        "handle_export_selected_items: путь вне data_dir: %s", resolved
                    )
                else:
                    file_path.write_text(content, encoding="utf-8")
                    save_path = str(file_path)
            except Exception as exc:
                logger.warning("Не удалось сохранить выбранный экспорт в файл: %s", exc)

        return {
            "ok": True,
            "content": content,
            "entries": len(selected_items),
            "path": save_path,
        }

    def _render_selected_items_markdown(
        self,
        items: "list[Any]",
        export_ts: str,
    ) -> str:
        """Рендерит список HistoryItem в Markdown. Переиспользует логику из
        handle_export_history_markdown, но принимает готовый список items."""
        ts_list = [it.ts for it in items if it.ts]
        earliest_ts = self._format_ts_human(ts_list[-1]) if ts_list else "?"
        latest_ts = self._format_ts_human(ts_list[0]) if ts_list else "?"

        lines: list[str] = [
            "# Krab Ear — Экспорт выбранных записей",
            "",
            f"**Период:** {earliest_ts} — {latest_ts}  ",
            f"**Записей:** {len(items)}  ",
            f"**Экспорт:** {export_ts}",
            "",
            "---",
            "",
        ]

        languages_used: set[str] = set()
        for idx, item in enumerate(items, start=1):
            ts_human = self._format_ts_human(item.ts)
            duration_str = self._format_duration_human(item.audio_duration_sec)

            section_title = f"## {idx}. {ts_human}"
            if duration_str:
                section_title += f" ({duration_str})"
            lines.append(section_title)
            lines.append("")

            meta: list[str] = []
            if item.source_lang:
                meta.append(f"**Язык:** {item.source_lang}")
                languages_used.add(item.source_lang)
            if item.target_lang and item.translation_status == "ok":
                languages_used.add(item.target_lang)

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

            if item.translated_text and item.translation_status == "ok":
                mode_label = item.translation_mode or "перевод"
                lines.append("")
                lines.append(f"> **Перевод** ({mode_label}): {item.translated_text}")

            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## Сводная статистика")
        lines.append("")
        lines.append(f"- **Всего записей:** {len(items)}")
        if languages_used:
            lines.append(f"- **Языки:** {', '.join(sorted(languages_used))}")
        lines.append(f"- **Экспортировано:** {export_ts}")
        lines.append("")

        return "\n".join(lines)

    def _render_selected_items_srt(
        self,
        items: "list[Any]",
        params: dict[str, Any],
    ) -> str:
        """Рендерит список HistoryItem в единый SRT-файл. Каждая запись —
        отдельный блок субтитров. Переиспользует логику _srt_timestamp."""
        srt_lines: list[str] = []
        seq = 0
        for item in items:
            diar = item.diarization
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers_set = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers_set) >= 2 and turns:
                    for turn in turns:
                        speaker = turn.get("speaker", "SPEAKER_00")
                        turn_text = str(turn.get("text", "")).strip()
                        if not turn_text:
                            continue
                        seq += 1
                        start_sec = float(turn.get("start", 0.0) or 0.0)
                        end_sec = float(
                            turn.get("end", start_sec + 1.0) or start_sec + 1.0
                        )
                        srt_lines.append(str(seq))
                        srt_lines.append(
                            f"{self._srt_timestamp(start_sec)} --> "
                            f"{self._srt_timestamp(end_sec)}"
                        )
                        if self._should_include_speaker_labels(params):
                            lbl = self._resolve_speaker_name(
                                speaker, lang=getattr(item, "source_lang", None)
                            )
                            srt_lines.append(f"{lbl}: {turn_text}")
                        else:
                            srt_lines.append(f"[{speaker}]: {turn_text}")
                        srt_lines.append("")
                    continue
            # Нет диаризации — один сегмент на запись
            seq += 1
            duration = item.audio_duration_sec or 0.0
            end_ts = (
                self._srt_timestamp(duration) if duration > 0 else "00:00:01,000"
            )
            srt_lines.append(str(seq))
            srt_lines.append(f"00:00:00,000 --> {end_ts}")
            srt_lines.append(item.text)
            srt_lines.append("")

        return "\n".join(srt_lines)

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
        # Privacy mode gate (wave-36, HIGH B1): structured JSON export carries the full
        # transcript corpus to the IPC response / clipboard / file — withhold in privacy.
        if self._is_privacy_mode():
            return {"ok": False, "entries": 0, "chars": 0, "path": None, "reason": "privacy_mode_active"}

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

    @staticmethod
    def _neutralize_csv(val: str) -> str:
        """Defuse CSV formula injection by prepending a single-quote when the
        value starts with a formula-leading character (=, +, -, @, |, %).
        Wave-27 applied this to the speaker column; wave-31 mirrors it to the
        text and translation columns.
        """
        if val and val[0] in ('=', '+', '-', '@', '|', '%'):
            return "'" + val
        return val

    def handle_export_history_csv(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспорт истории в CSV формат."""
        # Privacy mode gate (wave-36, HIGH B1): CSV export writes the full transcript
        # corpus to the IPC response / clipboard / file — withhold in privacy mode.
        if self._is_privacy_mode():
            return {"ok": False, "entries": 0, "file": None, "reason": "privacy_mode_active"}

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
            # E2 (wave-31): neutralize formula-leading chars in text/translation columns.
            # E3 (wave-31): fix lang/duration columns — to_dict() uses source_lang and
            # audio_duration_sec, not the non-existent "lang"/"duration" keys.
            raw_text = item.get("text", "") or ""
            raw_translation = translation or ""
            lang_val = item.get("source_lang", "") or item.get("lang", "") or ""
            dur_raw = item.get("audio_duration_sec")
            if dur_raw is None:
                dur_raw = item.get("duration_sec")
            duration_val = str(dur_raw) if dur_raw is not None else ""
            writer.writerow([
                item.get("ts", ""),
                self._neutralize_csv(raw_text),
                self._neutralize_csv(raw_translation),
                lang_val,
                item.get("confidence", ""),
                duration_val,
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
        # Privacy mode gate (wave-35, MED B5): clipboard entries hold transcript text.
        if self._is_privacy_mode():
            return {"items": [], "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-35, MED B5): do not surface stored clipboard text.
        if self._is_privacy_mode():
            return {"ok": False, "reason": "privacy_mode_active"}

        history_id = str(params.get("history_id", "")).strip()
        if not history_id:
            raise RuntimeError("history_id обязателен")
        # wave-29 thread-safety: _clipboard_history — общий по ссылке список, в который
        # RecordingCoreService (поток завершения записи) делает .append() конкурентно с
        # этим IPC-обработчиком. Итерируем по снимку list(...) — в CPython это атомарная
        # копия под GIL, исключающая неконсистентную итерацию по мутируемому списку.
        # (handle_get_clipboard_history уже безопасен: slice [-limit:] копирует.)
        for entry in reversed(list(self._clipboard_history)):
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

    def cleanup_old_history_days(self, days: int) -> int:
        """Удаляет записи старше days дней; возвращает количество удалённых.

        Внутренний помощник, вызываемый как IPC-хендлером
        (handle_cleanup_old_history), так и PurgeScheduler.
        Вся каскадная очистка (версии, цепочки, семантический индекс,
        трекер воспроизведения, .md файлы) включена — DRY.
        """
        result = self.handle_cleanup_old_history({"older_than_days": days})
        return int(result.get("deleted_count", 0))

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

        # BUG2 fix (W1726): cascade chain cleanup for bulk-deleted items.
        # W1664 fixed the single-delete path (handle_delete_history_item) but
        # missed this bulk path — age-deleted items left phantom item_ids in
        # recording_chains.json.  Mirror the same pattern used in
        # handle_delete_history_item.
        if to_delete and self._recording_chain_mgr is not None:
            for item in to_delete:
                try:
                    self._recording_chain_mgr.remove_item_from_all_chains(item.id)
                except Exception:
                    logger.warning(
                        "recording_chain bulk cleanup failed for item %s", item.id, exc_info=True
                    )

        # W1773: cascade semantic-search embedding removal for bulk-deleted items.
        # The single-delete path (handle_delete_history_item) already calls
        # self._semantic_searcher.remove_item; mirror the same pattern here so
        # age-deleted transcripts don't leave orphan embedding rows in
        # embeddings.npy.
        if to_delete and self._semantic_searcher is not None:
            for item in to_delete:
                try:
                    self._semantic_searcher.remove_item(item.id)
                except Exception:
                    logger.warning(
                        "semantic_search bulk cleanup failed for item %s", item.id, exc_info=True
                    )

        # W1773: cascade playback-stats removal for bulk-deleted items.
        # Mirror handle_delete_history_item (F4 W1343 cascade).
        if to_delete and self._playback_tracker is not None:
            for item in to_delete:
                try:
                    self._playback_tracker.remove_stats(item.id)
                except Exception:
                    logger.warning(
                        "playback_tracker bulk cleanup failed for item %s", item.id, exc_info=True
                    )

        # W1762: стираем .md файлы транскриптов для каждой удалённой записи.
        # Зеркалирует логику single-delete (handle_delete_history_item).
        for item in to_delete:
            self._erase_transcript_md(item.ts, item.id)

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
            transcripts_deleted (int): количество удалённых .md файлов в transcripts/ (W1749)
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

        # --- 1b. W1749 CRITICAL-2: compact history.ndjson to physically erase transcript text.
        # Tombstoning alone only logically hides items; the cleartext remains in the NDJSON
        # file on disk.  compact_with_stats() rewrites history.ndjson dropping all tombstoned
        # rows, so the on-disk file no longer contains any transcript text after this call.
        try:
            self.store.compact_with_stats()
        except Exception:
            logger.warning("purge_all_data: compact failed — cleartext may remain in history.ndjson", exc_info=True)
            secondary_errors.append("compact")

        # --- 1b-2. wave-36 (MED B3): physically delete history_calendar_links.ndjson.
        # CalendarLinker stores {item_id → Calendar.app event title/id} in this StateStore
        # sidecar journal.  compact_with_stats() (step 1b) only *selectively rewrites* it,
        # keeping entries whose id is still active — but (a) it is wrapped in try/except above
        # so a failed compaction leaves the FULL journal on disk, and (b) even on success the
        # file itself survives (truncated content, not removed).  Event titles are user PII
        # (meeting names around real people).  An explicit unlink guarantees the journal is
        # gone after a privacy-wipe regardless of compaction outcome.  state_store.__init__
        # re-touches an empty file on next start, so the store stays consistent.
        try:
            (Path(self.store.data_dir) / "history_calendar_links.ndjson").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление history_calendar_links.ndjson не удалось", exc_info=True)
            secondary_errors.append("calendar_links")

        # --- 1c. W1749 CRITICAL-2 / W1771 GAP-1: delete ALL export artefacts in transcripts/.
        # Each transcription writes a timestamped Markdown file under <data_dir>/transcripts/
        # (TranscriptWriter), but the export handlers ALSO write sibling files there with
        # different extensions, each carrying the FULL cleartext transcript:
        #   *.md   / *.md.tmp  — TranscriptWriter Markdown (+ in-flight atomic-rename temp)
        #   *.html             — handle_export_html_report(save_to_file=True)  ← most PII-dense
        #   *.srt              — handle_export_history_srt(save_to_file=True)
        #   *.json             — handle_export_history(format=json, save_to_file=True)
        #   *.csv              — handle_export_history_csv(save_to_file=True)
        # W1766 #9 (MED) closed only the *.md.tmp gap; W1771 GAP-1 broadens the sweep to
        # every export extension (the prior *.md-only glob left report_*.html etc. behind).
        # Каждое расширение перечислено явным glob-литералом (видны статическому guard-у
        # audit_purge_coverage как покрытые — sibling-extension detection), затем
        # объединяются с общим обходом каталога — гарантия, что ни один файл (включая
        # будущие/неучтённые расширения) не переживёт purge.
        transcripts_deleted = 0
        try:
            transcripts_dir = Path(self.store.data_dir) / "transcripts"
            if transcripts_dir.is_dir():
                # Явные glob-семейства экспортных артефактов (покрывают каждое расширение
                # явно для статического guard-а) + полный обход каталога (страховка).
                export_files = set(transcripts_dir.glob("*.md"))
                export_files |= set(transcripts_dir.glob("*.md.tmp"))
                export_files |= set(transcripts_dir.glob("*.html"))
                export_files |= set(transcripts_dir.glob("*.srt"))
                export_files |= set(transcripts_dir.glob("*.json"))
                export_files |= set(transcripts_dir.glob("*.csv"))
                export_files |= {p for p in transcripts_dir.iterdir() if p.is_file()}
                for export_path in export_files:
                    try:
                        export_path.unlink(missing_ok=True)
                        transcripts_deleted += 1
                    except OSError:
                        logger.warning("purge_all_data: could not delete transcript file %s", export_path, exc_info=True)
                if len(export_files) > transcripts_deleted:
                    secondary_errors.append("transcripts")
        except Exception:
            logger.warning("purge_all_data: transcript directory deletion failed", exc_info=True)
            secondary_errors.append("transcripts")

        # --- 2. W1771 GAP-3: БЕЗУСЛОВНАЯ очистка версий транскрипций (true wipe).
        # Раньше здесь был cleanup_for_ids(current_active_ids) — он стирал версии
        # только тех записей, что попали в текущий снимок active. Версии уже
        # удалённых ранее (orphan) записей при этом ПЕРЕЖИВАЛИ purge, хотя
        # transcript_versions.ndjson содержит полный cleartext-текст всех версий.
        # clear_all() безусловно усекает весь NDJSON — ни одной версии (включая
        # orphan) не остаётся. Не зависит от наличия active-записей.
        if self._transcript_versions is not None:
            try:
                self._transcript_versions.clear_all()
            except Exception:
                logger.warning(
                    "purge_all_data: transcript_versions clear_all failed", exc_info=True
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

        # --- 8. W1765: очистить биометрику спикеров (speaker_fingerprints.json + speaker_aliases.json) ---
        # Критично для GDPR: 512-мерные голосовые отпечатки — биометрические ПДн;
        # псевдонимы — реальные имена людей. Оба файла должны исчезать при privacy-wipe.
        if self._speaker_manager is not None:
            try:
                self._speaker_manager.clear_all()
            except Exception:
                logger.warning(
                    "purge_all_data: speaker_manager.clear_all failed", exc_info=True
                )
                secondary_errors.append("speaker_fingerprints")

        # --- 9. W1765: очистить статистику воспроизведения (playback_stats.json) ---
        # play_count / total_listened_sec / last_played — косвенные ПДн (паттерн пользования).
        if self._playback_tracker is not None:
            try:
                self._playback_tracker.clear_all()
            except Exception:
                logger.warning(
                    "purge_all_data: playback_tracker.clear_all failed", exc_info=True
                )
                secondary_errors.append("playback")

        # --- 10. W1766 #7 (MED): очистить webhooks.json (HMAC-секреты) ---
        # webhooks.json хранит signing-секреты в открытом виде (0600) и переживает purge.
        # purge_all() захватывает _lock, очищает in-memory реестр + статистику,
        # перезаписывает файл пустым объектом и удаляет его с диска.
        if self._webhook_manager is not None:
            try:
                self._webhook_manager.purge_all()
            except Exception:
                logger.warning(
                    "purge_all_data: webhook_manager.purge_all failed", exc_info=True
                )
                secondary_errors.append("webhooks")

        # --- 11. W1766 #10 (MED): очистить Obsidian vault (.md файлы транскрипций) ---
        # Синхронизированные .md содержат полный STT-текст и выживают без этого шага.
        # purge_all_synced_files() — no-op если vault не настроен (безопасно).
        obsidian_deleted = 0
        if self._obsidian_sync is not None:
            try:
                obsidian_deleted = self._obsidian_sync.purge_all_synced_files()
            except Exception:
                logger.warning(
                    "purge_all_data: obsidian_sync.purge_all_synced_files failed", exc_info=True
                )
                secondary_errors.append("obsidian")

        # --- 12. W1767 #2 (HIGH): удалить migration backups (<data_dir>/backups/) ---
        # DataMigrator._create_backup() копирует history.ndjson и settings.json в
        # <data_dir>/backups/migration_backup_<ts>/.  Полные снапшоты истории
        # сохраняются там бессрочно и переживают purge без этого шага.
        try:
            import shutil as _shutil
            _backups_dir = Path(self.store.data_dir) / "backups"
            if _backups_dir.is_dir():
                _backup_count = sum(1 for _ in _backups_dir.iterdir() if _.is_dir())
                _shutil.rmtree(_backups_dir, ignore_errors=True)
                logger.info(
                    "purge_all_data: удалено %d migration backup директорий из %s",
                    _backup_count,
                    _backups_dir,
                )
        except Exception:
            logger.warning("purge_all_data: удаление backups/ не удалось", exc_info=True)
            secondary_errors.append("backups")

        # --- 13. W1767 #3 (HIGH): удалить shares/ и shares_index.json ---
        # SharingManager сохраняет полный текст транскрипций в <data_dir>/shares/
        # и индекс в <data_dir>/shares/shares_index.json.  Пакеты содержат STT-текст
        # и переживают purge без этого шага.
        try:
            import shutil as _shutil
            _shares_dir = Path(self.store.data_dir) / "shares"
            if _shares_dir.is_dir():
                _shutil.rmtree(_shares_dir, ignore_errors=True)
                logger.info("purge_all_data: удалена директория shares/")
            # wave-33 A1 (HIGH): rmtree удаляет файлы, но SharingManager._index —
            # RAM-копия с полным текстом транскрипций — переживает purge и продолжает
            # отдавать данные через get_shared. clear() сбрасывает in-memory индекс.
            if self._sharing_manager is not None:
                self._sharing_manager.clear()
        except Exception:
            logger.warning("purge_all_data: удаление shares/ не удалось", exc_info=True)
            secondary_errors.append("shares")

        # --- 13b. C2b: удалить tmp_meeting/ (временные диар-окна встречи) ---
        # _job_diar_window пишет WAV-окно (голос пользователя) в
        # <data_dir>/tmp_meeting/ и удаляет его в finally того же тика; после
        # краха backend посреди тика файл пережил бы purge без этого шага.
        try:
            import shutil as _shutil
            _tmp_meeting_dir = Path(self.store.data_dir) / "tmp_meeting"
            if _tmp_meeting_dir.is_dir():
                _shutil.rmtree(_tmp_meeting_dir, ignore_errors=True)
                logger.info("purge_all_data: удалена директория tmp_meeting/")
        except Exception:
            logger.warning("purge_all_data: удаление tmp_meeting/ не удалось", exc_info=True)
            secondary_errors.append("tmp_meeting")

        # --- 14. W1767 #7 (MED): очистить translation_cache.json ---
        # TranslationCache хранит хэш→переведённый_текст (LRU до 5000 записей).
        # Переводы транскрипций содержат PII и переживают purge без этого шага.
        # clear() захватывает _lock, очищает OrderedDict + записывает пустой JSON,
        # затем удаляет файл; атомарно через tmp→replace.
        if self._translation_cache is not None:
            try:
                self._translation_cache.clear()
                # После clear() файл translation_cache.json содержит пустой {}.
                # Физически удаляем его тоже, чтобы не оставлять даже пустой артефакт.
                _cache_path = Path(self.store.data_dir) / "translation_cache.json"
                _cache_path.unlink(missing_ok=True)
                logger.info("purge_all_data: translation_cache.json очищен и удалён")
            except Exception:
                logger.warning("purge_all_data: очистка translation_cache не удалась", exc_info=True)
                secondary_errors.append("translation_cache")

        # --- 15. W1767 #8 (MED): сбросить glossary в settings.json ---
        # translation_glossary хранится в settings.json как {"source": "target", ...}.
        # Словари могут содержать имена собственных и другие ПДн.
        # Используем store.save_settings для атомарной перезаписи файла.
        try:
            _current_settings = self.store.load_settings()
            if _current_settings.get("translation_glossary"):
                _current_settings["translation_glossary"] = {}
                self.store.save_settings(_current_settings)
                # Инвалидируем TTL-кэш настроек, если доступен
                if self._settings_svc is not None:
                    try:
                        self._settings_svc.invalidate_cache()
                    except Exception:
                        pass  # invalidate failure не блокирует purge
                logger.info("purge_all_data: translation_glossary сброшен в settings.json")
        except Exception:
            logger.warning("purge_all_data: сброс translation_glossary не удался", exc_info=True)
            secondary_errors.append("translation_glossary")

        # --- 16. W1767 #9 (MED): очистить vocabulary.json ---
        # VocabularyStore хранит пользовательский словарь STT (слова для Whisper hotword bias).
        # Словарь может содержать имена, термины и другие ПДн пользователя.
        # clear_all() захватывает _lock и удаляет vocabulary.json с диска.
        if self._vocabulary_store is not None:
            try:
                self._vocabulary_store.clear_all()
                logger.info("purge_all_data: vocabulary.json удалён")
            except Exception:
                logger.warning("purge_all_data: очистка vocabulary_store не удалась", exc_info=True)
                secondary_errors.append("vocabulary")

        # --- 17. W1767 #10 (MED): удалить settings_backups/ ---
        # SettingsBackup сохраняет rolling-снапшоты settings.json в
        # ~/Library/Application Support/KrabEar/settings_backups/ (или KRAB_EAR_SETTINGS_BACKUP_DIR).
        # Бэкапы могут содержать glossary и другие настройки с PII.
        # Путь читается из SettingsBackup.get_backup_dir() — уважает env override.
        if self._settings_backup is not None:
            try:
                import shutil as _shutil
                _sb_dir = self._settings_backup.get_backup_dir()
                if _sb_dir is not None and Path(_sb_dir).is_dir():
                    _shutil.rmtree(Path(_sb_dir), ignore_errors=True)
                    logger.info("purge_all_data: settings_backups/ удалён: %s", _sb_dir)
            except Exception:
                logger.warning("purge_all_data: удаление settings_backups/ не удалось", exc_info=True)
                secondary_errors.append("settings_backups")

        # ==================================================================
        # W1770: закрытие оставшихся privacy-purge пробелов (audit_purge_coverage).
        # Каждый шаг защищён собственным try/except → secondary_errors.append(...).
        # Прямой rmtree/unlink для файловых/директорных хранилищ; вызов метода
        # коллаборатора только там, где надо также очистить in-memory состояние.
        # ==================================================================
        # Резолв data_dir защищён: повреждённый store.data_dir (напр. не-строка)
        # не должен ронять весь purge — прямые файловые шаги пропускаются, но
        # collaborator-методы (знают свои пути сами) всё равно отрабатывают ниже.
        _data_dir: Path | None
        try:
            _data_dir = Path(self.store.data_dir)
        except Exception:
            logger.warning("purge_all_data: некорректный data_dir — прямые файловые шаги пропущены", exc_info=True)
            secondary_errors.append("data_dir")
            _data_dir = None

        # --- 18. W1770: удалить сырое аудио (audio/) ---
        # AudioRecorder/конвейер сохраняют сырые WAV/PCM записей под <data_dir>/audio/.
        # Сырое аудио — это голос пользователя (биометрия) и должно исчезать при wipe.
        try:
            import shutil as _shutil
            _audio_dir = _data_dir / "audio"
            if _audio_dir.is_dir():
                _shutil.rmtree(_audio_dir, ignore_errors=True)
                logger.info("purge_all_data: удалена директория audio/")
        except Exception:
            logger.warning("purge_all_data: удаление audio/ не удалось", exc_info=True)
            secondary_errors.append("audio")

        # --- 19. W1770: удалить сорванные записи (failed_recordings/) ---
        # При сбое транскрибации сырое аудио откладывается в <data_dir>/failed_recordings/.
        # Тот же класс данных, что и audio/ — голос пользователя.
        try:
            import shutil as _shutil
            _failed_dir = _data_dir / "failed_recordings"
            if _failed_dir.is_dir():
                _shutil.rmtree(_failed_dir, ignore_errors=True)
                logger.info("purge_all_data: удалена директория failed_recordings/")
        except Exception:
            logger.warning("purge_all_data: удаление failed_recordings/ не удалось", exc_info=True)
            secondary_errors.append("failed_recordings")

        # --- 20. W1770: удалить экспортированные транскрипции (exports/, auto_exports/, timeline/) ---
        # ExportScheduler/история пишут экспортированные транскрипции (SRT/CSV/MD/JSON/HTML)
        # и таймлайн-экспорты под эти директории. Все содержат полный STT-текст.
        # Имена директорий заданы литералами явно (а не циклом по переменной), чтобы
        # статический guard audit_purge_coverage мог их зачесть как покрытые.
        try:
            import shutil as _shutil
            if (_data_dir / "exports").is_dir():
                _shutil.rmtree(_data_dir / "exports", ignore_errors=True)
                logger.info("purge_all_data: удалена директория exports/")
        except Exception:
            logger.warning("purge_all_data: удаление exports/ не удалось", exc_info=True)
            secondary_errors.append("exports")
        try:
            import shutil as _shutil
            if (_data_dir / "auto_exports").is_dir():
                _shutil.rmtree(_data_dir / "auto_exports", ignore_errors=True)
                logger.info("purge_all_data: удалена директория auto_exports/")
        except Exception:
            logger.warning("purge_all_data: удаление auto_exports/ не удалось", exc_info=True)
            secondary_errors.append("auto_exports")
        try:
            import shutil as _shutil
            if (_data_dir / "timeline").is_dir():
                _shutil.rmtree(_data_dir / "timeline", ignore_errors=True)
                logger.info("purge_all_data: удалена директория timeline/")
        except Exception:
            logger.warning("purge_all_data: удаление timeline/ не удалось", exc_info=True)
            secondary_errors.append("timeline")

        # --- 21. W1770: удалить расписание экспорта (export_schedule.json) ---
        # ExportScheduler хранит конфигурацию авто-экспорта (включая output-пути,
        # привязанные к выгрузке PII-истории). Стираем вместе с экспортами.
        try:
            (_data_dir / "export_schedule.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление export_schedule.json не удалось", exc_info=True)
            secondary_errors.append("export_schedule")

        # --- 22. W1770: очистить метаданные сессий (sessions.ndjson) через SessionTracker ---
        # SessionTracker.clear_all() (#1605) сбрасывает in-memory буфер + активную сессию
        # и удаляет sessions.ndjson (имя устройства, время старта/конца, режим —
        # косвенные ПДн, раскрывают паттерны записи). Дополнительно явный unlink файла
        # под data_dir — гарантирует физическое удаление и зачёт статическим guard-ом.
        if self._session_tracker is not None:
            try:
                self._session_tracker.clear_all()
                logger.info("purge_all_data: sessions.ndjson очищен")
            except Exception:
                logger.warning("purge_all_data: session_tracker.clear_all не удался", exc_info=True)
                secondary_errors.append("sessions")
        if _data_dir is not None:
            try:
                (_data_dir / "sessions.ndjson").unlink(missing_ok=True)
            except Exception:
                logger.warning("purge_all_data: удаление sessions.ndjson не удалось", exc_info=True)
                secondary_errors.append("sessions")

        # --- 23. W1770: очистить коллекции (collections.json) через CollectionManager ---
        # CollectionManager.purge_all() (#1613) сбрасывает in-memory реестр и удаляет
        # collections.json — пользовательские имена/описания коллекций (free-text PII)
        # вместе со ссылками на item_id истории. Дополнительно явный unlink файла под
        # data_dir — гарантирует физическое удаление и зачёт статическим guard-ом.
        if self._collection_manager is not None:
            try:
                self._collection_manager.purge_all()
                logger.info("purge_all_data: collections.json очищен")
            except Exception:
                logger.warning("purge_all_data: collection_manager.purge_all не удался", exc_info=True)
                secondary_errors.append("collections")
        if _data_dir is not None:
            try:
                (_data_dir / "collections.json").unlink(missing_ok=True)
            except Exception:
                logger.warning("purge_all_data: удаление collections.json не удалось", exc_info=True)
                secondary_errors.append("collections")

        # --- 24. W1771 GAP-3: очистить журнал воспроизведения событий (event_replay.ndjson) ---
        # EventReplayManager сохраняет полные payload-ы событий (включая транскрипт-текст
        # в STT/translation событиях) для replay — и на диске (event_replay.ndjson), и
        # в in-memory кольцевом буфере. Раньше здесь был прямой unlink файла, но это
        # оставляло cleartext в RAM-кольце И ломало открытый файловый дескриптор
        # (запись идёт в режиме "w"/"a"). clear() усекает файл ИМЕННО через открытый
        # дескриптор (seek(0)+truncate(0)) И очищает кольцо — корректный privacy-wipe.
        if self._event_replay is not None:
            try:
                self._event_replay.clear()
                logger.info("purge_all_data: event_replay очищен (файл усечён + ring)")
            except Exception:
                logger.warning("purge_all_data: event_replay.clear() не удался", exc_info=True)
                secondary_errors.append("event_replay")
        # Подчищаем возможный compaction-temp (.tmp) — отдельный путь, дескриптором
        # не управляется; и сам .ndjson, если коллаборатор не подключён (fallback).
        if _data_dir is not None:
            try:
                (_data_dir / "event_replay.ndjson.tmp").unlink(missing_ok=True)
                if self._event_replay is None:
                    (_data_dir / "event_replay.ndjson").unlink(missing_ok=True)
            except Exception:
                logger.warning("purge_all_data: удаление event_replay.ndjson.tmp не удалось", exc_info=True)
                secondary_errors.append("event_replay")

        # --- 25. W1770: удалить аудит-трейл IPC (audit_*.ndjson) ---
        # AuditLogger пишет ежедневный NDJSON со списком IPC-вызовов и временными
        # метками. Значения параметров не пишутся (sensitive методы redact-ятся), но
        # сам трейл «что/когда вызывал пользователь» — косвенные usage-pattern ПДн.
        # Compliance-журнал privacy_audit.log (home-rooted) НЕ трогаем — это легальный
        # след самого purge.
        try:
            _audit_count = 0
            for _audit_path in _data_dir.glob("audit_*.ndjson"):
                try:
                    _audit_path.unlink(missing_ok=True)
                    _audit_count += 1
                except OSError:
                    logger.warning("purge_all_data: не удалось удалить %s", _audit_path, exc_info=True)
                    secondary_errors.append("audit_logs")
            if _audit_count:
                logger.info("purge_all_data: удалено %d audit_*.ndjson файлов", _audit_count)
        except Exception:
            logger.warning("purge_all_data: удаление audit_*.ndjson не удалось", exc_info=True)
            secondary_errors.append("audit_logs")

        # --- 26. W1770: удалить авто-глоссарий (auto_glossary.json) ---
        # AutoGlossary кэширует имена собственные и термины, извлечённые ИЗ истории
        # транскрипций (transcript-derived). Это PII пользователя.
        try:
            (_data_dir / "auto_glossary.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление auto_glossary.json не удалось", exc_info=True)
            secondary_errors.append("auto_glossary")

        # --- 27. W1770/wave-1770 HIGH: очистить историю поиска (в памяти + файл) ---
        # SearchHistoryManager хранит последние поисковые запросы пользователя (PII).
        # Прямой unlink файла не очищает in-memory _entries — они продолжают возвращаться
        # через get_recent_searches/get_popular_searches до рестарта. clear_search_history()
        # очищает и RAM, и файл атомарно.
        try:
            if self._search_history_mgr is not None:
                self._search_history_mgr.clear_search_history()
            else:
                # Fallback для случая когда late-inject не сработал (тесты без полного wiring).
                (_data_dir / "search_history.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: очистка search_history не удалась", exc_info=True)
            secondary_errors.append("search_history")

        # --- 28. W1770: удалить пользовательские горячие слова и legacy-словарь ---
        # hotwords.json — заданные пользователем триггер-слова (имена/термины);
        # vocabulary.txt — legacy STT-словарь (имена/термины). Оба содержат PII.
        try:
            (_data_dir / "hotwords.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление hotwords.json не удалось", exc_info=True)
            secondary_errors.append("hotwords")
        try:
            (_data_dir / "vocabulary.txt").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление vocabulary.txt не удалось", exc_info=True)
            secondary_errors.append("vocabulary_txt")

        # --- 29. W1770: удалить usage/recap/scheduler-стейты (usage-pattern ПДн) ---
        # usage_stats.json — ежедневная статистика (кол-во записей/длительность/слова);
        # recap_state.json — дата последней отправки дайджеста; scheduled_recordings.json —
        # будущие запланированные записи. Все раскрывают паттерны пользования.
        # Имена заданы литералами явно (а не циклом), чтобы статический guard их зачёл.
        try:
            (_data_dir / "usage_stats.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление usage_stats.json не удалось", exc_info=True)
            secondary_errors.append("usage_stats")
        try:
            (_data_dir / "recap_state.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление recap_state.json не удалось", exc_info=True)
            secondary_errors.append("recap_state")
        try:
            (_data_dir / "scheduled_recordings.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("purge_all_data: удаление scheduled_recordings.json не удалось", exc_info=True)
            secondary_errors.append("scheduled_recordings")

        # --- 30. W1770: удалить REST Bearer-токены (api_tokens.json) — СЕКРЕТЫ ---
        # RestAuth хранит SHA-256 хэши Bearer-токенов для REST API (порт 5005).
        # Это аутентификационные секреты и обязаны исчезать при privacy-wipe.
        try:
            (_data_dir / "api_tokens.json").unlink(missing_ok=True)
            logger.info("purge_all_data: api_tokens.json удалён (REST secrets)")
        except Exception:
            logger.warning("purge_all_data: удаление api_tokens.json не удалось", exc_info=True)
            secondary_errors.append("api_tokens")

        # --- 31. W1771 GAP-2: удалить пользовательские шаблоны (templates.json) ---
        # TemplateManager хранит свободный текст `text` без фильтрации — email-подписи
        # с реальными именами/телефонами, приветствия вокруг настоящих имён. Это PII
        # (ранее templates.json был ошибочно в allowlist как «app config»).
        # purge_all() удаляет файл под _lock; builtin-шаблоны зашиты в коде и не теряются.
        # Дополнительный явный unlink под data_dir — fallback + зачёт статическим guard-ом.
        if self._template_manager is not None:
            try:
                self._template_manager.purge_all()
                logger.info("purge_all_data: templates.json очищен")
            except Exception:
                logger.warning("purge_all_data: template_manager.purge_all не удался", exc_info=True)
                secondary_errors.append("templates")
        if _data_dir is not None:
            try:
                (_data_dir / "templates.json").unlink(missing_ok=True)
            except Exception:
                logger.warning("purge_all_data: удаление templates.json не удалось", exc_info=True)
                secondary_errors.append("templates")

        # --- 32. W1771 GAP-3: сбросить in-memory буфер live-субтитров (raw voice) ---
        # LiveSubsService накапливает base64 PCM 16 kHz system-audio в RAM до flush.
        # Это сырой голос (биометрия); файлового артефакта нет, поэтому только in-memory
        # reset() (без flush/STT/EventBus) гарантирует, что накопленное аудио стёрто.
        if self._live_subs_service is not None:
            try:
                self._live_subs_service.reset()
                logger.info("purge_all_data: live_subs PCM-буфер сброшен")
            except Exception:
                logger.warning("purge_all_data: live_subs_service.reset() не удался", exc_info=True)
                secondary_errors.append("live_subs")

        # --- 33. Wave-18 GAP-2: сбросить in-memory поисковые кэши StateStore ---
        # store.compact_with_stats() (шаг 1b) усекает history.ndjson на ДИСКЕ, но
        # StateStore держит RAM-слепки cleartext-текста ради ускорения поиска:
        #   _search_index (SearchIndex._texts) — полный текст ВСЕХ записей;
        #   _recent_search_index (+ signature) — последние ~4000 «стогов».
        # Без сброса полный текст истории переживает purge в памяти и снова
        # раскрывается через search_history до рестарта. reset_search_caches()
        # берёт store._lock и очищает оба кэша (#W18).
        # hasattr-guard: production StateStore всегда реализует метод; минимальные
        # тестовые fake-store (без поисковых кэшей) корректно пропускаются, не
        # засоряя secondary_errors (зеркалит is-not-None-guard остальных шагов).
        if hasattr(self.store, "reset_search_caches"):
            try:
                self.store.reset_search_caches()
                logger.info("purge_all_data: in-memory поисковые кэши StateStore сброшены")
            except Exception:
                logger.warning("purge_all_data: store.reset_search_caches() не удался", exc_info=True)
                secondary_errors.append("search_caches")

        # --- 34. Wave-18 GAP-1: очистить контекстную память STT (ContextMemory) ---
        # ContextMemory._texts — RAM-only deque последних 50 СЫРЫХ транскриптов
        # (полный PII), re-exposable через get_context_memory IPC. Файлового
        # артефакта нет, поэтому только in-memory clear() гарантирует, что
        # накопленный текст стёрт при wipe-all.
        if self._context_memory is not None:
            try:
                self._context_memory.clear()
                logger.info("purge_all_data: ContextMemory очищена (deque транскриптов)")
            except Exception:
                logger.warning("purge_all_data: context_memory.clear() не удался", exc_info=True)
                secondary_errors.append("context_memory")

        # --- 35. Wave-18 GAP-1: очистить in-memory историю буфера обмена ---
        # _clipboard_history — последние ~20 вставленных транскрипций (полный PII),
        # re-exposable через get_clipboard_history / repaste_item IPC. Это общий
        # список (передан по ссылке из BackendService), поэтому очищаем in-place
        # (.clear()), а не переприсваиваем — чтобы все владельцы ссылки увидели
        # опустошение.
        try:
            self._clipboard_history.clear()
            logger.info("purge_all_data: история буфера обмена очищена")
        except Exception:
            logger.warning("purge_all_data: очистка clipboard_history не удалась", exc_info=True)
            secondary_errors.append("clipboard_history")

        # --- 36. Wave-22: очистить in-memory реестр async-задач JobTracker ---
        # JobTracker._jobs хранит terminal-задачи (status=done/failed/cancelled),
        # содержащие полный текст транскрипций в items[].text и errors (в т.ч.
        # фрагменты транскриптов в сообщениях об ошибках). Задачи могут жить в
        # памяти до 1 часа (max_age_sec=3600) после завершения — без этого шага
        # PII переживает privacy-purge в RAM. Файлового артефакта нет; clear()
        # берёт _lock, устанавливает cancel_event для live-воркеров и опустошает
        # _jobs, _cancel_events, _evict_times, _cancel_events_ts за один проход.
        if self._job_tracker is not None:
            try:
                cleared = self._job_tracker.clear()
                logger.info("purge_all_data: JobTracker очищен (%d задач)", cleared)
            except Exception:
                logger.warning("purge_all_data: job_tracker.clear() не удался", exc_info=True)
                secondary_errors.append("job_tracker")

        # --- 37. Crypto-audit (2026-06-20): удалить ключ шифрования истории из Keychain.
        # Без этого выживший AES-256 ключ расшифровывает pre-purge бэкап history.ndjson
        # (Time Machine / iCloud / FS-снапшот) — ciphertext + живой ключ = весь текст.
        # delete_history_key — no-op без Keychain (KeystoreUnavailable на Linux/CI → не
        # ошибка purge). Сбрасываем ленивый крипто-кэш StateStore, чтобы следующая запись
        # (если шифрование оставлено включённым) сгенерировала НОВЫЙ ключ.
        try:
            from backend.crypto_keystore import delete_history_key, KeystoreUnavailable
            try:
                delete_history_key()
            except KeystoreUnavailable:
                pass  # нет Keychain (Linux/CI) → ключа нет → нечего удалять
            self.store._history_crypto_initialized = False
            self.store._history_crypto_instance = None
        except Exception:
            logger.warning(
                "purge_all_data: удаление ключа шифрования из Keychain не удалось", exc_info=True
            )
            secondary_errors.append("encryption_key")

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
                    "transcripts_deleted": transcripts_deleted,
                    "obsidian_deleted": obsidian_deleted,
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
                "transcripts_deleted": transcripts_deleted,
                "obsidian_deleted": obsidian_deleted,
                "semantic_purged": semantic_purged,
            },
        )
        # --- W1749 CRITICAL-1: loud error when purge is only partial ---
        if secondary_errors:
            self._push_error(
                code="history.purge_incomplete",
                message_debug=f"purge_all_data partial failure: {secondary_errors}",
                context={
                    "failed_steps": secondary_errors,
                    "data_dir": str(self.store.data_dir),
                },
            )

        logger.info(
            "purge_all_data: history=%d transcripts=%d chains=%d archive=%d bookmarks=%d calls=%d "
            "obsidian=%d semantic_purged=%s errors=%s",
            history_deleted,
            transcripts_deleted,
            chains_deleted,
            archive_deleted,
            bookmarks_deleted,
            call_sessions_deleted,
            obsidian_deleted,
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
            "transcripts_deleted": transcripts_deleted,
            "obsidian_deleted": obsidian_deleted,
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
        # Privacy mode gate (wave-35, LOW B8): summary echoes transcript content.
        if self._is_privacy_mode():
            return {"ok": False, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-35, HIGH B4): no transcript items over IPC.
        if self._is_privacy_mode():
            return {"items": [], "count": 0, "avg_confidence": 0.0, "reason": "privacy_mode_active"}

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
        # wave-38 MED: leaks top_speakers (speaker IDs), date_range, daily_counts
        # in privacy mode — reveals usage patterns. Gate mirrors get_recording_stats (wave-37).
        if self._is_privacy_mode():
            return {
                "total_items": 0, "total_duration_sec": 0.0, "total_words": 0,
                "avg_confidence": 0.0, "languages": {}, "date_range": None,
                "items_with_translation": 0, "items_with_diarization": 0,
                "avg_speakers": 0.0, "top_speakers": {}, "daily_counts": {},
                "reason": "privacy_mode_active",
            }
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
        # Privacy gate (wave-37, HIGH): full transcript corpus export must be
        # blocked while privacy mode is active — neither disk write nor inline
        # IPC content may leak.
        if self._is_privacy_mode():
            return {"file": None, "entries": 0, "content": "", "reason": "privacy_mode_active"}

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

        Privacy gate (wave-29): если privacy_mode_enabled → возвращает пустые списки.
        top_words/bigrams содержат текст транскрипций — утечка PII в privacy mode.
        Sibling guard: аналогично handle_get_keyword_cloud (уже гейтован ранее).

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
        if self._is_privacy_mode():
            return {
                "ok": True,
                "words": [],
                "bigrams": [],
                "top_words": [],
                "total_words": 0,
                "unique_words": 0,
                "vocabulary_richness": 0.0,
                "by_language": {},
                "reason": "privacy_mode_active",
            }

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
        # Privacy gate (wave-37, HIGH): raw history.ndjson contains full
        # cleartext transcripts — must not be copied to backups/ while
        # privacy mode is active.
        if self._is_privacy_mode():
            return {"backup_path": None, "size_mb": 0.0, "entries": 0, "reason": "privacy_mode_active"}

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

        # W1736: only restore from data_dir/backups/ — reject arbitrary backup_path.
        backups_root = Path(self.store.data_dir).resolve() / "backups"
        if backup_dir != backups_root and not backup_dir.is_relative_to(backups_root):
            raise RuntimeError(
                f"restore_history: backup_path {backup_dir!s} находится за пределами "
                f"разрешённой директории бекапов {backups_root!s}"
            )

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
        # Privacy mode gate (wave-35, HIGH B4): no transcript items over IPC.
        if self._is_privacy_mode():
            return {"ok": False, "reason": "privacy_mode_active"}

        threshold = float(params.get("similarity_threshold", 0.9))
        threshold = max(0.0, min(1.0, threshold))
        limit = int(params.get("limit", 500))

        items, _ = self.store.get_history_page_filtered(
            cursor=None,
            limit=limit,
            paste_status=None,
            translation_mode=None,
        )

        # wave-35: O(n^2) SequenceMatcher guard — cap the candidate set so a large
        # `limit` (user-controlled) cannot wedge the backend on a pairwise compare.
        MAX_DEDUP_ITEMS = 200
        if len(items) > MAX_DEDUP_ITEMS:
            return {"ok": False, "reason": "too many items for deduplication"}

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
        # Privacy mode gate (wave-36, HIGH B1): batch export writes the full transcript
        # corpus to a bundle directory in several formats — withhold in privacy mode.
        if self._is_privacy_mode():
            return {"dir": None, "files": {}, "errors": {}, "total_entries": 0, "reason": "privacy_mode_active"}

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
        # Privacy mode gate (wave-36, HIGH B1): the HTML report embeds the full transcript
        # corpus (most PII-dense export) — withhold in privacy mode (response + file).
        if self._is_privacy_mode():
            return {"ok": False, "html": "", "entries": 0, "chars": 0, "path": None, "reason": "privacy_mode_active"}

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

        # Optional «Сводка дня» card — the daily digest for the most recent day
        # present in the report (keeps the recap coherent with the report's
        # contents).  Privacy mode already short-circuited above, so this never
        # runs in privacy mode.  Best-effort: a digest failure must not break
        # the export.
        daily_digest_payload: dict[str, Any] | None = None
        if items_dicts:
            try:
                latest_ts = max(
                    (str(it.get("ts") or "") for it in items_dicts), default=""
                )
                recap_date = latest_ts[:10] or None
                from backend.daily_digest import DailyDigestGenerator

                digest = DailyDigestGenerator().generate_digest(
                    date_str=recap_date, store=self.store
                )
                if digest.total_recordings > 0:
                    daily_digest_payload = {
                        "date": digest.date,
                        "total_recordings": digest.total_recordings,
                        "total_duration_min": digest.total_duration_min,
                        "total_words": digest.total_words,
                        "languages_used": digest.languages_used,
                        "top_topics": digest.top_topics,
                        "highlights": digest.highlights,
                    }
            except Exception as exc:
                logger.debug("Сводка дня для HTML-отчёта пропущена: %s", exc)

        html_content = generator.generate_report(
            items=items_dicts, title=title, daily_digest=daily_digest_payload
        )

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
