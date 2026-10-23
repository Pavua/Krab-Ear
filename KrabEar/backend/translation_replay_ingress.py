"""Узкий авторизованный вход REST-перевода в существующий IPC-журнал."""
from __future__ import annotations

import hmac
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

_TOKEN_NAME = "translation_replay_token"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")


def read_ingress_token(data_dir: Path) -> str | None:
    """Читает только обычный private owner file; FIFO/symlink не блокируют поток."""
    try:
        fd = os.open(Path(data_dir) / _TOKEN_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            value = stream.read(65).decode("ascii")
            return value if _TOKEN.fullmatch(value) else None
    except (OSError, ValueError, UnicodeError):
        return None


class TranslationReplayIngress:
    def __init__(self, manager: Any, data_dir: Path) -> None:
        self._manager = manager
        data_dir = Path(data_dir)
        self._token = read_ingress_token(data_dir)
        if self._token is None:
            try:
                fd = os.open(data_dir / _TOKEN_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(secrets.token_hex(32))
            except OSError:
                pass  # Существующий небезопасный файл не заменяем; write API закрыт.
            self._token = read_ingress_token(data_dir)

    def _authorized(self, params: Any) -> bool:
        value = params.get("translation_token") if isinstance(params, dict) else None
        return (isinstance(value, str) and bool(self._token)
                and len(value) == 64 and value.isascii() and hmac.compare_digest(value, self._token))

    def begin(self, params: Any) -> dict:
        if not self._authorized(params):
            return {"status": "rejected"}
        try:
            if "epoch" not in params:
                return self._manager.translation_epoch()
            epoch = params["epoch"]
            if not isinstance(epoch, str) or len(epoch) != 32:
                return {"status": "invalid"}
            return self._manager.begin_translation_capture(expected_epoch=epoch)
        except Exception:
            return {"status": "error"}  # Не отражаем exception/text/token даже в логах.

    def commit(self, params: Any) -> dict:
        if not self._authorized(params):
            return {"status": "rejected"}
        try:
            return self._manager.commit_translation_capture(params.get("ticket"), params.get("payload"))
        except Exception:
            return {"status": "error"}
