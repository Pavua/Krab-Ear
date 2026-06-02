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

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import threading
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
        self._tokens: list[dict] = self._load()
        # mtime of api_tokens.json at the last successful load (float seconds).
        # Initialised to 0.0 when the file doesn't exist yet so the first
        # write/create still gets recorded correctly.
        self._file_mtime: float = self._read_mtime()

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
            "scopes": list(scopes) if scopes else ["*"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used": None,
        }
        with self._lock:
            self._reload_if_stale()
            self._tokens.append(entry)
            self._save(self._tokens)
        return raw, self._public_meta(entry)

    def list_tokens(self) -> list[dict]:
        """Return all tokens as public metadata (no hashes)."""
        with self._lock:
            return [self._public_meta(t) for t in self._tokens]

    def revoke_token(self, token_id: str) -> bool:
        """Remove token by id.  Returns True if found and removed."""
        with self._lock:
            self._reload_if_stale()
            before = len(self._tokens)
            self._tokens = [t for t in self._tokens if t["id"] != token_id]
            if len(self._tokens) < before:
                self._save(self._tokens)
                return True
            return False

    def verify_token(self, raw_token: str) -> Optional[dict]:
        """Verify raw_token.  Updates last_used and returns meta or None."""
        if not raw_token:
            return None
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with self._lock:
            self._reload_if_stale()
            for entry in self._tokens:
                # Защита от timing-oracle: constant-time сравнение хэшей.
                stored = entry.get("token_hash") or ""
                if hmac.compare_digest(stored, token_hash):
                    entry["last_used"] = datetime.now(timezone.utc).isoformat()
                    self._save(self._tokens)
                    return self._public_meta(entry)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _public_meta(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k != "token_hash"}

    def _read_mtime(self) -> float:
        """Return mtime of the token file, or 0.0 if it does not exist."""
        try:
            return os.stat(self._path).st_mtime
        except OSError:
            return 0.0

    def _reload_if_stale(self) -> None:
        """Re-read tokens from disk when the file mtime changed.

        Must be called while self._lock is held.

        Fast path: os.stat is cheap (~1 µs).  If the mtime is unchanged we
        skip the full JSON parse entirely.
        """
        current_mtime = self._read_mtime()
        if current_mtime != self._file_mtime:
            self._tokens = self._load()
            self._file_mtime = current_mtime

    def _load(self) -> list[dict]:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return []
        return []

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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Use a dedicated lock file (never renamed/replaced) so flock() always
        # operates on the same inode — immune to the "os.replace replaces
        # the locked inode" race that would allow two workers to hold
        # conflicting exclusive locks on different inodes simultaneously.
        self._lock_path.touch(exist_ok=True)
        with open(self._lock_path, "r+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                # Unique tmp name per call: multiple workers (each with their
                # own threading.Lock) may race to the flock; while one holds
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
                    # Update our cached mtime so the next _reload_if_stale()
                    # on this instance doesn't trigger a spurious re-read.
                    self._file_mtime = self._read_mtime()
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
