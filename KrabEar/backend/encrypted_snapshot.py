"""A5.2b1 — encrypted snapshot: подготовка и фиксация (write-only протокол).

Канон — спека `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`
§5 (шаги 1–6, абзацы про backup/manifest) и карточка
`docs/superpowers/plans/2026-09-26-a52b1-encrypted-snapshot.md`.

Протокол (спека §5, порядок обязателен):

  1. Проверить типы/контейнмент, политику, доступность ключа и отсутствие
     незавершённой операции. Symlink из реестра НЕ разыменовывается.
  2. Подготовить приватный staging и полностью собрать encrypted snapshot всех
     десяти журналов. Уже ENC1-строки ПРОВЕРЯЮТСЯ РАСШИФРОВКОЙ (tampered/
     malformed → отказ, а не молчаливый skip), plaintext-строки шифруются.
     Каждая выходная строка после расшифровки совпадает с исходной.
  3. Fsync staged-файлов и каталога. Durable manifest: только фиксированные
     имена, размер/хэш CIPHERTEXT, transaction ID, состояние и версия — без
     текста, ключа и хэшей plaintext.
  4. Durable `COMMITTING` — ДО первой замены. Повторная проверка fingerprint
     источников — перед заменой.
  5. Сохранить проверенный snapshot до завершения замен + fsync каталога.
  6. `COMMITTED` — только после read-back всех файлов.

Инварианты (нарушение = CRITICAL):
  * реестр — только ``state_store.history_journal_paths(data_dir)``; никаких
    glob'ов, никаких ``settings.json`` в payload (спека §1/§5: settings
    отделены и не могут понизить policy);
  * fingerprint источников НИКОГДА не попадает в manifest: журналы могут быть
    plaintext, и их sha256 был бы хэшем plaintext;
  * ``COMMITTED`` невозможен без успешного read-back всех файлов;
  * crash на ``COMMITTING``: никакого авто-отката в plaintext и никакого нового
    ключа — только fail-closed признак (``recover_pending_state``); реальная
    доказываемость — b2.

Restore и докатка recovery — b2 (в этом модуле их нет намеренно).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.state_store import (
    HISTORY_JOURNAL_FILENAMES,
    history_flock,
    history_journal_paths,
)

logger = logging.getLogger("KrabEar.Backend.EncryptedSnapshot")

SNAPSHOT_MANIFEST_VERSION = 1
SNAPSHOT_MANIFEST_FILENAME = "snapshot_manifest.json"

STATE_PREPARED = "PREPARED"
STATE_COMMITTING = "COMMITTING"
STATE_COMMITTED = "COMMITTED"

# Машинно-читаемые причины отказа (для IPC/логов/UI и для b2-доказ).
REASON_CRYPTO_UNAVAILABLE = "snapshot_crypto_unavailable"
REASON_POLICY_OFF = "snapshot_policy_off"
REASON_POLICY_UNAVAILABLE = "snapshot_policy_unavailable"
REASON_SOURCE_SYMLINK = "snapshot_source_symlink"
REASON_SOURCE_UNREADABLE = "snapshot_source_unreadable"
REASON_LINE_TAMPERED = "snapshot_line_tampered"
REASON_ROUNDTRIP_MISMATCH = "snapshot_roundtrip_mismatch"
REASON_PREPARED_MISSING = "snapshot_prepared_missing"
REASON_FINGERPRINT_MISMATCH = "snapshot_fingerprint_mismatch"
REASON_PENDING_OPERATION = "snapshot_pending_operation"
REASON_DESTINATION_EXISTS = "snapshot_destination_exists"
REASON_READBACK_FAILED = "snapshot_readback_failed"
REASON_PUBLISH_FAILED = "snapshot_publish_failed"
REASON_MANIFEST_INVALID = "snapshot_manifest_invalid"
REASON_OUTSIDE_BACKUPS_ROOT = "snapshot_outside_backups_root"
REASON_PERMISSIONS_FAILED = "snapshot_permissions_failed"
REASON_FSYNC_FAILED = "snapshot_fsync_failed"
REASON_RECOVERY_PENDING = "snapshot_recovery_pending"
REASON_STALE_STAGING = "snapshot_stale_staging"


class SnapshotOperationRefused(Exception):
    """Snapshot невозможен/небезопасен — операция отклонена.

    ``reason`` — машинно-читаемый код (см. ``REASON_*``). Наличие признака
    ``pending`` означает, что на диске осталась незавершённая транзакция
    (состояние COMMITTING) — безопасный путь в b1: ничего не откатываем.
    """

    def __init__(self, reason: str, message: str = "", *, pending: bool = False) -> None:
        self.reason = reason
        self.pending = pending
        super().__init__(message or reason)


# ----------------------------------------------------------------------
# Низкоуровневые помощники (durability)
# ----------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    """fsync каталога. Ошибка → отказ: без неё порядок COMMITTING недоказуем."""
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDONLY)
        os.fsync(fd)
    except OSError as exc:
        raise SnapshotOperationRefused(
            REASON_FSYNC_FAILED, f"fsync каталога не удался: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if fd != -1:
            os.close(fd)


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _write_file_durable(path: Path, blob: bytes) -> None:
    """Запись файла + fsync файла (запись каталога — отдельно)."""
    fd = -1
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, blob)
        os.fsync(fd)
    except OSError as exc:
        raise SnapshotOperationRefused(
            REASON_FSYNC_FAILED, f"запись {path.name} не удалась: {exc}"
        ) from exc
    finally:
        if fd != -1:
            os.close(fd)


def _write_manifest_atomic(snapshot_dir: Path, manifest: dict) -> None:
    """Финальная/промежуточная запись манифеста атомарно (tmp + replace + fsync)."""
    blob = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    tmp = snapshot_dir / f"{SNAPSHOT_MANIFEST_FILENAME}.tmp"
    _write_file_durable(tmp, blob)
    os.replace(tmp, snapshot_dir / SNAPSHOT_MANIFEST_FILENAME)
    _fsync_dir(snapshot_dir)


def _read_manifest(snapshot_dir: Path) -> dict | None:
    """Читает манифест. ``None`` — манифеста нет; нечитаемый — отказ."""
    path = snapshot_dir / SNAPSHOT_MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID, f"манифест {snapshot_dir.name} не читается: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != SNAPSHOT_MANIFEST_VERSION:
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID,
            f"манифест {snapshot_dir.name}: неизвестный формат/версия",
        )
    return payload


# ----------------------------------------------------------------------
# Реестр, fingerprint, построчное шифрование
# ----------------------------------------------------------------------


def _registry(data_dir: Path) -> tuple[Path, ...]:
    """Ровно десять управляемых журналов. Никаких glob'ов, никакого settings.json."""
    paths = history_journal_paths(Path(data_dir))
    if len(paths) != 10 or tuple(p.name for p in paths) != HISTORY_JOURNAL_FILENAMES:
        # Страховка от молчаливого изменения реестра в state_store.
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID, "реестр управляемых журналов изменился"
        )
    return paths


def _source_fingerprint(data_dir: Path) -> dict[str, dict[str, Any] | None]:
    """Fingerprint ИСТОЧНИКОВ (size + sha256). Только в памяти, НЕ в manifest.

    Журналы могут быть plaintext — их sha256 был бы хэшем plaintext, который
    спека §5 запрещает сохранять.
    """
    result: dict[str, dict[str, Any] | None] = {}
    for path in _registry(data_dir):
        if path.is_symlink():
            raise SnapshotOperationRefused(
                REASON_SOURCE_SYMLINK, f"{path.name} — symlink не разыменовывается"
            )
        if not path.exists():
            result[path.name] = None
            continue
        try:
            blob = path.read_bytes()
        except OSError as exc:
            raise SnapshotOperationRefused(
                REASON_SOURCE_UNREADABLE, f"{path.name} не читается: {exc}"
            ) from exc
        result[path.name] = {"size": len(blob), "sha256": _sha256(blob)}
    return result


def _split_ndjson_lines(text: str) -> list[str]:
    """Делит ТОЛЬКО по ``\\n`` — как writer и как text-mode reader журналов.

    🔴 ``str.splitlines()`` здесь неприменим: он дополнительно делит по
    ``\\v \\f \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029``, которых writer
    (``json.dumps(..., ensure_ascii=False) + "\\n"``) никогда не ставит, а
    reader (``for line in fh``) никогда не видит. Наивный splitlines рвал одну
    NDJSON-запись на 2–3 ENC1-строки и при этом оставлял снимок в состоянии
    COMMITTED — тихая порча данных, которую read-back не ловил (сверял он сам с
    собой же). Хвостовой перевод строки сохраняется точно.
    """
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # перевод строки в конце файла, а не пустая запись
    return lines


