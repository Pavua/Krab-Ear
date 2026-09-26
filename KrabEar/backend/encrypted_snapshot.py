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
    history_journal_paths,
)

logger = logging.getLogger("KrabEar.Backend.EncryptedSnapshot")

SNAPSHOT_MANIFEST_VERSION = 1
SNAPSHOT_MANIFEST_FILENAME = "snapshot_manifest.json"
STAGING_PREFIX = ".snapshot_staging_"

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
REASON_PREPARED_MISSING = "snapshot_prepared_missing"
REASON_FINGERPRINT_MISMATCH = "snapshot_fingerprint_mismatch"
REASON_PENDING_OPERATION = "snapshot_pending_operation"
REASON_DESTINATION_EXISTS = "snapshot_destination_exists"
REASON_READBACK_FAILED = "snapshot_readback_failed"
REASON_MANIFEST_INVALID = "snapshot_manifest_invalid"
REASON_FSYNC_FAILED = "snapshot_fsync_failed"
REASON_RECOVERY_PENDING = "snapshot_recovery_pending"


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


def _encrypt_journal(source: Path, crypto: Any) -> bytes:
    """Построчно: ENC1 → проверка расшифровкой, plaintext → шифрование.

    Отказ при любой нечитаемой/подделанной строке: молчаливый skip означал бы
    потерю данных при «успешном» снимке.

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

    out_lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
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

    if not out_lines:
        return b""
    body = "\n".join(out_lines)
    if text.endswith("\n"):
        body += "\n"
    return body.encode("utf-8")


# ----------------------------------------------------------------------
# Pending-транзакции
# ----------------------------------------------------------------------


def _snapshot_dirs(backup_dir: Path) -> list[Path]:
    """Каталоги-снимки (опубликованные) внутри backups-корня."""
    root = Path(backup_dir)
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(STAGING_PREFIX)
    )


def find_pending_transaction(*, backup_dir: Path) -> dict | None:
    """Первая незавершённая (не COMMITTED) транзакция в backups-корне.

    Учитываются и опубликованные каталоги (COMMITTING), и оставшиеся
    приватные staging-каталоги (PREPARED — публикация не началась).
    """
    root = Path(backup_dir)
    if not root.is_dir():
        return None
    candidates = _snapshot_dirs(root) + [
        p for p in sorted(root.iterdir())
        if p.is_dir() and p.name.startswith(STAGING_PREFIX)
    ]
    for path in candidates:
        try:
            manifest = _read_manifest(path)
        except SnapshotOperationRefused:
            # Нечитаемый манифест — тоже незавершённая транзакция: молчать нельзя.
            return {
                "state": "UNKNOWN",
                "transaction_id": None,
                "path": str(path),
                "published": not path.name.startswith(STAGING_PREFIX),
            }
        if manifest is None:
            continue
        if manifest.get("state") != STATE_COMMITTED:
            return {
                "state": manifest.get("state"),
                "transaction_id": manifest.get("transaction_id"),
                "path": str(path),
                "published": not path.name.startswith(STAGING_PREFIX),
            }
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

    # Шаг 1: незавершённая операция запрещает новую транзакцию (опубликованная).
    # Сканируется КОРЕНЬ backups, а не каталог-снимок: незавершённая транзакция
    # лежит рядом с новым назначением.
    pending = find_pending_transaction(backup_dir=backup_dir.parent)
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

    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = backup_dir.parent / f"{STAGING_PREFIX}{transaction_id}"
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
        "backup_dir": str(backup_dir),
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
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
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
    """
    backup_dir = Path(backup_dir)
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

    # Шаг 4: durable COMMITTING — ДО первой замены.
    manifest = dict(prepared["manifest"])
    manifest["state"] = STATE_COMMITTING
    _write_manifest_atomic(staging, manifest)

    # С этого момента транзакция необратима: авто-отката в plaintext нет
    # (спека §5), а признак COMMITTING обязан пережить crash для b2-доказки.
    try:
        # Шаг 5: публикация проверенного снимка.
        _publish_staging(staging, backup_dir)
    except OSError as exc:
        # Crash/сбой на первой замене: источники целы, признак COMMITTING
        # остаётся на диске — система fail-closed, отката нет.
        raise SnapshotOperationRefused(
            REASON_READBACK_FAILED,
            f"публикация снимка не удалась: {type(exc).__name__}: {exc}",
            pending=True,
        ) from exc

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
    _fsync_dir(backup_dir)
    logger.info(
        "encrypted_snapshot: транзакция %s зафиксирована (COMMITTED), %d файлов",
        transaction_id, len(manifest["files"]),
    )
    return {
        "ok": True,
        "state": STATE_COMMITTED,
        "transaction_id": transaction_id,
        "backup_dir": str(backup_dir),
        "files": manifest["files"],
        "size_bytes": prepared.get("size_bytes", 0),
        "readback": readback,
    }


def recover_pending_state(*, data_dir: Any, backup_dir: Any) -> dict:
    """Fail-closed признак незавершённой транзакции (доказка — b2).

    ``backup_dir`` здесь — КОРЕНЬ backups (каталог, где лежат снимки и их
    приватные staging-каталоги), а не каталог конкретного снимка.

    b1 НЕ откатывает снимок в plaintext, НЕ создаёт новый ключ и НЕ запускает
    обычное обслуживание: единственный честный ответ — «есть незавершённая
    операция, разбираться должна b2».

    ``data_dir`` принимается для контракта b2 (recovery сверяет источники) и в
    b1 намеренно не используется.
    """
    del data_dir  # контракт b2; в b1 источники не трогаем
    pending = find_pending_transaction(backup_dir=Path(backup_dir))
    if pending is None:
        return {
            "ok": True,
            "pending": False,
            "reason": None,
            "state": None,
            "transaction_id": None,
            "path": None,
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
