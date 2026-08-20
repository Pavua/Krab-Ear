"""Write-only memory ledger shared between Krab Ear and Krab (spec §4).

🔴 Sidecar lock, NOT the brain_lease pattern: brain_lease flocks the data file
and os.replace()s it under its own flock, splitting the lock domain across
inodes. Here the SIDECAR (never replaced) is the only flocked file; the data
file is written atomically (tmp+os.replace) while holding the sidecar lock.
🔴 C-ONE-PATH: one pure formula, no env channel (the P0d/12.07 class).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("KrabEar.Backend.MemoryLedger")

LEDGER_FILENAME = "memory_ledger.json"
LEDGER_LOCK_FILENAME = "memory_ledger.lock"
LEDGER_TTL_SEC = 120.0
_CORRUPT_KEEP = 5
_SCHEMA_V = 1


# 🔴 Тестовый шов (финальный гейт волны, HIGH-3): сотни юнит-тестов создают
# BackendService; без подмены пути они писали/стирали бы записи в РЕАЛЬНОМ
# ~/.openclaw владельца (класс «tests writing to production data»). Это НЕ
# env-канал (C-ONE-PATH запрещает env для ПРОДА) — прод поле не трогает,
# ставит только conftest, обратимо.
_TEST_PATH_OVERRIDE = None


def resolve_ledger_path() -> Path:
    """ЕДИНАЯ формула пути к общему leger-файлу. Абсолютный путь.

    Зеркалит event_bridge.resolve_token_path() (C-ONE-PATH, M8): формула ЧИСТАЯ
    — без env-канала. Оба процесса (Krab Ear и Krab) обязаны вычислять этот
    путь ОДИНАКОВО и логировать его при первом касании (см. _log_path_once).
    """
    if _TEST_PATH_OVERRIDE is not None:
        return (Path(_TEST_PATH_OVERRIDE) / LEDGER_FILENAME).resolve()
    return (Path.home() / ".openclaw" / LEDGER_FILENAME).resolve()


class LedgerClient:
    """Write-only клиент общего ledger'а (spec §4).

    Каждый владелец (``owner``) патчит ТОЛЬКО свой префикс ``<owner>/`` как
    дельту под sidecar-локом (RMW — read-modify-write), никогда не перезаписывая
    весь файл целиком. Данные другого владельца, ещё не протухшие по TTL,
    сохраняются без изменений.
    """

    def __init__(self, owner: str, path: Path | None = None, lock_timeout_sec: float = 1.0):
        self._owner = str(owner)
        self._path = Path(path) if path else resolve_ledger_path()
        self._lock_path = self._path.with_name(LEDGER_LOCK_FILENAME)
        self._lock_timeout = float(lock_timeout_sec)
        self.skipped_publishes = 0
        self._logged_path = False

    def _log_path_once(self) -> None:
        if not self._logged_path:
            logger.info("memory ledger: %s (owner=%s)", self._path, self._owner)
            self._logged_path = True

    def _acquire(self, nowait: bool) -> int:
        """Захватывает эксклюзивный flock ТОЛЬКО на sidecar-файл (never self._path)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if nowait:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except OSError:
            os.close(fd)
            raise

    def _load(self) -> dict:
        """Читает и парсит data-файл. Side-effect: битый JSON РОТИРУЕТСЯ в
        ``.corrupt-<ts>`` (ретеншн — 5 новейших), после чего возвращается
        свежий пустой документ — эта функция никогда не бросает исключение."""
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("entries"), dict):
                return doc
        except FileNotFoundError:
            return {"v": _SCHEMA_V, "entries": {}}
        except Exception:
            pass
        # corrupt (или неожиданная схема) → бэкап в сторону с ретеншном, начинаем с чистого листа
        try:
            bk = self._path.with_name(f"{self._path.name}.corrupt-{int(time.time())}")
            os.replace(self._path, bk)
            siblings = sorted(self._path.parent.glob(f"{self._path.name}.corrupt-*"))
            for old in siblings[:-_CORRUPT_KEEP]:
                old.unlink(missing_ok=True)
            logger.warning("memory ledger corrupt — backed up to %s", bk.name)
        except OSError:
            pass
        return {"v": _SCHEMA_V, "entries": {}}

    def _write_atomic(self, doc: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".ledger_tmp_")
        try:
            os.write(fd, json.dumps(doc, ensure_ascii=False).encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp, self._path)
            self._path.chmod(0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            Path(tmp).unlink(missing_ok=True)

    def publish_own(self, entries: dict) -> bool:
        """Публикует СВОИ записи как дельту (RMW). True=опубликовано,
        False=контенция лока (публикация пропущена, self.skipped_publishes++)."""
        self._log_path_once()
        now = time.time()
        try:
            lock_fd = self._acquire(nowait=False)
        except OSError:
            self.skipped_publishes += 1
            logger.warning(
                "memory ledger: lock contention — publish skipped (%d total)",
                self.skipped_publishes,
            )
            return False
        try:
            doc = self._load()
            kept: dict = {}
            for key, entry in doc["entries"].items():
                if key.startswith(self._owner + "/"):
                    continue  # заменяется ниже собственными свежими записями
                ts = entry.get("updated_ts") if isinstance(entry, dict) else None
                if isinstance(ts, (int, float)) and (now - ts) <= LEDGER_TTL_SEC:
                    kept[key] = entry
                # отсутствующий/протухший updated_ts = мёртвая запись чужого владельца → GC (fail-closed)
            for name, entry in entries.items():
                kept[f"{self._owner}/{name}"] = {
                    **entry,
                    "owner": self._owner,
                    "resident": name,
                    "updated_ts": now,
                }
            self._write_atomic({"v": _SCHEMA_V, "entries": kept})
            return True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def remove_own(self) -> None:
        """Graceful shutdown: убирает собственные записи (spec L2). Best-effort,
        никогда не бросает исключение наружу."""
        # 🔴 Ранний выход без flock, если удалять нечего. Не оптимизация:
        # тесты в чанке патчат fcntl.flock ГЛОБАЛЬНО (объект модуля один на
        # процесс), и наш служебный захват попадал в чужой spy — чужой тест
        # ловил LOCK_EX|LOCK_NB вместо своего LOCK_SH. Молчим, когда нечего
        # делать: это и корректнее по смыслу, и не шумит в общем процессе.
        if not self._path.exists():
            return
        try:
            lock_fd = self._acquire(nowait=True)
        except OSError:
            return
        try:
            doc = self._load()
            doc["entries"] = {
                k: v for k, v in doc["entries"].items() if not k.startswith(self._owner + "/")
            }
            self._write_atomic(doc)
        except Exception:
            logger.debug("memory ledger: remove_own failed", exc_info=True)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def read_all(self, nowait: bool = False) -> dict:
        """Читает весь ledger как есть (без GC — write-only observability output).

        Side-effect: как и ``_load``, может тихо ротировать битый файл в
        ``.corrupt-<ts>`` при чтении. При ``nowait=True`` и удержанном чужим
        процессом локе — НЕ ждёт, немедленно возвращает
        ``{"v":1,"entries":{}}``."""
        self._log_path_once()
        try:
            lock_fd = self._acquire(nowait=nowait)
        except OSError:
            return {"v": _SCHEMA_V, "entries": {}}
        try:
            return self._load()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