def _encrypt_journal(source: Path, crypto: Any) -> bytes:
    """Построчно: ENC1 → проверка расшифровкой, plaintext → шифрование.

    Отказ при любой нечитаемой/подделанной строке: молчаливый skip означал бы
    потерю данных при «успешном» снимке.

    Каждая выходная строка ОБЯЗАНА расшифровываться ровно в исходную, и число
    выходных строк обязано совпасть с числом исходных (round-trip-сторож).
    Проверка обязательна: снимок, который нельзя воспроизвести побайтово, не
    должен получать состояние PREPARED/COMMITTED.

    Отсутствующий журнал — валидная ПУСТАЯ запись реестра (спека §5: набор
    всегда полный); «есть, но не читается» — уже отказ.
    """
    if not source.exists():
        return b""
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SnapshotOperationRefused(
            REASON_SOURCE_UNREADABLE, f"{source.name} не читается: {exc}"
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotOperationRefused(
            REASON_SOURCE_UNREADABLE, f"{source.name}: не UTF-8: {exc}"
        ) from exc

    src_lines = _split_ndjson_lines(text)
    out_lines: list[str] = []
    for lineno, line in enumerate(src_lines, start=1):
        if crypto.is_encrypted(line):
            try:
                crypto.decrypt_line(line)
            except Exception as exc:  # noqa: BLE001 — tampered/malformed
                raise SnapshotOperationRefused(
                    REASON_LINE_TAMPERED,
                    f"{source.name}:{lineno}: ENC1-строка не расшифровывается: "
                    f"{type(exc).__name__}",
                ) from exc
            out_lines.append(line)  # проверена расшифровкой — сохраняем байт-в-байт
        else:
            out_lines.append(crypto.encrypt_line(line))

    # --- round-trip-сторож: снимок обязан воспроизводиться побайтово ---
    if len(out_lines) != len(src_lines):
        raise SnapshotOperationRefused(
            REASON_ROUNDTRIP_MISMATCH,
            f"{source.name}: число строк изменилось: {len(src_lines)} → {len(out_lines)}",
        )
    for lineno, (src_line, out_line) in enumerate(zip(src_lines, out_lines), start=1):
        if not crypto.is_encrypted(out_line):
            raise SnapshotOperationRefused(
                REASON_ROUNDTRIP_MISMATCH,
                f"{source.name}:{lineno}: выходная строка не ENC1",
            )
        try:
            decoded = crypto.decrypt_line(out_line)
        except Exception as exc:  # noqa: BLE001
            raise SnapshotOperationRefused(
                REASON_ROUNDTRIP_MISMATCH,
                f"{source.name}:{lineno}: выходная строка не расшифровывается: "
                f"{type(exc).__name__}",
            ) from exc
        if not crypto.is_encrypted(src_line) and decoded != src_line:
            raise SnapshotOperationRefused(
                REASON_ROUNDTRIP_MISMATCH,
                f"{source.name}:{lineno}: расшифровка выходной строки не совпала "
                "с исходной",
            )

    if not out_lines:
        return b""
    body = "\n".join(out_lines)
    if text.endswith("\n"):
        body += "\n"
    return body.encode("utf-8")


# ----------------------------------------------------------------------
# Где лежат снимки и что можно восстанавливать
# ----------------------------------------------------------------------

# Приватный корень staging внутри backups. Имя dot-prefixed — чтобы staging
# НИКОГДА не всплывал в списке бэкапов (в т.ч. legacy restore) и не выглядел
# как готовый бэкап. Публикация остаётся одной атомарной os.replace внутри
# одного filesystem: backups/.staging/<txid> → backups/<snapshot>.
STAGING_ROOT_NAME = ".staging"

# Каталоги, которые legacy restore ПРАВЕДОМ не может распознать: в них лежат
# ENC1-строки, а legacy restore копирует их через copy2 как есть.
SNAPSHOT_DIR_PREFIXES = ("snapshot_", "auto_snapshot_")

# Единственные каталоги, которые legacy restore понимает.
LEGACY_BACKUP_DIR_PREFIXES = ("backup_", "auto_backup_")

UNSUPPORTED_BACKUP_REASON = "unsupported_backup_format"


def classify_backup_dir(path: Any) -> str:
    """Классифицирует каталог в backups: ``legacy`` / ``snapshot`` / ``unsupported``.

    ``legacy``     — каталог, который restore вправе трогать (backup_*/auto_backup_*).
    ``snapshot``   — encrypted snapshot нового протокола (восстановление — b2).
    ``unsupported``— всё остальное: dot-prefixed (staging/мусор) и посторонние имена.

    Fail-closed по умолчанию: неизвестное имя НЕ считается legacy-бэкапом.
    Раньше подходил любой каталог с ``history.ndjson``, из-за чего снимок можно
    было скормить restore и затереть живую историю шифротекстом.
    """
    p = Path(path)
    if p.name.startswith("."):
        return "unsupported"
    if (p / SNAPSHOT_MANIFEST_FILENAME).exists():
        return "snapshot"
    if p.name.startswith(LEGACY_BACKUP_DIR_PREFIXES):
        return "legacy"
    return "unsupported"


def _backups_root(data_dir: Any) -> Path:
    return (Path(data_dir) / "backups").resolve()


def _require_inside_backups(data_dir: Any, dest: Any) -> Path:
    """Ограничивает снимок РАЗРЕШЁННЫМ корнем backups.

    Что проверяется (и это правда): снимок не может оказаться в постороннем
    каталоге, не может выйти через ``..`` и не может быть уведён наружу
    симлинком ВНУТРИ backups. Сравниваются разыменованные пути, поэтому подмена
    промежуточного компонента отсекается.

    Чего эта проверка НЕ делает (N2): если САМ ``<data_dir>/backups`` является
    симлинком, корень переносится целиком — снимок окажется там, куда реально
    указывает backups. Это НЕ обход: backups на другом томе — легальная
    конфигурация, и снимок обязан лежать рядом с остальными бэкапами. Наружу
    уходит только сам backups, а это уже отдельный (и куда более серьёзный)
    уровень: symlink внутри data_dir означает, что у процесса есть права на
    запись в data_dir. Возвращается РАЗЫМЕНОВАННЫЙ путь, чтобы вызывающий
    отчитывался о фактическом расположении снимка, а не о кажущемся.
    """
    root = _backups_root(data_dir)
    resolved = Path(dest).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise SnapshotOperationRefused(
            REASON_OUTSIDE_BACKUPS_ROOT,
            f"{resolved} находится вне {root} — снимок обязан лежать в backups/",
        )
    return resolved


def _staging_dir_for(backups_root: Path, transaction_id: str) -> Path:
    return Path(backups_root) / STAGING_ROOT_NAME / f".tx-{transaction_id}"


# ----------------------------------------------------------------------
# Pending-транзакции
# ----------------------------------------------------------------------


def _snapshot_dirs(backups_root: Path) -> list[Path]:
    """Опубликованные каталоги-снимки внутри backups-корня (без dot-prefixed)."""
    root = Path(backups_root)
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def _unpublished_staging_dirs(backups_root: Path) -> list[Path]:
    """Неопубликованные staging-каталоги (подготовка началась, замены не было)."""
    staging_root = Path(backups_root) / STAGING_ROOT_NAME
    if not staging_root.is_dir():
        return []
    return sorted(p for p in staging_root.iterdir() if p.is_dir())


def _candidate_signature(paths: list[Path]) -> tuple:
    """Дёшевая сигнатура набора каталогов-кандидатов (stat, без чтения файлов).

    Учитывает mtime/размер манифеста, поэтому перезапись манифеста НА МЕСТЕ
    (не через rename) тоже меняет сигнатуру и не прячется за кэшем.
    """
    parts: list[tuple] = []
    for path in paths:
        try:
            dir_stat = path.stat()
        except OSError:
            continue
        manifest = path / SNAPSHOT_MANIFEST_FILENAME
        try:
            m_stat = manifest.stat()
            parts.append((path.name, dir_stat.st_mtime_ns, m_stat.st_mtime_ns, m_stat.st_size))
        except OSError:
            parts.append((path.name, dir_stat.st_mtime_ns, 0, -1))
    return tuple(parts)


# Кэш последнего результата pending-скана: backups_root → (сигнатура, результат).
# N3: статус зовётся из UI/IPC часто, а prune при ON выключен — без кэша каждый
# вызов читал и парсил манифест КАЖДОГО снимка. Ключ кэша — сигнатура из stat'ов,
# поэтому любое изменение на диске (новый снимок, перезапись манифеста) даёт
# промах и полный перескан.
_PENDING_SCAN_CACHE: dict[str, tuple[tuple, dict | None]] = {}
_PENDING_CACHE_MAX_ROOTS = 32


def find_pending_transaction(*, backups_root: Path) -> dict | None:
    """Первая незавершённая (не COMMITTED) транзакция в backups-корне.

    Различает ОПУБЛИКОВАННУЮ транзакцию (каталог-снимок с COMMITTING — замена
    началась, нужен b2) и неопубликованный staging (подготовка, замены не
    было — это мусор, а не незавершённая операция). На ``published`` b2 обязан
    опираться при решении, доказывать ли что-то.
    """
    root = Path(backups_root)
    cache_key = str(root)
    if not root.is_dir():
        return None
    published = [(p, True) for p in _snapshot_dirs(root)]
    unpublished = [(p, False) for p in _unpublished_staging_dirs(root)]
    candidates = [p for p, _flag in published + unpublished]

    signature = _candidate_signature(candidates)
    cached = _PENDING_SCAN_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    def _result(state: Any, transaction_id: Any, path: Path, is_published: bool) -> dict:
        if len(_PENDING_SCAN_CACHE) >= _PENDING_CACHE_MAX_ROOTS:
            _PENDING_SCAN_CACHE.clear()
        _PENDING_SCAN_CACHE[cache_key] = (signature, {
            "state": state,
            "transaction_id": transaction_id,
            "path": str(path),
            "published": is_published,
        })
        return _PENDING_SCAN_CACHE[cache_key][1]

    for path, is_published in published + unpublished:
        try:
            manifest = _read_manifest(path)
        except SnapshotOperationRefused:
            # Нечитаемый манифест — тоже незавершённая транзакция: молчать нельзя.
            return _result("UNKNOWN", None, path, is_published)
        if manifest is None:
            continue
        if manifest.get("state") != STATE_COMMITTED:
            return _result(
                manifest.get("state"), manifest.get("transaction_id"), path, is_published
            )
    if len(_PENDING_SCAN_CACHE) >= _PENDING_CACHE_MAX_ROOTS:
        _PENDING_SCAN_CACHE.clear()
    _PENDING_SCAN_CACHE[cache_key] = (signature, None)
    return None


# ----------------------------------------------------------------------
# Публичный протокол
# ----------------------------------------------------------------------


def build_encrypted_snapshot(
    *,
    data_dir: Any,
    backup_dir: Any,
    crypto: Any,
    transaction_id: str,
    policy_on: bool,
) -> dict:
    """Шаги 1–3 спеки: приватный staging, полная сборка, fsync, manifest PREPARED.

    Snapshot ещё НЕ опубликован — это делает ``commit_encrypted_snapshot``.
    Возвращает ``prepared``-словарь (fingerprint источников — только в памяти).

    ИМЕНА ПАРАМЕТРОВ (b2 обязан читать буквально): ``backup_dir`` здесь — КАТАЛОГ
    СНИМКА, то есть публикуемое назначение внутри ``<data_dir>/backups``; он НЕ
    является корнем backups. Корнем backups оперируют ``recover_pending_state``
    и ``find_pending_transaction`` — у них параметр называется ``backups_root``.
    Одинаковое имя ``backup_dir`` у обоих раньше означало разные вещи.
    """
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)
    if crypto is None:
        raise SnapshotOperationRefused(
            REASON_CRYPTO_UNAVAILABLE, "ключ недоступен — encrypted snapshot невозможен"
        )
    if not policy_on:
        # Протокол существует только для ON; OFF-профиль идёт legacy-путём.
        raise SnapshotOperationRefused(
            REASON_POLICY_OFF, "snapshot-протокол вызывается только при Encryption ON"
        )
    if not str(transaction_id):
        raise SnapshotOperationRefused(REASON_PREPARED_MISSING, "пустой transaction_id")

    # Снимок обязан лежать внутри <data_dir>/backups (symlink не уводит наружу).
    dest = _require_inside_backups(data_dir, backup_dir)
    backups_root = dest.parent

    # 🔴 B2 (спека §5.4: «Readers/writers/restore проверяют pending state под тем
    # же lock»): незавершённый RESTORE — тоже незавершённая операция, и новый
    # снимок при ней брать нельзя. Пока замены не завершены, журналы могут быть
    # рваными, а снимок рваного набора станет «последним хорошим бэкапом»
    # владельца — мусором, неотличимым в list_backups. Маркер лежит в data_dir,
    # поэтому b1-скан backups его не видит: проверяем явно и ДО подготовки.
    restore_markers = restore_marker_dirs(data_dir)
    if restore_markers:
        raise SnapshotOperationRefused(
            REASON_RECOVERY_PENDING,
            f"незавершённый restore ({restore_markers[0].name}) — снимок поверх "
            "него зафиксировал бы промежуточное состояние",
            pending=True,
        )

    # Шаг 1: незавершённая операция запрещает новую транзакцию (опубликованная).
    # Сканируется КОРЕНЬ backups, а не каталог-снимка: незавершённая транзакция
    # лежит рядом с новым назначением.
    pending = find_pending_transaction(backups_root=backups_root)
    if pending and pending.get("published"):
        raise SnapshotOperationRefused(
            REASON_PENDING_OPERATION,
            f"незавершённая транзакция {pending.get('transaction_id')} "
            f"({pending.get('state')}) — требуется recovery",
            pending=True,
        )
    if pending:
        logger.warning(
            "encrypted_snapshot: оставлен неопубликованный staging %s "
            "(%s) — новая транзакция допустима",
            pending.get("path"), pending.get("state"),
        )

    registry = _registry(data_dir)
    fingerprint = _source_fingerprint(data_dir)

    _ensure_private_dir(backups_root)
    _ensure_private_dir(backups_root / STAGING_ROOT_NAME)
    staging = _staging_dir_for(backups_root, transaction_id)
    if staging.exists():
        raise SnapshotOperationRefused(
            REASON_DESTINATION_EXISTS, f"staging {staging.name} уже существует"
        )
    # Приватный staging (0700): в нём лежат только ENC1-строки.
    staging.mkdir(parents=True, exist_ok=False, mode=0o700)

    files_meta: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        for path in registry:
            blob = _encrypt_journal(path, crypto)
            _write_file_durable(staging / path.name, blob)
            total_bytes += len(blob)
            files_meta.append(
                {"name": path.name, "size": len(blob), "sha256": _sha256(blob)}
            )
        manifest = {
            "version": SNAPSHOT_MANIFEST_VERSION,
            "transaction_id": transaction_id,
            "state": STATE_PREPARED,
            "policy_at_capture": bool(policy_on),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": files_meta,
        }
        # Шаг 3: durable manifest + fsync каталога.
        _write_manifest_atomic(staging, manifest)
        _fsync_dir(staging)
    except Exception:
        # Не публикуем частично собранный снимок: приватный staging убираем.
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "ok": True,
        "state": STATE_PREPARED,
        "transaction_id": transaction_id,
        "staging_dir": str(staging),
        "backup_dir": str(dest),  # разрешённый путь: symlink разыменован
        "manifest": manifest,
        "files": files_meta,
        "size_bytes": total_bytes,
        "fingerprint": fingerprint,
    }


