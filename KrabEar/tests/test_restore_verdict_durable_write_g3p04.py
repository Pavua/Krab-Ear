"""G3-P04-05 / P04-07: fail-closed вердикт restore при сбое чтения диска и
полная запись файла в _write_file_durable."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend import encrypted_snapshot as es  # noqa: E402
from backend.health_check_service import restore_pending_status  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    es._RECOVERY_VERDICT_CACHE.clear()
    yield
    es._RECOVERY_VERDICT_CACHE.clear()


def _unreadable_data_dir(tmp_path: Path) -> Path:
    # iterdir() по файлу — NotADirectoryError (OSError): диск «не читается».
    path = tmp_path / "data_dir_is_a_file"
    path.write_text("x")
    return path


def test_disk_read_error_does_not_return_cached_all_clear(tmp_path):
    data_dir = _unreadable_data_dir(tmp_path)
    es._record_recovery_verdict(data_dir, {"ok": True, "pending": False, "rolled_forward": True})
    verdict = es.restore_verdict(data_dir)
    assert verdict is not None
    assert verdict["pending"] is True, "сбой чтения диска не может давать pending=False из кэша"
    assert verdict["ok"] is False
    assert verdict["reason"] == es.REASON_FSYNC_FAILED


def test_disk_read_error_without_cache_is_not_none(tmp_path):
    verdict = es.restore_verdict(_unreadable_data_dir(tmp_path))
    assert verdict is not None, "None означает «попыток не было», а мы просто не смогли проверить"
    assert verdict["pending"] is True


def test_health_status_is_consistent_on_disk_read_error(tmp_path):
    data_dir = _unreadable_data_dir(tmp_path)
    es._record_recovery_verdict(data_dir, {"ok": True, "pending": False})
    status = restore_pending_status(data_dir)
    assert status["restore_pending"] is True
    assert status["restore_recovery"]["pending"] is True


def test_readable_disk_without_marker_still_uses_cache(tmp_path):
    es._record_recovery_verdict(tmp_path, {"ok": True, "pending": False, "rolled_forward": True})
    verdict = es.restore_verdict(tmp_path)
    assert verdict["pending"] is False and verdict["rolled_forward"] is True


def test_write_file_durable_completes_short_writes(tmp_path):
    blob = bytes(range(256)) * 40
    real_write = es.os.write

    def short_write(fd, data):
        return real_write(fd, bytes(data[:7]))  # ядро вправе записать меньше

    target = tmp_path / "f.bin"
    with mock.patch.object(es.os, "write", side_effect=short_write):
        es._write_file_durable(target, blob)
    assert target.read_bytes() == blob


def test_write_file_durable_zero_progress_is_refused(tmp_path):
    with mock.patch.object(es.os, "write", return_value=0):
        with pytest.raises(es.SnapshotOperationRefused) as info:
            es._write_file_durable(tmp_path / "f.bin", b"payload")
    assert info.value.reason == es.REASON_FSYNC_FAILED
