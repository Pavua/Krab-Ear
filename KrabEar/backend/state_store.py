"""Локальное хранилище настроек и безлимитной истории Krab Ear.

Ключевые требования:
1) история хранится в append-only NDJSON;
2) удаление делается tombstone-записями;
3) статусы вставки обновляются отдельным журналом;
4) все операции записи защищены file-lock и атомарными replace.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Iterator

from core.parsing_utils import safe_json_loads

from .ipc_errors import IpcOperationalError
from .models import DEFAULT_SETTINGS, HistoryItem
from core.search_index import SearchIndex

logger = logging.getLogger("KrabEar.Backend.Store")

# Sentry KRAB-EAR-BACKEND-1V follow-up (2026-08-09): _lock() is one
# process-wide fcntl.flock(LOCK_EX), shared by ~50+ call sites. When a
# thread gets stuck WHILE HOLDING it (e.g. abandoned by the 180s IPC
# backstop-timeout but still blocked inside the syscall), every unrelated
# method that touches this lock — even ones with no expensive work of their
# own, like get_memory_stats via the post-handler privacy-gate read in
# cached_settings() — freezes too, piling up worker threads + IPC connection
# slots faster than the 180s backstop can reclaim them
# ("IPC: лимит 64 коннектов исчерпан"). See
# project_sentry_sweep_2026-08-05_ping_lock_contention.md for the full
# investigation.
#
# _LOCK_SLOW_WARN_SEC gates a diagnostic-only WARNING (who holds it / how
# long) that never changes behavior. _LOCK_ACQUIRE_TIMEOUT_SEC bounds actual
# ACQUISITION: a caller that can't get the flock within this deadline raises
# StateStoreLockTimeout instead of blocking forever. This affects ONLY
# callers who are WAITING — a thread that already holds the lock (e.g.
# migrate_history_encryption re-encrypting a large history under a single
# `with self._lock():`) is completely unaffected and keeps it for as long
# as it needs; the fix is bounding the QUEUE, not the WORK. 30s is chosen to
# be comfortably above any known legitimate hold (in-memory dict ops, small
# NDJSON appends/reads — observed sub-second even for large histories) while
# staying well under the 180s IPC backstop, so a genuinely stuck holder now
# frees up waiting connections in ~30s instead of ~180s-and-then-leaked.
_LOCK_SLOW_WARN_SEC = 2.0
_LOCK_ACQUIRE_TIMEOUT_SEC = 30.0
_LOCK_POLL_INTERVAL_SEC = 0.05


class StateStoreLockTimeout(IpcOperationalError):
    """Raised when ``_lock()`` cannot acquire the exclusive flock within the
    configured deadline (see ``_LOCK_ACQUIRE_TIMEOUT_SEC`` above).

    Subclasses ``IpcOperationalError`` so ``handle_request`` treats it as a
    genuine operational failure (loud: ``internal_error`` + Sentry), not a
    normal validation outcome — this is a stuck/contended lock, not a bad
    parameter.
    """


class StateStore:
    """Фасад для настроек и истории backend-сервиса."""

    def __init__(
        self,
        data_dir: Path,
        compact_threshold_bytes: int = 25 * 1024 * 1024,
        lock_acquire_timeout_sec: float = _LOCK_ACQUIRE_TIMEOUT_SEC,
    ) -> None:
        self.data_dir = data_dir
        self.compact_threshold_bytes = compact_threshold_bytes
        self._lock_acquire_timeout_sec = lock_acquire_timeout_sec

        self.settings_path = self.data_dir / "settings.json"
        self.history_path = self.data_dir / "history.ndjson"
        self.tombstones_path = self.data_dir / "history_tombstones.ndjson"
        # W1756: постоянный реестр удалённых id — пережи́вает компактирование.
        # compact() дописывает сюда tombstone-id перед очисткой tombstones_path,
        # чтобы import_history_ndjson мог блокировать resurrection даже после compact.
        self.purged_ids_path = self.data_dir / "history_purged_ids.ndjson"
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
                self.purged_ids_path,
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

        # D1 (wave-35 MED): O(1) in-memory existence set для set_paste_status /
        # delete_history_item.  До этого фикса существование item-а проверялось
        # через _id_exists_unlocked, который делал два O(n) скана: _load_deleted_ids
        # + _iter_history_items — оба под глобальным flock.  Теперь для check
        # достаточно одного hash-lookup.
        #
        # Инварианты:
        #   • add_history_item     → добавляет id в _active_ids
        #   • delete_history_item  → удаляет id из _active_ids
        #   • _compact_unlocked    → перестраивает _active_ids по финальному active-списку
        #   • reset_search_caches  → не трогает _active_ids (остаются корректны после purge)
        #
        # Заполняется лениво при первом обращении (lazy init), чтобы не тормозить
        # конструктор StateStore, который вызывается при каждом старте backend-а
        # до того, как lock-файл вообще прочитан.
        self._active_ids: set[str] | None = None

        # Шифрование истории (per-line AES-256-GCM).
        # Инициализируется лениво при первом обращении к _history_crypto_unlocked().
        # None означает "ещё не инициализировано", что отличается от "не доступно"
        # (для последнего используем False-sentinel после первой попытки).
        self._history_crypto_initialized: bool = False
        self._history_crypto_instance = None  # HistoryCrypto | None

        # fix/statestore-lock-reentrancy-deadlock: per-thread reentrancy guard
        # for _lock() (см. докстринг _lock ниже) — НЕ файловый lock, чисто
        # in-memory защита словарей depth/fileobj. Прецедент: core/mlx_lock.py
        # (RLock — реентерабельный по той же причине).
        self._lock_reentry_guard = threading.Lock()
        self._lock_depth: dict[int, int] = {}
        self._lock_fileobj: dict[int, Any] = {}
        # Slow-lock diagnostics (see _LOCK_SLOW_WARN_SEC above) — guarded by
        # the same _lock_reentry_guard as depth/fileobj above.
        self._lock_holder_label: str | None = None
        self._lock_holder_since: float | None = None

        # Phase B.2 — error_bus late-injection

    def _get_history_crypto(self):
        """Возвращает ``HistoryCrypto`` или ``None`` (ленивая инициализация).

        Инициализируется один раз при первом вызове.  Если шифрование
        отключено в настройках или Keychain недоступен — возвращает None.

        🔴 Вызывается из _maybe_encrypt/_maybe_decrypt, которые в свою очередь
        вызываются из _append_history_ndjson/_read_history_ndjson_unlocked, уже
        работающих под _lock().  Поэтому load_settings() (которая захватывает
        тот же lock) здесь вызвать нельзя — deadlock.  Вместо этого читаем
        settings.json напрямую без блокировки (atomic read, файл маленький).
        """
        if not self._history_crypto_initialized:
            self._history_crypto_initialized = True
            try:
                enabled = self._read_encryption_flag_unlocked()
                if enabled:
                    from backend.history_crypto import build_history_crypto
                    self._history_crypto_instance = build_history_crypto()
                # else: None — default off, no-op
            except Exception:
                logger.exception("StateStore: ошибка инициализации history crypto")
        return self._history_crypto_instance

    def _read_encryption_flag_unlocked(self) -> bool:
        """Читает флаг history_encryption_enabled из settings.json без захвата lock.

        Используется только из _get_history_crypto() (вызывается под lock).
        Безопасно: settings.json пишется атомарно через tmp + replace,
        поэтому неполные записи не встречаются.
        """
        try:
            if not self.settings_path.exists():
                return False
            payload = safe_json_loads(
                self.settings_path.read_text(encoding="utf-8"),
                default=None,
                context="settings.json (encryption flag check)",
            )
            if isinstance(payload, dict):
                return bool(payload.get("history_encryption_enabled", False))
        except Exception:
            logger.exception("StateStore._read_encryption_flag_unlocked: ошибка чтения")
        return False

    def _maybe_encrypt(self, json_str: str) -> str:
        """Шифрует строку JSON если шифрование включено и доступно.

        Если шифрование выключено или HistoryCrypto недоступен — возвращает
        json_str без изменений (поведение по умолчанию, байт-идентично текущему).
        """
        crypto = self._get_history_crypto()
        if crypto is None:
            return json_str
        try:
            return crypto.encrypt_line(json_str)
        except Exception as exc:
            # Шифрование включено, но упало → НЕ молчим: пишем plaintext (данные
            # не теряем), но громко уведомляем через error_bus — иначе это была бы
            # незаметная security-регрессия (пользователь думает, что зашифровано).
            logger.exception("StateStore._maybe_encrypt: ошибка шифрования, пишем plaintext")
            self._push_error(
                "history.encrypt_fail",
                f"encrypt_line failed: {type(exc).__name__}: {exc}",
                severity="error",
            )
            return json_str

    def _maybe_decrypt(self, raw_line: str) -> str:
        """Дешифрует строку если она зашифрована (определяется по SENTINEL).

        Plaintext строки проходят без изменений — это обеспечивает
        безопасное сосуществование открытых и зашифрованных записей в одном файле.
        """
        from backend.history_crypto import HistoryCrypto
        if not HistoryCrypto.is_encrypted(raw_line):
            return raw_line
        crypto = self._get_history_crypto()
        if crypto is None:
            # Ключ недоступен — не можем расшифровать
            logger.error(
                "StateStore._maybe_decrypt: зашифрованная строка найдена, "
                "но crypto недоступен — строка пропущена"
            )
            return ""  # пустая строка → safe_json_loads вернёт None → строка пропущена
        try:
            return crypto.decrypt_line(raw_line)
        except Exception:
            logger.exception("StateStore._maybe_decrypt: ошибка расшифровки — строка пропущена")
            return ""  # аналогично

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
    def _lock(self, timeout_sec: float | None = None) -> Iterator[None]:
        """Глобальный lock для журналов истории и настроек.

        ``timeout_sec``: опциональный override бюджета ожидания ИМЕННО для
        этого вызова (спека 2026-08-12 settings-read-nonblocking — короткий
        read-path бюджет для ``load_settings()`` из ``SettingsService.
        cached_settings()``). ``None`` (по умолчанию) — обычное поведение,
        используется общий ``self._lock_acquire_timeout_sec`` инстанса, как
        и для всех остальных ~50 call sites этого метода — их поведение
        этим параметром не затрагивается.

        Реентерабелен ПО ТРЕДУ (per-thread depth-counter): повторный вход с
        того же треда — no-op поверх уже взятого лока, вместо самозаклина на
        fcntl.flock (у каждого open() — свой open file description; flock не
        привязан к треду, поэтому вложенный вызов с того же треда раньше
        блокировался навечно). Реальный OS-level flock физически берётся и
        отпускается РОВНО ОДИН РАЗ — на самом внешнем входе/выходе — так что
        кросс-тредовая и кросс-процессная эксклюзивность (см.
        test_state_store_lock_invariants.py) не меняется.

        Откат при сбое фазы захвата (адверсариальный гейт Sonnet+Fable,
        HIGH): если touch()/open()/flock() бросает исключение (ENOSPC/
        EMFILE/EACCES — реалистично, у проекта есть DiskSpaceMonitor именно
        под low-disk сценарии) — инкремент depth-счётчика, уже сделанный
        выше, откатывается под тем же guard-локом, а частично открытый файл
        (open() прошёл, flock() упал) закрывается БЕЗ вызова LOCK_UN (лок
        не был взят — unlock незанятого лока не делаем). Без этого отката
        КАЖДЫЙ последующий вызов _lock() с этого же треда молча решал бы,
        что лок уже держится (depth != 0), и навсегда пропускал бы реальный
        fcntl.flock — тихий обход взаимоисключения хуже громкого дедлока,
        который чинил сам реентерабельный фикс.

        Sentry KRAB-EAR-BACKEND-1V follow-up (2026-08-09): захват ограничен
        по времени (``_lock_acquire_timeout_sec``, дефолт
        ``_LOCK_ACQUIRE_TIMEOUT_SEC``) через опрос ``LOCK_EX | LOCK_NB`` в
        цикле вместо блокирующего ``LOCK_EX`` — иначе один навсегда
        застрявший держатель (например абандонённый 180с IPC-backstop'ом,
        но всё ещё живой поток) вечно блокирует ЛЮБОЙ другой вызов, который
        копит рабочие потоки и IPC-коннекты быстрее, чем backstop успевает
        их освобождать. Таймаут действует ТОЛЬКО на ожидание захвата — уже
        держащий лок поток (например миграция шифрования всей истории под
        одним ``with self._lock():``) им не ограничен и держит лок сколько
        нужно; при истечении дедлайна бросается ``StateStoreLockTimeout``
        (тот же путь отката, что и для ENOSPC/EMFILE выше).
        """
        tid = threading.get_ident()
        with self._lock_reentry_guard:
            depth = self._lock_depth.get(tid, 0)
            acquired_here = depth == 0
            self._lock_depth[tid] = depth + 1
        if acquired_here:
            label = self._lock_caller_label()
            # timeout_sec=None → обычное поведение (общий инстанс-таймаут);
            # явный override используется только вызывающей стороной, которая
            # его передала (см. докстринг выше) — все остальные call sites
            # не видят разницы.
            effective_timeout = (
                self._lock_acquire_timeout_sec if timeout_sec is None else timeout_sec
            )
            with self._lock_reentry_guard:
                holder_label = self._lock_holder_label
                holder_since = self._lock_holder_since
            if holder_label is not None and holder_since is not None:
                held_for = time.monotonic() - holder_since
                if held_for >= _LOCK_SLOW_WARN_SEC:
                    logger.warning(
                        "StateStore._lock(): %s ждёт эксклюзивный flock — уже держит %s %.1fс",
                        label, holder_label, held_for,
                        extra={"waiter": label, "holder": holder_label, "held_for_sec": held_for},
                    )
            wait_start = time.monotonic()
            deadline = wait_start + effective_timeout
            lock_file = None
            try:
                self.lock_path.touch(exist_ok=True)
                lock_file = self.lock_path.open("r+", encoding="utf-8")
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            with self._lock_reentry_guard:
                                current_holder = self._lock_holder_label
                            raise StateStoreLockTimeout(
                                f"StateStore._lock(): {label} не получил эксклюзивный "
                                f"flock за {effective_timeout:.2f}с "
                                f"(последний известный держатель: {current_holder or 'неизвестно'})"
                            )
                        time.sleep(_LOCK_POLL_INTERVAL_SEC)
            except BaseException:
                with self._lock_reentry_guard:
                    depth = self._lock_depth[tid] - 1
                    if depth == 0:
                        del self._lock_depth[tid]
                    else:
                        self._lock_depth[tid] = depth
                    self._lock_fileobj.pop(tid, None)
                if lock_file is not None:
                    try:
                        lock_file.close()
                    except OSError:
                        pass
                raise
            wait_elapsed = time.monotonic() - wait_start
            if wait_elapsed >= _LOCK_SLOW_WARN_SEC:
                logger.warning(
                    "StateStore._lock(): %s ждал эксклюзивный flock %.1fс",
                    label, wait_elapsed,
                    extra={"waiter": label, "wait_sec": wait_elapsed},
                )
            with self._lock_reentry_guard:
                self._lock_fileobj[tid] = lock_file
                self._lock_holder_label = label
                self._lock_holder_since = time.monotonic()
        try:
            yield
        finally:
            held_label: str | None = None
            held_since: float | None = None
            with self._lock_reentry_guard:
                depth = self._lock_depth[tid] - 1
                if depth == 0:
                    del self._lock_depth[tid]
                    lock_file = self._lock_fileobj.pop(tid)
                    release_now = True
                    held_label = self._lock_holder_label
                    held_since = self._lock_holder_since
                    self._lock_holder_label = None
                    self._lock_holder_since = None
                else:
                    self._lock_depth[tid] = depth
                    release_now = False
            if release_now:
                if held_since is not None:
                    held_total = time.monotonic() - held_since
                    if held_total >= _LOCK_SLOW_WARN_SEC:
                        logger.warning(
                            "StateStore._lock(): %s держал эксклюзивный flock %.1fс",
                            held_label, held_total,
                            extra={"holder": held_label, "held_sec": held_total},
                        )
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()

    @staticmethod
    def _lock_caller_label() -> str:
        """Best-effort ``file:line:func`` label of the real ``with self._lock():`` call site.

        Diagnostic-only (see _LOCK_SLOW_WARN_SEC) — never allowed to affect
        locking itself. sys._getframe(3) from here reaches past this method's
        own frame, past the _lock() generator body that called it, and past
        contextlib's __enter__ (which resumes the generator via next()) to
        land on the actual caller.
        """
        try:
            frame = sys._getframe(3)
            filename = frame.f_code.co_filename.rsplit("/", 1)[-1]
            return f"{filename}:{frame.f_lineno}:{frame.f_code.co_name}"
        except Exception:
            return "unknown"

    def reset_search_caches(self) -> None:
        """Сбрасывает все in-memory поисковые кэши (privacy-purge / wipe-all).

        ``handle_purge_all_data`` тромбонит + компактит NDJSON на диске, но это
        НЕ затрагивает RAM-резидентные слепки cleartext-текста, которые StateStore
        держит для ускорения поиска:
          - ``_search_index`` (SearchIndex._texts) — полный текст ВСЕХ записей,
            построенный на fast-path ``search_history``;
          - ``_recent_search_index`` (+ signature) — последние ~4000 «стогов»
            (HistoryItem + конкатенированный текст) для быстрого поиска.
        Без этого сброса полный cleartext истории переживает purge в памяти и
        вновь раскрывается через ``search_history`` до следующего рестарта.

        Берёт ``_lock`` (тот же файловый замок, что и build-путь индекса) —
        сериализация против параллельной перестройки кэшей во время purge.
        """
        with self._lock():
            self._search_index.clear()
            self._recent_search_index = []
            self._recent_search_index_signature = None

    def load_settings(self, lock_timeout_sec: float | None = None) -> dict[str, Any]:
        """Читает настройки и дополняет их дефолтами.

        ``lock_timeout_sec``: опциональный override read-path бюджета
        ожидания flock (спека 2026-08-12 settings-read-nonblocking) — см.
        ``_lock(timeout_sec=...)``. ``None`` (по умолчанию) — обычное
        поведение, инстанс-таймаут. При истечении заданного бюджета бросает
        ``StateStoreLockTimeout``; вызывающая сторона (``SettingsService.
        cached_settings()``) обязана сама решить fail-closed фоллбэк — здесь
        никакого тихого дефолта нет НАМЕРЕННО (иначе теряется разница между
        «настроек нет» и «настроек не удалось прочитать вовремя»).
        """
        with self._lock(timeout_sec=lock_timeout_sec):
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
        from core.atomic_io import atomic_write_text
        unique_words = sorted(list(set(w.strip() for w in words if w.strip())))
        with self._lock():
            atomic_write_text(self.vocabulary_path, "\n".join(unique_words) + "\n")

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
        # W1643 F2 HIGH: fields previously silently dropped on the standard write path
        tags: list | None = None,
        favorite: bool = False,
        audio_path: str = "",
        is_protected: bool = False,
        reasoning: str | None = None,
        action_items: list | None = None,
        decisions: list | None = None,
        questions: list | None = None,
        privacy_mode: bool = False,
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
            tags=tags,
            favorite=favorite,
            audio_path=audio_path,
            is_protected=is_protected,
            reasoning=reasoning,
            action_items=action_items,
            decisions=decisions,
            questions=questions,
            privacy_mode=privacy_mode,
        )
        try:
            with self._lock():
                self._append_history_ndjson(item.to_dict())
                # D1 (wave-35): keep in-memory id set consistent after write.
                if self._active_ids is not None:
                    self._active_ids.add(item.id)
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

    def _id_exists_unlocked(self, item_id: str) -> bool:
        """Проверяет, существует ли активная запись с данным id (без захвата lock).

        Лёгкая проверка: итерирует основной журнал + отфильтровывает tombstone-ids.
        Не применяет delta-override'ы (status/tags/etc) — нам нужен только факт
        наличия строки в history.ndjson минус логически удалённые записи.
        """
        deleted = self._load_deleted_ids_unlocked()
        if item_id in deleted:
            return False
        for item in self._iter_history_items_unlocked():
            if item.id == item_id:
                return True
        return False

    def _ensure_active_ids_unlocked(self) -> set[str]:
        """Возвращает in-memory множество активных item id.

        При первом вызове (или после сброса через _compact_unlocked) выполняет
        единственный O(n) скан NDJSON и кэширует результат в self._active_ids.
        Все последующие вызовы — O(1).  Должен вызываться только под _lock().
        """
        if self._active_ids is None:
            self._active_ids = {item.id for item in self._load_active_items_unlocked()}
        return self._active_ids

    def set_paste_status(self, item_id: str, paste_status: str) -> bool:
        """Записывает обновление статуса вставки отдельным append-журналом.

        Перед записью проверяет, что запись с данным id существует в активном
        хранилище. Это защищает от спама junk-ids, которые раздувают
        history_status.ndjson без пользы (каждый read — O(n) по журналу).

        D1 (wave-35 MED): проверка существования переключена с O(n)
        _id_exists_unlocked (два NDJSON-скана) на O(1) hash-lookup
        в _ensure_active_ids_unlocked() (один scan при первом обращении,
        далее in-memory set).
        """
        clean_id = item_id.strip()
        if not clean_id:
            return False

        payload = {"id": clean_id, "paste_status": paste_status.strip() or "failed"}
        with self._lock():
            if clean_id not in self._ensure_active_ids_unlocked():
                return False
            self._append_ndjson(self.status_path, payload)
        return True

    def delete_history_item(self, item_id: str) -> bool:
        """Логически удаляет запись через tombstone.

        Перед записью tombstone проверяет, что запись существует в активном
        хранилище. Это блокирует спам junk-ids, раздувающих tombstones.ndjson.
        """
        clean_id = item_id.strip()
        if not clean_id:
            return False
        with self._lock():
            if not self._id_exists_unlocked(clean_id):
                return False
            self._append_tombstone_ndjson({"id": clean_id})
            # D1 (wave-35): keep in-memory id set consistent after tombstone.
            if self._active_ids is not None:
                self._active_ids.discard(clean_id)
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
            if filter_from_ts is not None and self._ts_to_naive_utc_str(item.ts) < filter_from_ts:
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
                if filter_from_ts is not None and self._ts_to_naive_utc_str(item.ts) < filter_from_ts:
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
            if filter_from_ts is not None and self._ts_to_naive_utc_str(item.ts) < filter_from_ts:
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
    def _ts_to_naive_utc_str(ts: str) -> str:
        """Нормализует ISO-timestamp в tz-naive UTC строку для лексикографического сравнения.

        Обеспечивает обратную совместимость:
        - tz-naive  «2026-05-29T12:00:00»        → возвращается без изменений
        - tz-aware  «2026-05-29T12:00:00+00:00»  → «2026-05-29T12:00:00»

        Все старые записи в NDJSON — tz-naive.  Новые (W1671+) — UTC +00:00.
        Приведение обоих к одному формату позволяет смешанные сравнения.
        """
        if ts.endswith("+00:00"):
            return ts[:-6]
        if ts.endswith("Z"):
            return ts[:-1]
        return ts

    @staticmethod
    def _parse_ts_to_naive_utc(ts: str) -> datetime:
        """Парсит ISO-timestamp в tz-naive UTC datetime.

        Tz-aware значения конвертируются в UTC, затем tzinfo убирается.
        Tz-naive значения считаются уже UTC (legacy-совместимость).
        """
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return datetime.min
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

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
        if from_ts is not None and StateStore._ts_to_naive_utc_str(item.ts) < from_ts:
            return False
        if to_ts is not None and StateStore._ts_to_naive_utc_str(item.ts) > to_ts:
            return False
        return True

    def import_history_ndjson(self, path: Path) -> dict[str, int]:
        """Импортирует записи истории из NDJSON без дублей по ``id``.

        W1471 F2 (восстановлено в W1756 — тело было silently reverted в 7536be71):
        Множество пропуска включает как активные id, так и tombstone-id
        (``_load_deleted_ids_unlocked``), чтобы удалённые записи не могли
        воскреснуть после компактирования (когда tombstones очищены, а id
        уже не числится в ``_iter_history_items_unlocked``).

        Exploit sequence без фикса:
          1. удаляем запись → tombstone записан; строка ещё есть в history.ndjson.
          2. compact() → history.ndjson перезаписан только активными, tombstones очищены.
          3. import того же NDJSON → item.id не в known_ids → resurrection.
        """
        source_path = path.expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            raise RuntimeError("Файл импорта не найден")

        with self._lock():
            known_ids = (
                {item.id for item in self._iter_history_items_unlocked()}
                | self._load_deleted_ids_unlocked()
            )
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

                self._append_history_ndjson(item.to_dict())
                known_ids.add(item.id)
                # D1 (wave-36 LOW): keep in-memory id set consistent with items
                # added via import so that subsequent set_paste_status calls on
                # imported ids don't get a false-negative from the O(1) check.
                if self._active_ids is not None:
                    self._active_ids.add(item.id)
                imported += 1

        return {"imported": imported, "skipped": skipped, "errors": errors}

    # Максимальное число строк в history_status.ndjson перед принудительным
    # компактированием. При спаме junk-ids журнал растёт без ограничений и
    # делает каждый _load_status_overrides_unlocked() дорогим O(n) сканом.
    _STATUS_JOURNAL_LINE_CAP = 50_000

    def maybe_compact(self) -> bool:
        """Запускает компактирование при превышении порога размера файла.

        Дополнительно проверяет, не превысил ли журнал статусов
        history_status.ndjson лимит строк (_STATUS_JOURNAL_LINE_CAP = 50 000).
        Это защита от атаки спамом set_paste_status с уникальными ids:
        при нормальном использовании журнал содержит O(active_count) строк;
        при спаме он неограниченно растёт, замедляя каждый history-read.
        """
        with self._lock():
            try:
                current_size = self.history_path.stat().st_size
            except FileNotFoundError:
                return False

            # Принудительное компактирование при перегрузке журнала статусов.
            status_lines = self._count_ndjson_entries_unlocked(self.status_path)
            if status_lines > self._STATUS_JOURNAL_LINE_CAP:
                logger.warning(
                    "history_status.ndjson exceeds line cap, forcing compaction",
                    extra={"status_lines": status_lines, "cap": self._STATUS_JOURNAL_LINE_CAP},
                )
                self._compact_unlocked()
                return True

            if current_size <= self.compact_threshold_bytes:
                return False

            self._compact_unlocked()
            return True

    def maybe_compact_async(
        self,
        job_tracker: "Any | None" = None,
    ) -> "str | None":
        """Запускает maybe_compact() в daemon-потоке и немедленно возвращает управление.

        Используется на startup и в scheduled paths вместо прямого вызова
        maybe_compact(), чтобы не блокировать IPC-цикл на время 75 MB I/O.

        Args:
            job_tracker: опциональный JobTracker для отслеживания состояния задачи.
                Если None — задача не регистрируется в JobTracker.

        Returns:
            job_id если job_tracker передан и порог превышен, иначе None.
        """
        # Быстрая проверка размера — без захвата file-lock.
        try:
            current_size = self.history_path.stat().st_size
        except FileNotFoundError:
            return None

        if current_size <= self.compact_threshold_bytes:
            return None

        job_id: "str | None" = None
        if job_tracker is not None:
            job_id = job_tracker.create_job(total_files=1)

        def _worker() -> None:
            if job_tracker is not None and job_id is not None:
                job_tracker.update(job_id, status="running", current_stage="compact")
            try:
                self.maybe_compact()
                if job_tracker is not None and job_id is not None:
                    job_tracker.mark_done(job_id, items=[], errors=[])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "maybe_compact_async failed",
                    extra={"error": str(exc)},
                )
                if job_tracker is not None and job_id is not None:
                    job_tracker.mark_failed(job_id, str(exc))

        t = threading.Thread(target=_worker, daemon=True, name="StateStore-compact-async")
        t.start()
        return job_id

    def compact(self) -> bool:
        """Явная команда компактирования истории."""
        self.compact_with_stats()
        return True

    def compact_async(
        self,
        job_tracker: "Any | None" = None,
    ) -> "str | None":
        """Запускает полное компактирование (compact_with_stats) в daemon-потоке.

        В отличие от maybe_compact_async() — всегда запускает компактирование
        независимо от текущего размера файла.  Предназначен для IPC-вызовов,
        которым нужна немедленная отдача управления без ожидания I/O.

        Args:
            job_tracker: опциональный JobTracker для отслеживания состояния задачи.

        Returns:
            job_id если job_tracker передан, иначе None.
        """
        job_id: "str | None" = None
        if job_tracker is not None:
            job_id = job_tracker.create_job(total_files=1)

        def _worker() -> None:
            if job_tracker is not None and job_id is not None:
                job_tracker.update(job_id, status="running", current_stage="compact")
            try:
                self.compact_with_stats()
                if job_tracker is not None and job_id is not None:
                    job_tracker.mark_done(job_id, items=[], errors=[])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "compact_async failed",
                    extra={"error": str(exc)},
                )
                if job_tracker is not None and job_id is not None:
                    job_tracker.mark_failed(job_id, str(exc))

        t = threading.Thread(target=_worker, daemon=True, name="StateStore-compact-async-full")
        t.start()
        return job_id

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

        now_naive = datetime.now()
        threshold_dt = now_naive - timedelta(days=days)

        with self._lock():
            active = self._load_active_items_unlocked()

        to_delete = [
            item
            for item in active
            if item.ts and self._parse_ts_to_naive_utc(item.ts) < threshold_dt
        ]

        oldest_age_days = None
        if active:
            oldest_ts_str = min(
                (item.ts for item in active if item.ts), default=None
            )
            if oldest_ts_str:
                oldest_dt = self._parse_ts_to_naive_utc(oldest_ts_str)
                oldest_age_days = (now_naive - oldest_dt).days

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
                self.action_items_path,
                self.calendar_links_path,
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
                    oldest_dt = self._parse_ts_to_naive_utc(oldest_ts_str)
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

        # Use UTC for today_iso so it matches item.ts (stored as UTC ISO).
        # Keep as naive (remove tzinfo) so comparison with _parse_ts_to_naive_utc results works.
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        today_iso = now_naive.date().isoformat()
        last_24h_threshold = now_naive - timedelta(hours=24)

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
            item_dt = self._parse_ts_to_naive_utc(item.ts) if item.ts else None
            if item_dt is not None and item_dt != datetime.min and item_dt >= last_24h_threshold:
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
        """Возвращает количество активных (не удаленных) записей.

        Переиспользует инкрементально поддерживаемый ``_active_ids`` (см.
        ``_ensure_active_ids_unlocked``) вместо полного O(n) скана
        history.ndjson + delta-журналов на КАЖДЫЙ вызов — этот метод
        вызывается из ``handle_ping`` на каждый 3с heartbeat HealthMonitor.
        """
        with self._lock():
            return len(self._ensure_active_ids_unlocked())

    def _compact_unlocked(self) -> None:
        """Собирает активные записи в новый основной журнал и очищает дельты.

        После компактирования вызывает опциональный хук ``_on_compact_hook``
        (если задан через late-injection) с множеством активных item_id-ов,
        что позволяет внешним сервисам (напр. TranscriptVersionManager) удалить
        orphaned-данные для tombstone-записей.
        """
        active = self._load_active_items_unlocked()
        _active_ids = {item.id for item in active}
        _all_ids: set[str] = set()
        try:
            for payload in self._read_history_ndjson_unlocked(self.tombstones_path):
                item_id = str(payload.get("id", "")).strip()
                if item_id:
                    _all_ids.add(item_id)
        except Exception:
            pass
        _tombstoned_ids = _all_ids - _active_ids

        tmp_history = self.history_path.with_suffix(".ndjson.tmp")
        _history_replaced = False

        try:
            with tmp_history.open("w", encoding="utf-8") as fh:
                for item in active:
                    line = self._maybe_encrypt(json.dumps(item.to_dict(), ensure_ascii=False))
                    fh.write(line + "\n")
                fh.flush()
                # W853 fix 1: fsync before the atomic rename so the data is
                # guaranteed to be on disk if a crash occurs during replace().
                os.fsync(fh.fileno())

            tmp_history.replace(self.history_path)
            _history_replaced = True
        finally:
            # W1715 BUG 3 fix: clean up the tmp file if the atomic replace did
            # not happen (e.g. json.dumps raised or fsync failed due to disk
            # full).  After a successful replace() the file is gone, so guard
            # with .exists() to avoid a spurious unlink error.
            if not _history_replaced and tmp_history.exists():
                try:
                    tmp_history.unlink()
                except OSError:
                    pass

        # W1756: перед очисткой tombstones дописываем удалённые id в постоянный
        # реестр purged_ids_path, чтобы import_history_ndjson мог блокировать
        # resurrection даже после compact (tombstones_path очищается ниже).
        if _tombstoned_ids:
            try:
                for _pid in _tombstoned_ids:
                    self._append_ndjson(self.purged_ids_path, {"id": _pid})
            except Exception:  # noqa: BLE001 — purge-persist failure must not break compact
                logger.exception("_compact_unlocked: ошибка записи purged_ids_path")

        # W853 fix 2: truncate each delta journal atomically via tmp-file +
        # fsync + rename.  A plain write_text("") is not atomic — a crash
        # between two truncations leaves some journals cleared and others
        # intact, producing orphaned overrides on the next load.
        for journal_path in [
            self.tombstones_path, self.status_path, self.tags_path,
            self.favorites_path, self.text_updates_path, self.action_items_path,
        ]:
            _tmp = journal_path.with_suffix(".tmp")
            _tmp.write_text("", encoding="utf-8")
            with _tmp.open("r+", encoding="utf-8") as _fh:
                _fh.flush()
                os.fsync(_fh.fileno())
            _tmp.replace(journal_path)

        # W1715 BUG 1 fix: rewrite annotations and calendar_links journals
        # keeping only entries for items that are still active.  Before this
        # fix both journals were never truncated on compaction, so deleted
        # items' entries accumulated forever (unbounded growth proportional to
        # cumulative deletes) and were scanned in full on every IPC read.
        #
        # We use a selective-rewrite strategy (matching how annotations and
        # calendar_links are last-write-wins by id) rather than a plain
        # truncation: the journals may contain entries for items that are *not*
        # being deleted in this compaction cycle, so we must keep those.
        for journal_path, key_field in [
            (self.annotations_path, "id"),
            (self.calendar_links_path, "id"),
        ]:
            surviving_lines: list[str] = []
            for payload in self._read_ndjson_unlocked(journal_path):
                entry_id = str(payload.get(key_field, "")).strip()
                if entry_id and entry_id in _active_ids:
                    surviving_lines.append(
                        json.dumps(payload, ensure_ascii=False) + "\n"
                    )
            _tmp = journal_path.with_suffix(".tmp")
            _tmp.write_text("".join(surviving_lines), encoding="utf-8")
            with _tmp.open("r+", encoding="utf-8") as _fh:
                _fh.flush()
                os.fsync(_fh.fileno())
            _tmp.replace(journal_path)

        # Вызываем хук постобработки, если он подключён (напр. TranscriptVersionManager).
        on_compact = getattr(self, "_on_compact_hook", None)
        if on_compact is not None:
            try:
                active_ids = {item.id for item in active}
                on_compact(active_ids)
            except Exception:
                logger.exception("_compact_unlocked: ошибка в _on_compact_hook")

        _versioner = getattr(self, "_transcript_versioner", None)
        if _versioner is not None and _tombstoned_ids:
            for _item_id in _tombstoned_ids:
                try:
                    _versioner.purge_versions_for_item(_item_id)
                except Exception:
                    logger.exception("_compact_unlocked: версии id=%s", _item_id)

        # D1 (wave-35): reset the in-memory active-ids cache after compaction so
        # _ensure_active_ids_unlocked() rebuilds it from the freshly compacted
        # history.ndjson on the next call — delta journals (tombstones, status,
        # tags, etc.) have been cleared, making a stale cached set incorrect.
        self._active_ids = _active_ids  # _active_ids was already built above

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
            # W1715 BUG 2 fix: use _append_ndjson so the write is fsynced,
            # matching every other delta-journal write path.
            self._append_ndjson(self.action_items_path, entry)
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
            entry: dict[str, Any] = {"id": clean_id, "text": text}
            if confidence is not None:
                entry["confidence"] = round(float(confidence), 4)
            # W1715 BUG 2 fix: use _append_ndjson so the write is fsynced,
            # matching every other delta-journal write path.
            self._append_ndjson(self.text_updates_path, entry)
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
        """Собирает множество удалённых идентификаторов.

        W1756: читает как живые tombstones (tombstones_path), так и
        постоянный реестр (purged_ids_path), записанный compact() перед
        очисткой tombstones.  Гарантирует, что import_history_ndjson
        не воскрешает записи даже после компактирования.
        """
        deleted: set[str] = set()
        # tombstones_path использует шифрование (если включено) — читаем через
        # _read_history_ndjson_unlocked, который применяет _maybe_decrypt.
        # purged_ids_path никогда не шифруется (только ids, не PII).
        for payload in self._read_history_ndjson_unlocked(self.tombstones_path):
            item_id = str(payload.get("id", "")).strip()
            if item_id:
                deleted.add(item_id)
        for payload in self._read_ndjson_unlocked(self.purged_ids_path):
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

    def _read_history_ndjson_unlocked(self, path: Path) -> Iterator[dict[str, Any]]:
        """Читает NDJSON-файл истории с опциональной расшифровкой строк.

        Используется для history.ndjson и tombstones.ndjson, которые могут
        содержать смесь открытых и зашифрованных строк.  Plaintext строки
        проходят без изменений через ``_maybe_decrypt``.
        """
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                decrypted = self._maybe_decrypt(raw)
                if not decrypted:
                    # _maybe_decrypt вернул пустую строку → ошибка расшифровки,
                    # строка уже залогирована → пропускаем.
                    continue
                payload = safe_json_loads(decrypted)
                if payload is None:
                    continue
                if isinstance(payload, dict):
                    yield payload

    def _iter_history_items_unlocked(self) -> Iterator[HistoryItem]:
        """Итератор по основному журналу истории (с расшифровкой если включено)."""
        for payload in self._read_history_ndjson_unlocked(self.history_path):
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

    def _append_history_ndjson(self, payload: dict[str, Any]) -> None:
        """Append к history.ndjson с опциональным шифрованием строки.

        Шифрование применяется к JSON-строке ДО передачи в _append_ndjson_raw
        (sink записи), что позволяет тестам патчить _append_ndjson_raw и
        перехватывать ошибки записи на диск.
        """
        json_str = self._maybe_encrypt(json.dumps(payload, ensure_ascii=False))
        self._append_ndjson_raw(self.history_path, json_str)

    def _append_tombstone_ndjson(self, payload: dict[str, Any]) -> None:
        """Append к tombstones с опциональным шифрованием строки."""
        json_str = self._maybe_encrypt(json.dumps(payload, ensure_ascii=False))
        self._append_ndjson_raw(self.tombstones_path, json_str)

    @staticmethod
    def _append_ndjson_raw(path: Path, line: str) -> None:
        """Атомарный append уже готовой строки (без JSON-сериализации) с flush/fsync."""
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

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

    # ------------------------------------------------------------------
    # At-rest encryption migration
    # ------------------------------------------------------------------

    def migrate_history_encryption(self, progress_cb=None):
        """Encrypt all plaintext lines in history.ndjson using AES-256-GCM.

        Data-safety guarantees:
        - Reads raw lines without decrypting (preserves ENC1: lines as-is).
        - Creates a .bak copy BEFORE touching the live file (transient, not permanent).
        - Writes to a .migration_tmp then atomically renames (os.replace).
        - Self-verifies: decrypts every line in the new encrypted file back to a
          non-empty value; if any line fails, rolls back by restoring .bak as
          history.ndjson and returns {"ok": False, "reason": "verification_failed"}.
        - On successful verification, securely removes the .bak (best-effort overwrite
          with zeros then unlink); a removal failure only logs a warning and does NOT
          fail the migration — the encrypted file is already live and correct.
        - The .bak is intentionally transient: the recovery mechanism for
          encryption-at-rest is the Keychain key (HistoryCrypto), not a permanent
          plaintext sidecar sitting on disk.
        - Fully idempotent: already-encrypted ENC1: lines are passed through unchanged.
        - Tombstone/delete lines are preserved exactly.

        Args:
            progress_cb: optional callable(total, done, encrypted, pct, status)
                         called after each line and once more at completion.

        Returns:
            {"ok": True, "encrypted": int, "total": int, "already_encrypted": int}
            {"ok": False, "reason": str}
        """
        import shutil as _shutil
        from backend.history_crypto import HistoryCrypto

        # Force re-init so we pick up a freshly enabled flag.
        self._history_crypto_initialized = False
        crypto = self._get_history_crypto()
        if crypto is None:
            return {"ok": False, "reason": "encryption_unavailable"}

        with self._lock():
            if not self.history_path.exists() or self.history_path.stat().st_size == 0:
                return {"ok": False, "reason": "empty_file"}

            # Step 1: read all raw lines without decrypting
            raw_lines = []
            with self.history_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.rstrip("\n")
                    if stripped:
                        raw_lines.append(stripped)

            total = len(raw_lines)
            encrypted_count = 0
            already_count = 0
            out_lines = []

            # Step 2: encrypt plaintext only; ENC1: lines pass through unchanged
            for idx, raw in enumerate(raw_lines):
                if HistoryCrypto.is_encrypted(raw):
                    out_lines.append(raw)
                    already_count += 1
                else:
                    out_lines.append(crypto.encrypt_line(raw))
                    encrypted_count += 1
                if progress_cb is not None:
                    done = idx + 1
                    pct = int(done * 100 / total)
                    try:
                        progress_cb(total, done, encrypted_count, pct, "encrypting")
                    except Exception:
                        pass

            # Step 3: write .bak BEFORE touching the live file
            bak_path = self.history_path.with_suffix(".ndjson.bak")
            _shutil.copy2(str(self.history_path), str(bak_path))

            # Step 4: write tmp, then atomic replace
            tmp_path = self.history_path.with_suffix(".ndjson.migration_tmp")
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    for out_line in out_lines:
                        fh.write(out_line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(str(tmp_path), str(self.history_path))
            except Exception:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise

            # Step 5: self-verify — decrypt every ENC1 line back; roll back on failure
            verification_ok = True
            try:
                with self.history_path.open("r", encoding="utf-8") as fh:
                    for vline in fh:
                        vline = vline.rstrip("\n")
                        if not vline:
                            continue
                        if HistoryCrypto.is_encrypted(vline):
                            decrypted = crypto.decrypt_line(vline)
                            if not decrypted:
                                verification_ok = False
                                break
                        # Plaintext pass-through lines (should be 0 after migration) are OK
            except Exception:
                verification_ok = False

            if not verification_ok:
                # Roll back: restore plaintext original from .bak
                try:
                    os.replace(str(bak_path), str(self.history_path))
                except Exception as restore_exc:
                    logger.error(
                        "migrate_history_encryption: verification failed AND rollback failed",
                        extra={"restore_error": str(restore_exc)},
                    )
                return {"ok": False, "reason": "verification_failed"}

            # Step 6: verification passed — securely remove the plaintext .bak
            try:
                bak_size = bak_path.stat().st_size
                with bak_path.open("r+b") as fh:
                    fh.write(b"\x00" * bak_size)
                    fh.flush()
                    os.fsync(fh.fileno())
                bak_path.unlink()
            except Exception as wipe_exc:
                # Non-fatal: encrypted live file is already in place; log + continue
                logger.warning(
                    "migrate_history_encryption: не удалось безопасно удалить .bak",
                    extra={"bak_path": str(bak_path), "error": str(wipe_exc)},
                )

        # Step 7: reset crypto cache (file changed on disk)
        self._history_crypto_initialized = False
        self._search_index.clear()
        self._recent_search_index = []
        self._recent_search_index_signature = None

        if progress_cb is not None:
            try:
                progress_cb(total, total, encrypted_count, 100, "done")
            except Exception:
                pass

        return {
            "ok": True,
            "encrypted": encrypted_count,
            "total": total,
            "already_encrypted": already_count,
        }

    def get_history_encryption_status(self):
        """Return encryption statistics for history.ndjson.

        Scans raw lines to count ENC1: vs plaintext.
        No lock needed — read-only scan of the live file.

        Returns:
            {
                "enabled": bool,
                "total": int,
                "encrypted": int,
                "plaintext": int,
                "pct": int,
            }
        """
        from backend.history_crypto import HistoryCrypto

        enabled = self._read_encryption_flag_unlocked()
        total = 0
        encrypted = 0
        if self.history_path.exists():
            with self.history_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    raw = line.strip()
                    if not raw:
                        continue
                    total += 1
                    if HistoryCrypto.is_encrypted(raw):
                        encrypted += 1
        plaintext = total - encrypted
        pct = int(encrypted * 100 / total) if total > 0 else 0
        return {
            "enabled": enabled,
            "total": total,
            "encrypted": encrypted,
            "plaintext": plaintext,
            "pct": pct,
        }
