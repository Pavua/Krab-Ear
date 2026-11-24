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
* крах после маркера → ``roll_forward_legacy_locked`` докатывает набор из
  staging (sha256 сверяется с маркером). Ключ и политика ON не нужны —
  в отличие от encrypted-докатки, источник истины здесь сам staging.
  Докатка может прийти не сразу (первая точка обслуживания после краха), а
  процесс в это окно работает: ledger объединяется с ТЕКУЩИМ живым, записи,
  сделанные в окне, переносятся (H1, как у encrypted-ветки);
* жёсткий kill ДО маркера оставляет staging без маркера — следующий restore
  под тем же lock'ом убирает его (``sweep_orphan_restore_staging_locked``).

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
    REASON_RECOVERY_PENDING,
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
    restore_marker_dirs,
    restore_staging_dirs,
)
from backend.purge_fs import rmtree_strict

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
    files, stats, _blocked = _build_legacy_set(source_dir=backup_dir, data_dir=data_dir,
                                               source_reason=REASON_LEGACY_UNREADABLE)
    return files, stats


def _build_legacy_set(*, source_dir: Path, data_dir: Path,
                      source_reason: str) -> tuple[dict[str, bytes], dict[str, int], set[str]]:
    """Источник (бэкап или staging докатки) ⊕ ТЕКУЩИЙ живой ledger.

    Объединение идемпотентно: повторный проход по уже объединённому набору
    даёт те же байты, поэтому докатка после повторного краха безопасна.
    """
    source_dir = Path(source_dir)
    data_dir = Path(data_dir)
    backup = {name: _parse_plaintext_journal(source_dir / name, reason=source_reason)
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
    return files, stats, blocked


# ----------------------------------------------------------------------
# Окно pending: записи, сделанные между крахом и докаткой (H1)
# ----------------------------------------------------------------------


def _known_lines(path: Path) -> tuple[set[str], set[str]]:
    """Строки и id журнала без отказов (для сравнения, не для записи)."""
    lines: set[str] = set()
    ids: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return lines, ids
    for line in text.splitlines():
        if not line.strip():
            continue
        lines.add(line)
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        raw_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(raw_id, str) and raw_id.strip():
            ids.add(raw_id.strip())
    return lines, ids


def _collect_window_records(*, data_dir: Path, files: dict[str, bytes], staging: Path,
                            prerestore_dir: Path | None,
                            blocked: set[str]) -> tuple[dict[str, list[str]], dict[str, int], list[str]]:
    """Строки живых history/status, которых нет ни в staging, ни в pre-restore копии.

    Живой набор в момент докатки = «до restore» (его хранит pre-restore копия)
    или частично заменённый staging, плюс то, что владелец записал после краха.
    Только последнее переносится: прежнее restore сознательно отбрасывает, а
    staging и так ляжет целиком. Удалённое (``blocked``) не переносится —
    запрет resurrection сильнее сохранности. Неразбираемая строка не
    выбрасывается молча: счётчик + warning.
    """
    data_dir = Path(data_dir)
    carry: dict[str, list[str]] = {HISTORY_NAME: [], STATUS_NAME: []}
    counters = {"carried": 0, "delta_carried": 0, "excluded_deleted": 0, "unparsable": 0}
    warnings: list[str] = []
    pre_ok = prerestore_dir is not None and prerestore_dir.is_dir() and not prerestore_dir.is_symlink()
    if not pre_ok:
        # Без страховки «прежнее» от «нового» не отличить: переносим всё, чего нет
        # в staging (сохранность важнее точности), и говорим об этом.
        warnings.append("prerestore_copy_unavailable")
    for name in (HISTORY_NAME, STATUS_NAME):
        live = data_dir / name
        if live.is_symlink() or not live.is_file():
            continue
        known, known_ids = _known_lines_from_bytes(files.get(name, b""))
        # Сырые строки staging тоже «известны»: при частичной замене живой журнал
        # уже равен staging, и удалённая в окне запись оттуда — не запись окна.
        staged_lines, staged_ids = _known_lines(staging / name)
        known |= staged_lines
        known_ids |= staged_ids
        if pre_ok:
            pre_lines, pre_ids = _known_lines(prerestore_dir / name)
            known |= pre_lines
            known_ids |= pre_ids
        try:
            text = live.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SnapshotOperationRefused(REASON_LIVE_LEDGER_UNREADABLE, f"{name} не читается: {exc}") from exc
        is_history = name == HISTORY_NAME
        window_start = 0
        if not is_history:
            # Статус — append-only дельты БЕЗ времени: ok→failed→ok даёт одинаковые
            # строки, а читатель берёт последнюю по id. Дедуп по тексту потерял бы
            # переход, поэтому окно = хвост после базы, которой живой файл
            # начинается (pre-restore байты, если замена не дошла; байты staging,
            # если дошла), и переносится целиком, по порядку, с повторами.
            window_start, base_known = _status_window_start(
                text, bases=[_read_optional(staging / name)]
                + ([_read_optional(prerestore_dir / name)] if pre_ok else []))
            if not base_known:
                warnings.append(f"status_window_base_unknown: {name}")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            if is_history and line in known:
                continue
            if not is_history and lineno <= window_start:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                payload = None
            raw_id = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(raw_id, str) or not raw_id.strip():
                counters["unparsable"] += 1
                warnings.append(f"window_record_unparsable: {name}:{lineno}")
                continue
            item_id = raw_id.strip()
            if item_id in blocked:
                counters["excluded_deleted"] += 1
                continue
            if is_history and item_id in known_ids:
                continue  # дубль записи по id — две записи одного id в выходе = порча
            carry[name].append(line)
            if is_history:
                known.add(line)
                known_ids.add(item_id)
            counters["carried" if is_history else "delta_carried"] += 1
    if any(counters.values()):
        logger.warning("legacy_restore: окно pending — перенесено записей %d, статусов %d, "
                       "исключено удалённых %d, неразбираемых %d", counters["carried"],
                       counters["delta_carried"], counters["excluded_deleted"], counters["unparsable"])
    return carry, counters, warnings


def _read_optional(path: Path) -> str | None:
    """Текст файла; нет файла → пустая база; не читается/symlink → None (не база)."""
    if path.is_symlink():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        return None


def _status_window_start(text: str, *, bases: list[str | None]) -> tuple[int, bool]:
    """Число строк живого status-журнала, принадлежащих базе (не окну).

    База — самый длинный кандидат, которым живой текст начинается по границе
    строки. Не нашлось → ``(0, False)``: весь журнал — окно (сохранность важнее
    точности), вызывающий пишет warning.
    """
    best: str | None = None
    for base in bases:
        if base is None or not text.startswith(base):
            continue
        if base and not base.endswith("\n") and text[len(base):len(base) + 1] not in ("", "\n"):
            continue  # совпадение посреди строки — не граница
        if best is None or len(base) > len(best):
            best = base
    if best is None:
        return 0, False
    return len(best.splitlines()), True


def _known_lines_from_bytes(blob: bytes) -> tuple[set[str], set[str]]:
    """Строки и id выходного набора. Байты собраны нами из строк, прошедших
    ``_parse_plaintext_journal``, но сравнение не должно падать на инварианте:
    неразбираемая строка остаётся «известной» по тексту, id не даёт."""
    lines: set[str] = set()
    ids: set[str] = set()
    for line in blob.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        lines.add(line)
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        raw_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(raw_id, str) and raw_id.strip():
            ids.add(raw_id.strip())
    return lines, ids


# ----------------------------------------------------------------------
# Сироты после жёсткого kill
# ----------------------------------------------------------------------


def sweep_orphan_restore_staging_locked(data_dir: Path) -> list[str]:
    """Убирает restore-staging БЕЗ маркера и осиротевшие tmp-фрагменты.

    Вызывать только под history-lock и только когда незавершённой транзакции
    нет: тогда staging без маркера не принадлежит ни одному работающему
    restore (encrypted-ветка держит тот же lock-файл) — это след kill до
    маркера. Иначе он навсегда блокирует retention (``restore_staging_dirs``)
    и держит в data_dir plaintext-копию журналов. Сбой уборки не мешает restore.
    """
    data_dir = Path(data_dir)
    removed: list[str] = []
    try:
        with_marker = set(restore_marker_dirs(data_dir))
        orphans = [p for p in restore_staging_dirs(data_dir) if p not in with_marker]
    except SnapshotOperationRefused:
        return removed
    for orphan in orphans:
        try:
            rmtree_strict(orphan)
        except OSError:
            logger.warning("legacy_restore: staging-сирота %s не убран", orphan.name, exc_info=True)
            continue
        removed.append(str(orphan))
    _cleanup_restore_tmp(data_dir, None)
    if removed:
        logger.warning("legacy_restore: убраны staging без маркера (kill до маркера): %s", removed)
        try:
            _fsync_dir(data_dir)
        except SnapshotOperationRefused:
            logger.warning("legacy_restore: fsync после уборки сирот не удался", exc_info=True)
    return removed


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


def _files_meta(blobs: dict[str, bytes]) -> list[dict]:
    return [{"name": name, "size": len(blobs[name]), "sha256": _sha256(blobs[name])}
            for name in _ordered_names(blobs)]


def _replace_and_readback(*, data_dir: Path, blobs: dict[str, bytes], transaction_id: str) -> dict:
    files_meta = _files_meta(blobs)
    try:
        for entry in files_meta:
            name = str(entry["name"])
            tmp = data_dir / f"{name}{RESTORE_TMP_SUFFIX}{transaction_id}"
            _write_file_durable(tmp, blobs[name])
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
    if restore_marker_dirs(data_dir):
        raise SnapshotOperationRefused(REASON_RECOVERY_PENDING, "незавершённый restore на диске")
    sweep_orphan_restore_staging_locked(data_dir)
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

    readback = _replace_and_readback(data_dir=data_dir, blobs=files, transaction_id=transaction_id)
    _commit_marker(staging, marker, data_dir)
    logger.info("legacy_restore: %s COMMITTED из %s (%d файлов)", transaction_id, backup_dir, len(files_meta))
    return {"ok": True, "state": RESTORE_STATE_COMMITTED, "transaction_id": transaction_id,
            "pre_restore_snapshot": str(pre_restore_backup), "files": files_meta, "readback": readback}


def roll_forward_legacy_locked(*, data_dir: Path, staging: Path, marker: dict) -> dict:
    """Докатка legacy-транзакции из её staging (под history-lock вызывающего).

    Источник — байты staging, сверенные с size/sha256 маркера: ключ и политика
    не нужны, повторная проверка бэкапа тоже (он мог быть удалён после краха).
    Несовпадение → fail-closed отказ, ничего не трогаем.

    Между крахом и докаткой процесс работал (точка обслуживания — не старт):
    staging объединяется с ТЕКУЩИМ живым ledger (удаления и purge окна не
    отменяются), записи окна переносятся. Staging не переписывается: повторный
    краш → следующий проход заново собирает тот же результат.
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
    names = {entry["name"] for entry in files_meta}
    if not set(LEGACY_JOURNALS) <= names:
        raise SnapshotOperationRefused(REASON_MANIFEST_INVALID, "legacy-маркер без полного набора журналов")
    blobs, _stats, blocked = _build_legacy_set(source_dir=staging, data_dir=data_dir,
                                               source_reason=REASON_MANIFEST_INVALID)
    pre = marker.get("pre_restore_snapshot")
    carry, counters, warnings = _collect_window_records(
        data_dir=data_dir, files=blobs, staging=staging, prerestore_dir=Path(pre) if pre else None,
        blocked=blocked)
    for name, lines in carry.items():
        if lines:
            blobs[name] = blobs[name] + _body(lines)
    if SETTINGS_NAME in names:
        blobs[SETTINGS_NAME] = (staging / SETTINGS_NAME).read_bytes()
    readback = _replace_and_readback(data_dir=data_dir, blobs=blobs, transaction_id=transaction_id)
    committed = dict(marker)
    committed["files"] = _files_meta(blobs)
    _commit_marker(staging, committed, data_dir)
    # Эмбеддинги прежней истории не должны подняться в новом процессе: докатка
    # может идти мимо HistoryService (auto_backup на старте), где RAM-сброса нет.
    from backend.semantic_search import invalidate_index_files

    index_reset = bool(invalidate_index_files(data_dir))
    logger.warning("legacy_restore: %s докатан (roll-forward)", transaction_id)
    return {"ok": True, "state": RESTORE_STATE_COMMITTED, "transaction_id": transaction_id,
            "pre_restore_snapshot": pre, "readback": readback,
            "semantic_reindex_required": index_reset,
            "records_carried": counters["carried"],
            "delta_records_carried": counters["delta_carried"],
            "records_excluded_deleted": counters["excluded_deleted"],
            "records_unparsable": counters["unparsable"],
            "records_at_risk": counters["excluded_deleted"] + counters["unparsable"],
            "warnings": warnings}
