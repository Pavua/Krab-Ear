"""Локальная координация snapshot/publish и нескольких очисток."""
from types import SimpleNamespace
import multiprocessing
from unittest.mock import patch

import pytest

from backend.plaintext_export_authorization import PlaintextExportDenied
from backend.timeline_purge_guard import guard_timeline_purge, timeline_guard


def _separate_purge(profile, result):
    gate = timeline_guard(SimpleNamespace(data_dir=profile))
    with gate.purge():
        pass
    result.put("complete")


def test_other_process_purge_invalidates_snapshot(tmp_path):
    store = SimpleNamespace(data_dir=tmp_path)
    gate = timeline_guard(store)
    with gate.snapshot() as generation:
        pass
    ctx = multiprocessing.get_context("spawn")
    result = ctx.Queue()
    child = ctx.Process(target=_separate_purge, args=(tmp_path, result))
    child.start()
    child.join(30)
    try:
        assert child.exitcode == 0
        assert result.get(timeout=2) == "complete"
        with pytest.raises(PlaintextExportDenied):
            with gate.publish(generation):
                pass
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
        result.close()


def test_same_profile_stores_share_guard_and_overlapping_purges(tmp_path):
    a = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    b_store = SimpleNamespace(data_dir=tmp_path / ".")
    b = timeline_guard(b_store)
    assert a is b
    with a.snapshot() as generation:
        pass
    with a.purge():
        pass
    with b.purge():
        pass
    with pytest.raises(PlaintextExportDenied):
        with b.publish(generation):
            pass
    with b.snapshot() as next_generation:
        pass
    with b.publish(next_generation):
        pass


def test_refusal_does_not_invalidate_and_exception_releases_purge(tmp_path):
    class Owner:
        store = SimpleNamespace(data_dir=tmp_path)

        @guard_timeline_purge
        def purge(self, params):
            if params.get("confirm"):
                raise RuntimeError("synthetic failure")
            return "refused"

    owner = Owner()
    gate = timeline_guard(owner.store)
    with gate.snapshot() as before:
        pass
    assert owner.purge({}) == "refused"
    with gate.publish(before):
        pass
    with pytest.raises(RuntimeError):
        owner.purge({"confirm": True})
    with pytest.raises(PlaintextExportDenied):
        with gate.publish(before):
            pass
    with pytest.raises(PlaintextExportDenied):
        with gate.snapshot():
            pass


def test_corrupt_marker_denies_old_publication_and_purge_recovers(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    with gate.snapshot() as before:
        pass
    (tmp_path / ".timeline-purge-guard").write_bytes(b"partial")
    with pytest.raises(PlaintextExportDenied):
        with gate.publish(before):
            pass
    with gate.purge():
        pass
    with pytest.raises(PlaintextExportDenied):
        with gate.publish(before):
            pass
    with gate.snapshot():
        pass


def test_corrupt_marker_recovers_without_purge_and_invalidates_old_snapshot(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    with gate.snapshot() as before:
        pass
    (tmp_path / ".timeline-purge-guard").write_bytes(b"")
    with gate.snapshot() as recovered:
        pass
    assert recovered != before
    with pytest.raises(PlaintextExportDenied):
        with gate.publish(before):
            pass
    with gate.publish(recovered):
        pass


def test_symlink_marker_never_touches_target(tmp_path):
    target = tmp_path / "synthetic-target"
    target.write_bytes(b"unchanged")
    (tmp_path / ".timeline-purge-guard").symlink_to(target)
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(OSError):
        with gate.snapshot():
            pass
    assert target.read_bytes() == b"unchanged"


def test_epoch_fsync_failure_prevents_purge_body(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    with gate.snapshot():
        pass
    entered = False
    with patch("backend.timeline_purge_guard.os.fsync", side_effect=OSError("synthetic")):
        with pytest.raises(OSError):
            with gate.purge():
                entered = True
    assert not entered


def test_partial_purge_blocks_snapshot_until_successful_retry(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    gate.run_purge(lambda: {"complete": False, "errors": ["synthetic"]})
    assert gate.recovery_pending()
    with pytest.raises(PlaintextExportDenied):
        with gate.snapshot():
            pass
    gate.run_purge(lambda: {"complete": True, "errors": []})
    assert not gate.recovery_pending()
    with gate.snapshot():
        pass


def test_nested_purge_does_not_commit_before_outer_cleanup(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))

    def outer():
        gate.run_purge(lambda: {"complete": True})
        assert gate.recovery_pending()
        raise RuntimeError("synthetic wrapper failure")

    with pytest.raises(RuntimeError):
        gate.run_purge(outer)
    assert gate.recovery_pending()


def test_pending_directory_fsync_failure_prevents_any_deletion(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    from unittest.mock import Mock
    wipe = Mock()
    with patch.object(gate, "_sync_directory", side_effect=OSError("synthetic")):
        with pytest.raises(OSError):
            gate.run_purge(wipe)
    wipe.assert_not_called()
    assert gate.recovery_pending()


def test_pending_symlink_fails_closed_without_touching_target(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    target = tmp_path / "target"
    target.write_text("synthetic")
    (tmp_path / ".purge-pending").symlink_to(target)
    with pytest.raises(OSError):
        gate.run_purge(lambda: {"complete": True})
    assert target.read_text() == "synthetic"
    assert gate.recovery_pending()


def test_backup_resume_stays_inside_purge_lock(tmp_path):
    import threading
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    entered = threading.Event()
    finished = threading.Event()

    def next_purge():
        entered.set()
        gate.run_purge(lambda: {"complete": True})
        finished.set()

    worker = threading.Thread(target=next_purge)

    def resume_backup():
        assert not gate.recovery_pending()
        worker.start()
        assert entered.wait(2)
        assert not finished.wait(0.1)

    try:
        gate.run_purge(lambda: {"complete": True}, after_complete=resume_backup)
        assert finished.wait(2)
    finally:
        if worker.ident is not None:
            worker.join(2)
    assert not worker.is_alive()
