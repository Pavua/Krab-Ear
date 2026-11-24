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
import uuid
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

# Исходы строгой записи (G3-P02-02). Три состояния, а не bool: «не записано» и
# «неизвестно» требуют разных действий владельца, а «неизвестно» нельзя выдавать
# ни за успех, ни за доказанный отказ.
AUDIT_CONFIRMED = "confirmed"      # запись дошла до fsync без исключения
AUDIT_NOT_WRITTEN = "not_written"  # сбой ДО начала записи — на диске ничего не появилось
AUDIT_UNKNOWN = "unknown"          # сбой во время/после записи — запись могла лечь


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

_KEY_LENGTH = 32
# Сколько раз переигрываем «прочитать → (карантин | создать)», если соседний
# процесс успел изменить файл ключа между нашими шагами.
_KEY_ATTEMPTS = 3


class AuditKeyUnavailable(RuntimeError):
    """Ключ журнала не удалось ни прочитать, ни надёжно сохранить на диск.

    G3-P04-01: раньше при сбое записи возвращался ключ, которого нет на диске.
    Записи с ним после рестарта не проверить ничем — это хуже, чем отказ:
    журнал выглядел бы рабочим, а его подписи были бы невоспроизводимы.
    """


def _write_all(fd: int, data: bytes) -> None:
    """os.write может записать меньше запрошенного — дописываем до конца."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("os.write вернул 0 байт")
        view = view[written:]


def _fsync_dir(directory: Path) -> None:
    """Делает долговечной саму запись каталога (link/rename нового ключа)."""
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_key_file(key_path: Path) -> bytes | None:
    """Содержимое файла ключа или None, если файла нет.

    Прочие OSError (EACCES/EIO/...) пробрасываются: временный отказ чтения НЕ
    повод заводить новый ключ — все прежние записи журнала стали бы
    непроверяемыми, а исправный ключ на диске остался бы брошенным.
    """
    try:
        return key_path.read_bytes()
    except FileNotFoundError:
        return None


def _quarantine_key(key_path: Path, bad_len: int) -> str | None:
    """Убирает ключ неверной длины в сторону, ничего не затирая.

    Возвращает имя карантинного файла или None, если файл уже исчез (его
    обработал соседний процесс). Испорченный ключ — улика: сохраняем его байты
    для разбора, а не перетираем новым, как делалось до G3-P04-01.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    name = f"{_KEY_FILENAME}.invalid-{stamp}-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(key_path, key_path.parent / name)
    except FileNotFoundError:
        return None
    _fsync_dir(key_path.parent)
    logger.error(
        "PrivacyAuditLogger: ключ неверной длины %d отправлен в карантин как %s; "
        "записи, подписанные прежним ключом, проверить больше нельзя",
        bad_len,
        name,
    )
    return name


def _publish_new_key(key_path: Path) -> bytes | None:
    """Создаёт новый ключ и публикует его БЕЗ перезаписи существующего.

    Возвращает ключ или None, если соседний процесс опубликовал свой первым
    (тогда читать нужно его). Публикация через ``os.link``: в отличие от
    rename она не перетирает чужой файл, иначе два одновременно стартующих
    процесса (backend и REST) подписывали бы журнал разными ключами.
    """
    key = os.urandom(_KEY_LENGTH)
    fd, tmp_path = tempfile.mkstemp(dir=key_path.parent, prefix=".privacy_audit.key.")
    try:
        try:
            _write_all(fd, key)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp_path, key_path)
        except FileExistsError:
            return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    _fsync_dir(key_path.parent)
    return key


def _load_or_create_key_ex(data_dir: Path) -> tuple[bytes, dict[str, Any] | None]:
    """Ключ журнала и сведения о карантине (если старый ключ был испорчен).

    Исходы:
      * файл есть и длина верна — возвращаем его;
      * длина неверна — файл уходит в карантин, создаётся новый ключ, второй
        элемент кортежа описывает ротацию (её записывает в журнал конструктор);
      * файла нет — создаём новый, не перетирая ключ соседа, выигравшего гонку;
      * ключ не удалось ни прочитать, ни сохранить — ``AuditKeyUnavailable``.

    Каталог блокируется ``flock`` на время всей процедуры: без этого процесс,
    прочитавший испорченный ключ, мог бы отправить в карантин уже ИСПРАВНЫЙ
    ключ, опубликованный соседом между чтением и переименованием.
    """
    key_path = data_dir / _KEY_FILENAME
    quarantine: dict[str, Any] | None = None
    try:
        dir_fd = os.open(data_dir, os.O_RDONLY)
    except OSError as exc:
        raise AuditKeyUnavailable(f"каталог ключа недоступен: {exc.__class__.__name__}") from exc
    try:
        fcntl.flock(dir_fd, fcntl.LOCK_EX)
        for _ in range(_KEY_ATTEMPTS):
            try:
                existing = _read_key_file(key_path)
            except OSError:
                logger.exception("PrivacyAuditLogger: ошибка чтения ключа, ключ не тронут")
                raise
            if existing is not None and len(existing) == _KEY_LENGTH:
                return existing, quarantine
            try:
                if existing is not None:
                    name = _quarantine_key(key_path, len(existing))
                    if name is not None:
                        quarantine = {"key_length": len(existing), "quarantined_as": name}
                    continue
                created = _publish_new_key(key_path)
            except OSError as exc:
                logger.exception("PrivacyAuditLogger: ошибка записи ключа")
                raise AuditKeyUnavailable(
                    f"не удалось сохранить ключ журнала: {exc.__class__.__name__}"
                ) from exc
            if created is not None:
                return created, quarantine
        raise AuditKeyUnavailable("ключ журнала меняется конкурентно, попытки исчерпаны")
    finally:
        os.close(dir_fd)  # закрытие дескриптора снимает flock


