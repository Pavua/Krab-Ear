"""REST-фразы в едином журнале: ticket, purge, retry и конечная память."""
from unittest.mock import patch

import pytest
from backend.event_replay import EventReplayManager


def payload(text="повтор"):
    return {"text": text, "translation": "repeat", "start_ts": 0.0,
            "end_ts": 3.0, "language_detected": "ru"}


def cursor(m, old=None, limit=200):
    return m.handle_get_event_log({"cursor_mode": True, "cursor": old,
                                  "event_type": "live_subs.result", "limit": limit})


def test_completed_rest_phrase_enters_same_cursor_journal_once():
    m = EventReplayManager()
    old = cursor(m)["cursor"]
    ticket = m.begin_translation_capture()
    first = m.commit_translation_capture(ticket["ticket"], payload())
    duplicate = m.commit_translation_capture(ticket["ticket"], payload())
    assert m.commit_translation_capture(ticket["ticket"], payload("changed retry"))["status"] == "conflict"
    assert first["status"] == "accepted" and duplicate["status"] == "duplicate"
    assert first["seq"] == duplicate["seq"]
    events = cursor(m, old)["events"]
    assert [e["data"]["text"] for e in events] == ["повтор"]
    assert events[0]["data"]["event_id"] == ticket["ticket"]


def test_same_text_with_new_tickets_is_not_deduplicated():
    m = EventReplayManager()
    for _ in range(2):
        t = m.begin_translation_capture()
        assert m.commit_translation_capture(t["ticket"], payload())["status"] == "accepted"
    assert len(cursor(m)["events"]) == 2


def test_purge_and_restart_reject_late_completion_and_clear_ticket_metadata():
    m = EventReplayManager()
    t = m.begin_translation_capture()["ticket"]
    m.clear()
    assert m.commit_translation_capture(t, payload())["status"] == "stale"
    assert EventReplayManager().commit_translation_capture(t, payload())["status"] == "stale"
    assert cursor(m)["events"] == []


def test_expired_or_unknown_ticket_cannot_be_reintroduced_by_retry():
    m = EventReplayManager()
    with patch("backend.event_replay.time.monotonic", return_value=100):
        t = m.begin_translation_capture()["ticket"]
        assert m.commit_translation_capture(t, payload())["status"] == "accepted"
    with patch("backend.event_replay.time.monotonic", return_value=1000):
        assert m.commit_translation_capture(t, payload())["status"] == "stale"
        fresh = m.begin_translation_capture()["ticket"]
        assert fresh != t
        assert m.commit_translation_capture(t, payload())["status"] == "stale"
    assert len(cursor(m)["events"]) == 1


def test_ticket_memory_bounded_and_no_payload_copied_into_ticket_table():
    m = EventReplayManager()
    tickets = [m.begin_translation_capture() for _ in range(257)]
    assert sum(t["status"] == "ready" for t in tickets) == 256
    assert tickets[-1]["status"] == "busy"
    assert m.commit_translation_capture(tickets[0]["ticket"], payload())["status"] == "accepted"
    assert "повтор" not in repr(m._translation_tickets)


def test_privacy_and_failed_settings_refuse_both_begin_and_commit():
    settings = {"privacy_mode_enabled": False}
    m = EventReplayManager(settings_provider=lambda: settings)
    t = m.begin_translation_capture()["ticket"]
    settings["privacy_mode_enabled"] = True
    assert m.begin_translation_capture()["status"] == "privacy"
    assert m.commit_translation_capture(t, payload())["status"] == "privacy"
    def broken():
        raise OSError("synthetic unavailable")
    m._settings_provider = broken
    assert m.begin_translation_capture()["status"] == "privacy"


@pytest.mark.parametrize("bad", [
    {**payload(), "text": "x" * 16385}, {**payload(), "start_ts": float("nan")},
    {**payload(), "end_ts": True}, {**payload(), "text": 42},
    {**payload(), "unknown_field": "sensitive"},
])
def test_invalid_payload_is_rejected_without_consuming_or_logging_text(bad):
    m = EventReplayManager()
    t = m.begin_translation_capture()["ticket"]
    assert m.commit_translation_capture(t, bad)["status"] == "invalid"
    assert cursor(m)["events"] == []
    assert m.commit_translation_capture(t, payload())["status"] == "accepted"


