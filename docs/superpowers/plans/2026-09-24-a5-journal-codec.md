# A5.1: единый codec журналов истории — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть plaintext append/read/compaction обходы десяти журналов истории.

**Architecture:** Сохранить ENC1 и существующий StateStore. Один instance writer
применяет `_maybe_encrypt`, все managed readers используют decrypting helper;
compaction готовит результат до изменения активных файлов. Не вводить новый
storage engine и не объявлять multi-file rename атомарной транзакцией.

**Tech Stack:** Python 3.12/3.14, pytest, существующий AESGCM, POSIX flock.

**Spec:** `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`, §§1–4, 9.

**База:** `5ebfb8375392e45bd4d12da908cd8e7b47a642b0`,
`origin/codex/krab-ear-v2`; worktree `ear-a5-history-completion`.

## Global Constraints

- Source-only: `history_encryption_enabled` в production не менять.
- Только synthetic `tmp_path`/тестовый ключ; системный Keychain не вызывать.
- Не читать/копировать живую историю, не запускать запись/встречу/агент.
- Общий checkout, Main Krab и Voice Gateway не менять; `git add` явными путями.
- Python 3.12 CI, macOS Bash 3.2, без новых зависимостей.
- Все production writers уже вызываются под history lock; reader не должен
  превращать shared-lock чтение во вложенный exclusive-lock.
- Тяжёлые tests/build — после свежего resource snapshot; без слепых rerun.
- Для этой карточки Swift UI не меняется. A5.2/A5.3, live inventory и activation
  остаются отдельными шагами из спеки, не считаются выполненными этим PR.

## Review Focus

- ENC1 только в sidecar + потеря settings: запрет нового plaintext (Task 1).
- Прямой writer RecordingMerger: ENC1 на merged item и tombstones (Task 1).
- Повреждённая annotation/calendar: compaction не стирает их (Task 2).
- Crypto failure в подготовке rewrite: активные файлы неизменны (Task 2).
- Ошибка purged-ID durability: tombstones сохраняются, нет resurrection (Task 2).

## Task 1: writer, readers, counters и sidecar-only detection

**Files:**
- Modify: `KrabEar/backend/state_store.py`
- Create: `KrabEar/tests/test_history_journal_encryption_a5.py`
- Modify: `KrabEar/tests/test_state_store_w853_fsync_atomicity.py`

**Interfaces:**
- Consumes: `_maybe_encrypt(str) -> str`, `_maybe_decrypt(str) -> str`,
  `_read_history_ndjson_unlocked(Path) -> Iterator[dict]`, reentrant `_lock()`.
- Produces: `_history_journal_paths() -> tuple[Path, ...]`; instance
  `_append_ndjson(Path, dict) -> None`; instance
  `_count_ndjson_entries_unlocked(Path) -> int` for managed journals.

- [ ] **Step 1: добавить synthetic RED-тесты** в новый test-файл:

