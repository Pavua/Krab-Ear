# A5.2a: legacy backup/restore plaintext gates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** При `history_encryption_enabled=True` legacy-операции, создающие
plaintext-копию управляемой истории (manual/auto backup, restore, archive,
transcript versions, schema migration), отказывают ДО mkdir/touch/copy/append/
rewrite/prune с машинно-читаемой причиной, а OFF-профиль сохраняется бит-в-бит.

**Architecture:** Один fail-closed policy-читатель — тот же механизм, что
`StateStore._read_encryption_flag_unlocked` (A5 #2049), вынесенный в
module-level функцию `read_history_encryption_flag()` без изменения семантики.
Guard вызывается до lock (не создавать storage) и повторно ПОД тем же
межпроцессным history-flock: store-backed sink'и — под `StateStore._lock()`,
sink'и без store-ссылки (DataMigrator, TranscriptVersionManager) — под
module-level `history_flock(data_dir)` на тот же `data_dir/history.lock`
(закрывает OFF→ON гонку через второй StateStore). Явный owner purge
(`clear_all`) не блокируется. Encrypted snapshot/restore — A5.2b; эта карточка
возвращает честный отказ.

**Tech Stack:** Python 3.12/3.14, pytest, существующий POSIX flock
(`StateStore._lock` + module-level `history_flock` для sink'ов без store,
per-thread reentrancy SH/EX), `core.parsing_utils.safe_json_loads`.
Новых зависимостей нет.

**Spec:** `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`, §§1, 4–6, 9.

**Handoff:** `docs/superpowers/handoffs/2026-09-25-a5-lifecycle-start.md`, §«Порядок
следующей реализации» п.1, §«Наблюдаемость отказа», §«RED случаи для карточки A5.2a».

**База:** `a0d101638e4b91d54f430df5e59bb7cf28214b92`,
`origin/codex/krab-ear-v2`; worktree `.worktrees/a52a-legacy-backup-gate`.

**Баны (playbook §1):**

- База только `origin/codex/krab-ear-v2`. Не `audit/*`, не detached stale worktree.
- `git add` явными путями. Никогда `-A`.
- Не запускать собранный `KrabEarAgent` / `open "Krab Ear.app"` — `SingleInstanceGuard` убьёт прод.
- Прод-backend: только `scripts/safe_backend_restart.command`. Не голый `launchctl kickstart -k`.
- Не трогать Main Krab (`start_krab.command` / `Stop Krab.command` / `~/.openclaw`) и VG `.env`.
- Не мержить PR #1875. Не дообучать `krab_ru` синтетическим TTS.
- Не строить второй EventBridge. Не возвращать wake word на SSE.
- Не включать `REST_IN_PROCESS_ENABLED` в проде.
- Визуал Swift (цвета/шрифты/layout): только `agy` + Gemini 3.1 Pro.
- После правки SOURCE гонять зависящие тесты на обоих языках.
- `BackendService(...)` в тесте → `service.close()` в `tearDown`.
- Не наследовать `threading.Thread` в тест-стабах, если `start()` не зовёт `super().start()`.
- Тест со сторонними/ML-зависимостями обязан скипаться ЧИСТО по именно тому импорту.
- Секреты (`lens_keys.env`, `hf_token`, `gh auth token`) использовать, **не печатать**.
- **Brain/GPU:** никогда `memory_conductor_enforce*` / `enforce_brain`.

**Карточные баны A5.2a (сверх playbook):**

- `history_encryption_enabled` в прод-коде остаётся OFF. Только synthetic
  tmp-профили и in-memory поведение; system Keychain и живая история владельца
  не читаются и не копируются.
- НЕ трогать `KrabEar/backend/service.py` и `KrabEar/core/engine.py`.
- `KrabEar/backend/state_store.py` — только contract wiring: чистый вынос
  fail-closed чтения в module-level функцию без изменения семантики.
- Никаких рестартов backend / launchctl / живых e2e / ML-GPU тестов.
- Запускать только перечисленные test-файлы точечно, `-q`.

## Global Constraints

- Guard и sink под одним lock-контрактом: `StateStore._lock` — единый per-thread
  реентерабельный flock; НИКОГДА exclusive поверх удерживаемого shared.
- Policy-читатель НЕ обращается к Keychain: только settings.json + ENC1-скан
  десяти журналов. Повреждённые/нечитаемые settings и неверный тип флага →
  «считаем ON» (отказ), а не OFF.
- Не заменять `cached_settings()` default False как источник policy.
- `settings.json` не восстанавливается поверх текущей policy (`copy2` запрещён при ON).
- Явный owner purge (`clear_all`) и каскадные удаления (delete/cleanup) не блокируются.

## Review Focus

- Sidecar-only ENC1 при потерянных settings → guard ON без Keychain (Task 1).
- OFF→ON через второй StateStore между первичной проверкой и lock → повторная
  проверка под lock блокирует sink (Task 2/3).
- Retention overflow при ON не prune'ит старые backup'ы/meta (Task 3).
- Archive/version construction при ON не создаёт storage, backend assembly жив (Task 4/5).
- Schema migrate/rollback при ON возвращает `MigrationResult`, не произвольный
  failure dict, и не создаёт ложный success-log (Task 6).

---

### Task 1: fail-closed policy reader как единый механизм

**Files:**
- Modify: `KrabEar/backend/state_store.py` (вынести `_read_encryption_flag_unlocked`
  и `_has_encrypted_history_unlocked` в module-level функции; публичный реестр
  имён десяти журналов)
- Create: `KrabEar/backend/history_encryption_policy.py`
- Create: `KrabEar/tests/test_a52a_legacy_backup_gate.py`

**Interfaces:**
- Produces: `state_store.HISTORY_JOURNAL_FILENAMES: tuple[str, ...]`,
  `state_store.history_journal_paths(data_dir) -> tuple[Path, ...]`,
  `state_store.has_encrypted_history_in(journal_paths) -> bool`,
  `state_store.read_history_encryption_flag(settings_path, journal_paths, *, push_error=None) -> bool`.
- Produces: `history_encryption_policy.OPERATION_UNAVAILABLE_REASON`,
  `HistoryEncryptionOperationUnavailable`, `store_policy_reader(store)`,
  `data_dir_policy_reader(data_dir)`, `policy_blocks(policy_read)`.
- Semantics preserved exactly: missing settings + no ENC1 → False; missing
  settings + ENC1 → True; flag absent beside ENC1 → True; non-bool flag → True;
  corrupt/non-object/unreadable → True (+ loud `history.encrypt_fail`).

- [ ] **Step 1: Write the failing tests for the failure modes**

См. полный файл `KrabEar/tests/test_a52a_legacy_backup_gate.py` в Task 2–6; для
этой задачи — класс `TestManualBackupPolicyFailureModes` (входит в общий файл).

- [ ] **Step 2: Run tests to verify they fail**

Run:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52a_legacy_backup_gate.py -q`
Expected: `ModuleNotFoundError: No module named 'backend.history_encryption_policy'`
(красный по правильной причине: guard-модуля ещё нет).

- [ ] **Step 3: Write minimal implementation**

В `state_store.py` добавить module-level (до `class StateStore`):

```python
# A5.2a: явный реестр десяти управляемых журналов истории. Единый источник
# имён для policy-guard'ов без StateStore-ссылки (DataMigrator,
# TranscriptVersionManager). Не glob — см. спеку A5 §1.
HISTORY_JOURNAL_FILENAMES: tuple[str, ...] = (
    "history.ndjson",
    "history_tombstones.ndjson",
    "history_purged_ids.ndjson",
    "history_status.ndjson",
    "history_tags.ndjson",
    "history_favorites.ndjson",
    "history_annotations.ndjson",
    "history_text_updates.ndjson",
    "history_action_items.ndjson",
    "history_calendar_links.ndjson",
)


def history_journal_paths(data_dir: Path) -> tuple[Path, ...]:
    base = Path(data_dir)
    return tuple(base / name for name in HISTORY_JOURNAL_FILENAMES)


def has_encrypted_history_in(journal_paths) -> bool:
    """Есть ли ENC1-строки в любом из переданных журналов."""
    from backend.history_crypto import SENTINEL

    for raw in journal_paths:
        path = Path(raw)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            if any(line.startswith(SENTINEL) for line in fh):
                return True
    return False


def read_history_encryption_flag(
    settings_path: Path,
    journal_paths,
    *,
    push_error=None,
) -> bool:
    """Fail-closed чтение флага history_encryption_enabled (A5 #2049)."""
    settings_path = Path(settings_path)
    journal_paths = tuple(Path(p) for p in journal_paths)

    def _flag(message: str) -> bool:
        if push_error is not None:
            try:
                push_error("history.encrypt_fail", message, "error")
            except Exception:  # noqa: BLE001
                logger.exception(
                    "read_history_encryption_flag: push_error failed"
                )
        return True

    try:
        if not settings_path.exists():
            if has_encrypted_history_in(journal_paths):
                return _flag(
                    "settings missing while encrypted history exists; "
                    "assuming enabled"
                )
            return False
        payload = safe_json_loads(
            settings_path.read_text(encoding="utf-8"),
            default=None,
            context="settings.json (encryption flag check)",
        )
        if isinstance(payload, dict):
            if "history_encryption_enabled" not in payload:
                if has_encrypted_history_in(journal_paths):
                    return _flag(
                        "encryption flag missing beside ENC1 history; "
                        "assuming enabled"
                    )
                return False
            flag = payload["history_encryption_enabled"]
            if isinstance(flag, bool):
                return flag
            return _flag(
                "encryption flag has invalid type; assuming enabled"
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "StateStore._read_encryption_flag_unlocked: ошибка чтения"
        )
        return _flag(
            "encryption flag unreadable, assuming enabled: "
            f"{type(exc).__name__}: {exc}"
        )
    logger.error(
        "StateStore._read_encryption_flag_unlocked: settings не объект, "
        "считаем шифрование включённым"
    )
    return _flag(
        "encryption flag unreadable (settings not an object), "
        "assuming enabled"
    )
```

Заменить тела трёх методов StateStore на делегирование:

```python
def _history_journal_paths(self) -> tuple[Path, ...]:
    return history_journal_paths(self.data_dir)

def _has_encrypted_history_unlocked(self) -> bool:
    return has_encrypted_history_in(self._history_journal_paths())

def _read_encryption_flag_unlocked(self) -> bool:
    return read_history_encryption_flag(
        self.settings_path,
        self._history_journal_paths(),
        push_error=self._push_error,
    )
```

Создать `KrabEar/backend/history_encryption_policy.py`:

```python
"""A5.2a — единый guard plaintext-копий управляемой истории.

При ``history_encryption_enabled`` (или неопределённой политике) legacy-
операции, создающие plaintext-копию истории, отклоняются ДО
mkdir/touch/copy/append/rewrite/prune. Политика читается тем же fail-closed
механизмом, что ``StateStore._read_encryption_flag_unlocked`` (A5 #2049).
Guard НЕ трогает Keychain. OFF-профиль сохраняет прежнее поведение.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from backend.state_store import (
    history_journal_paths,
    read_history_encryption_flag,
)

logger = logging.getLogger("KrabEar.Backend.HistoryEncryptionPolicy")

OPERATION_UNAVAILABLE_REASON = "history_encryption_operation_unavailable"


class HistoryEncryptionOperationUnavailable(Exception):
    """Plaintext-операция над историей запрещена при encryption ON."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.reason = OPERATION_UNAVAILABLE_REASON
        super().__init__(f"{operation}: {OPERATION_UNAVAILABLE_REASON}")


def data_dir_policy_reader(data_dir: Path | str) -> Callable[[], bool]:
    """Fail-closed reader для менеджера без StateStore-ссылки."""
    base = Path(data_dir)
    settings_path = base / "settings.json"
    journals = history_journal_paths(base)
    return lambda: read_history_encryption_flag(settings_path, journals)


def store_policy_reader(store: Any) -> Callable[[], bool]:
    """Fail-closed reader для менеджера с StateStore.

    Использует существующий ``StateStore._read_encryption_flag_unlocked``.
    Метод обязан быть определён на КЛАССЕ store: проверка через
    ``getattr(type(store), ...)`` не даёт ``MagicMock`` авто-создать атрибут
    (иначе любой mock-store выглядел бы как ON и ломал OFF-контроль).
    Лёгкие тестовые двойники без этого метода считаются OFF.
    """
    class_reader = getattr(type(store), "_read_encryption_flag_unlocked", None)
    if callable(class_reader):
        bound = getattr(store, "_read_encryption_flag_unlocked", None)
        if callable(bound):
            return lambda: bool(bound())
    return lambda: False


def policy_blocks(policy_read: Callable[[], bool] | None) -> bool:
    """Fail-closed: ошибка чтения политики означает «считаем ON» (отказ)."""
    if policy_read is None:
        return False
    try:
        return bool(policy_read())
    except Exception:  # noqa: BLE001
        logger.exception(
            "history_encryption_policy: ошибка чтения политики — считаем ON"
        )
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: policy/backup failure-mode RED-кейсы переходят в
PASS после Task 2 (guard установлен в `handle_backup_history`).

---

### Task 2: manual backup + restore gate

**Files:**
- Modify: `KrabEar/backend/history_service.py`
  (`handle_backup_history`, `handle_restore_history`)
- Test: `KrabEar/tests/test_a52a_legacy_backup_gate.py`

**Interfaces:**
- Consumes: `history_encryption_policy.store_policy_reader`, `policy_blocks`,
  `OPERATION_UNAVAILABLE_REASON`.
- Produces: `handle_backup_history.on == {"backup_path": None, "size_mb": 0.0,
  "entries": 0, "ok": False, "reason": "history_encryption_operation_unavailable"}`;
  `handle_restore_history.on == {"restored_entries": 0, "backup_date": "unknown",
  "ok": False, "reason": "..."}`.

- [ ] **Step 1: Write the failing tests**

```python
def _data_snapshot(data_dir: Path) -> dict:
    """Байты файлов данных, кроме служебных flock-файлов (*.lock)."""
    return {
        p.name: _bytes(p)
        for p in sorted(data_dir.iterdir())
        if p.is_file() and p.suffix != ".lock"
    }


class TestManualBackupPolicyFailureModes:
    def test_manual_backup_refuses_on_and_writes_nothing(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        store = StateStore(data_dir)
        svc = HistoryService(store=store)
        (data_dir / "history.ndjson").write_text('{"id":"a"}\n', encoding="utf-8")
        before = _data_snapshot(data_dir)
        with patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
            result = svc.handle_backup_history({})
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert not (data_dir / "backups").exists()
        after = _data_snapshot(data_dir)
        assert after == before

    def test_manual_backup_refuses_on_corrupt_settings_without_keychain(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_settings(data_dir, "{synthetic-corruption")
        store = StateStore(data_dir)
        svc = HistoryService(store=store)
        with patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
            result = svc.handle_backup_history({})
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert not (data_dir / "backups").exists()

    def test_manual_backup_refuses_on_invalid_flag_type(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_settings(data_dir, {"history_encryption_enabled": None})
        store = StateStore(data_dir)
        svc = HistoryService(store=store)
        with patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
            result = svc.handle_backup_history({})
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert not (data_dir / "backups").exists()

    def test_manual_backup_refuses_when_settings_missing_but_sidecar_enc1(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        svc = HistoryService(store=store)
        (data_dir / "history_tags.ndjson").write_text(
            _enc1_line(), encoding="utf-8"
        )
        assert not (data_dir / "settings.json").exists()
        with patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
            result = svc.handle_backup_history({})
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert not (data_dir / "backups").exists()

    def test_manual_backup_refuses_when_policy_read_raises(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        svc = HistoryService(store=store)
        with patch.object(
            store,
            "_read_encryption_flag_unlocked",
            side_effect=RuntimeError("synthetic policy failure"),
        ), patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
            result = svc.handle_backup_history({})
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert not (data_dir / "backups").exists()


class TestManualBackupRestoreGate:
    def test_manual_backup_off_control_still_backs_up(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        svc = HistoryService(store=store)
        result = svc.handle_backup_history({})
        assert result.get("backup_path")
        assert Path(result["backup_path"]).is_dir()

    def test_manual_backup_recheck_under_lock_blocks_off_to_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        svc = HistoryService(store=store, cached_settings=lambda: {})
        second = StateStore(data_dir)
        real_lock = store._lock
        state = {"flipped": False}

        @contextmanager
        def flipping_lock(*args, **kwargs):
            if not state["flipped"]:
                state["flipped"] = True
                second.save_settings({"history_encryption_enabled": True})
            with real_lock(*args, **kwargs):
                yield

        with patch.object(store, "_lock", flipping_lock):
            result = svc.handle_backup_history({})
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert not (data_dir / "backups").exists()

    def test_restore_refuses_on_and_keeps_current_profile(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        svc = HistoryService(store=store)
        store.add_history_item(text="synthetic restore seed")
        backup = svc.handle_backup_history({})
        backup_dir = Path(backup["backup_path"])
        store.save_settings({"history_encryption_enabled": True})
        history_before = _bytes(store.history_path)
        settings_before = _bytes(data_dir / "settings.json")
        result = svc.handle_restore_history(
            {"backup_path": str(backup_dir), "restore_settings": True}
        )
        assert result["ok"] is False
        assert result["reason"] == REASON
        assert _bytes(store.history_path) == history_before
        assert _bytes(data_dir / "settings.json") == settings_before
        assert store.load_settings()["history_encryption_enabled"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52a_legacy_backup_gate.py -q -k "ManualBackup"`
Expected: FAIL — `result["ok"]`/`result["reason"]` отсутствуют (handler всё ещё пишет plaintext backup), либо `KeyError`.

- [ ] **Step 3: Write minimal implementation**

`handle_backup_history`: после privacy gate добавить policy-reader и первичную
проверку; mkdir/copy перенести под `store._lock()` с повторной проверкой:

```python
from backend.history_encryption_policy import (
    OPERATION_UNAVAILABLE_REASON,
    policy_blocks,
    store_policy_reader,
)
...
if self._is_privacy_mode():
    return {"backup_path": None, "size_mb": 0.0, "entries": 0,
            "reason": "privacy_mode_active"}

policy_read = store_policy_reader(self.store)
refusal = {
    "backup_path": None,
    "size_mb": 0.0,
    "entries": 0,
    "ok": False,
    "reason": OPERATION_UNAVAILABLE_REASON,
}
if policy_blocks(policy_read):
    return refusal

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
backups_dir = Path(self.store.data_dir) / "backups"
backup_dir = backups_dir / f"backup_{ts}"
files_to_backup = [...]

total_bytes = 0
with self.store._lock():
    # OFF→ON через второй StateStore до получения lock: повторная проверка.
    if policy_blocks(policy_read):
        return refusal
    backup_dir.mkdir(parents=True, exist_ok=True)
    for src in files_to_backup:
        if src.exists():
            dst = backup_dir / src.name
            shutil.copy2(src, dst)
            total_bytes += dst.stat().st_size

entries = self.store.count_active_items()
# meta/backup_meta.json — как раньше, вне lock.
```

`handle_restore_history`: policy-проверка в самом начале (до валидации пути,
чтобы ON не давал частичного результата) и повторно под существующим
`with self.store._lock():` до `copy2`:

```python
policy_read = store_policy_reader(self.store)
if policy_blocks(policy_read):
    return {
        "restored_entries": 0,
        "backup_date": "unknown",
        "ok": False,
        "reason": OPERATION_UNAVAILABLE_REASON,
    }
...
with self.store._lock():
    if policy_blocks(policy_read):
        return { ...тот же refusal... }
    ... copy2 ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: PASS.

---

### Task 3: auto backup gate + наблюдаемый status

**Files:**
- Modify: `KrabEar/backend/auto_backup.py`
  (`__init__`, `_do_backup`, `check_and_backup`, `get_auto_backup_status`)
- Test: `KrabEar/tests/test_a52a_legacy_backup_gate.py`

**Interfaces:**
- Produces: `check_and_backup.on == {"backed_up": False,
  "skipped_reason": "history_encryption_operation_unavailable", "backup_path": None}`;
  `get_auto_backup_status.on` добавляет `"encryption_operation_unavailable": True`
  и `"skipped_reason": "..."`.
- Preserves: OFF lock-count 1, mkdir/copy still under store lock.

- [ ] **Step 1: Write the failing tests**

```python
class TestAutoBackupGate:
    def _manager(self, store, **kwargs):
        return AutoBackupManager(store=store, interval_hours=0, **kwargs)

    def test_check_and_backup_refuses_and_preserves_existing(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        backups = data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        old = backups / "auto_backup_20200101_000000"
        old.mkdir()
        (old / "backup_meta.json").write_text('{"old": true}', encoding="utf-8")
        meta = backups / "auto_backup_meta.json"
        meta.write_text('{"last_backup_ts": null, "backup_count": 5}', encoding="utf-8")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = self._manager(store)
        history_before = _bytes(store.history_path)
        out = mgr.check_and_backup()
        assert out["backed_up"] is False
        assert out["skipped_reason"] == REASON
        assert meta.read_text(encoding="utf-8") == '{"last_backup_ts": null, "backup_count": 5}'
        assert (old / "backup_meta.json").read_text(encoding="utf-8") == '{"old": true}'
        assert _bytes(store.history_path) == history_before
        dirs = [p.name for p in backups.iterdir() if p.is_dir()]
        assert dirs == ["auto_backup_20200101_000000"]

    def test_status_reports_operation_unavailable(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = self._manager(store)
        status = mgr.get_auto_backup_status()
        assert status["encryption_operation_unavailable"] is True
        assert status["skipped_reason"] == REASON

    def test_retention_overflow_preserved_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        backups = data_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        created = []
        for i in range(4):
            d = backups / f"auto_backup_2020010{i}_000000"
            d.mkdir()
            (d / "backup_meta.json").write_text(f'{{"i": {i}}}', encoding="utf-8")
            created.append(d.name)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = self._manager(store, max_copies=1)
        out = mgr.check_and_backup()
        assert out["skipped_reason"] == REASON
        dirs = sorted(p.name for p in backups.iterdir() if p.is_dir())
        assert dirs == sorted(created)

    def test_off_control_still_backs_up(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        mgr = self._manager(store)
        out = mgr.check_and_backup()
        assert out["backed_up"] is True

    def test_recheck_under_store_lock_blocks_off_to_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        second = StateStore(data_dir)
        mgr = self._manager(store)
        real_lock = store._lock
        state = {"flipped": False}

        @contextmanager
        def flipping_lock(*args, **kwargs):
            if not state["flipped"]:
                state["flipped"] = True
                second.save_settings({"history_encryption_enabled": True})
            with real_lock(*args, **kwargs):
                yield

        with patch.object(store, "_lock", flipping_lock):
            out = mgr.check_and_backup()
        assert out["backed_up"] is False
        assert out["skipped_reason"] == REASON
        backups = data_dir / "backups"
        assert not backups.exists() or not any(
            p.is_dir() and p.name.startswith("auto_backup_") for p in backups.iterdir()
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52a_legacy_backup_gate.py -q -k "AutoBackup"`
Expected: FAIL — ON-профиль всё ещё создаёт `auto_backup_*`/meta, `status` без
`encryption_operation_unavailable`.

- [ ] **Step 3: Write minimal implementation**

`__init__`: `self._encryption_policy_read = store_policy_reader(store)`.
`_encryption_blocked()`: `return policy_blocks(getattr(self, "_encryption_policy_read", None))`.
`check_and_backup`: проверка после `purged` до `self._lock`, повторно под
`self._lock` перед `_do_backup`; `_do_backup()` обернуть `try/except
HistoryEncryptionOperationUnavailable → refusal`. `_do_backup`: перенести
`backup_dir.mkdir(...)` внутрь `with self._store_lock():` и повторно проверить
policy ПОД store-lock до mkdir; при ON — `raise
HistoryEncryptionOperationUnavailable("auto_backup")`.
`get_auto_backup_status`: добавить `encryption_operation_unavailable` и
`skipped_reason`.

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: PASS.

---

### Task 4: ArchiveManager gate (construction/archive/unarchive, purge preserved)

**Files:**
- Modify: `KrabEar/backend/archive_manager.py`
- Test: `KrabEar/tests/test_a52a_legacy_backup_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestArchiveManagerGate:
    def test_constructs_without_storage_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        store = StateStore(data_dir)
        ArchiveManager(store=store)
        assert not (data_dir / "archive").exists()

    def test_archive_items_direct_and_ipc_refuse_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        item = store.add_history_item(text="synthetic archive seed")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = ArchiveManager(store=store)
        tombstones_before = _bytes(store.tombstones_path)
        result = mgr.archive_items([item.id])
        assert isinstance(result, dict)
        assert result["ok"] is False and result["reason"] == REASON
        assert not (data_dir / "archive").exists()
        assert _bytes(store.tombstones_path) == tombstones_before
        ipc = mgr.handle_archive_items({"item_ids": [item.id]})
        assert ipc["ok"] is False and ipc["reason"] == REASON

    def test_unarchive_items_refuses_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        off_mgr = ArchiveManager(store=store)
        off_mgr._archive_path.write_text('{"id":"seed","text":"x"}\n', encoding="utf-8")
        before = _bytes(off_mgr._archive_path)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = ArchiveManager(store=store)
        result = mgr.unarchive_items(["seed"])
        assert result["ok"] is False and result["reason"] == REASON
        assert _bytes(off_mgr._archive_path) == before
        ipc = mgr.handle_unarchive_items({"item_ids": ["seed"]})
        assert ipc["ok"] is False and ipc["reason"] == REASON

    def test_explicit_purge_not_blocked_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        off_mgr = ArchiveManager(store=store)
        off_mgr._archive_path.write_text('{"id":"seed","text":"x"}\n', encoding="utf-8")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = ArchiveManager(store=store)
        removed = mgr.clear_all()
        assert removed >= 1
        assert mgr._archive_path.read_text(encoding="utf-8") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52a_legacy_backup_gate.py -q -k "Archive"`
Expected: FAIL — ON-профиль создаёт `archive/` при конструировании и
append'ит archive.ndjson.

- [ ] **Step 3: Write minimal implementation**

`__init__`: `self._encryption_policy_read = store_policy_reader(store)`; mkdir/
touch только если `not policy_blocks(...)`. `archive_items`/`unarchive_items`:
guard в начале → `{"ok": False, "reason": OPERATION_UNAVAILABLE_REASON}`; повтор
под `_store._lock()` (atomic) / `self._lock` (fallback, unarchive). `clear_all`:
`self._archive_dir.mkdir(parents=True, exist_ok=True)` в начале — explicit purge
не блокируется.

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: PASS.

---

### Task 5: TranscriptVersionManager gate (construction/save/revert/cap/orphan)

**Files:**
- Modify: `KrabEar/backend/transcript_versioning.py`
- Test: `KrabEar/tests/test_a52a_legacy_backup_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestTranscriptVersionGate:
    def test_constructs_without_storage_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        TranscriptVersionManager(data_dir=data_dir)
        assert not (data_dir / "transcript_versions.ndjson").exists()

    def test_save_version_direct_and_ipc_refuse_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = TranscriptVersionManager(data_dir=data_dir)
        with pytest.raises(HistoryEncryptionOperationUnavailable):
            mgr.save_version("item-1", "synthetic text")
        assert not (data_dir / "transcript_versions.ndjson").exists()
        ipc = mgr.handle_save_transcript_version(
            {"item_id": "item-1", "text": "synthetic text"}
        )
        assert ipc["ok"] is False and ipc["reason"] == REASON

    def test_revert_to_version_refuses_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        seed = TranscriptVersionManager(data_dir=data_dir)
        seed.save_version("item-1", "first", "stt_raw")
        before = _bytes(data_dir / "transcript_versions.ndjson")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = TranscriptVersionManager(data_dir=data_dir)
        with pytest.raises(HistoryEncryptionOperationUnavailable):
            mgr.revert_to_version("item-1", 1)
        assert _bytes(data_dir / "transcript_versions.ndjson") == before
        ipc = mgr.handle_revert_transcript_version(
            {"item_id": "item-1", "version_num": 1}
        )
        assert ipc["ok"] is False and ipc["reason"] == REASON

    def test_orphan_cleanup_blocked_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        seed = TranscriptVersionManager(data_dir=data_dir)
        seed.save_version("gone", "text", "manual")
        before = _bytes(data_dir / "transcript_versions.ndjson")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = TranscriptVersionManager(data_dir=data_dir)
        assert mgr.purge_orphaned_versions(set()) == 0
        assert _bytes(data_dir / "transcript_versions.ndjson") == before

    def test_version_cap_rewrite_blocked_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        seed = TranscriptVersionManager(data_dir=data_dir)
        seed.save_version("item-1", "text", "manual")
        before = _bytes(data_dir / "transcript_versions.ndjson")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = TranscriptVersionManager(data_dir=data_dir)
        records = [
            {"item_id": "item-1", "version_num": i, "text": "t", "source": "manual"}
            for i in range(1, 60)
        ]
        out = mgr._enforce_version_cap("item-1", records)
        assert out is records
        assert _bytes(data_dir / "transcript_versions.ndjson") == before

    def test_explicit_version_purge_not_blocked_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        seed = TranscriptVersionManager(data_dir=data_dir)
        seed.save_version("item-1", "text", "manual")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        mgr = TranscriptVersionManager(data_dir=data_dir)
        removed = mgr.clear_all()
        assert removed >= 1
        assert (data_dir / "transcript_versions.ndjson").read_text(encoding="utf-8") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52a_legacy_backup_gate.py -q -k "TranscriptVersion"`
Expected: FAIL — ON-профиль создаёт файл версий и append'ит.

- [ ] **Step 3: Write minimal implementation**

`__init__`: `self._encryption_policy_read = data_dir_policy_reader(data_dir)`;
mkdir/touch только если `not policy_blocks(...)`. `save_version`/
`revert_to_version`: guard в начале → `raise HistoryEncryptionOperationUnavailable`.
`_enforce_version_cap`: guard → вернуть `all_records` без rewrite.
`purge_orphaned_versions`: guard → `0`. IPC handlers ловят
`HistoryEncryptionOperationUnavailable` → `{"ok": False, "reason": REASON}`.
`delete_versions_for`/`cleanup_for_ids`/`clear_all` не блокируются.

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: PASS.

---

### Task 6: DataMigrator gate (migrate/rollback, no false backup)

**Files:**
- Modify: `KrabEar/backend/data_migrator.py`
  (`MigrationResult`, `migrate`, `rollback_migration`, `handle_run_migration`)
- Test: `KrabEar/tests/test_a52a_legacy_backup_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestDataMigratorGate:
    def test_migrate_refuses_and_writes_nothing_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        history = data_dir / "history.ndjson"
        history.write_text(json.dumps({"id": "1", "text": "v1"}) + "\n", encoding="utf-8")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        before = _bytes(history)
        migrator = DataMigrator(data_dir=data_dir)
        result = migrator.migrate(data_dir)
        assert isinstance(result, MigrationResult)
        assert result.reason == REASON
        assert result.backup_path == ""
        assert _bytes(history) == before
        assert not (data_dir / "backups").exists()
        ipc = migrator.handle_run_migration({})
        assert ipc["reason"] == REASON

    def test_rollback_refuses_when_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        backup = data_dir / "backups" / "migration_backup_x"
        backup.mkdir(parents=True)
        (backup / "history.ndjson").write_text('{"id":"restored"}\n', encoding="utf-8")
        (data_dir / "history.ndjson").write_text('{"id":"current"}\n', encoding="utf-8")
        _write_settings(data_dir, {"history_encryption_enabled": True})
        before = _bytes(data_dir / "history.ndjson")
        migrator = DataMigrator(data_dir=data_dir)
        result = migrator.rollback_migration(data_dir, str(backup))
        assert result["ok"] is False and result["reason"] == REASON
        assert _bytes(data_dir / "history.ndjson") == before
        ipc = migrator.handle_rollback_migration(
            {"backup_path": str(backup), "confirm": True}
        )
        assert ipc["ok"] is False and ipc["reason"] == REASON

    def test_migrate_off_regression_still_migrates(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        (data_dir / "history.ndjson").write_text(
            json.dumps({"id": "1", "text": "v1"}) + "\n", encoding="utf-8"
        )
        migrator = DataMigrator(data_dir=data_dir)
        result = migrator.migrate(data_dir)
        assert result.items_migrated == 1
        assert Path(result.backup_path).is_dir()

    def test_noop_current_schema_creates_no_false_backup(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        item = {"id": "1", "text": "v2", "tags": [], "favorite": False}
        (data_dir / "history.ndjson").write_text(json.dumps(item) + "\n", encoding="utf-8")
        migrator = DataMigrator(data_dir=data_dir)
        result = migrator.migrate(data_dir)
        assert result.backup_path == ""
        assert not (data_dir / "backups").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52a_legacy_backup_gate.py -q -k "DataMigrator"`
Expected: FAIL — ON-профиль создаёт `migration_backup_*` и rewrite'ит history.

- [ ] **Step 3: Write minimal implementation**

`MigrationResult` добавить `reason: str | None = None` (default — безопасно для
существующих позиционных конструкций). `migrate`: после проверки target —
`if policy_blocks(data_dir_policy_reader(data_dir)): return MigrationResult(...,
backup_path="", reason=REASON)` до `_create_backup`. `rollback_migration`: guard
в начале → `{"ok": False, "reason": REASON, "restored_files": [],
"backup_path": str(backup_path)}`. `handle_run_migration`: добавить
`"reason": result.reason` в ответ.

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: PASS.

---

### Task 7: полный gate-прогон

- [ ] **Step 1: новый suite green**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52a_legacy_backup_gate.py -q`

- [ ] **Step 2: зависимые suites green**

Run:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_backup_restore.py KrabEar/tests/test_auto_backup.py KrabEar/tests/test_auto_backup_advanced.py KrabEar/tests/test_auto_backup_lock_w1768.py KrabEar/tests/test_auto_backup_perms_w21.py KrabEar/tests/test_archive_manager.py KrabEar/tests/test_transcript_versioning.py KrabEar/tests/test_transcript_versioning_w1045.py KrabEar/tests/test_transcript_versioning_w1563.py KrabEar/tests/test_data_migrator.py KrabEar/tests/test_purge_toctou_w25.py KrabEar/tests/test_history_encryption_failclosed_a5.py -q`

- [ ] **Step 3: ubuntu-parity + audit**

Run: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_a52a_legacy_backup_gate.py`
Run: `make audit-all`

---

## Implementation notes (уточнено в GREEN)

- `store_policy_reader` проверяет метод через `getattr(type(store), ...)`, а не
  через экземпляр: `MagicMock`-store иначе авто-создаёт
  `_read_encryption_flag_unlocked` и ложный ON ломал OFF-контроль
  `test_auto_backup*.py`. Store без класс-метода, но с `data_dir`, больше НЕ
  отключает гейты молча: fallback — `data_dir_policy_reader(data_dir)` (см.
  review-follow-up MINOR-1; фейковые store'ы приведены к валидному OFF `{}`).
- Byte-snapshot в `test_manual_backup_refuses_on_and_writes_nothing` исключает
  `*.lock`: `_is_privacy_mode()` через `store.load_settings()` (pre-existing
  поведение OFF-профиля) создаёт `history.lock`; это не plaintext-copy sink.
- Патч `store._read_encryption_flag_unlocked` через `patch.object` на реальном
  StateStore остаётся виден reader'у: класс-метод есть, instance-атрибут
  подменён → guard fail-closed.
- `AutoBackupManager._do_backup`: `mkdir` перенесён ПОД `_store_lock()`, чтобы
  «проверка до mkdir» и «snapshot под lock» выполнялись одним контрактом.

## Явно вне scope

- A5.2b (encrypted snapshot/manifest/recovery), A5.2c (inventory), A5.3 (export
  session capability). При ON backup/restore возвращают честный
  `history_encryption_operation_unavailable`, не частичный результат.
- `history_encryption_enabled` в прод-коде не включается.

## A5.2a review follow-up (BLOCK от 2026-09-26 закрыт)

Независимый adversarial-ревью вернул BLOCK. Закрыто новым коммитом (не amend).

**CRITICAL-1 — data_migrator TOCTOU.** `migrate`/`rollback_migration` решали
policy до flock, затем `_create_backup`/`copy2`/rewrite шли без re-check.
Фикс: `migrate` держит один `history_flock(data_dir)` на guard→re-check→
backup→rewrite (`_migrate_v1_to_v2(_lock_held=True)` не берёт nested flock);
`rollback_migration` — один `history_flock` с re-check после захвата.
RED: `TestDataMigratorGate::test_migrate_recheck_under_flock_blocks_off_to_on`,
`::test_rollback_recheck_under_flock_blocks_off_to_on` (патч
`backend.data_migrator.history_flock` флипает ON на входе; на HEAD код патч не
зовёт и plaintext-backup/rewrite выполняется → RED).

**CRITICAL-2 — transcript_versioning TOCTOU.** `save_version`/`revert_to_version`
append'или без общего lock. Фикс: re-check политики и append — под
`history_flock(self._data_dir)` (тот же файл, что `save_settings`).
`purge_orphaned_versions` flock НЕ берёт: он вызывается из
`StateStore._compact_unlocked` через `_on_compact_hook` ПОД store._lock на том
же треде — nested flock дал бы вечный дедлок (инвариант зафиксирован
комментарием в коде). RED:
`TestTranscriptVersionGate::test_save_version_recheck_under_lock_blocks_off_to_on`,
`::test_revert_recheck_under_lock_blocks_off_to_on`.

**MAJOR-1 — unarchive_items re-check не под store flock.** Фикс:
`with self._lock, store_lock_ctx:` (store `_lock()` для StateStore,
`nullcontext` для fake-store без `_lock`) + re-check policy после захвата.
RED: `TestArchiveManagerGate::test_unarchive_recheck_under_store_lock_blocks_off_to_on`.

**MINOR-1 — silent OFF fallback.** `store_policy_reader` без класс-метода теперь
падает на `data_dir_policy_reader(getattr(store, "data_dir", ...))`, а не
`lambda: False`. Фейковые store'ы `test_auto_backup*.py` /
`test_wave91_rmtree_races.py` приведены к валидному OFF `{}` (раньше держали
битый "dummy", что при новом контракте = fail-closed ON). RED:
`TestPolicyReaderFallback::test_proxy_store_uses_data_dir_on` (и контрольные
`_off` / `without_data_dir`).

**MINOR-3 (optional).** Отказ `migrate` больше не перечитывает
`get_schema_version` ради `from_version`: initial-refusal возвращает
`from_version="unknown"`; под-lock re-check использует уже вычисленный `current`.
`MigrationResult.reason` — machine-readable.

**MINOR-4 (optional).** `data_dir_policy_reader(data_dir, *, push_error=None)`
пробрасывает ErrorBus-колбэк в `read_history_encryption_flag` (проводка
caller-side — A5.2b).

**MINOR-5.** Добавлены re-check тесты для Archive/TranscriptVersion и тест
fallback `store_policy_reader` (см. выше). Тест «нет ложного success-log»
невозможен без правки service.py — остаётся долгом.

**MAJOR-Availability (второй review-раунд, PASS-WITH-NITS).** Фикс MAJOR-1
внёс Availability-регресс: `unarchive_items` держал глобальный `history.lock`
на весь цикл, включая semantic `index_item` → синхронный ML `_encode`
(`archive_manager.py:525`), тогда как `archive_items` для этого капнут
`_MAX_ARCHIVE_BATCH=100`, а `unarchive_items` не капился и читал до
`_MAX_ARCHIVE_LOAD=50k`. Большой unarchive блокировал записи/compaction истории
и мог валить concurrent IPC в `StateStoreLockTimeout` (30с).

Фикс (новый коммит, бонусы MAJOR-1 сохранены):
- `_MAX_UNARCHIVE_BATCH = 100`; `_validate_archive_ids(item_ids, *,
  max_batch=...)` обобщён, `unarchive_items` валидирует/капит ДО store-flock
  (вернуть `ok=False, reason=too_many_ids` на >100).
- `index_item` вынесен ИЗ-ПОД store-flock: под локом копим `to_index` успешно
  восстановленных записей, после отпускания лок отпускаем и индексируем, с
  re-check `_current_epoch()` — при конкурентном `clear_all` semantic index
  пропускается (иначе PII-метаданные воскресли бы в индексе).
- RED: `TestUnarchiveAvailability::test_unarchive_over_batch_rejected_before_store_lock`
  (на HEAD `_lock` входил, cap отсутствовал) и
  `::test_unarchive_indexes_after_store_lock_released` (spy фиксировал depth=1;
  после фикса depth=0). Контроль `::test_unarchive_at_batch_cap_still_enters_lock`.

**MINOR-2 — долг A5.2b (НЕ чинится здесь).** `service.py:1234` логирует
`data_migrator: migration complete ...` безусловно, даже когда `migrate`
вернул `reason=history_encryption_operation_unavailable` (смешанный v1.0 + ON).
`service.py` заморожен баном карточки. Код-видимый маркер:
`data_migrator.A5_2B_CALLER_SUCCESS_LOG_DEBT`. A5.2b обязан сделать startup-лог
честным (проверять `MigrationResult.reason`).

**Lock-контракт (честная формулировка).** Store-backed sink'и (manual backup/
restore, auto backup, archive) берут `StateStore._lock()`. Sink'и без
store-ссылки (migrate/rollback, save_version/revert_to_version) берут
module-level `history_flock(data_dir)` — тот же файл `data_dir/history.lock`,
reentrancy-safe внутри helper'а, bounded wait → `StateStoreLockTimeout`.
`history_flock` НЕ реентерабелен относительно внешнего `StateStore._lock` на
том же треде (nested flock на новом fd), поэтому вызывается только из sink'ов,
про которые доказано, что они не выполняются под store._lock; orphan-cleanup
хук сюда не входит.

