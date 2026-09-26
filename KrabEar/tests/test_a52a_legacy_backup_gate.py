"""A5.2a — legacy backup/restore/archive/version/migration plaintext gates.

При включённом ``history_encryption_enabled`` (или неопределённой политике)
операции, создающие plaintext-копию управляемой истории, обязаны отклоняться
ДО mkdir/touch/copy/append/rewrite/prune, с машинно-читаемой причиной.
OFF-профиль сохраняет прежнее поведение.

Только synthetic tmp-профили; system Keychain не трогается.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.archive_manager import ArchiveManager
from backend.auto_backup import AutoBackupManager
from backend.data_migrator import DataMigrator, MigrationResult
from backend.history_encryption_policy import (
    OPERATION_UNAVAILABLE_REASON as REASON,
    HistoryEncryptionOperationUnavailable,
    store_policy_reader,
)
from backend.history_service import HistoryService
from backend.state_store import StateStore
from backend.transcript_versioning import TranscriptVersionManager


def _data_dir(tmp_path: Path) -> Path:
    base = tmp_path / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_settings(data_dir: Path, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (data_dir / "settings.json").write_text(text, encoding="utf-8")


def _bytes(path: Path):
    return path.read_bytes() if path.exists() else None


def _data_snapshot(data_dir: Path) -> dict:
    """Байты файлов данных, кроме служебных flock-файлов (*.lock)."""
    return {
        p.name: _bytes(p)
        for p in sorted(data_dir.iterdir())
        if p.is_file() and p.suffix != ".lock"
    }


def _enc1_line() -> str:
    return "ENC1:deadbeef\n"


def _no_keychain(*_a, **_k):
    raise AssertionError("policy guard must not touch the Keychain")


def _flipping_flock_patch(module_name: str, second_store: StateStore):
    """Патч ``backend.<module_name>.history_flock`` (create=True).

    На входе в lock флипает ``history_encryption_enabled=true`` через второй
    StateStore, затем берёт реальный ``history_flock`` (если он уже существует
    после фикса). На текущем HEAD код патч не вызывает → флип не происходит,
    и plaintext-sink выполняется: RED по правильной причине.
    """
    try:
        from backend.state_store import history_flock as real_flock
    except ImportError:  # HEAD до фикса
        real_flock = None

    @contextmanager
    def _flip(data_dir, *args, **kwargs):
        second_store.save_settings({"history_encryption_enabled": True})
        if real_flock is None:
            yield
        else:
            with real_flock(data_dir, *args, **kwargs):
                yield

    return patch(f"backend.{module_name}.history_flock", _flip, create=True)


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
        # A5.2b1: при ON auto-backup идёт в encrypted snapshot, которому нужен
        # ключ. Закрываем Keychain, чтобы тест оставался детерминированным и не
        # создавал реальный ключ: недоступный ключ ⇒ тот же честный отказ.
        with patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
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
        # A5.2b1: ключ недоступен ⇒ снимок невозможен ⇒ отказ до prune.
        with patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
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

        with patch.object(store, "_lock", flipping_lock), patch(
            "backend.history_crypto.build_history_crypto", side_effect=_no_keychain
        ):
            out = mgr.check_and_backup()
        assert out["backed_up"] is False
        assert out["skipped_reason"] == REASON
        backups = data_dir / "backups"
        assert not backups.exists() or not any(
            p.is_dir() and p.name.startswith("auto_backup_") for p in backups.iterdir()
        )


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

    def test_unarchive_recheck_under_store_lock_blocks_off_to_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        item = store.add_history_item(text="synthetic unarchive race")
        off_mgr = ArchiveManager(store=store)
        off_mgr.archive_items([item.id])
        before = _bytes(store.history_path)
        mgr = ArchiveManager(store=store)  # OFF на конструировании
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
            result = mgr.unarchive_items([item.id])
        assert result["ok"] is False and result["reason"] == REASON
        assert _bytes(store.history_path) == before


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

    def test_save_version_recheck_under_lock_blocks_off_to_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        mgr = TranscriptVersionManager(data_dir=data_dir)
        second = StateStore(data_dir)
        before = _bytes(data_dir / "transcript_versions.ndjson")
        with _flipping_flock_patch("transcript_versioning", second):
            with pytest.raises(HistoryEncryptionOperationUnavailable):
                mgr.save_version("item-1", "synthetic race")
        assert _bytes(data_dir / "transcript_versions.ndjson") == before

    def test_revert_recheck_under_lock_blocks_off_to_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        seed = TranscriptVersionManager(data_dir=data_dir)
        seed.save_version("item-1", "first", "stt_raw")
        mgr = TranscriptVersionManager(data_dir=data_dir)
        second = StateStore(data_dir)
        before = _bytes(data_dir / "transcript_versions.ndjson")
        with _flipping_flock_patch("transcript_versioning", second):
            with pytest.raises(HistoryEncryptionOperationUnavailable):
                mgr.revert_to_version("item-1", 1)
        assert _bytes(data_dir / "transcript_versions.ndjson") == before


class _ProxyStore:
    """Store без класс-метода `_read_encryption_flag_unlocked`, но с data_dir."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = str(data_dir)


