"""Cross-process LM Studio «brain» lease coordination.

Цель: Krab Ear и Krab userbot оба используют LM Studio «brain» (30-35B MLX модель
на том же Metal GPU). Одновременный active inference → GPU stuck → reboot (подтверждено
в production). Этот модуль реализует кооперативный лиз через flock + JSON payload,
так что два процесса могут передавать «brain» через общий lock-файл без дополнительного
сервера координации.

ВАЖНО — graceful degradation:
    Каждая публичная функция обёрнута в try/except и НИКОГДА не пробрасывает исключения
    в вызывающий код. Любая ошибка (нет fcntl, нет прав, диск полон, сломан JSON)
    → WARNING в лог и безопасное значение:
        - acquire_brain_lease → True (Ear не блокируется)
        - release_brain_lease → no-op
        - current_lease_holder → None
    Lease — это оптимизация, а не hard dependency recording pipeline.

Lock path (кросс-проектный contract):
    Default: ~/.openclaw/lm_studio_brain.lock
    Override: аргумент функции ИЛИ env var KRAB_EAR_BRAIN_LEASE_PATH.

Payload schema (JSON в lock file):
    {
        "owner":       str,   # e.g. "krab_ear" | "krab"
        "pid":         int,
        "acquired_ts": float,  # time.time()
        "exp_ts":      float   # time.time() + ttl_sec
    }

Семантика OS flock (LOCK_EX|LOCK_NB):
    flock держим ТОЛЬКО на время чтения/записи payload (≪1 мс), НЕ на всё время лиза.
    Логический лиз = TTL в payload. Краш вызывающего процесса не «вешает» lock-файл:
    следующий acquire заберёт лиз по истёкшему TTL.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("KrabEar.BrainLease")

# Default shared lock path (кросс-проектный contract с Krab side).
_DEFAULT_LOCK_PATH = Path.home() / ".openclaw" / "lm_studio_brain.lock"

# Default TTL when not specified by caller.
_DEFAULT_TTL_SEC = 30.0


def _resolve_lock_path(lock_path: Optional[Path] = None) -> Path:
    """Resolve the effective lock path from arg or env var."""
    if lock_path is not None:
        return Path(lock_path)
    env_val = os.environ.get("KRAB_EAR_BRAIN_LEASE_PATH", "").strip()
    if env_val:
        return Path(env_val)
    return _DEFAULT_LOCK_PATH


def _ensure_parent(path: Path) -> None:
    """mkdir -p the lock file's parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)


