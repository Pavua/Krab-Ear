"""A5: синтетическая проверка шифрования всех журналов StateStore."""

import json

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