```python
import json
import os
from pathlib import Path

import pytest

from backend.history_crypto import HistoryCrypto
from backend.recording_merger import RecordingMerger
from backend.state_store import HistoryEncryptionUnavailable, StateStore

JOURNALS = (
    "history_path", "tombstones_path", "purged_ids_path", "status_path",
    "tags_path", "favorites_path", "annotations_path", "text_updates_path",
    "action_items_path", "calendar_links_path",
)


@pytest.fixture
def protected_store(tmp_path, monkeypatch):
    crypto = HistoryCrypto(b"S" * 32)
    monkeypatch.setattr("backend.history_crypto.build_history_crypto", lambda: crypto)
    (tmp_path / "settings.json").write_text(
        json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
    )
    return StateStore(tmp_path), crypto


@pytest.mark.parametrize("name", JOURNALS)
def test_all_journal_appends_are_encrypted(protected_store, name):
    store, crypto = protected_store
    target = getattr(store, name)
    payload = {"id": "synthetic-id", "text": "SYNTHETIC_A5_SECRET"}
    with store._lock():
        store._append_ndjson(target, payload)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].startswith("ENC1:")
    assert "SYNTHETIC_A5_SECRET" not in lines[0]
    assert json.loads(crypto.decrypt_line(lines[0])) == payload


def test_encrypted_overrides_are_read_and_counted(protected_store):
    store, _ = protected_store
    item = store.add_history_item(text="SYNTHETIC_ORIGINAL")
    assert store.update_history_item_text(item.id, "SYNTHETIC_EDIT", 0.75)
    assert store.update_history_item_tags(item.id, ["synthetic-tag"])
    assert store.update_history_item_favorite(item.id, True)
    assert store.set_paste_status(item.id, "ok")
    assert store.update_history_item_action_items(item.id, ["task"], ["decision"], ["question"])
    assert store.set_annotation(item.id, "SYNTHETIC_NOTE")
    assert store.update_history_item_calendar(item.id, {"title": "SYNTHETIC_EVENT"})
    loaded = store.get_history_item_by_id(item.id)
    assert loaded.text == "SYNTHETIC_EDIT"
    assert loaded.tags == ["synthetic-tag"] and loaded.favorite is True
    assert loaded.paste_status == "ok" and loaded.action_items == ["task"]
    assert loaded.decisions == ["decision"] and loaded.questions == ["question"]
    assert store.get_annotation(item.id) == "SYNTHETIC_NOTE"
    assert store.get_history_item_calendar(item.id) == {"title": "SYNTHETIC_EVENT"}
    stats = store.get_history_stats()
    assert stats["history_lines"] == 1 and stats["status_lines"] == 1


@pytest.mark.parametrize("name", JOURNALS[1:])
def test_sidecar_enc1_without_settings_cannot_write_plaintext(protected_store, monkeypatch, name):
    store, crypto = protected_store
    getattr(store, name).write_text(crypto.encrypt_line('{"id":"deleted"}') + "\n", encoding="utf-8")
    store.settings_path.unlink()
    monkeypatch.setattr("backend.history_crypto.build_history_crypto", lambda: None)
    fresh = StateStore(store.data_dir)
    with pytest.raises(HistoryEncryptionUnavailable):
        fresh.add_history_item(text="SYNTHETIC_AFTER_SETTINGS_LOSS")
    assert fresh.history_path.read_bytes() == b""


def test_recording_merger_cannot_bypass_encryption(protected_store):
    store, _ = protected_store
    first = store.add_history_item(text="SYNTHETIC_FIRST")
    second = store.add_history_item(text="SYNTHETIC_SECOND")
    merger = RecordingMerger()
    merger.cascade_delete_fn = lambda item_id, item_ts: None
    result = merger.merge_items([first.id, second.id], store, delete_originals=True)
    assert result["merged_from"] == [first.id, second.id]
    for path in (store.history_path, store.tombstones_path):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines and all(line.startswith("ENC1:") for line in lines)
```

- [ ] **Step 2: RED** — после проверки ресурсов запуск одного файла:

```bash
EAR_TEST_PY='/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python'
PYTHONPATH="$PWD/KrabEar" "$EAR_TEST_PY" scripts/run_isolated_pytest.py "$EAR_TEST_PY" -m pytest KrabEar/tests/test_history_journal_encryption_a5.py -q --timeout=30
```

Ожидается FAIL: generic append содержит JSON вместо ENC1; encrypted stats
возвращает 0; sidecar-only settings-loss допускает plaintext. Не принимать
ошибку импорта или обращения к реальному Keychain как правильный RED.

- [ ] **Step 3: единый реестр и codec**, точные тела методов:

```python
def _history_journal_paths(self) -> tuple[Path, ...]:
    return (
        self.history_path, self.tombstones_path, self.purged_ids_path,
        self.status_path, self.tags_path, self.favorites_path,
        self.annotations_path, self.text_updates_path, self.action_items_path,
        self.calendar_links_path,
    )

def _append_ndjson(self, path: Path, payload: dict[str, Any]) -> None:
    """Append managed journal; caller owns history lock."""
    line = self._maybe_encrypt(json.dumps(payload, ensure_ascii=False))
    self._append_ndjson_raw(path, line)

def _append_history_ndjson(self, payload: dict[str, Any]) -> None:
    self._append_ndjson(self.history_path, payload)

def _append_tombstone_ndjson(self, payload: dict[str, Any]) -> None:
    self._append_ndjson(self.tombstones_path, payload)

def _count_ndjson_entries_unlocked(self, path: Path) -> int:
    return sum(1 for _ in self._read_history_ndjson_unlocked(path))
```

Удалить `@staticmethod` только у двух изменённых instance-методов. В
`_has_encrypted_history_unlocked` заменить tuple двух путей на
`self._history_journal_paths()`. В override readers для text/action/tags/
favorite/annotation/purged/status/calendar заменить вызов
`self._read_ndjson_unlocked(path)` на `self._read_history_ndjson_unlocked(path)`.
Raw reader в `import_history_ndjson(source_path)` оставить для внешнего импорта.
Комментарий «purged IDs никогда не шифруется» заменить новым контрактом.

В существующем fsync-тесте заменить class-call
`StateStore._append_ndjson(target, payload)` на `store._append_ndjson(target, payload)`;
сохранить проверку ровно одного fsync на каждую запись.

- [ ] **Step 4: GREEN** — повторить точную команду RED; затем отдельными
  процессами проверить `test_history_encryption_failclosed_a5.py`,
  `test_state_store.py`, `test_state_store_w853_fsync_atomicity.py`.