def _read_payload(fd: int) -> Optional[dict]:
    """Read and parse JSON payload from the beginning of fd. Returns None on any error."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = b""
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            raw += chunk
        if not raw.strip():
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _write_payload(fd: int, payload: dict, path: Path) -> None:
    """Overwrite the lock file with JSON payload atomically via temp-file + rename.

    The flock (LOCK_EX) is already held by the caller for the whole read-modify-write
    cycle, so the rename is logically atomic with respect to other flocking processes.
    Building the bytes BEFORE any truncation ensures the old payload survives an ENOSPC:
    if the temp-file write fails, the original ``path`` content is untouched and ``fd``
    still holds the flock so the caller can unlock cleanly.
    """
    raw = json.dumps(payload).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".brain_lease_tmp_")
    try:
        os.write(tmp_fd, raw)
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_path, str(path))
    except Exception:
        if tmp_fd != -1:
            os.close(tmp_fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def acquire_brain_lease(
    owner: str,
    ttl_sec: float = _DEFAULT_TTL_SEC,
    lock_path: Optional[Path] = None,
) -> bool:
    """Try to acquire the cross-process brain lease.

    Args:
        owner:     Logical owner name, e.g. "krab_ear" or "krab".
        ttl_sec:   Lease duration in seconds. A process crash leaves the lease
                   expired after this duration — the next caller reclaims it.
        lock_path: Override the lock file path (for testing). Falls back to
                   KRAB_EAR_BRAIN_LEASE_PATH env var → ~/.openclaw/lm_studio_brain.lock.

    Returns:
        True  — lease acquired (or was already held by this owner, or graceful-degraded).
        False — lease held by a *different* owner and not yet expired.

    Guarantees:
        NEVER raises. Any filesystem / fcntl error → logs WARNING, returns True so
        Ear is never blocked by lease machinery.
    """
    try:
        path = _resolve_lock_path(lock_path)
        _ensure_parent(path)

        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Acquire OS flock briefly — only to atomically read+write the payload.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another process is mid-write; this is a transient <1 ms write-race,
                # NOT a "held by another owner" denial. Graceful-degrade → True so Ear
                # is never blocked by flock write contention (matches outer except contract).
                logger.debug("BrainLease: flock LOCK_NB contention for owner=%r — graceful True", owner)
                return True

            now = time.time()
            payload = _read_payload(fd)

            if payload is not None:
                existing_owner = payload.get("owner", "")
                exp_ts = float(payload.get("exp_ts", 0.0))
                if existing_owner != owner and now < exp_ts:
                    # Held by someone else AND not expired — we cannot acquire.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    logger.debug(
                        "BrainLease: acquire denied — held by %r (expires in %.1fs)",
                        existing_owner, exp_ts - now,
                    )
                    return False

            # Free (no payload) OR expired OR same owner — write our claim.
            new_payload = {
                "owner": owner,
                "pid": os.getpid(),
                "acquired_ts": now,
                "exp_ts": now + ttl_sec,
            }
            _write_payload(fd, new_payload, path)
            fcntl.flock(fd, fcntl.LOCK_UN)

            logger.info(
                "BrainLease: acquired by %r (pid=%d, ttl=%.0fs)",
                owner, os.getpid(), ttl_sec,
            )
            return True

        finally:
            os.close(fd)

    except Exception as exc:
        logger.warning(
            "BrainLease: acquire error (graceful degradation — returning True): %s",
            exc,
        )
        return True  # Never block recording on lease machinery failure.


def release_brain_lease(
    owner: str,
    lock_path: Optional[Path] = None,
) -> None:
    """Release the brain lease if currently held by `owner`.

    No-op (and no error) if the lease is held by someone else or does not exist.

    Guarantees:
        NEVER raises.
    """
    try:
        path = _resolve_lock_path(lock_path)
        if not path.exists():
            return

        fd = os.open(str(path), os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            payload = _read_payload(fd)

            if payload is not None and payload.get("owner") == owner:
                os.ftruncate(fd, 0)
                logger.info("BrainLease: released by %r", owner)
            else:
                current = payload.get("owner") if payload else None
                logger.debug(
                    "BrainLease: release skipped — current owner is %r, caller is %r",
                    current, owner,
                )

            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    except FileNotFoundError:
        pass  # Already gone — release is a no-op.
    except Exception as exc:
        logger.warning("BrainLease: release error (ignored): %s", exc)


def current_lease_holder(
    lock_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return the current (non-expired) lease payload, or None.

    Returns:
        dict with keys {owner, pid, acquired_ts, exp_ts} if lease is held and
        not expired, else None.

    Guarantees:
        NEVER raises.
    """
    try:
        path = _resolve_lock_path(lock_path)
        if not path.exists():
            return None

        fd = os.open(str(path), os.O_RDONLY)
        try:
            # Use LOCK_SH for reading — non-blocking; on failure still read (best-effort).
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                payload = _read_payload(fd)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                # Another process is writing — best-effort read without lock.
                payload = _read_payload(fd)
        finally:
            os.close(fd)

        if payload is None:
            return None

        exp_ts = float(payload.get("exp_ts", 0.0))
        if time.time() >= exp_ts:
            return None  # Expired.

        return payload

    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("BrainLease: current_lease_holder error (returning None): %s", exc)
        return None
