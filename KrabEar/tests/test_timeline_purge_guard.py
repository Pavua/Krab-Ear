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
    with gate.snapshot():
        pass


def test_corrupt_marker_denies_export_but_confirmed_purge_recovers(tmp_path):
    gate = timeline_guard(SimpleNamespace(data_dir=tmp_path))
    with gate.snapshot() as before:
        pass
    (tmp_path / ".timeline-purge-guard").write_bytes(b"partial")
    with pytest.raises(PlaintextExportDenied):
        with gate.snapshot():
            pass
    with gate.purge():
        pass
    with pytest.raises(PlaintextExportDenied):
        with gate.publish(before):
            pass
    with gate.snapshot():
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