def test_initial_snapshot_is_shared_tail_200_and_incremental_pages_preserve_mixed_order():
    m = EventReplayManager()
    old = cursor(m)["cursor"]
    for i in range(200):
        m.record_event("live_subs.result", payload(f"ipc{i}"))
        t = m.begin_translation_capture()["ticket"]
        m.commit_translation_capture(t, payload(f"rest{i}"))
    initial = cursor(m)
    assert len(initial["events"]) == 200 and not initial["has_more"]
    first = cursor(m, old)
    assert first["has_more"] and len(first["events"]) == 200
    second = cursor(m, first["cursor"])
    assert len(second["events"]) == 200 and not second["has_more"]
    assert [e["data"]["text"] for e in first["events"][:2]] == ["ipc0", "rest0"]


def test_expected_epoch_cannot_rebind_old_ws_audio_after_purge():
    m = EventReplayManager()
    old_epoch = m.translation_epoch()["epoch"]
    m.clear()
    assert m.begin_translation_capture(expected_epoch=old_epoch)["status"] == "stale"
    assert m.begin_translation_capture(expected_epoch=m.translation_epoch()["epoch"])["status"] == "ready"


def test_purge_between_privacy_check_and_ticket_append_cannot_resurrect_phrase():
    m = EventReplayManager()
    t = m.begin_translation_capture()["ticket"]
    def purge_then_allow():
        m.clear()
        return {}
    m._settings_provider = purge_then_allow
    assert m.commit_translation_capture(t, payload())["status"] == "stale"
    assert len(m._buffer) == 0


def test_huge_integer_time_is_invalid_not_handler_exception():
    m = EventReplayManager()
    t = m.begin_translation_capture()["ticket"]
    assert m.commit_translation_capture(t, {**payload(), "end_ts": 10**1000})["status"] == "invalid"


def test_cursor_byte_budget_advances_through_large_mixed_page():
    import json
    m = EventReplayManager()
    old = cursor(m)["cursor"]
    for i in range(100):
        m.record_event("live_subs.result", payload("i" * 12000))
        t = m.begin_translation_capture()["ticket"]
        m.commit_translation_capture(t, payload("r" * 12000))
    seen = []
    for _ in range(20):
        page = cursor(m, old)
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 512 * 1024
        seen.extend(page["events"])
        assert page["cursor"] != old
        old = page["cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == 200


def test_compaction_failure_cannot_make_accepted_ticket_retry_append_twice(tmp_path):
    m = EventReplayManager(persist_path=tmp_path / "events.ndjson", max_file_bytes=1)
    try:
        with patch("backend.event_replay.os.replace", side_effect=OSError("synthetic disk failure")):
            m.record_event("status", {})
        t = m.begin_translation_capture()["ticket"]
        first = m.commit_translation_capture(t, payload())
        second = m.commit_translation_capture(t, payload())
        assert first["status"] == "accepted" and second["status"] == "duplicate"
        assert first["seq"] == second["seq"]
        assert len(cursor(m)["events"]) == 1
    finally:
        m.close()


def test_ticket_is_consumed_before_fallible_persistence_completion(monkeypatch):
    m = EventReplayManager()
    t = m.begin_translation_capture()["ticket"]
    append = m._append_locked
    def append_then_fail(entry):
        append(entry)
        raise ValueError("synthetic persistence completion failure")
    monkeypatch.setattr(m, "_append_locked", append_then_fail)
    with pytest.raises(ValueError):
        m.commit_translation_capture(t, payload())
    monkeypatch.setattr(m, "_append_locked", append)
    assert m.commit_translation_capture(t, payload())["status"] == "duplicate"
    assert len(cursor(m)["events"]) == 1


def test_oversize_legacy_ipc_entry_reports_gap_and_advances_without_payload():
    m = EventReplayManager()
    old = cursor(m)["cursor"]
    m.record_event("live_subs.result", payload("sensitive" * 100000))
    m.record_event("live_subs.result", payload("next"))
    p = cursor(m, old)
    assert p["gap"] == "retention" and len(p["events"]) == 2
    assert p["events"][0]["data"] == {"redacted": True, "reason": "replay_size_limit"}
    assert p["events"][1]["data"]["text"] == "next"
    assert cursor(m, p["cursor"])["events"] == []
