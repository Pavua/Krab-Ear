"""E6 gate: red tests for findings confirmed against Krab Ear ea9931122.

Each test asserts the SAFE behaviour. "red" tests fail on ea9931122 and must
turn green after the fix; "killer" tests pass on ea9931122 and fail under the
named mutation. Synthetic data only, tmp_path, no network, no real settings.
Place under KrabEar/tests/ (uses _plaintext_export_test_helpers).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from _plaintext_export_test_helpers import history_service_with_off_policy
from backend.state_store import StateStore


# ----------------------------------------------------------------- helpers
def _history(tmp_path: Path, *, privacy: bool = False):
    store = StateStore(tmp_path / "data")
    svc = history_service_with_off_policy(store=store)
    if privacy:
        store.save_settings(
            {"history_encryption_enabled": False, "privacy_mode_enabled": True},
            validated_repair=True,
        )
    return store, svc


class _Settings:
    def __init__(self, payload):
        self.payload = payload

    def cached_settings(self, nowait: bool = False):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def _bare_core(privacy):
    from backend.recording_core_service import RecordingCoreService

    core = RecordingCoreService.__new__(RecordingCoreService)
    core._settings_svc = _Settings({"privacy_mode_enabled": privacy})
    core._preview_lock = threading.Lock()
    core._preview_text = "секретная фраза из превью"
    core._preview_duration_sec = 3.0
    return core


# ------------------------------------------- RED: deletion completeness (md)
def test_red_delete_erases_first_of_day_transcript_md(tmp_path: Path) -> None:
    """history_service.py:873-900 globs {date}-*{HHMMSS}*.md, but
    transcript_writer.py:168-176 names the day's first file {date}-Транскрибация.md."""
    from backend.transcript_writer import TranscriptWriter

    store, svc = _history(tmp_path)
    item = store.add_history_item(text="приватный текст", paste_status="ok")
    md = TranscriptWriter.write_transcript(
        {"text": "приватный текст", "ts": item.ts}, store.data_dir / "transcripts"
    )
    assert "-Транскрибация.md" in md.name  # date-only first file of the day
    assert svc.handle_delete_history_item({"id": item.id}) == {"deleted": True}
    assert not md.exists(), "plaintext transcript survived per-item delete"


def test_red_delete_erases_batch_import_transcript_md(tmp_path: Path) -> None:
    """recording_core_service.py:4008-4014 writes import transcripts as
    {YYYYMMDD_HHMMSS}_{stem}.md (default config: privacy OFF, encryption OFF);
    the delete glob never matches that format."""
    store, svc = _history(tmp_path)
    item = store.add_history_item(text="текст импортированного файла", paste_status="ok")
    tdir = store.data_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    md = tdir / f"{time.strftime('%Y%m%d_%H%M%S')}_meeting.md"
    md.write_text("# Транскрипт: meeting.m4a\n\nтекст импортированного файла\n", encoding="utf-8")
    svc.handle_delete_history_item({"id": item.id})
    assert not md.exists(), "batch-import transcript survived per-item delete"


# ----------------------------------------------- RED: privacy asymmetry, tags
def _tagged_item_then_privacy_on(tmp_path: Path):
    store, svc = _history(tmp_path)
    item = store.add_history_item(text="x", paste_status="ok")
    store.update_history_item_tags(item.id, ["medical", "family"])
    store.save_settings(
        {"history_encryption_enabled": False, "privacy_mode_enabled": True},
        validated_repair=True,
    )
    assert svc._is_privacy_mode() is True
    assert svc.handle_get_tags({"id": item.id})["tags"] == []  # sibling gate holds
    return svc, item


def test_red_add_tag_does_not_disclose_existing_tags_in_privacy(tmp_path: Path) -> None:
    svc, item = _tagged_item_then_privacy_on(tmp_path)
    out = svc.handle_add_tag({"id": item.id, "tag": "reviewed"})
    # ни одна из исходных меток записи не должна раскрываться (раньше проверялась только первая)
    assert {"medical", "family"}.isdisjoint(out.get("tags") or []), out


