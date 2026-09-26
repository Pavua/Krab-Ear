"""Автоматическое резервное копирование истории Krab Ear.

AutoBackupManager выполняет резервное копирование оппортунистически —
при вызове check_and_backup() — без фоновых потоков.
Настройки хранятся в файле auto_backup_meta.json в директории backups/.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Optional

from backend.settings_backup import SENSITIVE_FIELDS as _SENSITIVE_FIELDS
from backend.encrypted_snapshot import (
    SnapshotOperationRefused,
    create_encrypted_snapshot,
    recover_pending_state,
)
from backend.history_encryption_policy import (
    OPERATION_UNAVAILABLE_REASON as _ENC_OP_UNAVAILABLE,
    HistoryEncryptionOperationUnavailable,
    policy_blocks,
    store_policy_reader,
)

logger = logging.getLogger("KrabEar.Backend.AutoBackup")

# Настройки по умолчанию
AUTO_BACKUP_INTERVAL_HOURS: int = 24
AUTO_BACKUP_MAX_COPIES: int = 7

# Версия sidecar-протокола последнего исхода backup-цикла (backups/.last_result.json).
LAST_RESULT_VERSION: int = 1


class AutoBackupManager:
    """Управляет автоматическими резервными копиями истории.

    Thread-safe. Не создаёт фоновых потоков — копирование происходит
    только при явном вызове check_and_backup().
    """

    META_FILENAME = "auto_backup_meta.json"

    def __init__(
        self,
        store: Any,
        interval_hours: int = AUTO_BACKUP_INTERVAL_HOURS,
        max_copies: int = AUTO_BACKUP_MAX_COPIES,
        enabled: bool = True,
        settings_fn: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        """
        Args:
            store: StateStore — источник файлов для резервного копирования.
            interval_hours: минимальный интервал между бэкапами (часы).
            max_copies: максимальное количество хранимых бэкапов.
            enabled: если False — check_and_backup() ничего не делает.
            settings_fn: опциональный callable → current settings dict.
                         Используется для privacy_mode gate в check_and_backup().
                         None = privacy gate disabled (safe default — no backup skipped).
        """
        self.store = store
        self.interval_hours = interval_hours
        self.max_copies = max_copies
        self.enabled = enabled
        self._settings_fn = settings_fn
        self._lock = threading.Lock()
        # wave-25 (B2): privacy-purge guard. handle_purge_all_data делает rmtree(backups/),
        # но фоновый/оппортунистический backup-цикл может ПЕРЕСОЗДАТЬ директорию сразу
        # после очистки → PII-снапшоты истории воскресают (TOCTOU). set_purged()
        # взводит этот Event и удаляет backups/; пока он взведён, check_and_backup()
        # пропускает запись молча. clear_purged() снимает флаг после завершения purge
        # (будущие бэкапы снова разрешены). threading.Event сам по себе thread-safe.
        self._purged = threading.Event()
        # A5.2a: fail-closed policy-reader legacy plaintext backup. При
        # Encryption ON check_and_backup/_do_backup отказывают до mkdir и не
        # трогают старые backup'ы/meta. Guard не трогает Keychain.
        self._encryption_policy_read = store_policy_reader(store)
        # A5.2b1: последний наблюдённый исход backup-цикла. Живёт в памяти И в
        # sidecar-протоколе (backups/.last_result.json), чтобы причина отказа
        # переживала рестарт backend: иначе свежий процесс сообщал бы «всё
        # хорошо» на профиле, где backup заведомо невозможен (N1).
        # В auto_backup_meta.json НЕ пишем: этот файл при отказе обязан остаться
        # нетронутым (контракт A5.2a), и он не переживает purge отдельно.
        self._last_backup_kind: Optional[str] = None
        self._last_refusal_reason: Optional[str] = None
        self._load_last_result()

    def _encryption_blocked(self) -> bool:
        """True, если legacy plaintext auto-backup запрещён политикой."""
        return policy_blocks(getattr(self, "_encryption_policy_read", None))

    def _is_privacy_mode(self) -> bool:
        """FAIL-CLOSED чтение ``privacy_mode_enabled``.

        Неизвестное состояние приватности ⇒ считаем privacy ON. Тот же контракт,
        что у ``RecordingCoreService._privacy_mode_enabled``. ``settings_fn`` в
        проде — ``cached_settings()``; OSError из ``StateStore._lock()``
        (ENOSPC/EMFILE/EACCES) не должен открывать гейт и копировать history.

        🔴 Отсутствие ключа — НЕ сбой: настройки прочитаны, режим просто выключен.
        ``settings_fn is None`` после ``__init__`` — отсутствие источника (тесты).
        ``__new__`` без ``__init__`` даёт AttributeError → True, в отличие от
        ``enabled`` (getattr → False).
        """
        try:
            settings_fn = self._settings_fn
            if settings_fn is None:
                return False
            return bool(settings_fn().get("privacy_mode_enabled", False))
        except Exception:
            logger.warning(
                "Не удалось прочитать privacy_mode_enabled — считаем privacy ON (fail-closed)",
                exc_info=True,
            )
            return True

    # ------------------------------------------------------------------
    # Вспомогательные свойства
    # ------------------------------------------------------------------

    @property
    def backups_dir(self) -> Path:
        return Path(self.store.data_dir) / "backups"

    @property
    def _meta_path(self) -> Path:
        return self.backups_dir / self.META_FILENAME

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _load_meta(self) -> dict:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"last_backup_ts": None, "backup_count": 0}

    def _save_meta(self, meta: dict) -> None:
        self.backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Ensure the dir itself is restricted even if it already existed.
        try:
            os.chmod(self.backups_dir, 0o700)
        except OSError:
            pass
        self._meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(self._meta_path, 0o600)
        except OSError:
            pass

    def _list_auto_backups(self) -> list[Path]:
        """Возвращает список папок авто-бэкапов, отсортированных по имени (старые → новые)."""
        if not self.backups_dir.exists():
            return []
        dirs = sorted(
            d for d in self.backups_dir.iterdir()
            if d.is_dir() and d.name.startswith("auto_backup_")
        )
        return dirs

    def _list_snapshot_dirs(self) -> list[Path]:
        """Каталоги encrypted snapshot'ов (``auto_snapshot_*``).

        Отдельно от ``_list_auto_backups``: это другой формат и другой
        retention, смешивать их в одном счётчике нельзя. Счётчик ДЕШЁВЫЙ —
        только имена каталогов, без чтения манифестов (N3).
        """
        if not self.backups_dir.exists():
            return []
        return sorted(
            d for d in self.backups_dir.iterdir()
            if d.is_dir() and d.name.startswith("auto_snapshot_")
        )

    # ------------------------------------------------------------------
    # Sidecar протокола: последний исход цикла (переживает рестарт)
    # ------------------------------------------------------------------

    @property
    def _last_result_path(self) -> Path:
        """``backups/.last_result.json`` — dot-prefixed, поэтому не всплывает
        ни в списке бэкапов, ни в legacy restore, и удаляется purge'ом вместе
        с каталогом backups/."""
        return self.backups_dir / ".last_result.json"

    def _load_last_result(self) -> None:
        """Восстанавливает последний исход после рестарта. Битый файл игнорируем."""
        path = self._last_result_path
        try:
            if not path.is_file():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("auto_backup: не читается sidecar %s", path, exc_info=True)
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("kind")
        reason = payload.get("refusal_reason")
        self._last_backup_kind = kind if kind in ("encrypted_snapshot", "legacy_plaintext") else None
        self._last_refusal_reason = reason if isinstance(reason, str) and reason else None

    def _record_result(self, kind: Optional[str], refusal_reason: Optional[str]) -> None:
        """Фиксирует исход цикла в памяти и в sidecar (только метаданные)."""
        self._last_backup_kind = kind
        self._last_refusal_reason = refusal_reason
        payload = {
            "version": LAST_RESULT_VERSION,
            "kind": kind,
            "refusal_reason": refusal_reason,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self._last_result_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._last_result_path)
            os.chmod(self._last_result_path, 0o600)
        except OSError:
            # Sidecar — вспомогательная наблюдаемость, не путь записи: его
            # недоступность не должна ломать сам backup.
            logger.warning("auto_backup: не удалось записать sidecar результата", exc_info=True)

    def _clear_result(self) -> None:
        self._last_backup_kind = None
        self._last_refusal_reason = None
        try:
            self._last_result_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("auto_backup: не удалось удалить sidecar результата", exc_info=True)

    def _prune_old_backups(self) -> int:
        """Удаляет старые авто-бэкапы, оставляя не более max_copies.

        Returns:
            Количество удалённых копий.
        """
        backups = self._list_auto_backups()
        to_delete = backups[: max(0, len(backups) - self.max_copies)]
        for d in to_delete:
            try:
                shutil.rmtree(d, ignore_errors=True)
                logger.info("Удалён старый авто-бэкап: %s", d)
            except Exception as exc:
                logger.warning("Не удалось удалить авто-бэкап %s: %s", d, exc)
        return len(to_delete)

    def _store_lock(self) -> ContextManager[Any]:
        """Возвращает контекст-менеджер file-lock'а StateStore.

        W1768: снимок истории обязан быть атомарным относительно append-ов и
        компактирования — берём тот же fcntl.flock, что сериализует все записи
        истории (StateStore._lock). У реального StateStore это всегда вызываемый
        @contextmanager-метод. Если же store._lock не вызываемый (например,
        облегчённый тестовый фейк без настоящего lock) — деградируем к нулевому
        контексту, чтобы не падать; в проде эта ветка недостижима.
        """
        lock_factory = getattr(self.store, "_lock", None)
        if callable(lock_factory):
            return lock_factory()
        logger.debug(
            "auto_backup: store._lock недоступен/не вызываем — снимок без file-lock "
            "(ожидаемо только в тестах с фейковым store)"
        )
        return contextlib.nullcontext()

    def _do_backup(self) -> dict:
        """Выполняет резервное копирование и возвращает метаданные."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.backups_dir / f"auto_backup_{ts}"

        # Файлы истории/статуса копируются verbatim; settings.json — только после редакции.
        plain_files = [
            self.store.history_path,
            self.store.tombstones_path,
            self.store.status_path,
        ]

        total_bytes = 0
        copied_files = []
        # W1768: снимок истории должен быть АТОМАРНЫМ относительно append-ов и
        # компактирования. StateStore сериализует ВСЕ записи истории через
        # fcntl-flock (state_store._lock). Без удержания этого lock компактирование,
        # попавшее МЕЖДУ copy2() отдельных файлов, спарит pre-compact history.ndjson
        # с post-compact tombstones (или наоборот) → при восстановлении воскресшие
        # либо потерянные записи (integrity/privacy-регрессия).
        # A5.2a: mkdir перенесён ПОД lock, а policy повторно проверяется ПОСЛЕ
        # захвата — OFF→ON через второй StateStore не должен успеть создать
        # plaintext-копию. При ON sink не трогается вовсе.
        with self._store_lock():
            if self._encryption_blocked():
                # A5.2b1: при Encryption ON legacy plaintext-копия не создаётся,
                # но и backup не отказывает — это тот же encrypted snapshot из
                # ручного пути ( спека §5). Всё под тем же store-lock; при
                # недоступном ключе/сбое протокола — машинно-читаемый отказ.
                return self._encrypted_snapshot(ts)
            # Create with restricted permissions so PII is not world-readable.
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(backup_dir, 0o700)
            except OSError:
                pass
            for src in plain_files:
                if Path(src).exists():
                    dst = backup_dir / Path(src).name
                    shutil.copy2(src, dst)
                    try:
                        os.chmod(dst, 0o600)
                    except OSError:
                        pass
                    total_bytes += dst.stat().st_size
                    copied_files.append(Path(src).name)

            # settings.json — редактируем чувствительные поля перед записью (W897 AB-2).
            settings_src = Path(self.store.settings_path)
            if settings_src.exists():
                try:
                    raw = json.loads(settings_src.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        safe = {k: v for k, v in raw.items() if k not in _SENSITIVE_FIELDS}
                    else:
                        safe = raw  # неожиданный формат — копируем как есть
                except Exception as exc:
                    logger.warning("auto_backup: не удалось прочитать settings.json для редакции: %s", exc)
                    safe = {}
                dst = backup_dir / settings_src.name
                dst.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
                try:
                    os.chmod(dst, 0o600)
                except OSError:
                    pass
                total_bytes += dst.stat().st_size
                copied_files.append(settings_src.name)

        # count_active_items() сам берёт store._lock — поэтому ТОЛЬКО вне блока выше.
        entries = 0
        try:
            entries = self.store.count_active_items()
        except Exception:
            pass

        meta = {
            "backup_ts": ts,
            "entries": entries,
            "size_bytes": total_bytes,
            "files": copied_files,
            "auto": True,
        }
        meta_file = backup_dir / "backup_meta.json"
        meta_file.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(meta_file, 0o600)
        except OSError:
            pass

        size_mb = round(total_bytes / (1024 * 1024), 3)
        logger.info(
            "Авто-бэкап создан: %s (%s МБ, %d записей)", backup_dir, size_mb, entries
        )
        return {
            "backup_path": str(backup_dir),
            "backup_ts": ts,
            "size_mb": size_mb,
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # A5.2b1 — encrypted snapshot (только при Encryption ON)
    # ------------------------------------------------------------------

    def _history_crypto_for_snapshot(self):
        """Ключ истории для snapshot'а или ``None``.

        Тот же ``HistoryCrypto``, которым StateStore шифрует журналы: снимок
        обязан читаться тем же ключом. Любая ошибка → ``None`` → отказ.
        """
        getter = getattr(self.store, "_get_history_crypto", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001
            logger.exception("auto_backup: не удалось получить ключ истории")
            return None

    def _encrypted_snapshot(self, ts: str) -> dict:
        """Auto-backup при Encryption ON: encrypted snapshot (вызывается под lock).

        Имена каталогов ``auto_snapshot_*`` намеренно НЕ совпадают с legacy
        ``auto_backup_*``: retention/prune старых копий не должна находить
        снимки нового протокола (инвентаризация и удаление legacy — A5.2c).
        """
        crypto = self._history_crypto_for_snapshot()
        if crypto is None:
            logger.error(
                "auto_backup: encryption on, но ключ недоступен — snapshot "
                "невозможен (%s)",
                _ENC_OP_UNAVAILABLE,
            )
            raise HistoryEncryptionOperationUnavailable("auto_backup")

        snapshot_dir = self.backups_dir / f"auto_snapshot_{ts}"
        try:
            result = create_encrypted_snapshot(
                data_dir=self.store.data_dir,
                backup_dir=snapshot_dir,
                crypto=crypto,
                transaction_id=f"auto_backup_{ts}",
                policy_on=True,
                policy_read=self._encryption_policy_read,
            )
        except SnapshotOperationRefused as exc:
            logger.warning("auto_backup: encrypted snapshot отклонён: %s (%s)", exc.reason, exc)
            raise HistoryEncryptionOperationUnavailable("auto_backup") from exc

        return {
            "backup_path": str(snapshot_dir),
            "backup_ts": ts,
            "size_mb": round(result.get("size_bytes", 0) / (1024 * 1024), 3),
            # entries НЕ считаем здесь: count_active_items() сам берёт
            # store-lock, а мы внутри него. Считает вызывающий — как в
            # legacy-ветке _do_backup (MAJOR-5: раньше здесь был hardcoded 0
            # с обещанием «вызывающий заполнит», а вызывающий пробрасывал).
            "entries": 0,
            "encrypted": True,
            "state": result["state"],
            "transaction_id": result["transaction_id"],
        }

    # ------------------------------------------------------------------
    # Privacy-purge guard (wave-25 B2)
    # ------------------------------------------------------------------

    def set_purged(self) -> None:
        """Помечает менеджер как «после privacy-purge» и удаляет backups/.

        Вызывается из handle_purge_all_data ДО/во время очистки. Взводит флаг
        (последующие check_and_backup() пропускаются молча) и сразу удаляет
        директорию backups/, если она существует — закрывает окно, в котором
        оппортунистический backup-цикл мог пересоздать PII-снапшоты сразу после
        rmtree() в purge-теле.
        """
        self._purged.set()
        with self._lock:
            # Sidecar исхода лежит внутри backups/ и удаляется rmtree, но
            # память менеджера надо сбросить явно: иначе статус продолжит
            # «помнить» бэкап, которого больше нет (N1).
            self._clear_result()
            try:
                if self.backups_dir.exists():
                    shutil.rmtree(self.backups_dir, ignore_errors=True)
                    logger.info("auto_backup: backups/ удалён (set_purged)")
            except Exception:
                logger.warning("auto_backup: set_purged rmtree backups/ не удался", exc_info=True)

    def clear_purged(self) -> None:
        """Снимает purge-флаг — будущие бэкапы снова разрешены.

        Вызывается из handle_purge_all_data ПОСЛЕ завершения всех wipe-шагов, чтобы
        нормальное авто-резервное копирование возобновилось со следующего цикла.
        """
        self._purged.clear()

    def is_purged(self) -> bool:
        """True, если менеджер находится в post-purge состоянии (бэкапы заморожены)."""
        return self._purged.is_set()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def check_and_backup(self) -> dict:
        """Создаёт резервную копию, если с последнего бэкапа прошло > interval_hours.

        Returns:
            dict с ключами:
                backed_up (bool): True если бэкап был выполнен
                skipped_reason (str | None): причина пропуска или None
                backup_path (str | None): путь к бэкапу или None
        """
        if not self.enabled:
            return {"backed_up": False, "skipped_reason": "disabled", "backup_path": None}

        # wave-1770 HIGH: skip backup when privacy_mode_enabled — history.ndjson contains
        # full transcript text (PII). The manual backup (handle_backup_history) already
        # gates on privacy_mode; auto-backup must do the same.
        # 2026-09: IO/lock сбой чтения settings — тоже skip (fail-closed), не pass.
        if self._is_privacy_mode():
            return {
                "backed_up": False,
                "skipped_reason": "privacy_mode",
                "backup_path": None,
            }

        # wave-25 (B2): после privacy-purge бэкапы заморожены до clear_purged().
        # Без этого фоновый/оппортунистический цикл пересоздал бы backups/ с PII
        # сразу после rmtree() в purge-теле (TOCTOU).
        if self._purged.is_set():
            return {"backed_up": False, "skipped_reason": "purged", "backup_path": None}

        # A5.2b1: при Encryption ON auto-backup больше не отказывается — идёт
        # encrypted snapshot (тот же путь, что и ручной backup). Отказ при
        # недоступном ключе/сбое протокола остаётсяfail-closed и наблюдаем
        # через skipped_reason; mkdir/copy при ON не происходит вовсе.
        #
        # A5.2a-контракт сохранён: решение принимается ПОД store-lock (в
        # _do_backup), повторно после захвата; старые backup'ы/meta при отказе
        # не трогаются.

        with self._lock:
            meta = self._load_meta()
            last_ts_str: str | None = meta.get("last_backup_ts")

            if last_ts_str is not None:
                try:
                    last_dt = datetime.fromisoformat(last_ts_str)
                    # Нормализуем к UTC
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    elapsed_hours = (now - last_dt).total_seconds() / 3600
                    if elapsed_hours < self.interval_hours:
                        return {
                            "backed_up": False,
                            "skipped_reason": "too_soon",
                            "backup_path": None,
                            "hours_since_last": round(elapsed_hours, 2),
                            "hours_until_next": round(self.interval_hours - elapsed_hours, 2),
                        }
                except Exception:
                    pass  # Повреждённая метадата — делаем бэкап

            # wave-25 (B2): перепроверяем purge-флаг ПОД lock прямо перед записью.
            # set_purged() взводит Event ДО захвата _lock (для rmtree backups/),
            # поэтому, если purge стартовал, пока мы держим lock, этот re-check его
            # увидит и не даст _do_backup() пересоздать только что удалённый backups/.
            if self._purged.is_set():
                return {"backed_up": False, "skipped_reason": "purged", "backup_path": None}

            # A5.2a: OFF→ON через второй StateStore, пока мы ждали self._lock.
            # A5.2b1: решение «снимок или отказ» принимает _do_backup ПОД
            # store-lock (единственная точка проверки политики и ключа) — здесь
            # дублировать её нельзя: чтение ключа вне store-lock лишний раз
            # трогает Keychain и создаёт второе место, где живёт решение.
            try:
                result = self._do_backup()
            except HistoryEncryptionOperationUnavailable:
                # ON пойман под store-lock — sink не тронут, причина видима.
                self._record_result(None, _ENC_OP_UNAVAILABLE)
                return {
                    "backed_up": False,
                    "skipped_reason": _ENC_OP_UNAVAILABLE,
                    "backup_path": None,
                }
            # Retention (A5.2a §6: gate ДО prune) — только для OFF-профиля.
            # При ON снимки нового протокола и legacy-копии НЕ удаляются:
            # инвентаризация и решение по старым plaintext — A5.2c.
            if not result.get("encrypted"):
                self._prune_old_backups()

            meta["last_backup_ts"] = datetime.now(timezone.utc).isoformat()
            meta["backup_count"] = meta.get("backup_count", 0) + 1
            self._save_meta(meta)
            self._record_result(
                "encrypted_snapshot" if result.get("encrypted") else "legacy_plaintext",
                None,
            )

            # MAJOR-5: entries считаются ЗДЕСЬ, вне store-lock (снимок создавался
            # под ним, а count_active_items() сам берёт store-lock). Для
            # legacy-копии счётчик уже посчитан в _do_backup, поэтому ветка
            # трогает только снимок.
            if result.get("encrypted"):
                try:
                    result["entries"] = self.store.count_active_items()
                except Exception:  # noqa: BLE001 — как в legacy-ветке
                    logger.warning(
                        "auto_backup: не удалось посчитать записи для снимка",
                        exc_info=True,
                    )

            return {
                "backed_up": True,
                "skipped_reason": None,
                "backup_path": result["backup_path"],
                "backup_ts": result["backup_ts"],
                "size_mb": result["size_mb"],
                "entries": result["entries"],
            }

    def get_auto_backup_status(self) -> dict:
        """Возвращает статус авто-резервного копирования.

        Returns:
            dict с ключами:
                enabled (bool)
                last_backup_ts (str | None): ISO-8601 время последнего бэкапа
                next_backup_ts (str | None): ISO-8601 время следующего запланированного бэкапа
                total_backups (int): количество авто-бэкапов на диске
                interval_hours (int)
                max_copies (int)
                backups_dir (str)
        """
        with self._lock:
            meta = self._load_meta()
            last_ts_str: str | None = meta.get("last_backup_ts")
            next_ts_str: str | None = None

            if last_ts_str is not None:
                try:
                    last_dt = datetime.fromisoformat(last_ts_str)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    from datetime import timedelta
                    next_dt = last_dt + timedelta(hours=self.interval_hours)
                    next_ts_str = next_dt.isoformat()
                except Exception:
                    pass

            total_backups = len(self._list_auto_backups())
            snapshots = self._list_snapshot_dirs()
            encryption_on = self._encryption_blocked()

            # A5.2b1 (MAJOR-4): «backup недоступен» ≠ «legacy plaintext-копия
            # недоступна». При ON backup ЖИВ: он пишет encrypted snapshot. Старый
            # код возвращал здесь True всегда при ON — владелец видел «backup
            # недоступен» даже когда снимок только что зафиксирован, а реальный
            # отказ (нет ключа) от неё был неотличим.
            #
            # Единственный случай, когда backup при ON действительно невозможен
            # прямо сейчас, — незавершённая опубликованная транзакция снимка
            # (нужен b2). Проверяется чтением каталогов, БЕЗ обращения к Keychain
            # (политика чтения флага Keychain не трогает — A5.2a).
            recovery = None
            if encryption_on:
                recovery = recover_pending_state(
                    data_dir=self.store.data_dir, backups_root=self.backups_dir
                )
            blocked_by_pending = bool(recovery and recovery.get("pending"))

            # Что РЕАЛЬНО было последним: снимок, legacy-копия или отказ.
            # N1.2: если последний цикл ОТКАЗАЛ, вид выводится из отказа, и
            # наличие каталогов снимка его НЕ перезаписывает — иначе статус
            # одновременно утверждал бы «последний бэкап — снимок» и «последний
            # бэкап — отказ». Вывод из каталогов допустим только когда исход
            # вообще неизвестен (свежий профиль с уже существующими бэкапами).
            last_refusal = self._last_refusal_reason
            last_kind = self._last_backup_kind
            if last_kind is None and last_refusal is None:
                if snapshots:
                    last_kind = "encrypted_snapshot"
                elif total_backups:
                    last_kind = "legacy_plaintext"
            if last_refusal is None and blocked_by_pending:
                last_refusal = recovery.get("reason")

            # N1.1/N1.3: «недоступно» = есть ДОКАЗАННЫЙ отказ (записанный циклом,
            # переживает рестарт) ИЛИ незавершённая опубликованная транзакция.
            # Раньше здесь было только blocked_by_pending, из-за чего полный отказ
            # backup-цикла (например, недоступный ключ при ON) был полностью
            # невидим в тех полях, которые A5.2a ввела ради наблюдаемости.
            unavailable = blocked_by_pending or last_refusal is not None
            return {
                "enabled": self.enabled,
                "last_backup_ts": last_ts_str,
                "next_backup_ts": next_ts_str,
                # Прежнее значение: только legacy auto_backup_* — потребители
                # (UI) не должны внезапно увидеть в нём снимки.
                "total_backups": total_backups,
                "encrypted_snapshots": len(snapshots),
                "interval_hours": self.interval_hours,
                "max_copies": self.max_copies,
                "backups_dir": str(self.backups_dir),
                "encryption_operation_unavailable": unavailable,
                "skipped_reason": last_refusal,
                # A5.2b1: честная развязка «что произошло» / «почему отказ».
                "encryption_on": encryption_on,
                "last_backup_kind": last_kind,
                "last_refusal_reason": last_refusal,
            }