- [ ] **Step 5: проверить caller-lock contract**:

```bash
rg -n '_append_ndjson\(' KrabEar/backend
git diff --check
git add KrabEar/backend/state_store.py KrabEar/tests/test_history_journal_encryption_a5.py KrabEar/tests/test_state_store_w853_fsync_atomicity.py
git commit -m 'fix: encrypt all managed history journal appends'
```

## Task 2: compaction без plaintext rewrite и потери deletion ledger

**Files:** Modify `KrabEar/backend/state_store.py` и новый test-файл Task 1.

**Interfaces:** потребляет общий codec; сохраняет публичный `compact()` и
`compact_with_stats()`, включая post-compact hooks. Не меняет формат ENC1.

- [ ] **Step 1: добавить RED-кейсы**:

```python
def journal_snapshot(store):
    return {name: getattr(store, name).read_bytes() for name in JOURNALS}


def test_compact_preserves_encrypted_notes_calendar_and_deletions(protected_store):
    store, _ = protected_store
    live = store.add_history_item(text="SYNTHETIC_LIVE")
    deleted = store.add_history_item(text="SYNTHETIC_DELETED")
    store.set_annotation(live.id, "SYNTHETIC_NOTE")
    store.update_history_item_calendar(live.id, {"title": "SYNTHETIC_EVENT"})
    store.delete_history_item(deleted.id)
    store.compact()
    assert store.get_annotation(live.id) == "SYNTHETIC_NOTE"
    assert store.get_history_item_calendar(live.id) == {"title": "SYNTHETIC_EVENT"}
    for name in JOURNALS:
        assert all(line.startswith("ENC1:") for line in getattr(store, name).read_text().splitlines())
    with store._lock():
        assert deleted.id in store._load_deleted_ids_unlocked()
    ledger = store.purged_ids_path.read_bytes()
    store.compact()
    assert store.purged_ids_path.read_bytes() == ledger


@pytest.mark.parametrize("name", ["annotations_path", "calendar_links_path", "purged_ids_path"])
def test_corrupt_sidecar_aborts_compact_without_live_changes(protected_store, name):
    store, _ = protected_store
    store.add_history_item(text="SYNTHETIC_LIVE")
    getattr(store, name).write_text("ENC1:broken\n", encoding="utf-8")
    before = journal_snapshot(store)
    with pytest.raises(HistoryEncryptionUnavailable):
        store.compact()
    assert journal_snapshot(store) == before


def test_crypto_failure_preparing_annotation_does_not_change_journals(protected_store, monkeypatch):
    store, crypto = protected_store
    item = store.add_history_item(text="SYNTHETIC_LIVE")
    store.set_annotation(item.id, "SYNTHETIC_NOTE")
    before = journal_snapshot(store)
    original = crypto.encrypt_line

    def fail_note(raw):
        if '"note"' in raw:
            raise RuntimeError("synthetic rewrite encryption failure")
        return original(raw)

    monkeypatch.setattr(crypto, "encrypt_line", fail_note)
    with pytest.raises(HistoryEncryptionUnavailable):
        store.compact()
    assert journal_snapshot(store) == before


def test_purged_id_write_failure_keeps_tombstones(protected_store, monkeypatch):
    store, _ = protected_store
    item = store.add_history_item(text="SYNTHETIC_DELETE")
    store.delete_history_item(item.id)
    before = journal_snapshot(store)
    original = store._append_ndjson_raw

    def fail_purged(path, line):
        if path == store.purged_ids_path:
            raise OSError("synthetic disk failure")
        return original(path, line)

    monkeypatch.setattr(store, "_append_ndjson_raw", fail_purged)
    with pytest.raises(OSError):
        store.compact()
    assert store.tombstones_path.read_bytes() == before["tombstones_path"]
    assert store.history_path.read_bytes() == before["history_path"]


def test_retry_fsyncs_existing_purged_id_before_clearing_tombstones(protected_store, monkeypatch):
    store, _ = protected_store
    item = store.add_history_item(text="SYNTHETIC_DELETE")
    store.delete_history_item(item.id)
    tombstones_before = store.tombstones_path.read_bytes()
    ledger_stat = store.purged_ids_path.stat()
    real_fsync = os.fsync
    real_replace = Path.replace
    fail_once = True
    events = []

    def ledger_fsync(fd):
        nonlocal fail_once
        current = os.fstat(fd)
        is_ledger = (current.st_dev, current.st_ino) == (ledger_stat.st_dev, ledger_stat.st_ino)
        if is_ledger and fail_once:
            fail_once = False
            raise OSError("synthetic failure after ledger write")
        result = real_fsync(fd)
        if is_ledger:
            events.append("ledger-durable")
        return result

    def traced_replace(source, target):
        if Path(target) == store.tombstones_path:
            events.append("tombstones-replaced")
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", ledger_fsync)
    monkeypatch.setattr(Path, "replace", traced_replace)
    with pytest.raises(OSError):
        store.compact()
    assert store.purged_ids_path.read_bytes()
    assert store.tombstones_path.read_bytes() == tombstones_before
    events.clear()
    store.compact()
    assert events.index("ledger-durable") < events.index("tombstones-replaced")
```

