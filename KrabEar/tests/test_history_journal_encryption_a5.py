"""A5: синтетическая проверка шифрования всех журналов StateStore."""

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