class _LockCountingStore:
    """Fake-store со spy ``_lock``: считает входы, без реального flock."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = str(data_dir)
        self.lock_enters = 0

    @contextmanager
    def _lock(self, *args, **kwargs):
        self.lock_enters += 1
        yield

    def get_history_item_by_id(self, item_id):
        return None

    def delete_history_item(self, item_id):
        return False

    def add_history_item(self, text="", **kwargs):
        return None


class _SemanticSpy:
    """Spy semantic_searcher, фиксирующий глубину store-flock в момент вызова."""

    def __init__(self, lock_depth: dict) -> None:
        self._lock_depth = lock_depth
        self.calls: list[dict] = []

    def index_item(self, item_id, text):
        self.calls.append({"item_id": item_id, "depth": self._lock_depth["n"]})

    def remove_item(self, item_id):
        pass


class TestUnarchiveAvailability:
    """MAJOR (review): unarchive не держит глобальный history.lock на ML-путь."""

    def test_unarchive_over_batch_rejected_before_store_lock(self, tmp_path):
        from backend import archive_manager as _am

        data_dir = _data_dir(tmp_path)
        store = _LockCountingStore(data_dir)
        mgr = ArchiveManager(store=store)
        over = getattr(_am, "_MAX_UNARCHIVE_BATCH", 100) + 1
        result = mgr.unarchive_items([f"id-{i}" for i in range(over)])
        assert result.get("ok") is False
        assert result.get("reason") == "too_many_ids"
        assert store.lock_enters == 0

    def test_unarchive_at_batch_cap_still_enters_lock(self, tmp_path):
        from backend import archive_manager as _am

        data_dir = _data_dir(tmp_path)
        store = _LockCountingStore(data_dir)
        mgr = ArchiveManager(store=store)
        at_cap = getattr(_am, "_MAX_UNARCHIVE_BATCH", 100)
        result = mgr.unarchive_items([f"id-{i}" for i in range(at_cap)])
        assert result.get("reason") != "too_many_ids"
        assert store.lock_enters >= 1

    def test_unarchive_indexes_after_store_lock_released(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        store = StateStore(data_dir)
        lock_depth = {"n": 0}
        real_lock = store._lock

        @contextmanager
        def tracking_lock(*args, **kwargs):
            with real_lock(*args, **kwargs):
                lock_depth["n"] += 1
                try:
                    yield
                finally:
                    lock_depth["n"] -= 1

        spy = _SemanticSpy(lock_depth)
        mgr = ArchiveManager(store=store, semantic_searcher=spy)
        mgr._archive_path.write_text(
            '{"id":"seed","text":"semantic text"}\n', encoding="utf-8"
        )
        with patch.object(store, "_lock", tracking_lock):
            result = mgr.unarchive_items(["seed"])
        assert result["unarchived_count"] == 1
        assert spy.calls == [{"item_id": "seed", "depth": 0}]


class TestPolicyReaderFallback:
    def test_proxy_store_uses_data_dir_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        _write_settings(data_dir, {"history_encryption_enabled": True})
        assert store_policy_reader(_ProxyStore(data_dir))() is True

    def test_proxy_store_uses_data_dir_off(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        assert store_policy_reader(_ProxyStore(data_dir))() is False

    def test_store_without_data_dir_is_off(self):
        class _NoDir:
            pass

        assert store_policy_reader(_NoDir())() is False


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

    def test_migrate_recheck_under_flock_blocks_off_to_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        (data_dir / "history.ndjson").write_text(
            json.dumps({"id": "1", "text": "v1"}) + "\n", encoding="utf-8"
        )
        migrator = DataMigrator(data_dir=data_dir)
        second = StateStore(data_dir)
        before = _bytes(data_dir / "history.ndjson")
        with _flipping_flock_patch("data_migrator", second):
            result = migrator.migrate(data_dir)
        assert result.reason == REASON
        assert result.backup_path == ""
        assert _bytes(data_dir / "history.ndjson") == before
        assert not (data_dir / "backups").exists()

    def test_rollback_recheck_under_flock_blocks_off_to_on(self, tmp_path):
        data_dir = _data_dir(tmp_path)
        backup = data_dir / "backups" / "migration_backup_x"
        backup.mkdir(parents=True)
        (backup / "history.ndjson").write_text('{"id":"restored"}\n', encoding="utf-8")
        (data_dir / "history.ndjson").write_text('{"id":"current"}\n', encoding="utf-8")
        migrator = DataMigrator(data_dir=data_dir)
        second = StateStore(data_dir)
        before = _bytes(data_dir / "history.ndjson")
        with _flipping_flock_patch("data_migrator", second):
            result = migrator.rollback_migration(data_dir, str(backup))
        assert result["ok"] is False and result["reason"] == REASON
        assert _bytes(data_dir / "history.ndjson") == before