- [ ] **Step 2: RED** — тем же runner запуск нового test-файла с `-k compact`
  и отдельно `-k purged_id_write_failure`. Ожидается потеря annotations при
  plaintext parser, plaintext rewrite, либо подавленная ошибка purged IDs.
- [ ] **Step 3: перед изменением live history подготовить весь crypto output**:

```python
active = self._load_active_items_unlocked()
active_ids = {item.id for item in active}
deleted_ids = {
    str(payload.get("id", "")).strip()
    for payload in self._read_history_ndjson_unlocked(self.tombstones_path)
    if str(payload.get("id", "")).strip()
} - active_ids
already_purged = {
    str(payload.get("id", "")).strip()
    for payload in self._read_history_ndjson_unlocked(self.purged_ids_path)
}
history_lines = [self._maybe_encrypt(json.dumps(item.to_dict(), ensure_ascii=False)) for item in active]
survivors = {}
for path in (self.annotations_path, self.calendar_links_path):
    survivors[path] = [
        self._maybe_encrypt(json.dumps(payload, ensure_ascii=False)) + "\n"
        for payload in self._read_history_ndjson_unlocked(path)
        if str(payload.get("id", "")).strip() in active_ids
    ]
purged_lines = [
    self._maybe_encrypt(json.dumps({"id": item_id}))
    for item_id in sorted(deleted_ids - already_purged)
]
for line in purged_lines:
    self._append_ndjson_raw(self.purged_ids_path, line)
if deleted_ids:
    with self.purged_ids_path.open("r+b") as ledger_file:
        os.fsync(ledger_file.fileno())
```

Использовать `history_lines` в существующем fsync/tmp/replace блоке вместо
повторного encrypt. Существующий selective-rewrite блок получает
`survivors[path]`, не читает/шифрует после первой live-замены. Удалить прежний
best-effort append purged IDs с подавлением Exception и сохранить порядок
«durable purged IDs → replace history → clear deltas». Hooks и cache reset
используют те же prepared `active_ids/deleted_ids`. Не добавлять implicit purge
в другие каталоги. Ненулевой IO failure прекращает compaction, cleanup удаляет
только собственные временные файлы. Transaction/crash-recovery всего набора
остаётся A5.2; эта карточка не объявляет её выполненной.

Повторный fsync ledger обязателен даже при пустом `purged_lines`: предыдущая
попытка могла записать ID, но упасть на fsync. Сам факт наличия ID в файле
не доказывает его durability; tombstones до успешного fsync не очищаются.

- [ ] **Step 4: GREEN** — весь новый файл, затем отдельными процессами
  `test_state_store_w1715_integrity.py`, `test_state_store_w853_fsync_atomicity.py`,
  `test_state_store_toctou_W1479.py`, `test_recording_merger_wave1776.py`,
  `test_history_encryption_failclosed_a5.py`.
- [ ] **Step 5: commit**:

```bash
git diff --check
git add KrabEar/backend/state_store.py KrabEar/tests/test_history_journal_encryption_a5.py
git commit -m 'fix: preserve encrypted sidecars during history compaction'
```

## Финальная проверка этой карточки

- [ ] Прочитать весь diff; убедиться, что production-файлы/ключи не затронуты.
- [ ] В свободное окно `make audit-all` (StateStore service/persistence diff).
- [ ] Ubuntu parity для нового/изменённого test-файлов через существующий
  `scripts/pre_merge_py312_check.sh` только после read-only проверки `/tmp/py312`:
  скрипт умеет пересоздавать окружение, нельзя непреднамеренно удалить shared venv.
- [ ] Независимый whole-diff security review сильной моделью, закрыть findings.
- [ ] Fresh remote base; PR описывает A5.1 и открытые A5.2/A5.3, exact head CI.
- [ ] После разрешённого merge проверить exact post-merge CI; release и
  encryption activation не следуют автоматически из этой карточки.
- [ ] Обновить handoff: SHA, команды/результаты, оставшиеся границы.

Исполнение: последовательно в этой задаче; отдельный reviewer читает diff.
Не запускать свежего исполнителя на каждый маленький шаг ради формальности.