def _load_or_create_key(data_dir: Path) -> bytes:
    """Читает ключ из <data_dir>/privacy_audit.key или создаёт новый (0o600)."""
    return _load_or_create_key_ex(data_dir)[0]


# Метка нечитаемой строки в verify_chain (отличается от любого JSON-значения).
_UNPARSABLE = object()


def _log_unparsable(where: str, line_no: int, length: int) -> None:
    """Сообщает о нечитаемой строке журнала БЕЗ её содержимого (G3-P04-02).

    В строке журнала приватности лежат details событий — сырой текст через %r
    утекал бы в общий лог процесса, который читают и пересылают куда шире.
    Номера строки (среди непустых, с 1) и длины достаточно, чтобы найти её.
    """
    logger.warning(
        "PrivacyAuditLogger: %s — нечитаемая строка %d (длина %d байт)",
        where,
        line_no,
        length,
    )


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
        self._secret_key, quarantine = _load_or_create_key_ex(self._log_path.parent)
        # Инстанс-уровневый lock: сериализует весь цикл compute→write→update _last_hash
        # Требуется тестом test_log_lock_exists (W1029 F1)
        self._log_lock: threading.Lock = threading.Lock()
        # Читаем существующий лог и вычисляем текущий tip цепочки
        self._last_hash: str | None = self._read_chain_tip()
        if quarantine is not None:
            # G3-P04-01: смена ключа обязана быть видна в САМОМ журнале, а не
            # только в логе процесса: verify_chain дальше честно покажет обрыв
            # на старых записях, и эта запись объясняет почему.
            self._write_event("privacy_audit", "key_quarantined", quarantine)

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

    @staticmethod
    def _tail_is_torn_locked(fh: Any) -> bool:
        """True, если непустой файл не кончается переводом строки (обрыв записи)."""
        size = fh.seek(0, os.SEEK_END)
        if size == 0:
            return False
        fh.seek(size - 1)
        return fh.read(1) != b"\n"

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

        Best-effort: ошибка записи логируется и НЕ пробрасывается (поведение
        для всех существующих вызывающих не менялось). Кому нужен результат —
        ``log_event_strict``.

        Args:
            category: категория события (sentry, translation, …).
            action:   действие (blocked, forced_offline, …).
            details:  дополнительные данные (опционально).
        """
        self._write_event(category, action, details)

    def log_event_strict(
        self,
        category: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Как ``log_event``, но возвращает исход записи.

        Возвращает одну из констант ``AUDIT_CONFIRMED`` / ``AUDIT_NOT_WRITTEN`` /
        ``AUDIT_UNKNOWN``. Исключений не бросает. Для обязательных
        комплаенс-записей (purge): «complete» вправе опираться только на
        ``AUDIT_CONFIRMED``.
        """
        return self._write_event(category, action, details)

    def _write_event(
        self,
        category: str,
        action: str,
        details: dict[str, Any] | None,
    ) -> str:
        """Общая запись события; возвращает исход (см. константы ``AUDIT_*``)."""
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
            # Фаза нужна, чтобы отличить «доказанно не записано» от «неизвестно»:
            #   pre     — до первого байта записи (open/flock/чтение tip/хеш);
            #   writing — write/flush/fsync начались, исход неопределён;
            #   done    — fsync вернулся без ошибки, запись долговечна.
            phase = "pre"

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
                        if self._tail_is_torn_locked(fh):
                            # G3-P04-02: после краха посреди записи файл кончается
                            # обрывком без "\n". Без разделителя новая запись
                            # склеилась бы с ним в одну нечитаемую строку и
                            # пропала бы вместе с обрывком.
                            line = "\n" + line
                        phase = "writing"
                        fh.write(line.encode("utf-8"))
                        fh.flush()
                        os.fsync(fh.fileno())
                        phase = "done"
                    finally:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

                # Кэш обновляем только после успешной записи. Источник истины —
                # файл; это значение остаётся лишь подсказкой для диагностики.
                self._last_hash = entry_hash
                return AUDIT_CONFIRMED

            except Exception:
                logger.exception(
                    "PrivacyAuditLogger: ошибка записи события category=%s action=%s",
                    category,
                    action,
                )
                if phase == "done":
                    # fsync прошёл — запись долговечна; упал только хвост
                    # (unlock/close). Это не повод считать комплаенс-запись потерянной.
                    return AUDIT_CONFIRMED
                if phase == "pre":
                    return AUDIT_NOT_WRITTEN
                return AUDIT_UNKNOWN

    def verify_chain(self) -> dict[str, Any]:
        """Проверяет целостность HMAC хеш-цепочки лога.

        Проходит лог от начала до конца, перевычисляя каждый entry_hash и
        проверяя соответствие prev_hash. Записи без хеш-полей (legacy) допустимы
        только ПРЕФИКСОМ журнала (до первой хешированной); после неё такая
        запись — нарушение. Первая хешированная запись обязана иметь
        prev_hash=None (иначе снесена голова).

        Нечитаемая строка (не JSON, не UTF-8) после начала цепочки — тоже
        нарушение (G3-P04-02): раньше её молча пропускали, и вставленный мусор
        или оборванная после краха запись давали valid=True. В легаси-префиксе
        она допустима наравне с легаси-записями — его цепочка не защищает.

        Returns:
            dict с ключами:
              - valid (bool): True если цепочка не нарушена.
              - first_broken_index (int | None): индекс первой нарушенной записи
                среди непустых строк журнала (нечитаемые строки тоже считаются).
              - checked (int): количество непустых строк.
              - reason (str | None): причина нарушения (``unparsable_line``,
                ``unhashed_after_chain``, ``bad_hash_fields``, ``head_missing``,
                ``prev_hash_mismatch``, ``entry_hash_mismatch``, ``read_error``).
        """
        def broken(idx: int | None, reason: str, checked: int) -> dict[str, Any]:
            return {"valid": False, "first_broken_index": idx, "checked": checked, "reason": reason}

        if not self._log_path.exists():
            return {"valid": True, "first_broken_index": None, "checked": 0, "reason": None}

        # Каждая непустая строка — либо разобранный объект, либо _UNPARSABLE.
        entries: list[Any] = []
        try:
            with self._log_path.open("rb") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    lines = fh.readlines()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка чтения лога при verify_chain")
            return broken(None, "read_error", 0)

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                _log_unparsable("verify_chain", len(entries) + 1, len(line))
                entries.append(_UNPARSABLE)

        checked = len(entries)

        # Для проверки цепочки нам нужно отслеживать ожидаемый prev_hash
        # среди записей, у которых есть хеш-поля.
        # Legacy-записи (без entry_hash) просто сбрасывают цепочку к None —
        # такая же логика как при init.
        expected_prev: str | None = None
        in_chain = False  # True когда мы уже видели хотя бы одну хешированную запись

        for idx, entry in enumerate(entries):
            if entry is _UNPARSABLE:
                if in_chain:
                    return broken(idx, "unparsable_line", checked)
                continue  # обрывок в легаси-префиксе, цепочкой не защищён

            has_hash_fields = isinstance(entry, dict) and "entry_hash" in entry and "prev_hash" in entry

            if not has_hash_fields:
                if in_chain:
                    # Запись без хеш-полей ПОСЛЕ первой хешированной: писатель
                    # всегда ставит оба поля, значит это стёртые хеши (подделка
                    # без ключа) или вставка. Легаси допустим только ПРЕФИКСОМ.
                    return broken(idx, "unhashed_after_chain", checked)
                # Легаси-префикс (журнал до появления хеш-цепочки)
                continue

            # Запись с хеш-полями
            stored_entry_hash = entry["entry_hash"]
            stored_prev_hash = entry["prev_hash"]
            # Писатель кладёт только hex-строки (prev_hash ещё и None у головы).
            # Иной тип — подделка; compare_digest на нём бросил бы TypeError.
            if not isinstance(stored_entry_hash, str) or not (
                stored_prev_hash is None or isinstance(stored_prev_hash, str)
            ):
                return broken(idx, "bad_hash_fields", checked)

            # Первая хешированная запись начинает цепочку с prev_hash=None
            # (писатель так и делает, в т.ч. после легаси-префикса). Иное —
            # снесена голова журнала.
            if not in_chain and stored_prev_hash is not None:
                return broken(idx, "head_missing", checked)

            # Дальше prev_hash обязан совпасть с entry_hash предыдущей записи
            if in_chain and stored_prev_hash != expected_prev:
                return broken(idx, "prev_hash_mismatch", checked)

            # Перевычисляем entry_hash от тела (без хеш-полей)
            body = {k: v for k, v in entry.items() if k not in ("prev_hash", "entry_hash")}
            recomputed = _compute_entry_hash(self._secret_key, stored_prev_hash, body)

            if not hmac.compare_digest(recomputed.encode("ascii"), stored_entry_hash.encode("utf-8")):
                return broken(idx, "entry_hash_mismatch", checked)

            expected_prev = stored_entry_hash
            in_chain = True

        return {"valid": True, "first_broken_index": None, "checked": checked, "reason": None}

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

            line_no = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                line_no += 1
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    _log_unparsable("read_entries", line_no, len(line))
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
                    _log_unparsable("summarize", summary["total"], len(line))
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
