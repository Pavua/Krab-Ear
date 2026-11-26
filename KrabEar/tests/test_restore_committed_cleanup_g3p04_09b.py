"""G3-privacy-04 P04-09 (находки 4–5): уборка staging после COMMITTED и пути в mismatches.

4. Тихий ``rmtree(ignore_errors=True)`` оставлял маркер COMMITTED: recovery
   навсегда видел «незавершённый» restore без следа в ответе. Теперь сбой уборки
   виден в warnings (+ error-лог), а следующий проход повторяет уборку.
5. ``str(OSError)`` тянул абсолютный путь профиля в ``mismatches``/warnings;
   теперь только тип ошибки и errno.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import backend.encrypted_snapshot as es
from backend.state_store import HISTORY_JOURNAL_FILENAMES

from test_a52b2_snapshot_restore import _recover, _restore_markers  # noqa: E402
from test_restore_committed_marker_reverify_g3p04_09 import (  # noqa: E402
    _plant_committed_marker,
    _restored_profile,
)


def _rmtree_always_fails(monkeypatch, leftover):
    real = shutil.rmtree

    def boom(path, *a, **k):
        if Path(path) == leftover:
            raise PermissionError(13, "denied", str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(es.shutil, "rmtree", boom)


class TestCommittedCleanupPostcondition:
    def test_persistent_rmtree_failure_retires_marker_so_backups_not_blocked(
        self, tmp_path, monkeypatch
    ):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-stuck")
        _rmtree_always_fails(monkeypatch, leftover)

        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert result["reason"] == es.REASON_COMMITTED_CLEANUP_FAILED
        assert "committed_staging_cleanup_failed:PermissionError" in result["warnings"]
        assert str(data_dir) not in repr(result["warnings"])
        # Диск — источник истины: маркера нет, бэкапы/restore не блокируются.
        assert es.has_pending_restore(data_dir) is False
        assert result["pending"] is False
        assert leftover.exists(), "остаток каталога честно остаётся для purge"
        assert leftover in es.restore_staging_dirs(data_dir), "purge его видит без маркера"

    def test_marker_that_cannot_be_retired_reports_pending_true(self, tmp_path, monkeypatch):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-glued")
        _rmtree_always_fails(monkeypatch, leftover)

        def no_replace(*a, **k):
            raise PermissionError(13, "denied")

        monkeypatch.setattr(es.os, "replace", no_replace)
        monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))

        result = _recover(data_dir, crypto)

        assert es.has_pending_restore(data_dir) is True
        assert result["pending"] is True
        assert result["reason"] == es.REASON_COMMITTED_CLEANUP_FAILED

    def test_unverified_has_priority_over_cleanup_failed(self, tmp_path, monkeypatch):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        journal = data_dir / HISTORY_JOURNAL_FILENAMES[0]
        journal.write_bytes(journal.read_bytes() + b"x\n")
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-both")
        _rmtree_always_fails(monkeypatch, leftover)

        result = _recover(data_dir, crypto)

        assert result["reason"] == es.REASON_COMMITTED_UNVERIFIED
        assert any(w.startswith("committed_marker_mismatch:") for w in result["warnings"])
        assert any(w.startswith("committed_staging_cleanup_failed:") for w in result["warnings"])

    def test_next_recovery_after_retired_marker_is_clean(self, tmp_path, monkeypatch):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-retry")
        _rmtree_always_fails(monkeypatch, leftover)
        _recover(data_dir, crypto)
        monkeypatch.undo()

        second = _recover(data_dir, crypto)

        assert second["ok"] is True
        assert second["warnings"] == []
        assert second["pending"] is False

    def test_successful_cleanup_has_no_warning(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-ok")
        result = _recover(data_dir, crypto)
        assert result["warnings"] == []
        assert result["pending"] is False
        assert not leftover.exists()


class TestMismatchDoesNotLeakPath:
    def test_unreadable_journal_reports_errno_not_path(self, tmp_path, monkeypatch):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        target = data_dir / HISTORY_JOURNAL_FILENAMES[0]
        real = Path.read_bytes

        def deny(self):
            if self == target:
                raise PermissionError(13, "denied", str(self))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", deny)
        out = es._readback_live_journals(data_dir=data_dir, files_meta=files)

        assert out["ok"] is False
        text = repr(out["mismatches"])
        assert "PermissionError errno=13" in text
        assert str(data_dir) not in text


class TestRestorePathReflectsDisk:
    def test_restore_reports_cleanup_failure_and_retires_marker(self, tmp_path, monkeypatch):
        from backend.encrypted_snapshot import restore_encrypted_snapshot
        from test_a52b2_snapshot_restore import (
            _crypto, _data_dir, _fill_profile, _make_snapshot, _policy_on, _settings_on,
        )

        data_dir = _data_dir(tmp_path)
        crypto = _crypto()
        _fill_profile(data_dir, crypto)
        _settings_on(data_dir)
        snapshot_dir = _make_snapshot(data_dir, crypto)
        real = shutil.rmtree

        def boom(path, *a, **k):
            if Path(path).name.startswith(es.RESTORE_STAGING_PREFIX):
                raise PermissionError(13, "denied", str(path))
            return real(path, *a, **k)

        monkeypatch.setattr(es.shutil, "rmtree", boom)
        result = restore_encrypted_snapshot(
            data_dir=data_dir, backups_root=data_dir / "backups", snapshot_dir=snapshot_dir,
            crypto=crypto, policy_read=_policy_on(data_dir),
        )

        assert result["ok"] is True
        assert "committed_staging_cleanup_failed:PermissionError" in result["warnings"]
        assert result["pending"] is False
        assert es.has_pending_restore(data_dir) is False


class TestRetiredStagingDoesNotBlockRetention:
    """Находка Codex к #2292: каталог со снятым маркером не должен глушить retention."""

    def _stuck_retired(self, tmp_path, monkeypatch, txid):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, txid)
        _rmtree_always_fails(monkeypatch, leftover)
        _recover(data_dir, crypto)
        assert es._is_retired_staging(leftover)
        return data_dir, crypto, leftover

    def test_retention_runs_with_retired_staging_present(self, tmp_path, monkeypatch):
        data_dir, crypto, leftover = self._stuck_retired(tmp_path, monkeypatch, "tx-ret")
        result = es.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=5
        )
        assert result.get("skipped_reason") != es.REASON_RECOVERY_PENDING
        assert leftover.exists()

    def test_active_staging_without_marker_still_blocks_retention(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        active = data_dir / f"{es.RESTORE_STAGING_PREFIX}active"
        active.mkdir()
        assert not es._is_retired_staging(active)
        result = es.prune_snapshot_family(
            backups_root=data_dir / "backups", data_dir=data_dir, max_copies=5
        )
        assert result["skipped_reason"] == es.REASON_RECOVERY_PENDING

    def test_next_recovery_retries_removal_of_retired_staging(self, tmp_path, monkeypatch):
        data_dir, crypto, leftover = self._stuck_retired(tmp_path, monkeypatch, "tx-retry2")
        monkeypatch.undo()
        _recover(data_dir, crypto)
        assert not leftover.exists()


class TestProdEntryRetriesRetired:
    """REVISE пары: прод-вход recover_pending_restore_from_store обязан убирать retired."""

    def test_from_store_removes_retired_staging_without_any_marker(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-prod")
        _rmtree_always_fails(monkeypatch, leftover)
        _recover(data_dir, crypto)
        assert es._is_retired_staging(leftover) and es.has_pending_restore(data_dir) is False
        monkeypatch.undo()

        out = es.recover_pending_restore_from_store(SimpleNamespace(data_dir=data_dir))

        assert out is None, "маркера нет — вердикт не нужен"
        assert not leftover.exists()

    def test_unlink_fallback_still_marks_retired(self, tmp_path, monkeypatch):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-fb")
        _rmtree_always_fails(monkeypatch, leftover)

        def no_replace(*a, **k):
            raise PermissionError(13, "denied")

        monkeypatch.setattr(es.os, "replace", no_replace)
        _recover(data_dir, crypto)

        assert es.has_pending_restore(data_dir) is False
        assert es._is_retired_staging(leftover), "иначе каталог вечно глушит retention"