def verify_snapshot_readback(*, backup_dir: Any) -> dict:
    """Шаг 6 (проверочная часть): перечитывает ВСЕ файлы и сверяет с manifest."""
    snapshot_dir = Path(backup_dir)
    manifest = _read_manifest(snapshot_dir)
    if manifest is None:
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID, f"{snapshot_dir.name}: манифест отсутствует"
        )
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != 10:
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID, f"{snapshot_dir.name}: неполный набор в манифесте"
        )

    mismatches: list[str] = []
    listed = [entry.get("name") for entry in entries]
    # Набор — ровно реестр, никаких посторонних имён (спека §1/§5).
    if set(listed) != set(HISTORY_JOURNAL_FILENAMES) or len(listed) != 10:
        mismatches.append(
            f"набор файлов не совпадает с реестром: {sorted(map(str, listed))}"
        )
    # Лишние файлы в снимке (например, подброшенный рядом plaintext) —
    # снимок с ними не считается прочитанным. Проверяем ЛЮБУЮ запись в каталоге,
    # включая ПОДКАТАЛОГИ: раньше смотрели только is_file(), и подложенный
    # подкаталог с содержимым проходил молча.
    for path in sorted(snapshot_dir.iterdir()):
        if path.name == SNAPSHOT_MANIFEST_FILENAME:
            continue
        if path.name in listed and path.is_file():
            continue
        kind = "каталог" if path.is_dir() else "файл"
        mismatches.append(f"лишний {kind} в снимке: {path.name}")

    for entry in entries:
        name = entry.get("name")
        path = snapshot_dir / str(name)
        if not path.is_file():
            mismatches.append(f"{name}: отсутствует")
            continue
        try:
            blob = path.read_bytes()
        except OSError as exc:
            mismatches.append(f"{name}: не читается ({exc})")
            continue
        if len(blob) != entry.get("size"):
            mismatches.append(f"{name}: size {len(blob)} != {entry.get('size')}")
            continue
        if _sha256(blob) != entry.get("sha256"):
            mismatches.append(f"{name}: sha256 не совпадает")

    return {
        "ok": not mismatches,
        "state": manifest.get("state"),
        "transaction_id": manifest.get("transaction_id"),
        "checked": len(entries),
        "mismatches": mismatches,
    }


