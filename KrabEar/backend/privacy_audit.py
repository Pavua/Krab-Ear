"""PrivacyAuditLogger — singleton для записи событий режима конфиденциальности.

Каждая запись — строка NDJSON в ~/Library/Application Support/KrabEar/privacy_audit.log.
Все операции записи защищены fcntl.flock (как в state_store.py).

W974 (restored W1533): каждая запись содержит HMAC-SHA256 хеш-цепочку:
  - prev_hash: хеш предыдущей записи (None для первой)
  - entry_hash: HMAC-SHA256(key, prev_hash + "|" + json.dumps(body, sort_keys=True))
Ключ хранится в <data_dir>/privacy_audit.key (режим 0o600), генерируется при первом запуске.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.PrivacyAudit")

# Права журнала при СОЗДАНИИ: privacy-метаданные не для чтения всем подряд.
# У существующего файла права не меняются — это решение владельца, не наше.
_LOG_FILE_MODE = 0o600
# Предел окна хвостового поиска tip'а: дальше честнее начать цепочку заново,
# чем читать многомегабайтный лог на каждую запись.
_MAX_TIP_SCAN_BYTES = 4 * 1024 * 1024

_LOG_FILENAME = "privacy_audit.log"

# Каталог журнала переопределяется этой переменной. Нужна для изоляции тестов и
# dev-инстансов от боевого compliance-журнала (инцидент 2026-08-23: 44 907 из
# 50 041 записи оказались тестовым мусором). Читаем os.environ напрямую, а не
# settings из core/config.py: privacy_audit — листовой модуль без проектных
# импортов, config втянул бы цикл через backend.service.
_ENV_DIR_VAR = "KRAB_EAR_PRIVACY_AUDIT_DIR"


def _default_log_path() -> Path:
    """Путь журнала: env-переменная, иначе боевой home-rooted дефолт.

    Резолвится ЛЕНИВО, при создании инстанса, а не на импорте модуля: константа
    привязала бы изоляцию к порядку импортов — тот самый класс мин, что уже
    кусал репозиторий (sys.modules-стабы, chunk-pollution rest_server).

    Пустое/пробельное значение считается незаданным — fail-safe в сторону
    боевого дефолта, иначе опечатка увела бы журнал в текущий каталог.
    """
    raw = os.environ.get(_ENV_DIR_VAR, "")
    if raw.strip():
        candidate = Path(raw).expanduser()
        # Относительный путь тоже считается незаданным: он привязал бы
        # compliance-журнал к текущему каталогу процесса, а это ровно тот исход,
        # от которого guard выше и защищает. Абсолютность требуем явно.
        if candidate.is_absolute():
            return candidate / _LOG_FILENAME
    return Path.home() / "Library" / "Application Support" / "KrabEar" / _LOG_FILENAME


# Имя файла-ключа относительно родительской директории лога
_KEY_FILENAME = "privacy_audit.key"


# ---------------------------------------------------------------------------
# Module-level helpers (W974 / W1533)
# ---------------------------------------------------------------------------

def _load_or_create_key(data_dir: Path) -> bytes:
    """Читает ключ из <data_dir>/privacy_audit.key или создаёт новый.

    Файл создаётся с правами 0o600 (только владелец).
    Запись атомарная: пишем во временный файл, затем rename.

    Args:
        data_dir: директория, где хранится лог (родительская директория лога).

    Returns:
        32-байтный HMAC-ключ.
    """
    key_path = data_dir / _KEY_FILENAME

    if key_path.exists():
        try:
            key = key_path.read_bytes()
            if len(key) == 32:
                return key
            # Некорректная длина — пересоздаём
            logger.warning(
                "PrivacyAuditLogger: ключ имеет неверную длину %d, пересоздаём",
                len(key),
            )
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка чтения ключа, пересоздаём")

    # Генерируем новый ключ
    key = os.urandom(32)
    try:
        # Атомарная запись через временный файл в той же директории
        fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=".privacy_audit.key.")
        try:
            os.write(fd, key)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_path, key_path)
        # Устанавливаем права снова на случай если rename сбросил umask
        key_path.chmod(0o600)
    except Exception:
        logger.exception("PrivacyAuditLogger: ошибка записи ключа")
        # Чистим временный файл если rename не состоялся
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

    return key


def _compute_entry_hash(secret_key: bytes, prev_hash: str | None, entry_dict: dict) -> str:
    """Вычисляет HMAC-SHA256 хеш для записи лога.

    Сообщение = prev_hash + "|" + json.dumps(entry_dict, sort_keys=True).
    Если prev_hash is None, используется пустая строка.

    Args:
        secret_key: 32-байтный секретный ключ.
        prev_hash:  хеш предыдущей записи (None для первой).
        entry_dict: тело записи (без полей prev_hash и entry_hash).

    Returns:
        Hex-строка HMAC-SHA256 дайджеста.
    """
    prev_part = prev_hash if prev_hash is not None else ""
    body_part = json.dumps(entry_dict, sort_keys=True, ensure_ascii=False)
    message = (prev_part + "|" + body_part).encode("utf-8")
    return hmac.new(secret_key, message, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# PrivacyAuditLogger
# ---------------------------------------------------------------------------

class PrivacyAuditLogger:
    """Singleton для записи событий режима конфиденциальности в NDJSON-лог."""

    _instance: "PrivacyAuditLogger | None" = None
    # Класс-уровневый lock для защиты singleton creation (race condition W1027)
    _class_lock: threading.Lock = threading.Lock()

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path: Path = log_path if log_path is not None else _default_log_path()
        self._ensure_parent()
        # Загружаем/создаём HMAC-ключ
        self._secret_key: bytes = _load_or_create_key(self._log_path.parent)
        # Инстанс-уровневый lock: сериализует весь цикл compute→write→update _last_hash
        # Требуется тестом test_log_lock_exists (W1029 F1)
        self._log_lock: threading.Lock = threading.Lock()
        # Читаем существующий лог и вычисляем текущий tip цепочки
        self._last_hash: str | None = self._read_chain_tip()

    def _read_chain_tip_locked(self, fh: Any) -> str | None:
        """Хвост цепочки из ФАЙЛА по уже открытому дескриптору под flock.

        Вызывается только из ``log_event`` при удерживаемом ``LOCK_EX`` — это
        единственный способ узнать актуальный tip, когда в лог пишет несколько
        процессов (backend, REST, тесты): кэш ``self._last_hash`` к этому моменту
        может быть устаревшим на произвольное число чужих записей.

        Читает с КОНЦА расширяющимся окном, а не файл целиком: полное чтение на
        каждую запись дало бы O(N²) на логе в десятки тысяч строк.
        """
        size = fh.seek(0, os.SEEK_END)
        if size == 0:
            return None

        window = 8192
        while True:
            start = max(0, size - window)
            fh.seek(start)
            chunk = fh.read(size - start)
            lines = chunk.split(b"\n")
            # При start > 0 первая строка почти наверняка обрезана посередине.
            if start > 0:
                lines = lines[1:]

            for raw_line in reversed(lines):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(entry, dict) and "entry_hash" in entry:
                    return entry["entry_hash"]

            if start == 0:
                # Дочитали до начала: хешированных записей нет (legacy-лог).
                return None
            if window >= _MAX_TIP_SCAN_BYTES:
                # Хвост целиком из legacy/битых строк. Рвать цепочку молча нельзя.
                logger.warning(
                    "PrivacyAuditLogger: entry_hash не найден в последних %d байтах — "
                    "цепочка начнётся заново",
                    window,
                )
                return None
            window *= 4

    def _read_chain_tip(self) -> str | None:
        """Возвращает entry_hash последней записи с хешем, или None."""
        if not self._log_path.exists():
            return None
        last_hash: str | None = None
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    for raw_line in fh:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if "entry_hash" in entry:
                                last_hash = entry["entry_hash"]
                        except json.JSONDecodeError:
                            pass
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка чтения tip цепочки")
        return last_hash

    @classmethod
    def get_instance(cls, log_path: Path | None = None) -> "PrivacyAuditLogger":
        """Возвращает singleton-экземпляр (создаёт при первом вызове).

        Потокобезопасно: double-checked locking защищает от race на создание
        (W1027 / W1029 singleton concurrency fix).
        """
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = cls(log_path=log_path)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Сбрасывает singleton — используется только в тестах."""
        cls._instance = None

    def _ensure_parent(self) -> None:
        """Создаёт родительскую директорию если она отсутствует."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(
        self,
        category: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Дописывает одну NDJSON-строку в лог с HMAC хеш-цепочкой.

        Args:
            category: категория события (sentry, translation, …).
            action:   действие (blocked, forced_offline, …).
            details:  дополнительные данные (опционально).
        """
        # _log_lock сериализует цикл внутри процесса (W1027 F1 HIGH / W1029 fix),
        # flock — между процессами. Нужны ОБА: тредовый лок не виден соседнему
        # процессу, файловый — не виден соседнему треду того же процесса.
        with self._log_lock:
            # Тело записи (без хеш-полей) — именно от него считается hash
            body: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "category": category,
                "action": action,
                "details": details or {},
            }

            try:
                self._ensure_parent()
                # O_APPEND делает саму запись атомарной, O_RDWR нужен для чтения
                # хвоста, 0o600 применяется ТОЛЬКО при создании файла: журнал
                # приватности не должен быть доступен всем на чтение.
                fd = os.open(
                    self._log_path,
                    os.O_RDWR | os.O_APPEND | os.O_CREAT,
                    _LOG_FILE_MODE,
                )
                with os.fdopen(fd, "rb+") as fh:
                    # 🔴 Хеш ОБЯЗАН вычисляться под тем же локом, что и запись.
                    # Раньше prev_hash брался из кэша ДО flock — между вычислением
                    # и записью успевал вклиниться другой процесс, и цепочка
                    # разветвлялась (инцидент 2026-08-23: разрыв на записи 2784
                    # из 50 041, две записи с одним prev_hash).
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    try:
                        prev_hash = self._read_chain_tip_locked(fh)
                        entry_hash = _compute_entry_hash(self._secret_key, prev_hash, body)

                        entry: dict[str, Any] = dict(body)
                        entry["prev_hash"] = prev_hash
                        entry["entry_hash"] = entry_hash

                        line = json.dumps(entry, ensure_ascii=False) + "\n"
                        fh.write(line.encode("utf-8"))
                        fh.flush()
                        os.fsync(fh.fileno())
                    finally:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

                # Кэш обновляем только после успешной записи. Источник истины —
                # файл; это значение остаётся лишь подсказкой для диагностики.
                self._last_hash = entry_hash

            except Exception:
                logger.exception(
                    "PrivacyAuditLogger: ошибка записи события category=%s action=%s",
                    category,
                    action,
                )

    def verify_chain(self) -> dict[str, Any]:
        """Проверяет целостность HMAC хеш-цепочки лога.

        Проходит лог от начала до конца, перевычисляя каждый entry_hash и
        проверяя соответствие prev_hash. Записи без хеш-полей (legacy) считаются
        валидными и пропускаются (не ломают цепочку).

        Returns:
            dict с ключами:
              - valid (bool): True если цепочка не нарушена.
              - first_broken_index (int | None): индекс первой нарушенной записи.
              - checked (int): количество проверенных записей.
        """
        if not self._log_path.exists():
            return {"valid": True, "first_broken_index": None, "checked": 0}

        entries: list[dict[str, Any]] = []
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    lines = fh.readlines()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

            for raw_line in lines:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(
                        "PrivacyAuditLogger: verify_chain не может разобрать строку: %r",
                        line,
                    )
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка чтения лога при verify_chain")
            return {"valid": False, "first_broken_index": None, "checked": 0}

        checked = len(entries)

        # Для проверки цепочки нам нужно отслеживать ожидаемый prev_hash
        # среди записей, у которых есть хеш-поля.
        # Legacy-записи (без entry_hash) просто сбрасывают цепочку к None —
        # такая же логика как при init.
        expected_prev: str | None = None
        in_chain = False  # True когда мы уже видели хотя бы одну хешированную запись

        for idx, entry in enumerate(entries):
            has_hash_fields = "entry_hash" in entry and "prev_hash" in entry

            if not has_hash_fields:
                # Legacy-запись: сбрасываем ожидаемый prev_hash
                expected_prev = None
                in_chain = False
                continue

            # Запись с хеш-полями
            stored_entry_hash: str = entry["entry_hash"]
            stored_prev_hash: str | None = entry["prev_hash"]

            # Проверяем prev_hash только если мы уже были в цепочке
            if in_chain and stored_prev_hash != expected_prev:
                return {
                    "valid": False,
                    "first_broken_index": idx,
                    "checked": checked,
                }

            # Перевычисляем entry_hash от тела (без хеш-полей)
            body = {k: v for k, v in entry.items() if k not in ("prev_hash", "entry_hash")}
            recomputed = _compute_entry_hash(self._secret_key, stored_prev_hash, body)

            if not hmac.compare_digest(recomputed, stored_entry_hash):
                return {
                    "valid": False,
                    "first_broken_index": idx,
                    "checked": checked,
                }

            expected_prev = stored_entry_hash
            in_chain = True

        return {"valid": True, "first_broken_index": None, "checked": checked}

    def read_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        """Читает последние *limit* записей из лога.

        Args:
            limit: максимальное число записей (от самых последних).

        Returns:
            Список словарей с записями (порядок: от старых к новым).
            Пустой список если файл не существует.
        """
        if not self._log_path.exists():
            return []

        entries: list[dict[str, Any]] = []
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    lines = fh.readlines()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(
                        "PrivacyAuditLogger: не удалось разобрать строку: %r", line
                    )
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка чтения лога")

        # Возвращаем последние *limit* записей
        return entries[-limit:] if limit and len(entries) > limit else entries

    def total_count(self) -> int:
        """Возвращает общее число записей в лог-файле (без ограничения limit)."""
        if not self._log_path.exists():
            return 0
        count = 0
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    for line in fh:
                        if line.strip():
                            count += 1
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка подсчёта записей")
        return count

    def clear(self) -> None:
        """Удаляет файл лога. Идемпотентно — не ошибается если файл не существует.

        WARNING (W957): Этот метод НЕ экспонирован через IPC dispatch.
        Использовать ТОЛЬКО в unit-тестах и явных migration-скриптах.
        Не вызывать из production IPC-пути — разрушает compliance audit trail
        (W952 CRITICAL finding F-1).

        W974/W1533: сбрасывает _last_hash, чтобы после clear() новые записи
        начинали свежую цепочку (prev_hash=None).

        W1768: весь body выполняется под self._log_lock — тем же замком, что
        держит log_event(). Без него clear() мутировал _last_hash и удалял файл
        конкурентно с log_event() → порча HMAC хеш-цепочки / interleaved writes
        (MED data race). clear() НЕ вызывает log_event(), поэтому non-reentrant
        threading.Lock не приводит к deadlock.
        """
        with self._log_lock:
            try:
                self._log_path.unlink(missing_ok=True)
                self._last_hash = None
            except Exception:
                logger.exception("PrivacyAuditLogger: ошибка удаления лога")

    def summarize(self) -> dict[str, Any]:
        """Агрегаты журнала за ОДИН потоковый проход.

        Заменяет в get_privacy_dashboard пару total_count() +
        read_entries(limit=total_events): та читала файл дважды и
        материализовала весь журнал в список ради трёх чисел. На боевом
        50k-журнале это стоило ~0.2 с и росло без потолка.

        Семантика счётчиков сохранена побитово: total инкрементируется до
        разбора строки (как total_count, который считает и битые строки),
        by_type/last_ts наполняются после (как read_entries, который битые
        пропускает). Иначе total_events в дашборде разошёлся бы с суммой by_type.

        Парсинг JSON вынесен за пределы блокировки файла (как в read_entries() и
        verify_chain()): под LOCK_SH только readlines(), потом парсинг и
        агрегация вне критической секции.

        Returns:
            {"total": int, "last_ts": str | None, "by_type": dict[str, int]}
        """
        summary: dict[str, Any] = {"total": 0, "last_ts": None, "by_type": {}}
        if not self._log_path.exists():
            return summary

        lines: list[str] = []
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    lines = fh.readlines()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

            # Парсинг и агрегация вне критической секции
            for raw_line in lines:
                line = raw_line.strip()
                if not line:
                    continue
                summary["total"] += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "PrivacyAuditLogger: не удалось разобрать строку в summarize: %r",
                        line,
                    )
                    continue
                action = str(entry.get("action", "unknown"))
                summary["by_type"][action] = summary["by_type"].get(action, 0) + 1
                ts = entry.get("ts")
                if ts and (summary["last_ts"] is None or ts > summary["last_ts"]):
                    summary["last_ts"] = ts
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка агрегации журнала")

        return summary


# Удобная точка доступа к singleton
def get_privacy_audit_logger(log_path: Path | None = None) -> PrivacyAuditLogger:
    """Возвращает глобальный singleton PrivacyAuditLogger."""
    return PrivacyAuditLogger.get_instance(log_path=log_path)
