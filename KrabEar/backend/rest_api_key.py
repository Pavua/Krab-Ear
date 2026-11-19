"""Ключ локального REST по умолчанию (решение владельца 2026-11-15).

Раньше при пустых ``REST_API_KEY`` и ``REST_API_AUTH_ENABLED=False`` защищённые
маршруты отвечали любому локальному процессу. ``ensure_rest_api_key`` вызывается
при старте реального сервера (in-process в ``service.py`` и standalone в
``rest_server``): если ничего не настроено явно, он читает или создаёт
``<data_dir>/rest_api_key`` (0600) и кладёт значение в ``settings.REST_API_KEY``;
дальше работает прежний режим «legacy key» без правок ``require_api_key``.

Клиенты (Swift-агент, Voice Gateway, Краб) читают тот же файл или env
``KRAB_EAR_REST_API_KEY``. ``/health`` ключа не требует.

Инварианты:
  * явный ``REST_API_KEY``, ``REST_API_AUTH_ENABLED`` и опт-аут
    ``REST_API_ALLOW_ANONYMOUS`` уважаются, файл при этом не создаётся;
  * сбой чтения существующего файла НЕ открывает REST и не перезаписывает файл:
    процесс получает временный случайный ключ (fail-closed);
  * параллельные старты (backend и standalone REST) сходятся на одном ключе:
    файл создаётся атомарно через ``os.link`` из приватного tmp.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from core.config import settings

logger = logging.getLogger("KrabEar.Backend.RestApiKey")

KEY_FILENAME = "rest_api_key"
_MIN_KEY_LEN = 32


def _read_valid(path: Path) -> str | None:
    """Ключ из файла или None, если файла нет / содержимое негодно. OSError пробрасывается."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return raw if len(raw) >= _MIN_KEY_LEN else None


def _create_atomically(path: Path, key: str) -> bool:
    """Создаёт файл с ключом, только если его ещё нет. True — создан нами."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".rest_api_key.")
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (key + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _replace_invalid(path: Path, key: str) -> None:
    """Заменяет негодный (короткий/пустой) файл новым ключом атомарно."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".rest_api_key.")
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (key + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def _key_lock(base: Path):
    """Эксклюзивный межпроцессный flock на весь цикл чтение → создание/замена ключа.

    Без него два одновременных старта (backend и standalone REST) оба видели негодный
    или отсутствующий файл и каждый записывал СВОЙ ключ: клиенты получали разные ключи.
    Блокировка берётся на отдельном lock-файле (его inode не заменяется).
    """
    fd = os.open(base / ".rest_api_key.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _tighten_mode(path: Path) -> None:
    """Принятый ключ не должен читаться другими: приводим права к 0600."""
    try:
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        logger.warning("REST: не удалось ограничить права файла ключа %s до 0600", path, exc_info=True)


def _obtain_key(data_dir: Any) -> str:
    base = Path(data_dir)
    path = base / KEY_FILENAME
    try:
        base.mkdir(parents=True, exist_ok=True)
        with _key_lock(base):
            return _obtain_key_locked(base, path)
    except OSError:
        logger.exception(
            "REST: ключ %s недоступен — работаем с временным ключом процесса "
            "(auth остаётся включён, файл не тронут)",
            path,
        )
    return secrets.token_urlsafe(32)


def _obtain_key_locked(base: Path, path: Path) -> str:
    try:
        for _ in range(3):
            existing = _read_valid(path)
            if existing:
                _tighten_mode(path)
                return existing
            candidate = secrets.token_urlsafe(32)
            if path.exists():
                # Файл есть, но негоден (обрезан/пуст): заменяем атомарно.
                _replace_invalid(path, candidate)
                return candidate
            if _create_atomically(path, candidate):
                return candidate
            # Конкурент успел создать файл первым: перечитываем его.
        existing = _read_valid(path)
        if existing:
            _tighten_mode(path)
            return existing
    except OSError:
        raise
    return secrets.token_urlsafe(32)


def ensure_rest_api_key(data_dir: Any) -> str | None:
    """Включает Bearer-auth по умолчанию. Возвращает выданный ключ или None.

    None — ничего не делалось: ключ/токен-стор заданы явно или включён опт-аут.
    """
    if getattr(settings, "REST_API_ALLOW_ANONYMOUS", False):
        logger.warning("REST: REST_API_ALLOW_ANONYMOUS=true — защищённые маршруты открыты без ключа")
        return None
    if getattr(settings, "REST_API_AUTH_ENABLED", False):
        return None
    if str(getattr(settings, "REST_API_KEY", "") or "").strip():
        return None
    key = _obtain_key(data_dir)
    settings.REST_API_KEY = key
    logger.info("REST: Bearer-auth включён по умолчанию (ключ в %s/%s)", data_dir, KEY_FILENAME)
    return key
