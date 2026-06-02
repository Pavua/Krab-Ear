"""REST API token store (optional Bearer auth for port 5005).

Tokens are stored as SHA-256 hashes in api_tokens.json (chmod 0600).
Raw token is returned once at creation time and never persisted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class RestAuth:
    """Manage API tokens for the REST server."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "api_tokens.json"
        self._lock = threading.Lock()
        self._tokens: list[dict] = self._load()

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
            for entry in self._tokens:
                # Защита от timing-oracle: constant-time сравнение хэшей.
                stored = entry.get("token_hash") or ""
                if hmac.compare_digest(stored, token_hash):
                    entry["last_used"] = datetime.now(timezone.utc).isoformat()
                    self._save(self._tokens)
                    return self._public_meta(entry)
        return None

    @staticmethod
    def _public_meta(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k != "token_hash"}

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

        Используем os.open с флагом O_CREAT и режимом 0o600, чтобы файл
        создавался сразу с нужными правами — без window с 0644 (umask-based),
        которую давал plain open().
        Если запись или os.replace упали — удаляем tmp, чтобы не оставлять мусор.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(tokens, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
