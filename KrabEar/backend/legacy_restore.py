"""M04-05: транзакционный legacy-restore (Encryption OFF) на commit-протоколе A5.2b2.

До этого модуля ``HistoryService.handle_restore_history`` копировал журналы
бэкапа ``shutil.copy2`` поверх живых файлов по одному. Отказы, которые это давало:

1. крах/ENOSPC посреди копии — обрезанный живой ``history.ndjson``;
2. сбой между файлами — новая история со старыми tombstones (смесь);
3. журнал, которого нет в бэкапе, оставался живым (а бэкап кладёт файл только
   если он был — отсутствие означает «пусто на момент бэкапа»);
4. запись, удалённая после бэкапа, воскресала (tombstones заменялись);
5. битый/зашифрованный бэкап копировался как есть.

Протокол (тот же, что у encrypted-ветки, ``encrypted_snapshot.py``):

* всё чтение и проверка — ДО первой записи; ошибка → отказ, живые файлы целы;
* выходной набор пишется в приватный staging рядом с живыми журналами (тот же
  filesystem → ``os.replace`` атомарен), затем durable маркер ``COMMITTING``
  (``kind: legacy_plaintext`` + size/sha256 каждого файла) ДО первой замены;
* замены по одной, read-back всех файлов → маркер ``COMMITTED`` → уборка;
* крах после маркера → ``roll_forward_legacy_locked`` докатывает ТЕ ЖЕ байты
  из staging (sha256 сверяется с маркером). Ключ и политика ON не нужны —
  в отличие от encrypted-докатки, источник истины здесь сам staging.

Решения владельца (24.11): tombstones ОБЪЕДИНЯЮТСЯ с живыми (удаление
пользователя не отменяется restore'ом — как у ветки ON); история и статусы
фильтруются по ledger (живые tombstones ∪ живые purged ∪ tombstones бэкапа),
чтобы текст удалённых записей не возвращался на диск; ``history_purged_ids`` и
остальные дельта-журналы не входят в транзакцию и остаются живыми.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from backend.encrypted_snapshot import (
    REASON_APPLY_FAILED,
    REASON_DESTINATION_EXISTS,
    REASON_FSYNC_FAILED,
    REASON_MANIFEST_INVALID,
    REASON_PREPARED_MISSING,
    REASON_READBACK_FAILED,
    RESTORE_MARKER_VERSION,
    RESTORE_STATE_COMMITTED,
    RESTORE_STATE_COMMITTING,
    RESTORE_TMP_SUFFIX,
    SnapshotOperationRefused,
    _cancel_staging,
    _cleanup_restore_tmp,
    _ensure_private_dir,
    _fsync_dir,
    _new_transaction_id,
    _readback_live_journals,
    _restore_staging_dir,
    _sha256,
    _write_file_durable,
    _write_restore_marker,
)

logger = logging.getLogger("KrabEar.Backend.LegacyRestore")

#: Тип транзакции в restore-маркере (encrypted-маркеры этого поля не имеют).
LEGACY_RESTORE_KIND = "legacy_plaintext"

HISTORY_NAME = "history.ndjson"
TOMBSTONES_NAME = "history_tombstones.ndjson"
STATUS_NAME = "history_status.ndjson"
PURGED_NAME = "history_purged_ids.ndjson"
SETTINGS_NAME = "settings.json"
#: Журналы, которые legacy-бэкап содержит и restore заменяет транзакционно.
LEGACY_JOURNALS: tuple[str, ...] = (HISTORY_NAME, TOMBSTONES_NAME, STATUS_NAME)

#: Префикс страховочной копии: подпадает под ``LEGACY_BACKUP_DIR_PREFIXES`` —
#: копия видна в ``list_backups`` и восстанавливается обычным путём.
PRERESTORE_DIR_PREFIX = "backup_prerestore_"

REASON_LEGACY_UNREADABLE = "legacy_backup_unreadable"
REASON_LIVE_LEDGER_UNREADABLE = "legacy_restore_live_ledger_unreadable"
REASON_PRERESTORE_FAILED = "legacy_restore_prerestore_failed"


def _replace_live(tmp_file: Path, target: Path) -> None:
    """Единственная точка замены живого файла (её подменяют тесты краха)."""
    os.replace(str(tmp_file), str(target))


# ----------------------------------------------------------------------
# Чтение и проверка (без записи)
# ----------------------------------------------------------------------


def _parse_plaintext_journal(path: Path, *, reason: str) -> list[tuple[str, str | None]]:
    """Строки plaintext-журнала как ``(line, id)``. Нет файла → пусто.

    Любая строка, которая не JSON-объект, или ENC1-строка (зашифрованный журнал
    в legacy-бэкапе / OFF-профиле) → отказ ``reason``: «наверное, не запись»
    не угадываем, иначе битые данные легли бы в живую историю.
    """
    if path.is_symlink():
        raise SnapshotOperationRefused(reason, f"{path.name}: symlink не читается")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise SnapshotOperationRefused(reason, f"{path.name} не читается: {exc}") from exc
    out: list[tuple[str, str | None]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:  # ENC1-строка («ENC1:<base64>») — не JSON, отсекается здесь же
            payload = json.loads(line)
        except ValueError as exc:
            raise SnapshotOperationRefused(reason, f"{path.name}:{lineno}: не JSON") from exc
        if not isinstance(payload, dict):
            raise SnapshotOperationRefused(reason, f"{path.name}:{lineno}: не объект")
        raw_id = payload.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise SnapshotOperationRefused(reason, f"{path.name}:{lineno}: id не строка")
        out.append((line, raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None))
    return out


def _body(lines: list[str]) -> bytes:
    return ("".join(line + "\n" for line in lines)).encode("utf-8")


def prepare_legacy_restore_set(*, backup_dir: Path, data_dir: Path) -> tuple[dict[str, bytes], dict[str, int]]:
    """Собирает выходные байты трёх журналов. Только чтение; ошибка → отказ.

    Вызывается под store-lock (живой ledger читается в том же окне, что и
    замена). Возвращает ``(files, stats)``.
    """
    backup_dir = Path(backup_dir)
    data_dir = Path(data_dir)
    backup = {name: _parse_plaintext_journal(backup_dir / name, reason=REASON_LEGACY_UNREADABLE)
              for name in LEGACY_JOURNALS}
    live_tombstones = _parse_plaintext_journal(data_dir / TOMBSTONES_NAME, reason=REASON_LIVE_LEDGER_UNREADABLE)
    live_purged = _parse_plaintext_journal(data_dir / PURGED_NAME, reason=REASON_LIVE_LEDGER_UNREADABLE)

    backup_tomb_ids = {i for _, i in backup[TOMBSTONES_NAME] if i}
    live_tomb_ids = {i for _, i in live_tombstones if i}
    blocked = backup_tomb_ids | live_tomb_ids | {i for _, i in live_purged if i}

    filtered = 0
    files: dict[str, bytes] = {}
    for name in (HISTORY_NAME, STATUS_NAME):
        kept = []
        for line, item_id in backup[name]:
            if item_id and item_id in blocked:
                filtered += 1
                continue
            kept.append(line)
        files[name] = _body(kept)
        if name == HISTORY_NAME:
            restored_lines = len(kept)
    # Объединение tombstones: строки бэкапа как есть + живые id, которых там нет.
    tomb_lines = [line for line, _ in backup[TOMBSTONES_NAME]]
    seen = set(backup_tomb_ids)
    for line, item_id in live_tombstones:
        if item_id and item_id not in seen:
            tomb_lines.append(line)
            seen.add(item_id)
    files[TOMBSTONES_NAME] = _body(tomb_lines)
    stats = {"history_lines": restored_lines, "filtered_out_lines": filtered,
             "tombstones": len(seen), "blocked_ids": len(blocked)}
    return files, stats


# ----------------------------------------------------------------------
# Страховочная копия
# ----------------------------------------------------------------------


def write_prerestore_backup(*, data_dir: Path, transaction_id: str) -> Path:
    """Durable plaintext-копия живых журналов + settings ДО первой замены.

    OFF-профиль: plaintext-копия легальна (тот же формат, что у
    ``handle_backup_history``). Каталог 0700, файлы 0600. Ошибка → отказ до
    любой мутации живых файлов.
    """
    data_dir = Path(data_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backups_root = data_dir / "backups"
    backup_dir = backups_root / f"{PRERESTORE_DIR_PREFIX}{stamp}_{transaction_id[-8:]}"
    dest = backup_dir
    try:
        if dest.exists():
            raise SnapshotOperationRefused(REASON_DESTINATION_EXISTS, f"{dest.name} уже существует")
        _ensure_private_dir(dest.parent)
        _ensure_private_dir(dest)
        copied = 0
        for name in (*LEGACY_JOURNALS, SETTINGS_NAME):
            src = data_dir / name
            if src.is_symlink() or not src.is_file():
                continue
            blob = src.read_bytes()
            _write_file_durable(dest / name, blob)
            copied += len(blob)
        meta = {"backup_ts": stamp, "kind": "prerestore", "transaction_id": transaction_id,
                "size_bytes": copied}
        _write_file_durable(backup_dir / "backup_meta.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        _fsync_dir(dest)
        _fsync_dir(dest.parent)
    except SnapshotOperationRefused:
        raise
    except OSError as exc:
        raise SnapshotOperationRefused(REASON_PRERESTORE_FAILED, f"pre-restore копия не создана: {exc}") from exc
    return dest


# ----------------------------------------------------------------------
# Транзакция
# ----------------------------------------------------------------------


def _ordered_names(files: dict[str, bytes]) -> list[str]:
    """Журналы первыми, settings последним (как и прежний порядок commit)."""
    names = [n for n in LEGACY_JOURNALS if n in files]
    if SETTINGS_NAME in files:
        names.append(SETTINGS_NAME)
    return names


def _replace_and_readback(*, data_dir: Path, staging: Path, files_meta: list[dict],
                          transaction_id: str) -> dict:
    try:
        for entry in files_meta:
            name = str(entry["name"])
            tmp = data_dir / f"{name}{RESTORE_TMP_SUFFIX}{transaction_id}"
            _write_file_durable(tmp, (staging / name).read_bytes())
            _replace_live(tmp, data_dir / name)
    except Exception as exc:  # noqa: BLE001 — после COMMITTING только докатка
        _cleanup_restore_tmp(data_dir, transaction_id)
        raise SnapshotOperationRefused(
            REASON_APPLY_FAILED,
            f"замена живых журналов не удалась: {type(exc).__name__}: {exc}",
            pending=True,
        ) from exc
    _fsync_dir(data_dir)
    try:
        readback = _readback_live_journals(data_dir=data_dir, files_meta=files_meta)
    except Exception as exc:  # noqa: BLE001
        raise SnapshotOperationRefused(REASON_READBACK_FAILED, f"read-back не выполнен: {exc}",
                                       pending=True) from exc
    if not readback["ok"]:
        raise SnapshotOperationRefused(REASON_READBACK_FAILED,
                                       f"read-back не сошёлся: {readback['mismatches']}", pending=True)
    return readback


def _commit_marker(staging: Path, marker: dict, data_dir: Path) -> None:
    """COMMITTED + уборка. Живые файлы УЖЕ заменены: любой сбой здесь — pending
    (на диске остался маркер COMMITTING, докатка идемпотентна), не «отказ до записи»."""
    try:
        marker["state"] = RESTORE_STATE_COMMITTED
        marker["committed_at"] = datetime.now(timezone.utc).isoformat()
        _write_restore_marker(staging, marker)
        _fsync_dir(data_dir)
    except Exception as exc:  # noqa: BLE001
        raise SnapshotOperationRefused(
            getattr(exc, "reason", REASON_FSYNC_FAILED),
            f"маркер COMMITTED не записан: {type(exc).__name__}: {exc}",
            pending=True,
        ) from exc
    _cancel_staging(staging)
    try:
        _fsync_dir(data_dir)
    except SnapshotOperationRefused:
        # COMMITTED уже durable; несинхронизированная уборка staging безвредна —
        # recovery уберёт COMMITTED-маркер как завершённый.
        logger.warning("legacy_restore: fsync после уборки staging не удался", exc_info=True)


def apply_legacy_restore_locked(
    *,
    data_dir: Path,
    files: dict[str, bytes],
    backup_dir: Path,
    pre_restore_backup: str,
    transaction_id: str | None = None,
) -> dict:
    """Применяет подготовленный набор под УЖЕ взятым store-lock.

    До durable ``COMMITTING`` любой сбой отменяет staging — живые файлы целы.
    После — исключение несёт ``pending=True``: транзакция докатывается
    ``roll_forward_legacy_locked`` (точка обслуживания ``_recover_pending_restore``).
    """
    data_dir = Path(data_dir)
    transaction_id = transaction_id or _new_transaction_id("legacy-restore")
    staging = _restore_staging_dir(data_dir, transaction_id)
    if staging.exists():
        raise SnapshotOperationRefused(REASON_DESTINATION_EXISTS, f"staging {staging.name} уже существует")
    try:
        _ensure_private_dir(staging)
        files_meta = []
        for name in _ordered_names(files):
            blob = files[name]
            _write_file_durable(staging / name, blob)
            files_meta.append({"name": name, "size": len(blob), "sha256": _sha256(blob)})
        marker = {
            "version": RESTORE_MARKER_VERSION,
            "kind": LEGACY_RESTORE_KIND,
            "transaction_id": transaction_id,
            "state": RESTORE_STATE_COMMITTING,
            "target_snapshot": str(backup_dir),
            "pre_restore_snapshot": str(pre_restore_backup),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": files_meta,
        }
        _write_restore_marker(staging, marker)
        _fsync_dir(staging)
        _fsync_dir(data_dir)
    except SnapshotOperationRefused:
        _cancel_staging(staging)
        raise
    except OSError as exc:
        _cancel_staging(staging)
        raise SnapshotOperationRefused(REASON_FSYNC_FAILED, f"подготовка restore не удалась: {exc}") from exc

    readback = _replace_and_readback(data_dir=data_dir, staging=staging, files_meta=files_meta,
                                     transaction_id=transaction_id)
    _commit_marker(staging, marker, data_dir)
    logger.info("legacy_restore: %s COMMITTED из %s (%d файлов)", transaction_id, backup_dir, len(files_meta))
    return {"ok": True, "state": RESTORE_STATE_COMMITTED, "transaction_id": transaction_id,
            "pre_restore_snapshot": str(pre_restore_backup), "files": files_meta, "readback": readback}


def roll_forward_legacy_locked(*, data_dir: Path, staging: Path, marker: dict) -> dict:
    """Докатка legacy-транзакции из её staging (под history-lock вызывающего).

    Источник — байты staging, сверенные с size/sha256 маркера: ключ и политика
    не нужны, повторная проверка бэкапа тоже (он мог быть удалён после краха).
    Несовпадение → fail-closed отказ, ничего не трогаем.
    """
    data_dir = Path(data_dir)
    transaction_id = str(marker.get("transaction_id") or "")
    files_meta = marker.get("files")
    if not transaction_id or not isinstance(files_meta, list) or not files_meta:
        raise SnapshotOperationRefused(REASON_MANIFEST_INVALID, "legacy-маркер без транзакции/файлов")
    allowed = {*LEGACY_JOURNALS, SETTINGS_NAME}
    for entry in files_meta:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name not in allowed:
            raise SnapshotOperationRefused(REASON_MANIFEST_INVALID, f"legacy-маркер: чужое имя {name!r}")
        staged = staging / name
        if staged.is_symlink() or not staged.is_file():
            raise SnapshotOperationRefused(REASON_PREPARED_MISSING, f"staging {name} отсутствует")
        blob = staged.read_bytes()
        if len(blob) != entry.get("size") or _sha256(blob) != entry.get("sha256"):
            raise SnapshotOperationRefused(REASON_PREPARED_MISSING, f"staging {name} не совпадает с маркером")
    readback = _replace_and_readback(data_dir=data_dir, staging=staging, files_meta=files_meta,
                                     transaction_id=transaction_id)
    _commit_marker(staging, dict(marker), data_dir)
    # Эмбеддинги прежней истории не должны подняться в новом процессе: докатка
    # может идти мимо HistoryService (auto_backup на старте), где RAM-сброса нет.
    from backend.semantic_search import invalidate_index_files

    invalidate_index_files(data_dir)
    logger.warning("legacy_restore: %s докатан (roll-forward)", transaction_id)
    return {"ok": True, "state": RESTORE_STATE_COMMITTED, "transaction_id": transaction_id,
            "pre_restore_snapshot": marker.get("pre_restore_snapshot"), "readback": readback}