def _ensure_private_dir(path: Path) -> None:
    """mkdir + явный chmod 0700 (в т.ч. для уже существующего каталога).

    ``mkdir(mode=...)`` не применяет режим к существующему каталогу, поэтому
    права подтягиваются явно — в каталоге лежат только ENC1-строки, но лишняя
    видимость каталога не нужна никому.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise SnapshotOperationRefused(
            REASON_PERMISSIONS_FAILED,
            f"не удалось выставить 0700 на {path}: {exc}"
        ) from exc


def _cancel_staging(staging: Path) -> None:
    """Отмена транзакции ДО durable COMMITTING: приватный staging убирается.

    На диске не остаётся незавершённой транзакции, а отменённый снимок (в нём
    только ENC1) не копится мусором. После COMMITTING этот вызов ЗАПРЕЩЁН:
    там признак незавершённости обязан пережить crash ради b2-доказки.
    """
    import shutil

    shutil.rmtree(staging, ignore_errors=True)


def _publish_staging(staging: Path, backup_dir: Path) -> None:
    """Шаг 5: публикация проверенного снимка ОДНОЙ атомарной заменой + fsync.

    Отдельная функция (а не инлайн в commit) — единственная точка «первой
    замены»: её и подменяют тесты, чтобы доказать crash-семантику COMMITTING
    без тест-флагов в публичной сигнатуре.
    """
    if backup_dir.exists():
        raise SnapshotOperationRefused(
            REASON_DESTINATION_EXISTS, f"{backup_dir} уже существует — не перезаписываем"
        )
    os.replace(staging, backup_dir)
    _fsync_dir(backup_dir.parent)


def commit_encrypted_snapshot(
    *,
    data_dir: Any,
    backup_dir: Any,
    transaction_id: str,
    prepared: dict | None = None,
    policy_read: Callable[[], bool] | None = None,
) -> dict:
    """Шаги 4–6 спеки: COMMITTING → повторный fingerprint → замена → read-back.

    ``COMMITTED`` записывается ТОЛЬКО после успешного read-back всех файлов.

    ``snapshot_dir`` — КАТАЛОГ СНИМКА (публикуемое назначение), обязан лежать
    внутри ``<data_dir>/backups``.
    """
    backup_dir = Path(backup_dir)
    dest = _require_inside_backups(data_dir, backup_dir)
    if not prepared:
        raise SnapshotOperationRefused(
            REASON_PREPARED_MISSING, "commit без подготовленного снимка запрещён"
        )
    if prepared.get("transaction_id") != transaction_id:
        raise SnapshotOperationRefused(
            REASON_PREPARED_MISSING,
            f"transaction_id не совпадает: {prepared.get('transaction_id')} "
            f"!= {transaction_id}",
        )
    staging = Path(prepared["staging_dir"])
    if not staging.is_dir():
        raise SnapshotOperationRefused(
            REASON_PREPARED_MISSING, f"staging {staging} не найден — нечего публиковать"
        )

    # Политика должна остаться ON: иначе операция меняет смысл на ходу.
    if policy_read is not None and not policy_read():
        _cancel_staging(staging)
        raise SnapshotOperationRefused(
            REASON_POLICY_UNAVAILABLE,
            "политика изменилась до commit — публикация отменена",
        )

    # Шаг 4 (вторая половина): fingerprint источников ДО первой замены.
    current = _source_fingerprint(Path(data_dir))
    if current != prepared.get("fingerprint"):
        changed = sorted(
            name for name in set(current) | set(prepared.get("fingerprint") or {})
            if current.get(name) != (prepared.get("fingerprint") or {}).get(name)
        )
        # Отмена ДО durable COMMITTING: исходные файлы не тронуты, незавершённой
        # транзакции на диске не остаётся (спека §5 «до COMMITTING отмена …»).
        _cancel_staging(staging)
        raise SnapshotOperationRefused(
            REASON_FINGERPRINT_MISMATCH,
            f"источники изменились после подготовки: {changed}",
        )

    # Проверка адреса назначения — ДО durable COMMITTING (MAJOR-2).
    # «Каталог уже существует» — отказ ДО первой замены, то есть отмена по
    # спецификации §5 («до COMMITTING отмена оставляет исходные файлы без
    # изменений»). Если проверять это внутри публикации, отказ случился бы уже
    # ПОСЛЕ записи COMMITTING: на диске осталась бы транзакция, которая никогда
    # не начала замену, а `recover_pending_state` залип бы в pending навсегда.
    if backup_dir.exists():
        _cancel_staging(staging)
        raise SnapshotOperationRefused(
            REASON_DESTINATION_EXISTS, f"{backup_dir} уже существует — не перезаписываем"
        )

    # Шаг 4: durable COMMITTING — ДО первой замены.
    manifest = dict(prepared["manifest"])
    manifest["state"] = STATE_COMMITTING
    _write_manifest_atomic(staging, manifest)

    # С этого момента транзакция необратима: авто-отката в plaintext нет
    # (спека §5), а признак COMMITTING обязан пережить crash для b2-доказки.
    try:
        # Шаг 5: публикация проверенного снимка.
        _publish_staging(staging, dest)
    except OSError as exc:
        # Crash/сбой на первой замене: источники целы, признак COMMITTING
        # остаётся на диске — система fail-closed, отката нет.
        raise SnapshotOperationRefused(
            REASON_PUBLISH_FAILED,
            f"публикация снимка не удалась: {type(exc).__name__}: {exc}",
            pending=True,
        ) from exc
    except SnapshotOperationRefused as exc:
        # Отказ самой публикации после COMMITTING: транзакция уже durable,
        # чистить staging нельзя — это уничтожило бы доказательство для b2.
        raise SnapshotOperationRefused(exc.reason, str(exc), pending=True) from exc

    # Шаг 6: read-back ВСЕХ файлов; при расхождении состояние остаётся COMMITTING.
    # Сбой самого read-back — тоже fail-closed: COMMITTED не достигается,
    # признак COMMITTING остаётся на диске для b2-доказки.
    try:
        readback = verify_snapshot_readback(backup_dir=backup_dir)
    except SnapshotOperationRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — crash/сбой проверки
        raise SnapshotOperationRefused(
            REASON_READBACK_FAILED,
            f"read-back не выполнен: {type(exc).__name__}: {exc}",
            pending=True,
        ) from exc
    if not readback["ok"]:
        raise SnapshotOperationRefused(
            REASON_READBACK_FAILED,
            f"read-back не сошёлся: {readback['mismatches']}",
            pending=True,
        )

    manifest["state"] = STATE_COMMITTED
    _write_manifest_atomic(backup_dir, manifest)
    _fsync_dir(dest)
    logger.info(
        "encrypted_snapshot: транзакция %s зафиксирована (COMMITTED), %d файлов",
        transaction_id, len(manifest["files"]),
    )
    return {
        "ok": True,
        "state": STATE_COMMITTED,
        "transaction_id": transaction_id,
        "backup_dir": str(dest),  # разрешённый путь: symlink разыменован
        "files": manifest["files"],
        "size_bytes": prepared.get("size_bytes", 0),
        "readback": readback,
    }


def recover_pending_state(*, data_dir: Any, backups_root: Any) -> dict:
    """Fail-closed признак незавершённой транзакции (доказка — b2).

    ``backups_root`` — КОРЕНЬ backups (где лежат снимки и приватные
    staging-каталоги), а не каталог конкретного снимка. Имя параметра
    намеренно отличается от ``snapshot_dir`` в build/commit: раньше оба
    назывались ``backup_dir``, но означали разные вещи — b2 обязан понимать
    разницу по имени, а не по догадке.

    b1 НЕ откатывает снимок в plaintext, НЕ создаёт новый ключ и НЕ запускает
    обычное обслуживание: единственный честный ответ — «есть незавершённая
    операция, разбираться должна b2».

    ``data_dir`` принимается для контракта b2 (recovery сверяет источники) и в
    b1 намеренно не используется.
    """
    del data_dir  # контракт б2; в b1 источники не трогаем
    pending = find_pending_transaction(backups_root=Path(backups_root))
    if pending is None:
        return {
            "ok": True,
            "pending": False,
            "reason": None,
            "state": None,
            "transaction_id": None,
            "path": None,
            "published": False,
            "stale_staging": [],
        }

    # 🔴 Неопубликованный staging (MAJOR-6) — это МУСОР от crash в prepare, а не
    # незавершённая операция: публикация не начиналась, ни один файл снимка не
    # появился в backups/, откатывать нечего, ключ не нужен. Раньше он давал
    # pending=True навсегда (и ERROR в лог на каждую проверку), хотя новые
    # транзакции при этом спокойно шли — b2 не смог бы отличить «нужен разбор»
    # от «удали мусор и работай дальше».
    if not pending.get("published"):
        stale = [str(p) for p in _unpublished_staging_dirs(Path(backups_root))]
        logger.warning(
            "encrypted_snapshot: неопубликованный staging без признака замены — "
            "мусор, удалить безопасно: %s",
            stale,
        )
        return {
            "ok": True,
            "pending": False,
            "reason": REASON_STALE_STAGING,
            "state": pending.get("state"),
            "transaction_id": pending.get("transaction_id"),
            "path": pending.get("path"),
            "published": False,
            "stale_staging": stale,
        }

    logger.error(
        "encrypted_snapshot: обнаружена незавершённая транзакция %s (%s) в %s — "
        "fail-closed, авто-отката нет",
        pending.get("transaction_id"), pending.get("state"), pending.get("path"),
    )
    return {
        "ok": False,
        "pending": True,
        "reason": REASON_RECOVERY_PENDING,
        "state": pending.get("state"),
        "transaction_id": pending.get("transaction_id"),
        "path": pending.get("path"),
        "published": True,
        "stale_staging": [],
    }


def create_encrypted_snapshot(
    *,
    data_dir: Any,
    backup_dir: Any,
    crypto: Any,
    transaction_id: str,
    policy_on: bool,
    policy_read: Callable[[], bool] | None = None,
) -> dict:
    """Композиция prepare+commit — публичная точка входа для backup-путей.

    Наружу отдаётся только успешный COMMITTED-снимок; любой отказ поднимает
    ``SnapshotOperationRefused`` (у вызывающего — машинно-читаемый отказ).
    """
    prepared = build_encrypted_snapshot(
        data_dir=data_dir,
        backup_dir=backup_dir,
        crypto=crypto,
        transaction_id=transaction_id,
        policy_on=policy_on,
    )
    return commit_encrypted_snapshot(
        data_dir=data_dir,
        backup_dir=backup_dir,
        transaction_id=transaction_id,
        prepared=prepared,
        policy_read=policy_read,
    )


# ----------------------------------------------------------------------
# A5.2b2 — restore из проверенного снимка
# ----------------------------------------------------------------------

# Приватный staging RESTORE живёт РЯДОМ с живыми журналами, а не в backups:
# замена живого файла обязана быть os.replace в пределах ОДНОГО filesystem
# (backs может быть symlink на другой том — легальная конфигурация b1), и
# только этот каталог лежит на одном носителе с data_dir. Имя dot-prefixed и
# режим 0700: каталог не должен всплывать в списках и не должен быть виден.
RESTORE_STAGING_PREFIX = ".a52b2-restore-"
RESTORE_MARKER_FILENAME = "restore_marker.json"
RESTORE_MARKER_VERSION = 1
# Инфикс подготовленной копии журнала рядом с живым файлом. Тот же filesystem,
# что у data_dir (backs может быть symlink на другой том), поэтому замена —
# атомарный os.replace, а не копирование. 🔴 N5: литерал ОТЛИЧАЕТСЯ от префикса
# каталога staging намеренно — два пространства имён в одном data_dir, и purge
# обязан уметь их различать (аудит видит их как два семейства).
RESTORE_TMP_SUFFIX = ".a52b2-restore-tmp-"

# Состояния restore-маркера. Те же строки, что у b1-манифеста: маркер и манифест
# описывают одну транзакцию, и ``COMMITTED`` здесь, как и там, означает
# «read-back прошёл».
RESTORE_STATE_COMMITTING = STATE_COMMITTING
RESTORE_STATE_COMMITTED = STATE_COMMITTED

# Машинно-читаемые причины b2. Переиспользуемые b1-коды (``snapshot_*``)
# не дублируются: один словарь причин на всю волну.
REASON_REQUIRES_ENCRYPTION_ON = "snapshot_requires_encryption_on"
REASON_POLICY_MISMATCH = "snapshot_policy_mismatch"
REASON_RESTORE_SETTINGS_UNSUPPORTED = "restore_settings_unsupported_at_on"
REASON_LEDGER_UNREADABLE = "snapshot_ledger_unreadable"
REASON_LEDGER_MALFORMED = "snapshot_ledger_malformed"
REASON_RECORD_MALFORMED = "snapshot_record_malformed"
REASON_APPLY_FAILED = "snapshot_apply_failed"

# Два журнала deletion ledger: tombstones ∪ purged (спека §5). Выходной ledger
# restore пишется по объединению, поэтому порядок здесь важен только для
# читателя-человека.
LEDGER_JOURNAL_NAMES: tuple[str, str] = (
    "history_tombstones.ndjson",
    "history_purged_ids.ndjson",
)


def _require_restorable_snapshot_dir(backups_root: Any, snapshot_dir: Any) -> Path:
    """Containment каталога-снимка: только опубликованный снимок внутри backups.

    Три независимых слоя (спека §5 шаг 1: «Проверить типы/контейнмент файлов…
    Не следовать symlink из реестра»):

      * лексический слой — ``..``-выход и подмена корня backups отсекаются
        сравнением НЕ-разыменованных путей;
      * слой symlink-компонентов — ни одного symlink на пути к снимку, иначе
        проверялся бы не тот каталог, который указан;
      * разыменованный слой — главная гарантия: итоговый путь обязан лежать
        внутри РАЗЫМЕНОВАННОГО корня backups.

    Что где срабатывает (N1 ревью — «слой мёртв в проде» проверено и уточнено):

      * путь из IPC уже разыменован W1736-гейтом, поэтому прод-путь restore
        проходит по третьему слою; отвергать симлинкнутый ``data_dir`` нельзя —
        b1 сам считает backups на другом томе легальной конфигурацией;
      * лексический и symlink-слои живы в МОДУЛЬНОМ API (тесты, вызовы с путём
        как есть) и — важно — в RECOVERY: целевой снимок берётся из маркера
        «как записан», и подмена каталога на symlink после crash обязана быть
        поймана до докачки (регресс-тест
        ``test_recovery_refuses_symlinked_target_after_crash``).

    Сравнение только по одному из слоёв сломало бы один из двух честных случаев
    (либо symlink-inside, либо симлинкнутый data_dir).
    """
    root_raw = Path(backups_root)
    raw = Path(snapshot_dir)
    root_lex = Path(os.path.abspath(str(root_raw)))
    raw_lex = Path(os.path.abspath(str(raw)))

    try:
        rel = raw_lex.relative_to(root_lex)
    except ValueError:
        rel = None
    if rel is not None:
        if not rel.parts:
            raise SnapshotOperationRefused(
                REASON_OUTSIDE_BACKUPS_ROOT, "восстановление из самого backups/ запрещено"
            )
        cursor = root_lex
        for part in rel.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise SnapshotOperationRefused(
                    REASON_SOURCE_SYMLINK,
                    f"{cursor.name} — symlink не разыменовывается",
                )

    root = root_raw.resolve()
    resolved = raw.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise SnapshotOperationRefused(
            REASON_OUTSIDE_BACKUPS_ROOT,
            f"{resolved} находится вне {root} — снимок обязан лежать в backups/",
        )
    staging_root = root / STAGING_ROOT_NAME
    if resolved.is_relative_to(staging_root):
        raise SnapshotOperationRefused(
            REASON_STALE_STAGING,
            f"{resolved} — неопубликованный staging, а не снимок",
        )
    return resolved


def _decrypt_verified_lines(
    *, journal_file: Path, crypto: Any, reason_mismatch: str
) -> list[str]:
    """Каждая строка файла обязана быть ENC1 и расшифровываться. Иначе — отказ.

    Отказ, а не skip: молчаливая потеря строки означала бы потерю данных при
    «успешном» восстановлении. Не-ENC1 строка при текущей ON-policy — это
    ``policy_mismatch`` (plaintext-снимок), а не «повреждение».
    """
    try:
        raw = journal_file.read_bytes()
    except OSError as exc:
        raise SnapshotOperationRefused(
            REASON_SOURCE_UNREADABLE, f"{journal_file.name} не читается: {exc}"
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotOperationRefused(
            REASON_SOURCE_UNREADABLE, f"{journal_file.name}: не UTF-8: {exc}"
        ) from exc
    out: list[str] = []
    for lineno, line in enumerate(_split_ndjson_lines(text), start=1):
        if not line.strip():
            # Пустые строки допустимы (writer их не пишет, но файл мог быть
            # дописан сторонним инструментом) — не строка данных.
            continue
        if not crypto.is_encrypted(line):
            raise SnapshotOperationRefused(
                reason_mismatch,
                f"{journal_file.name}:{lineno}: строка не ENC1 — plaintext-снимок при "
                "включённой политике шифрования",
            )
        try:
            crypto.decrypt_line(line)
        except Exception as exc:  # noqa: BLE001 — чужой ключ или tamper
            raise SnapshotOperationRefused(
                REASON_LINE_TAMPERED,
                f"{journal_file.name}:{lineno}: ENC1-строка не расшифровывается "
                f"(чужой ключ или tamper): {type(exc).__name__}",
            ) from exc
        out.append(line)
    return out


def verify_snapshot(*, backups_root: Any, snapshot_dir: Any, crypto: Any) -> dict:
    """A5.2b2 Task 1 — ПОЛНАЯ read-only верификация снимка перед первой записью.

    Проверяется всё, что обязано быть верно до того, как живые журналы будут
    затронуты (спека §5: «Restore предварительно полностью проверяет snapshot и
    key… Незнакомый формат, неполный набор… отклоняются до первой записи»):

      * контейнмент: только опубликованный каталог-снимок внутри backups-корня,
        ни одного symlink-компонента (шаг 1);
      * манифест известной версии, набор — ровно десять имён реестра, без
        посторонних файлов (шаг 3 b1 → read-back);
      * size + sha256 CIPHERTEXT каждого файла совпадают с манифестом;
      * ``policy_at_capture`` — ON: plaintext-снимок при ON означал бы тихое
        понижение policy (карточка b2, решение 3);
      * расшифровка КАЖДОЙ строки КАЖДОГО файла (чужой ключ и tamper ловятся
        именно здесь — хэш ciphertext'а подмену бы замаскировал);
      * наличие ключа.

    Ни одной записи: функция не создаёт каталогов и не трогает живые журналы
    (тест ``test_verify_is_read_only`` фиксирует mtime+содержимое дерева).
    """
    if crypto is None:
        raise SnapshotOperationRefused(
            REASON_CRYPTO_UNAVAILABLE, "ключ недоступен — restore невозможен"
        )
    resolved = _require_restorable_snapshot_dir(backups_root, snapshot_dir)

    manifest = _read_manifest(resolved)
    if manifest is None:
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID, f"{resolved.name}: манифест отсутствует"
        )
    if manifest.get("policy_at_capture") is not True:
        # Неизвестная/выключенная policy на момент снятия — восстановление под
        # текущей ON-политикой означало бы понижение/нарушение policy.
        raise SnapshotOperationRefused(
            REASON_POLICY_MISMATCH,
            f"{resolved.name}: policy_at_capture="
            f"{manifest.get('policy_at_capture')!r} — снимок не снят при ON",
        )

    # Целостность и полнота набора — тем же read-back, что и в b1 (один
    # источник правды для «что значит проверенный снимок»).
    readback = verify_snapshot_readback(backup_dir=resolved)
    if not readback["ok"]:
        raise SnapshotOperationRefused(
            REASON_READBACK_FAILED,
            f"{resolved.name}: снимок не прошёл проверку целостности: "
            f"{readback['mismatches']}",
        )

    # Построчная проверка расшифровки — ПОСЛЕ хэшей, чтобы «честный» пересчёт
    # манифеста под подделанный ENC1 не прошёл незамеченным. Набор — тот же
    # реестр state_store, что уже проверен read-back'ом выше.
    lines: dict[str, int] = {}
    for name in HISTORY_JOURNAL_FILENAMES:
        verified = _decrypt_verified_lines(
            journal_file=resolved / name,
            crypto=crypto,
            reason_mismatch=REASON_POLICY_MISMATCH,
        )
        lines[name] = len(verified)

    logger.info(
        "encrypted_snapshot: снимок %s проверен (%d файлов, %d строк, tx=%s)",
        resolved.name, len(lines), sum(lines.values()), manifest.get("transaction_id"),
    )
    return {
        "ok": True,
        "snapshot_dir": str(resolved),
        "transaction_id": manifest.get("transaction_id"),
        "state": manifest.get("state"),
        "policy_at_capture": manifest.get("policy_at_capture"),
        "files": list(HISTORY_JOURNAL_FILENAMES),
        "checked": len(lines),
        "lines": lines,
        "readback": readback,
    }


def _record_id(decrypted: str, *, where: str, reason: str) -> str | None:
    """ID записи журнала. Неразбираемый JSON — отказ (данные не теряются молча)."""
    try:
        payload = json.loads(decrypted)
    except ValueError as exc:
        raise SnapshotOperationRefused(
            reason, f"{where}: строка не разбирается как JSON: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise SnapshotOperationRefused(reason, f"{where}: строка не JSON-объект")
    item_id = payload.get("id")
    if item_id is None:
        return None
    text = str(item_id).strip()
    return text or None


def collect_ledger_union(*, data_dir: Any, crypto: Any) -> tuple[str, ...]:
    """A5.2b2 Task 1 — tombstones ∪ purged ТЕКУЩЕГО профиля (read-only, fail-closed).

    Спека §5: «При недоступности ключа или повреждении текущего ledger restore
    прекращается без изменения файлов». Поэтому здесь нет ни одного молчаливого
    ``skip``: строка, которую нельзя прочитать/разобрать/определить, — отказ, а
    не «наверное, не ID».

    Пустой ledger (файлов нет) — валидное пустое объединение; это НЕ ошибка.
    """
    if crypto is None:
        raise SnapshotOperationRefused(
            REASON_CRYPTO_UNAVAILABLE,
            "ключ недоступен — текущий deletion ledger не проверить",
        )
    union: set[str] = set()
    for name in LEDGER_JOURNAL_NAMES:
        # Имя локали НЕ `path`: audit_purge_coverage выводит «корень в data_dir»
        # из имён, и generic-имя здесь заставило бы сканер считать ВСЕ
        # `path / <const>` в модуле (включая b1-манифест снимка) хранилищем
        # прямо в data_dir. Конкретное имя — и код понятнее, и аудит честнее.
        ledger_file = Path(data_dir) / name
        if ledger_file.is_symlink():
            raise SnapshotOperationRefused(
                REASON_SOURCE_SYMLINK, f"{name} — symlink не разыменовывается"
            )
        if not ledger_file.exists():
            continue
        try:
            raw = ledger_file.read_bytes()
        except OSError as exc:
            raise SnapshotOperationRefused(
                REASON_LEDGER_UNREADABLE, f"{name} не читается: {exc}"
            ) from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SnapshotOperationRefused(
                REASON_LEDGER_UNREADABLE, f"{name}: не UTF-8: {exc}"
            ) from exc
        for lineno, line in enumerate(_split_ndjson_lines(text), start=1):
            if not line.strip():
                continue
            if crypto.is_encrypted(line):
                try:
                    line = crypto.decrypt_line(line)
                except Exception as exc:  # noqa: BLE001 — чужой ключ или tamper
                    raise SnapshotOperationRefused(
                        REASON_LEDGER_UNREADABLE,
                        f"{name}:{lineno}: ENC1-строка ledger не расшифровывается: "
                        f"{type(exc).__name__}",
                    ) from exc
            item_id = _record_id(line, where=f"{name}:{lineno}", reason=REASON_LEDGER_MALFORMED)
            if not item_id:
                raise SnapshotOperationRefused(
                    REASON_LEDGER_MALFORMED,
                    f"{name}:{lineno}: в deletion ledger нет непустого id",
                )
            union.add(item_id)
    return tuple(sorted(union))


def _new_transaction_id(prefix: str) -> str:
    """Уникальный transaction_id: метка времени + случайные 4 байта.

    Случайная часть обязательна: две транзакции в одну секунду (снимок + restore,
    два restore подряд) не должны делить каталог staging.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{os.urandom(4).hex()}"


def _restore_staging_dir(data_dir: Path, transaction_id: str) -> Path:
    return Path(data_dir) / f"{RESTORE_STAGING_PREFIX}{transaction_id}"


def restore_marker_dirs(data_dir: Any) -> list[Path]:
    """Каталоги незавершённого restore в data_dir. Дёшево: один ``iterdir``.

    Единственная точка «есть ли незавершённая операция» — её же зовёт ленивый
    wiring в ``StateStore.__init__``. Содержимое маркера НЕ читается: на старте
    достаточно факта наличия.
    """
    base = Path(data_dir)
    try:
        entries = list(base.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SnapshotOperationRefused(
            REASON_FSYNC_FAILED, f"{base} не читается: {exc}"
        ) from exc
    found = [
        path
        for path in entries
        if path.name.startswith(RESTORE_STAGING_PREFIX)
        and not path.is_symlink()
        and path.is_dir()
        and (path / RESTORE_MARKER_FILENAME).is_file()
    ]
    return sorted(found)


def has_pending_restore(data_dir: Any) -> bool:
    """Дёшевая проверка наличия restore-маркера (для wiring'а)."""
    try:
        return bool(restore_marker_dirs(data_dir))
    except SnapshotOperationRefused:
        return True  # не смогли проверить — считаем «есть», recovery разберётся


def _require_policy_on(
    policy_read: Callable[[], bool], *, reason_when_off: str
) -> None:
    """Restore требует текущей policy ON (решение 1 карточки b2).

    Ошибка чтения политики ≠ «выключено» и ≠ «включено»: доказать ON не смогли —
    операция запрещена (fail-closed), потому что альтернатива — тихая расшифровка
    ENC1 в открытый вид, то есть понижение policy для данных, которые владелец
    шифровал.
    """
    try:
        on = bool(policy_read())
    except Exception as exc:  # noqa: BLE001
        logger.exception("encrypted_snapshot: политика шифрования не читается")
        raise SnapshotOperationRefused(
            REASON_POLICY_UNAVAILABLE,
            f"политика шифрования не читается ({type(exc).__name__}) — "
            "восстановление запрещено",
        ) from exc
    if not on:
        raise SnapshotOperationRefused(
            reason_when_off,
            "восстановление encrypted-снимка требует включённого шифрования: "
            "иначе данные были бы расшифрованы в открытый вид",
        )


def _read_restore_marker(staging: Path) -> dict:
    path = staging / RESTORE_MARKER_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID,
            f"restore-маркер {staging.name} не читается: {exc}",
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != RESTORE_MARKER_VERSION:
        raise SnapshotOperationRefused(
            REASON_MANIFEST_INVALID,
            f"restore-маркер {staging.name}: неизвестный формат/версия",
        )
    return payload


def _snapshot_ledger_ids(*, snapshot_dir: Path, crypto: Any) -> set[str]:
    """ID deletion ledger САМОГО снимка (его era). Уже проверен verify_snapshot."""
    ids: set[str] = set()
    for name in LEDGER_JOURNAL_NAMES:
        for lineno, line in enumerate(
            _decrypt_verified_lines(
                journal_file=snapshot_dir / name,
                crypto=crypto,
                reason_mismatch=REASON_POLICY_MISMATCH,
            ),
            start=1,
        ):
            item_id = _record_id(
                crypto.decrypt_line(line),
                where=f"{name}:{lineno}",
                reason=REASON_LEDGER_MALFORMED,
            )
            if not item_id:
                raise SnapshotOperationRefused(
                    REASON_LEDGER_MALFORMED,
                    f"{name}:{lineno}: в ledger снимка нет непустого id",
                )
            ids.add(item_id)
    return ids


def _build_restore_output(
    *,
    data_dir: Path,
    staging: Path,
    snapshot_dir: Path,
    crypto: Any,
    blocked: set[str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Готовит десять выходных ENC1-журналов в приватном staging.

    Фильтрация по ``blocked`` (ledger union) применяется к истории и ко всем
    дельтам: запись, удалённая владельцем, не может вернуться ни через один
    журнал (спека §5: «исключаются из восстановленной истории и связанных дельт»).
    Строки переносятся БАЙТ-В-БАЙТ из уже проверенного снимка — лишнего
    шифрования нет, а побайтовое совпадение с бэкапом остаётся доказуемым.

    Возвращает ``(files_meta, restored_entries, filtered_out_lines)``.
    """
    files_meta: list[dict[str, Any]] = []
    restored_entries = 0
    filtered_out = 0
    for name in HISTORY_JOURNAL_FILENAMES:
        if name in LEDGER_JOURNAL_NAMES:
            # Выходной ledger = ОБЪЕДИНЕНИЕ. Старый снимок не может его уменьшить.
            body = "".join(
                crypto.encrypt_line(json.dumps({"id": item_id}, ensure_ascii=False)) + "\n"
                for item_id in sorted(blocked)
            ).encode("utf-8")
        else:
            out_lines: list[str] = []
            verified = _decrypt_verified_lines(
                journal_file=snapshot_dir / name,
                crypto=crypto,
                reason_mismatch=REASON_POLICY_MISMATCH,
            )
            for lineno, line in enumerate(verified, start=1):
                item_id = _record_id(
                    crypto.decrypt_line(line),
                    where=f"{name}:{lineno}",
                    reason=REASON_RECORD_MALFORMED,
                )
                if item_id and item_id in blocked:
                    filtered_out += 1
                    continue
                out_lines.append(line)
            if name == "history.ndjson":
                restored_entries = len(out_lines)
            # Явные скобки вокруг join: тернарник внутри конкатенации вернул бы
            # `str + bytes` на пустом журнале (все строки отфильтрованы union'ом).
            body = ("\n".join(out_lines) + "\n").encode("utf-8") if out_lines else b""
        _write_file_durable(staging / name, body)
        files_meta.append(
            {"name": name, "size": len(body), "sha256": _sha256(body)}
        )
    return files_meta, restored_entries, filtered_out


def _replace_journal(tmp_file: Path, target: Path) -> None:
    """Одна атомарная замена живого журнала (``os.replace``, один filesystem)."""
    os.replace(str(tmp_file), str(target))


def _write_restore_marker(staging: Path, marker: dict) -> None:
    """Durable restore-маркер: tmp → fsync → replace → fsync каталога."""
    blob = json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    tmp = staging / f"{RESTORE_MARKER_FILENAME}.tmp"
    _write_file_durable(tmp, blob)
    os.replace(tmp, staging / RESTORE_MARKER_FILENAME)
    _fsync_dir(staging)


def _readback_live_journals(*, data_dir: Path, files_meta: list[dict]) -> dict:
    """Read-back ВСЕХ десяти живых журналов против ожидаемых size/sha256.

    Только после этого (и никогда раньше) restore-маркер может получить
    состояние COMMITTED — ровно как в b1 для снимка.
    """
    mismatches: list[str] = []
    for entry in files_meta:
        name = str(entry.get("name"))
        journal_file = Path(data_dir) / name
        if not journal_file.is_file():
            mismatches.append(f"{name}: отсутствует")
            continue
        try:
            blob = journal_file.read_bytes()
        except OSError as exc:
            mismatches.append(f"{name}: не читается ({exc})")
            continue
        if len(blob) != entry.get("size"):
            mismatches.append(f"{name}: size {len(blob)} != {entry.get('size')}")
            continue
        if _sha256(blob) != entry.get("sha256"):
            mismatches.append(f"{name}: sha256 не совпадает")
    return {"ok": not mismatches, "checked": len(files_meta), "mismatches": mismatches}


def _cleanup_restore_tmp(data_dir: Path, transaction_id: str | None = None) -> None:
    """Убирает подготовленные копии журналов ``*.a52b2-restore-tmp-<txid>``.

    Частично применённый restore восстанавливается ДОКАЗКОЙ (повторное
    применение), поэтому tmp-куски доказывать нечего — только мусор в data_dir.
    ``transaction_id=None`` означает «убрать все transaction-фрагменты» — так
    вызывает privacy purge: жёсткий kill мог случиться ДО появления маркера, и
    тогда фрагменты не принадлежат ни одной известной транзакции, но содержат
    расшифровываемую историю. Glob сознательно узкий: только наш инфикс.
    """
    base = Path(data_dir)
    pattern = (
        f"*{RESTORE_TMP_SUFFIX}{transaction_id}" if transaction_id else f"*{RESTORE_TMP_SUFFIX}*"
    )
    for tmp in sorted(base.glob(pattern)):
        try:
            tmp.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:  # noqa: BLE001 — чистка не должна ронять транзакцию
            logger.warning(
                "encrypted_snapshot: не удалось убрать фрагмент %s (%s)", tmp.name, exc
            )


def _apply_verified_snapshot_locked(
    *,
    data_dir: Path,
    snapshot_dir: Path,
    crypto: Any,
    transaction_id: str,
    pre_restore_snapshot: str,
    recovered: bool,
    staging: Path | None = None,
) -> dict:
    """Шаги 2–4 под store-lock: union → фильтрация → COMMITTING → замены → read-back.

    Вызывается и обычным restore, и recovery (roll-forward); оба вызова — уже
    под ``history_flock``, с повторной проверкой policy и повторной верификацией
    снимка (поэтому ни ``policy_read``, ни ``backups_root`` здесь и нет: делать
    вид, что проверка политики происходит внутри, было бы ложью).

    Разница: ``staging`` (у recovery — каталог незавершённой транзакции, он
    переиспользуется) и ``pre_restore_snapshot``/``recovered`` (страховка уже
    создана первым restore, второй раз она не нужна).
    """
    union = set(collect_ledger_union(data_dir=data_dir, crypto=crypto))
    blocked = union | _snapshot_ledger_ids(snapshot_dir=snapshot_dir, crypto=crypto)

    # 🔴 B1: каталог, который этот вызов НЕ создавал, — не мусор, а доказательство
    # незавершённой транзакции (recovery переиспользует каталог маркера). Убрать
    # его при отказе в prepare означало бы: рваный набор становится невидимым,
    # ``has_pending_restore`` молчит, а следующий restore рапортует об успехе
    # поверх него. Отменяется (рекурсивно) только то, что создал этот вызов.
    created_staging = staging is None
    if created_staging:
        staging = _restore_staging_dir(data_dir, transaction_id)
        if staging.exists():
            raise SnapshotOperationRefused(
                REASON_DESTINATION_EXISTS, f"staging {staging.name} уже существует"
            )
    elif not staging.is_dir():
        raise SnapshotOperationRefused(
            REASON_PREPARED_MISSING, f"staging {staging.name} не найден"
        )
    _ensure_private_dir(staging)

    try:
        files_meta, restored_entries, filtered_out_lines = _build_restore_output(
            data_dir=data_dir,
            staging=staging,
            snapshot_dir=snapshot_dir,
            crypto=crypto,
            blocked=blocked,
        )
        # Шаг 3 (середина): durable COMMITTING — ДО первой замены живого файла.
        marker = {
            "version": RESTORE_MARKER_VERSION,
            "transaction_id": transaction_id,
            "state": RESTORE_STATE_COMMITTING,
            "target_snapshot": str(snapshot_dir),
            "pre_restore_snapshot": pre_restore_snapshot,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": files_meta,
        }
        _write_restore_marker(staging, marker)
        _fsync_dir(staging)
    except SnapshotOperationRefused:
        # Отмена ДО durable COMMITTING: живые файлы не тронуты. Каталог убирается
        # ТОЛЬКО если его создал этот вызов (B1): в recovery-ветке переиспользуемый
        # каталог маркера — единственное доказательство незавершённого restore.
        if created_staging:
            _cancel_staging(staging)
        raise
    except OSError as exc:
        if created_staging:
            _cancel_staging(staging)
        raise SnapshotOperationRefused(
            REASON_FSYNC_FAILED,
            f"подготовка restore не удалась: {type(exc).__name__}: {exc}",
            pending=not created_staging,
        ) from exc

    # --- Шаг 3: замены. С этого момента транзакция необратима (отката нет). ---
    try:
        for entry in files_meta:
            name = str(entry["name"])
            tmp = data_dir / f"{name}{RESTORE_TMP_SUFFIX}{transaction_id}"
            _write_file_durable(tmp, (staging / name).read_bytes())
            _replace_journal(tmp, data_dir / name)
    except Exception as exc:  # noqa: BLE001 — crash/сбой замены
        _cleanup_restore_tmp(data_dir, transaction_id)
        raise SnapshotOperationRefused(
            REASON_APPLY_FAILED,
            f"замена живых журналов не удалась: {type(exc).__name__}: {exc}",
            pending=True,
        ) from exc
    _fsync_dir(data_dir)

    # --- Шаг 4: read-back ВСЕХ файлов. COMMITTED недостижим без него. ---
    try:
        readback = _readback_live_journals(data_dir=data_dir, files_meta=files_meta)
    except Exception as exc:  # noqa: BLE001
        raise SnapshotOperationRefused(
            REASON_READBACK_FAILED,
            f"read-back не выполнен: {type(exc).__name__}: {exc}",
            pending=True,
        ) from exc
    if not readback["ok"]:
        raise SnapshotOperationRefused(
            REASON_READBACK_FAILED,
            f"read-back не сошёлся: {readback['mismatches']}",
            pending=True,
        )

    marker["state"] = RESTORE_STATE_COMMITTED
    marker["committed_at"] = datetime.now(timezone.utc).isoformat()
    marker["restored_entries"] = restored_entries
    _write_restore_marker(staging, marker)
    _fsync_dir(data_dir)

    # Транзакция завершена: приватный staging больше не нужен. Маркер COMMITTED,
    # оставшийся после crash здесь, recovery уберёт как «уже завершённую».
    _cancel_staging(staging)
    _fsync_dir(data_dir)
    logger.info(
        "encrypted_snapshot: restore %s зафиксирован (COMMITTED) из %s, "
        "%d записей, отфильтровано строк по ledger %d",
        transaction_id, snapshot_dir, restored_entries, filtered_out_lines,
    )
    return {
        "ok": True,
        "state": RESTORE_STATE_COMMITTED,
        "transaction_id": transaction_id,
        "snapshot_dir": str(snapshot_dir),
        "pre_restore_snapshot": pre_restore_snapshot,
        # N3: restored_entries — строки ВОССТАНОВЛЕННОЙ history.ndjson;
        # filtered_out_lines — строки, вычеркнутые по ledger, по всем 10 журналам.
        "restored_entries": restored_entries,
        "restored_entries_source": "snapshot_lines",
        "ledger_blocked": len(blocked),
        "filtered_out_lines": filtered_out_lines,
        "files": files_meta,
        "readback": readback,
        "recovered": recovered,
    }


def _validated_marker_entries(marker: dict) -> int | None:
    """``restored_entries`` из маркера — только если значение пригодно.

    N4: маркер лежит на ДИСКЕ, а его ``restored_entries`` — число, записанное
    прошлой попыткой. Отдать его «как есть» значит показать непроверенное
    значение как правду (в подделанном/битом маркере там может быть что угодно).
    Поэтому значение проходит проверку типа, диапазона и правдоподобия (не
    больше суммарного размера подготовленного ciphertext-бандла — запись не
    может быть длиннее него). Всё остальное — честный ``None``.
    """
    value = marker.get("restored_entries")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    files = marker.get("files")
    if not isinstance(files, list) or not files:
        return None
    prepared = 0
    for entry in files:
        if not isinstance(entry, dict):
            return None
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            continue
        prepared += size
    if prepared <= 0 or value > prepared:
        return None
    return value


def _recovery_result(
    *,
    ok: bool,
    pending: bool,
    reason: str | None,
    state: str | None = None,
    transaction_id: str | None = None,
    snapshot_dir: str | None = None,
    pre_restore_snapshot: str | None = None,
    restored_entries: int = 0,
    rolled_forward: bool = False,
    stale_staging: list[str] | None = None,
    extra_markers: list[str] | None = None,
    snapshot_pending: bool = False,
) -> dict:
    """Единая форма ответа recovery (одно место → один словарь полей)."""
    return {
        "ok": ok,
        "pending": pending,
        "reason": reason,
        "state": state,
        "transaction_id": transaction_id,
        "snapshot_dir": snapshot_dir,
        "pre_restore_snapshot": pre_restore_snapshot,
        "restored_entries": restored_entries,
        "rolled_forward": rolled_forward,
        "stale_staging": list(stale_staging or []),
        "extra_markers": list(extra_markers or []),
        "snapshot_pending": snapshot_pending,
    }


def recover_pending_restore(
    *,
    data_dir: Any,
    backups_root: Any,
    crypto: Any,
    policy_read: Callable[[], bool] | None = None,
) -> dict:
    """A5.2b2 Task 3 — fail-closed recovery: докатка проверенного снимка.

    Спека §5: «После crash COMMITTING либо докатывается из этого snapshot, либо
    остаётся fail-closed до восстановления; непроверенный успех запрещён» и
    «Recovery при недоступном ключе не создаёт новый ключ и не запускает обычное
    обслуживание». Карточка b2, решение 6: докатывается ЦЕЛЕВОЙ снимок
    (roll-forward), pre-restore снимок остаётся страховкой для ручного решения.

    Разбор решений:

      * restore-маркера нет → дешёвый no-op. Состояние b1 (мусор из
        неопубликованного staging / pending опубликованного COMMITTING) доносится
        честно: «snapshot_stale_staging» либо «snapshot_recovery_pending»;
      * маркер ``COMMITTED`` → транзакция уже завершена, осталось убрать
        приватный staging (crash между записью COMMITTED и уборкой). Ничего не
        переделывается, живая история не перезаписывается;
      * маркер ``COMMITTING`` → повторная верификация целевого снимка и
        ДОКАЧКА тем же кодом, что и обычный restore (тот же commit-протокол,
        тот же ledger union, read-back → COMMITTED). Roll-forward, а не откат в
        pre-restore: тот остаётся на диске, его путь возвращается владельцу;
      * невозможно докачать (снимок повреждён, ключ недоступен, policy OFF) →
        fail-closed: причина машинно-читаема, путь pre-restore снимка в ответе,
        НИЧЕГО не удаляется, новый ключ не создаётся, обычное обслуживание
        (backup) не стартует — новые снимки блокирует тот же маркер.

    Возвращает словарь (никогда не бросает): вызывается из конструктора
    StateStore, где исключение означало бы «backend не стартует».
    """
    data_dir = Path(data_dir)
    backups_root = Path(backups_root)
    if policy_read is None:
        from backend.history_encryption_policy import data_dir_policy_reader

        policy_read = data_dir_policy_reader(data_dir)

    def _blocked(reason: str, **kwargs: Any) -> dict:
        logger.error(
            "encrypted_snapshot: restore recovery fail-closed (%s) — %s",
            reason,
            kwargs.get("snapshot_dir") or data_dir,
        )
        return _recovery_result(ok=False, pending=True, reason=reason, **kwargs)

    try:
        markers = restore_marker_dirs(data_dir)
    except SnapshotOperationRefused as exc:
        return _blocked(exc.reason)

    # Состояние b1 смотрим ВСЕГДА: владелец должен видеть честную картину
    # backups независимо от наличия restore-маркера.
    try:
        b1 = recover_pending_state(data_dir=data_dir, backups_root=backups_root)
    except SnapshotOperationRefused as exc:
        b1 = {"ok": False, "pending": True, "reason": exc.reason, "state": None}
    b1_pending = bool(b1.get("pending"))
    b1_reason = b1.get("reason")

    if not markers:
        if b1_pending:
            # Опубликованный COMMITTING снимка (backup) — докатывать его как
            # restore нельзя: это тихо заменило бы живую историю старым бэкапом.
            return _recovery_result(
                ok=False,
                pending=True,
                reason=b1_reason or REASON_RECOVERY_PENDING,
                state=b1.get("state"),
                snapshot_pending=True,
            )
        if b1_reason:
            return _recovery_result(
                ok=True,
                pending=False,
                reason=b1_reason,
                state=b1.get("state"),
                stale_staging=list(b1.get("stale_staging") or []),
            )
        return _recovery_result(ok=True, pending=False, reason=None)

    staging = markers[0]
    extra = [str(p) for p in markers[1:]]
    if extra:
        logger.warning(
            "encrypted_snapshot: найдено %d restore-маркеров, обрабатывается самый "
            "ранний по имени; остальные: %s",
            len(markers), extra,
        )
    try:
        marker = _read_restore_marker(staging)
    except SnapshotOperationRefused as exc:
        return _blocked(
            exc.reason, extra_markers=extra, snapshot_pending=b1_pending
        )

    transaction_id = str(marker.get("transaction_id") or "")
    pre_restore = marker.get("pre_restore_snapshot")
    target = marker.get("target_snapshot")
    common = {
        "state": marker.get("state"),
        "transaction_id": transaction_id or None,
        "snapshot_dir": str(target) if target else None,
        "pre_restore_snapshot": str(pre_restore) if pre_restore else None,
        "extra_markers": extra,
        "snapshot_pending": b1_pending,
    }

    # --- Маркер COMMITTED: транзакция завершена, осталась только уборка. ---
    if marker.get("state") == RESTORE_STATE_COMMITTED:
        _cancel_staging(staging)
        if transaction_id:
            _cleanup_restore_tmp(data_dir, transaction_id)
        _fsync_dir(data_dir)
        logger.info(
            "encrypted_snapshot: restore %s уже был COMMITTED — приватный staging убран",
            transaction_id,
        )
        marker_entries = _validated_marker_entries(marker)
        if marker.get("restored_entries") is not None and marker_entries is None:
            logger.warning(
                "encrypted_snapshot: restored_entries в маркере %s непригоден (%r) — "
                "не отдаём непроверенное значение",
                transaction_id, marker.get("restored_entries"),
            )
        return _recovery_result(
            ok=True,
            pending=False,
            reason=None,
            restored_entries=marker_entries or 0,
            **common,
        )

    # --- Маркер COMMITTING: докатка целевого снимка. ---
    try:
        # Fail-closed: без доказанной ON-политики расшифровка была бы понижением
        # policy. Ключ не создаём — ни нового, ни через Keychain.
        _require_policy_on(policy_read, reason_when_off=REASON_REQUIRES_ENCRYPTION_ON)
        if crypto is None:
            raise SnapshotOperationRefused(
                REASON_CRYPTO_UNAVAILABLE,
                "ключ недоступен — докачка невозможна, отката в plaintext нет",
            )
        if not target:
            raise SnapshotOperationRefused(
                REASON_MANIFEST_INVALID, "в restore-маркере нет целевого снимка"
            )
        with history_flock(data_dir):
            _require_policy_on(policy_read, reason_when_off=REASON_POLICY_UNAVAILABLE)
            # Повторная верификация: снимок могли подменить/испортить после crash.
            verify_snapshot(backups_root=backups_root, snapshot_dir=target, crypto=crypto)
            result = _apply_verified_snapshot_locked(
                data_dir=data_dir,
                snapshot_dir=Path(str(target)),
                crypto=crypto,
                transaction_id=transaction_id or _new_transaction_id("restore"),
                pre_restore_snapshot=str(pre_restore) if pre_restore else "",
                recovered=True,
                # ТОТ ЖЕ каталог staging: докачка — продолжение той же
                # транзакции, а не новая. Второй маркер не создаётся, поэтому
                # следующий запуск не увидит «две незавершённые операции».
                staging=staging,
            )
    except SnapshotOperationRefused as exc:
        return _blocked(exc.reason, **common)
    except Exception:  # noqa: BLE001 — recovery не имеет права бросить
        logger.exception("encrypted_snapshot: recovery неожиданно упал")
        return _blocked(
            REASON_RECOVERY_PENDING, **common
        )

    return _recovery_result(
        ok=True,
        pending=False,
        reason=None,
        state=result["state"],
        transaction_id=result["transaction_id"],
        snapshot_dir=result["snapshot_dir"],
        pre_restore_snapshot=result["pre_restore_snapshot"] or None,
        restored_entries=result["restored_entries"],
        rolled_forward=True,
        extra_markers=extra,
        snapshot_pending=b1_pending,
    )


def purge_pending_restore_staging(data_dir: Any) -> list[str]:
    """Убирает незавершённый restore целиком (privacy purge, A5.2b2).

    Два вида артефактов, и оба расшифровываются тем же ключом, что и живая
    история, поэтому privacy purge обязан убрать и их:

      * приватные каталоги staging с restore-маркером (где лежат подготовленные
        ENC1-журналы);
      * осиротевшие ``<journal>.a52b2-restore-tmp-<txid>`` рядом с живыми
        файлами — их оставляет жёсткий kill между записью tmp и ``os.replace``,
        и маркера в этот момент может ещё не быть (собственно проба ревьюера).

    Второй вид и есть причина, по которой одной чистки каталогов мало: раньше
    purge проходил, а фрагменты оставались и читались ключом.

    Узкие адресные glob'ы (не ``*`` по всему каталогу) — они же служат
    доказательством покрытия для ``audit_purge_coverage``, который теперь видит
    f-string семейства. Возвращает убранные пути — шаг наблюдаем в ответе
    purge. Ничего не делает, если артефактов нет (обычное состояние).
    """
    import shutil

    removed: list[str] = []
    base = Path(data_dir)
    # Приватные каталоги staging (в т.ч. те, где маркер не успел появиться).
    for staging in sorted(base.glob(f"{RESTORE_STAGING_PREFIX}*")):
        if not staging.is_dir() or staging.is_symlink():
            continue
        shutil.rmtree(staging, ignore_errors=True)
        removed.append(str(staging))
    # Осиротевшие <journal>.a52b2-restore-tmp-<txid> рядом с живыми файлами.
    for tmp in sorted(base.glob(f"*{RESTORE_TMP_SUFFIX}*")):
        if tmp.is_dir() and not tmp.is_symlink():
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            try:
                tmp.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:  # noqa: BLE001 — purge обязан дойти до конца
                logger.warning(
                    "encrypted_snapshot: purge не смог убрать фрагмент %s (%s)",
                    tmp.name, exc,
                )
                continue
        removed.append(str(tmp))
    if removed:
        _fsync_dir(base)
        logger.info(
            "encrypted_snapshot: privacy purge убрал артефакты незавершённого restore: %d",
            len(removed),
        )
    return removed


def read_pending_restore_verdict(data_dir: Any) -> dict | None:
    """Read-only вердикт о незавершённом restore. ``None`` — маркера нет.

    Ничего не пишет и НЕ докатывает: это честный «статус» для точек наблюдения
    (auto-backup status, UI). Само восстановление вызывают точки обслуживания
    (``recover_pending_restore_from_store``), а его вердикт кэшируется в
    ``last_restore_recovery()`` для диагностики.
    """
    markers = restore_marker_dirs(data_dir)
    if not markers:
        return None
    staging = markers[0]
    try:
        marker = _read_restore_marker(staging)
    except SnapshotOperationRefused as exc:
        return _recovery_result(
            ok=False,
            pending=True,
            reason=exc.reason,
            extra_markers=[str(p) for p in markers[1:]],
        )
    pre_restore = marker.get("pre_restore_snapshot")
    target = marker.get("target_snapshot")
    return _recovery_result(
        ok=False,
        pending=True,
        reason=REASON_RECOVERY_PENDING,
        state=marker.get("state"),
        transaction_id=str(marker.get("transaction_id") or "") or None,
        snapshot_dir=str(target) if target else None,
        pre_restore_snapshot=str(pre_restore) if pre_restore else None,
        extra_markers=[str(p) for p in markers[1:]],
    )


# Вердикт ПОСЛЕДНЕЙ реальной попытки восстановления (диагностика/статус).
# Хранится в памяти процесса: после рестарта вердикт всегда пересчитывается
# маркером на диске (см. ``read_pending_restore_verdict``), поэтому «протухшего»
# ответа быть не может — источник истины остаётся диск.
_LAST_RECOVERY_VERDICT: dict | None = None


def last_restore_recovery() -> dict | None:
    """Вердикт последней реальной попытки recovery (или ``None``)."""
    return _LAST_RECOVERY_VERDICT


def recover_pending_restore_from_store(store: Any) -> dict | None:
    """Точка входа recovery для точек обслуживания (backup/restore/auto).

    ``None`` — маркера на диске не было: работа не выполнялась вовсе. Проверка
    наличия маркера НЕ читает его содержимое и НЕ обращается к ключу, поэтому
    обычный вызов без незавершённого restore (в т.ч. OFF-профиль прода) не
    делает ни одного обращения к Keychain.

    Почему НЕ в ``StateStore.__init__`` (решение ревьюера M2): у фасада пять
    точек создания, recovery шёл ДО ``init_sentry``/late-injection ErrorBus, а
    вердикт всё равно никем не читался. Вместо этого докачка живёт там, где
    она обязана блокировать работу, и её вердикт кэшируется для статуса.
    """
    data_dir = Path(store.data_dir)
    if not has_pending_restore(data_dir):
        return None
    from backend.history_encryption_policy import store_policy_reader

    crypto_getter = getattr(store, "_get_history_crypto", None)
    crypto = crypto_getter() if callable(crypto_getter) else None
    global _LAST_RECOVERY_VERDICT
    try:
        _LAST_RECOVERY_VERDICT = recover_pending_restore(
            data_dir=data_dir,
            backups_root=data_dir / "backups",
            crypto=crypto,
            policy_read=store_policy_reader(store),
        )
        return _LAST_RECOVERY_VERDICT
    except Exception:  # noqa: BLE001 — вызывающий не имеет права упасть
        logger.exception("encrypted_snapshot: restore recovery не выполнен")
        return None


def restore_encrypted_snapshot(
    *,
    data_dir: Any,
    backups_root: Any,
    snapshot_dir: Any,
    crypto: Any,
    restore_settings: bool = False,
    policy_read: Callable[[], bool] | None = None,
) -> dict:
    """A5.2b2 Task 2 — восстановление из ПРОВЕРЕННОГО encrypted-снимка.

    Порядок (спека §5 шаги 1–6; карточка b2 решение 4):

      1. дешёвые проверки БЕЗ записи: ``restore_settings`` не поддерживается,
         ключ доступен, текущая policy ON, контейнмент, нет незавершённых
         операций, полная верификация снимка (10 имён, size+sha256, расшифровка
         каждой строки). Любой отказ — до первого байта в живых журналах;
      2. под store-lock (``history_flock`` — тот же файл, что ``StateStore._lock``):
         повторная проверка policy, повторная верификация снимка (между проверкой
         и lock'ом снимок могли изменить), **pre-restore снимок** текущего
         состояния через b1-протокол (страховка для ручного решения владельца),
         сборка ledger union под ТЕМ ЖЕ lock'ом;
      3. фильтрация объединением, запись десяти ENC1-журналов в приватный staging
         на том же filesystem, что и живые файлы, durable restore-маркер
         ``COMMITTING`` **до первой замены**, замены;
      4. read-back всех десяти → ``COMMITTED``; staging убирается; в ответе —
         честный ``restored_entries`` и путь pre-restore снимка.

    ``policy_read`` — тот же fail-closed reader, что у A5.2a. Если не передан,
     берётся ``data_dir_policy_reader(data_dir)`` (тот же механизм, без ссылки на
     StateStore), поэтому модуль тестируется без живой фасада.

    Нарушение инварианта = CRITICAL: ``settings.json`` не восстанавливается
    никогда, plaintext при ON не создаётся, resurrection невозможен.
    """
    data_dir = Path(data_dir)
    backups_root = Path(backups_root)
    snapshot_dir = Path(snapshot_dir)

    # --- Шаг 1: дешёвые проверки. Ни одной записи. ---
    if restore_settings:
        # Молча игнорировать явный запрос владельца нельзя (решение 2 карточки).
        raise SnapshotOperationRefused(
            REASON_RESTORE_SETTINGS_UNSUPPORTED,
            "settings.json не восстанавливается: OFF-настройки понизили бы "
            "текущую encryption policy",
        )
    if crypto is None:
        raise SnapshotOperationRefused(
            REASON_CRYPTO_UNAVAILABLE, "ключ недоступен — restore невозможен"
        )
    if policy_read is None:
        from backend.history_encryption_policy import data_dir_policy_reader

        policy_read = data_dir_policy_reader(data_dir)
    _require_policy_on(policy_read, reason_when_off=REASON_REQUIRES_ENCRYPTION_ON)
    resolved = _require_restorable_snapshot_dir(backups_root, snapshot_dir)
    if restore_marker_dirs(data_dir):
        raise SnapshotOperationRefused(
            REASON_RECOVERY_PENDING,
            "незавершённый restore на диске — сначала требуется recovery",
            pending=True,
        )
    b1_pending = find_pending_transaction(backups_root=backups_root)
    if b1_pending and b1_pending.get("published"):
        raise SnapshotOperationRefused(
            REASON_PENDING_OPERATION,
            f"незавершённая транзакция снимка {b1_pending.get('transaction_id')} "
            f"({b1_pending.get('state')}) — требуется разбор",
            pending=True,
        )
    if b1_pending:
        logger.warning(
            "encrypted_snapshot: оставлен неопубликованный staging %s — "
            "новая транзакция допустима",
            b1_pending.get("path"),
        )
    # Полная верификация ДО записи: неизвестный формат, неполный набор, чужой
    # ключ или tamper не должны дойти до первой замены.
    verify_snapshot(backups_root=backups_root, snapshot_dir=snapshot_dir, crypto=crypto)

    transaction_id = _new_transaction_id("restore")

    # --- Шаги 2–4: под тем же store-lock, что и StateStore._lock. ---
    with history_flock(data_dir):
        # Политика обязана остаться ON: ON→OFF на ходу операции означала бы
        # смену режима на лету (b1-формулировка той же проверки).
        _require_policy_on(policy_read, reason_when_off=REASON_POLICY_UNAVAILABLE)
        # Повторная верификация под lock: снимок читали ДО захвата, за это время
        # он мог измениться (спека §5 шаг 4 — повторная проверка перед заменами).
        verify_snapshot(backups_root=backups_root, snapshot_dir=snapshot_dir, crypto=crypto)

        # Pre-restore снимок ТЕКУЩЕГО состояния — страховка, а не откат: он
        # остаётся на диске, его путь возвращается владельцу (решение 6).
        pre_dir = backups_root / f"snapshot_prerestore_{transaction_id.split('-', 1)[1]}"
        create_encrypted_snapshot(
            data_dir=data_dir,
            backup_dir=pre_dir,
            crypto=crypto,
            transaction_id=f"pre_restore_{transaction_id}",
            policy_on=True,
            policy_read=policy_read,
        )
        return _apply_verified_snapshot_locked(
            data_dir=data_dir,
            snapshot_dir=resolved,
            crypto=crypto,
            transaction_id=transaction_id,
            pre_restore_snapshot=str(pre_dir),
            recovered=False,
        )
