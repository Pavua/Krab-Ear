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


class TestCommittedCleanupPostcondition:
    def test_failed_cleanup_is_reported_not_silent(self, tmp_path, monkeypatch):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-stuck")
        real = shutil.rmtree

        def boom(path, *a, **k):
            if Path(path) == leftover:
                raise PermissionError(13, "denied", str(path))
            return real(path, *a, **k)

        monkeypatch.setattr(es.shutil, "rmtree", boom)
        result = _recover(data_dir, crypto)

        assert result["ok"] is True
        assert "committed_staging_cleanup_failed:PermissionError" in result["warnings"]
        assert leftover.exists()
        # Абсолютный путь профиля не утекает в ответ.
        assert str(data_dir) not in repr(result["warnings"])

    def test_next_recovery_retries_and_clears(self, tmp_path, monkeypatch):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-retry")
        real = shutil.rmtree
        calls = {"n": 0}

        def flaky(path, *a, **k):
            if Path(path) == leftover and calls["n"] == 0:
                calls["n"] += 1
                raise OSError(5, "io")
            return real(path, *a, **k)

        monkeypatch.setattr(es.shutil, "rmtree", flaky)
        first = _recover(data_dir, crypto)
        assert any(w.startswith("committed_staging_cleanup_failed:") for w in first["warnings"])
        second = _recover(data_dir, crypto)
        assert second["ok"] is True
        assert second["warnings"] == []
        assert not leftover.exists()
        assert _restore_markers(data_dir) == []

    def test_successful_cleanup_has_no_warning(self, tmp_path):
        data_dir, crypto, snapshot_dir, files = _restored_profile(tmp_path)
        leftover = _plant_committed_marker(data_dir, snapshot_dir, files, "tx-ok")
        result = _recover(data_dir, crypto)
        assert result["warnings"] == []
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
