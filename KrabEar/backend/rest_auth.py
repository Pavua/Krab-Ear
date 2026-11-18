"""REST API token store (optional Bearer auth for port 5005).

Tokens are stored as SHA-256 hashes in api_tokens.json (chmod 0600).
Raw token is returned once at creation time and never persisted.

Multi-process coherence (wave-21):
  - _save uses fcntl.flock(LOCK_EX) on the token file so concurrent gunicorn
    workers serialise writes.
  - verify_token and revoke_token call _reload_if_stale() before touching
    self._tokens: if api_tokens.json mtime changed since the last load we
    re-read it from disk.  This propagates revocations / creations made by
    another worker on the very next request to this worker — at worst one
    stale request, not an infinite window.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class RestAuth:
    """Manage API tokens for the REST server."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "api_tokens.json"
        # Dedicated lock file — never renamed/replaced, always the same inode,
        # so flock() across processes reliably serialises writes.
        self._lock_path = Path(data_dir) / "api_tokens.lock"
        self._lock = threading.Lock()
        # Глубина удержания межпроцессного flock в ЭТОМ потоке: `_save` внутри транзакции
        # не берёт блокировку повторно (flock на новом fd того же файла блокирует сам поток).
        self._tx = threading.local()
        self._tokens: list[dict] = self._load()
        # mtime api_tokens.json при последней загрузке (0.0, если файла ещё нет) и подпись
        # (ctime, mtime, inode, размер). Одного mtime мало: его можно вернуть через utime, а
        # замена файла другим процессом с тем же mtime оставляла бы в памяти отозванный токен.
        self._file_mtime: float = self._read_mtime()
        self._file_sig: tuple[int, int, int, int] = self._read_sig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_token(self, name: str, scopes: Optional[list[str]] = None) -> tuple[str, dict]:
        """Create a new API token.  Returns (raw_token, meta) without hash."""
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        entry: dict = {
            "id": secrets.token_hex(8),
            "name": name,
            "token_hash": token_hash,
            # Явный пустой список остаётся пустым: молчаливое расширение до «*» выдавало бы
            # токену больше прав, чем запросили. «*» — только когда scopes не заданы.
            "scopes": list(scopes) if scopes is not None else ["*"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used": None,
        }
        with self._lock, self._file_lock():
            # Чтение-изменение-запись целиком под межпроцессным flock: без этого два
            # процесса теряли чужой успешно созданный токен (lost update).
            self._tokens = self._load_strict()
            self._tokens.append(entry)
            self._save(self._tokens)
        return raw, self._public_meta(entry)

    def list_tokens(self) -> list[dict]:
        """Return all tokens as public metadata (no hashes)."""
        with self._lock:
            return [self._public_meta(t) for t in self._tokens]

    def revoke_token(self, token_id: str) -> bool:
        """Remove token by id.  Returns True if found and removed."""
        with self._lock, self._file_lock():
            current = self._load_strict()
            remaining = [t for t in current if t.get("id") != token_id]
            self._tokens = remaining
            if len(remaining) < len(current):
                self._save(remaining)
                return True
            self._remember_file_state()
            return False

    # How often (in seconds) to flush a last_used timestamp update to disk
    # (wave-31 H2 MED fix).  Concurrent auth calls within this window share a
    # single flock(LOCK_EX) write instead of serialising on every call.
    _LAST_USED_WRITE_INTERVAL_SEC = 60

    def verify_token(self, raw_token: str) -> Optional[dict]:
        """Verify raw_token.  Updates last_used and returns meta or None.

        Lazy-write optimisation (wave-31 H2 MED): _save() is deferred until
        at least _LAST_USED_WRITE_INTERVAL_SEC seconds have elapsed since the
        last flush for this token.  This eliminates the per-request flock(LOCK_EX)
        contention that caused a serialisation DoS when 10+ auth calls arrived
        in parallel — each waiting for the exclusive file lock.
        """
        if not raw_token:
            return None
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with self._lock:
            self._reload_if_stale()
            for entry in self._tokens:
                # Защита от timing-oracle: constant-time сравнение хэшей.
                stored = entry.get("token_hash") or ""
                if hmac.compare_digest(stored, token_hash):
                    now = time.time()
                    # Parse existing last_used to a float epoch for comparison.
                    last_used_raw = entry.get("last_used")
                    last_used_ts: float = 0.0
                    if last_used_raw:
                        try:
                            from datetime import datetime as _dt
                            last_used_ts = _dt.fromisoformat(
                                last_used_raw.replace("Z", "+00:00")
                            ).timestamp()
                        except Exception:
                            last_used_ts = 0.0
                    entry["last_used"] = datetime.now(timezone.utc).isoformat()
                    # Only flush to disk when the timestamp would change by
                    # more than the deferred-write interval — avoids an
                    # flock(LOCK_EX) round-trip on every single auth request.
                    if abs(now - last_used_ts) > self._LAST_USED_WRITE_INTERVAL_SEC:
                        # Сброс last_used идёт перечитыванием файла под flock и правкой ТОЛЬКО
                        # этого токена: сохранение списка из памяти возвращало токен,
                        # отозванный другим процессом. Отозван — отказ.
                        if not self._flush_last_used(entry.get("id"), entry["last_used"]):
                            return None
                    return self._public_meta(entry)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _public_meta(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k != "token_hash"}

    def _read_mtime(self) -> float:
        """mtime файла токенов или 0.0, если его нет."""
        try:
            return os.stat(self._path).st_mtime
        except OSError:
            return 0.0

    def _read_sig(self) -> tuple[int, int, int, int]:
        """Подпись файла токенов (ctime_ns, mtime_ns, inode, размер); нули, если файла нет.

        Атомарная замена даёт новый inode, а ctime нельзя вернуть через utime, поэтому
        подмена файла с тем же mtime не остаётся незамеченной.
        """
        try:
            st = os.stat(self._path)
        except OSError:
            return (0, 0, 0, 0)
        return (st.st_ctime_ns, st.st_mtime_ns, st.st_ino, st.st_size)

    def _remember_file_state(self) -> None:
        self._file_mtime = self._read_mtime()
        self._file_sig = self._read_sig()

    def _reload_if_stale(self) -> None:
        """Перечитывает токены с диска, если mtime или подпись файла изменились.

        Вызывается под ``self._lock``. Быстрый путь — два ``os.stat``.
        """
        current_mtime = self._read_mtime()
        current_sig = self._read_sig()
        if current_mtime != self._file_mtime or current_sig != self._file_sig:
            self._tokens = self._load()
            self._file_mtime = current_mtime
            self._file_sig = current_sig

    @contextlib.contextmanager
    def _file_lock(self):
        """Межпроцессная эксклюзивная блокировка на всю транзакцию (re-entrant в потоке)."""
        depth = getattr(self._tx, "depth", 0)
        if depth:
            self._tx.depth = depth + 1
            try:
                yield
            finally:
                self._tx.depth -= 1
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        with open(self._lock_path, "r+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            self._tx.depth = 1
            try:
                yield
            finally:
                self._tx.depth = 0
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _flush_last_used(self, token_id: Optional[str], stamp: str) -> bool:
        """Сохраняет last_used одного токена. False — токена на диске уже нет (отозван)."""
        with self._file_lock():
            try:
                tokens = self._load_strict()
            except RuntimeError:
                # Хранилище повреждено: не перезаписываем, сам факт аутентификации уже решён.
                return True
            target = next((t for t in tokens if t.get("id") == token_id), None)
            self._tokens = tokens
            if target is None:
                self._remember_file_state()
                return False
            target["last_used"] = stamp
            self._save(tokens)
            return True

    def _load(self) -> list[dict]:
        """Терпимое чтение (старт, проверка токена): сбой даёт пустой список, то есть отказ."""
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as fh:
                    data = json.load(fh)
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def _load_strict(self) -> list[dict]:
        """Чтение для изменения: повреждённое хранилище НЕ превращается в пустое.

        Иначе любая запись (создание/отзыв токена) молча стирала бы все токены.
        """
        if not self._path.exists():
            return []
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("api_tokens.json повреждён: изменение отклонено") from exc
        if not isinstance(data, list) or not all(isinstance(t, dict) for t in data):
            raise RuntimeError("api_tokens.json повреждён: изменение отклонено")
        return data

    def _save(self, tokens: list[dict]) -> None:
        """Atomically write tokens file with 0600 permissions.

        Uses an exclusive fcntl.flock on the token file (created if absent)
        so concurrent gunicorn workers serialise writes and avoid clobber.
        The tmp-file + os.replace atomic swap is kept for crash safety.

        Используем os.open с флагом O_CREAT и режимом 0o600, чтобы файл
        создавался сразу с нужными правами — без window с 0644 (umask-based),
        которую давал plain open().
        Если запись или os.replace упали — удаляем tmp, чтобы не оставлять мусор.
        """
        with self._file_lock():
            # Unique tmp name per call: multiple workers (each with their own
            # threading.Lock) may race to the flock; while one holds
            # LOCK_EX the others have already opened their own tmp fd.
            # A shared static name like api_tokens.tmp would be stolen by
            # os.replace from whichever thread wins, leaving others with a
            # missing source for their os.replace call.
            tmp = self._path.with_name(
                self._path.stem + "." + uuid.uuid4().hex[:8] + ".tmp"
            )
            try:
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(tokens, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, self._path)
                # Update our cached signature so the next _reload_if_stale()
                # on this instance doesn't trigger a spurious re-read.
                self._remember_file_state()
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