def test_red_remove_tag_does_not_disclose_existing_tags_in_privacy(tmp_path: Path) -> None:
    svc, item = _tagged_item_then_privacy_on(tmp_path)
    out = svc.handle_remove_tag({"id": item.id, "tag": "nonexistent"})
    assert {"medical", "family"}.isdisjoint(out.get("tags") or []), out


# ------------------------------- RED: privacy asymmetry, recording core
@pytest.mark.parametrize(
    "owner_active,owner_requested",
    [("dictation", "dictation"), ("meeting", "dictation")],  # repeat, conflict
)
def test_red_start_response_hides_preview_in_privacy(owner_active, owner_requested) -> None:
    """recording_core_service.py:585-645 vs the gate at 1772-1773."""
    core = _bare_core(True)
    gen = {"owner": owner_active, "token": "t", "revision": 0, "start_request_id": None}
    out = core._active_generation_start_response_locked(gen, owner_requested, None)
    assert out["preview_text"] == "", out


def test_red_transcribe_progress_hides_terminal_items_in_privacy() -> None:
    """recording_core_service.py:2061-2095: no privacy check, unlike the
    double-gated terminal replay at 736-773."""
    from backend.job_tracker import JobTracker

    core = _bare_core(True)
    core._job_tracker = JobTracker()
    job = core._job_tracker.create_job(total_files=1)
    core._job_tracker.update(
        job, status="done", current_file="/Users/x/секрет.m4a",
        items=[{"text": "полный текст транскрипта", "audio_duration_sec": 1.0}],
    )
    out = core.handle_get_transcribe_progress({"job_id": job})
    blob = repr(out)
    assert "полный текст транскрипта" not in blob, out


# ---------------- RED (S3, defence in depth): typed fail-closed privacy getters
def test_red_history_privacy_getter_fails_closed_on_non_dict_settings(tmp_path: Path) -> None:
    """history_service.py:521-523: AttributeError → False (privacy OFF)."""
    _, svc = _history(tmp_path)
    svc._settings_svc = _Settings(None)  # .get on None → AttributeError
    assert svc._is_privacy_mode() is True


def test_red_recording_privacy_getter_rejects_non_bool_flag() -> None:
    """recording_core_service.py:355: bool(None) → False."""
    core = _bare_core(None)
    assert core._privacy_mode_enabled() is True


# ------------------------------ KILLER: get_tags privacy gate (X-TAG)
def test_kill_xtag_get_tags_gated_in_privacy(tmp_path: Path) -> None:
    store, svc = _history(tmp_path)
    item = store.add_history_item(text="x", paste_status="ok")
    store.update_history_item_tags(item.id, ["medical"])
    store.save_settings(
        {"history_encryption_enabled": False, "privacy_mode_enabled": True},
        validated_repair=True,
    )
    assert svc.handle_get_tags({"id": item.id})["tags"] == []


# ------------------------------ KILLERS: spill ownership guards (M1, M2)
# NB: existing tests already kill M1/M2 (test_recording_owner_matrix.py,
# test_recording_generation.py); these are standalone duplicates.
def _collision(tmp_path: Path):
    from backend.recording_spill import RecordingSpillWriter

    rescue = tmp_path / "rescue"
    a = RecordingSpillWriter(rescue_dir=rescue, sample_rate=16000, channels=1,
                             source="dictation", session_id="abc123")
    assert a.open() is True
    b = RecordingSpillWriter(rescue_dir=rescue, sample_rate=16000, channels=1,
                             source="meeting", session_id="abc123")
    assert b.open() is False  # collision: B owns nothing
    return a, b


def test_kill_m1_failed_writer_cannot_rewrite_foreign_meta(tmp_path: Path) -> None:
    a, b = _collision(tmp_path)
    before = a._meta_path.read_text(encoding="utf-8")
    assert b.rewrite_source("meeting") is False
    assert a._meta_path.read_text(encoding="utf-8") == before
    a.close()


def test_kill_m2_failed_writer_cannot_discard_foreign_files(tmp_path: Path) -> None:
    a, b = _collision(tmp_path)
    b.discard()
    assert a.part_path.exists() and a._meta_path.exists()
    a.close()
