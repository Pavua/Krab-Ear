"""Курсор восстановления фраз: настоящий bounded EventReplayManager."""

from backend.event_replay import EventReplayManager


def page(manager, cursor=None, limit=2):
    return manager.handle_get_event_log(
        {
            "cursor_mode": True,
            "cursor": cursor,
            "event_type": "live_subs.result",
            "limit": limit,
        }
    )


def emit(manager, text="повтор", kind="live_subs.result"):
    manager.record_event(
        kind, {"text": text, "translation": text}, ts="2026-10-23T01:00:00+00:00"
    )


def test_cursor_retains_identical_phrases_at_identical_timestamp():
    m = EventReplayManager()
    baseline = page(m)["cursor"]
    emit(m)
    emit(m)
    first = page(m, baseline, limit=1)
    assert len(first["events"]) == 1 and first["has_more"]
    second = page(m, first["cursor"], limit=1)
    assert len(second["events"]) == 1 and not second["has_more"]
    assert first["events"][0]["seq"] < second["events"][0]["seq"]
    assert page(m, second["cursor"])["events"] == []


def test_filtered_pages_advance_without_skipping_next_matching_event():
    m = EventReplayManager()
    baseline = page(m)["cursor"]
    emit(m, kind="status")
    emit(m, "one")
    emit(m, kind="status")
    emit(m, "two")
    p = page(m, baseline, limit=1)
    assert [e["data"]["text"] for e in p["events"]] == ["one"]
    assert p["has_more"]
    p2 = page(m, p["cursor"], limit=1)
    assert [e["data"]["text"] for e in p2["events"]] == ["two"]
    assert not p2["has_more"]
    emit(m, kind="status")
    empty = page(m, p2["cursor"])
    assert empty["events"] == [] and empty["cursor"] != p2["cursor"]


def test_first_open_returns_latest_bounded_phrases():
    m = EventReplayManager()
    for i in range(5):
        emit(m, str(i))
    p = page(m)
    assert [e["data"]["text"] for e in p["events"]] == ["3", "4"]
    assert not p["has_more"] and not p["gap"]


def test_retention_gap_is_explicit_and_available_pages_are_ordered():
    m = EventReplayManager(max_buffer=3)
    old = page(m)["cursor"]
    for i in range(5):
        emit(m, str(i))
    p = page(m, old)
    assert p["gap"] == "retention"
    assert [e["data"]["text"] for e in p["events"]] == ["2", "3"]
    p2 = page(m, p["cursor"])
    assert [e["data"]["text"] for e in p2["events"]] == ["4"]
    assert not p2["gap"]


def test_restart_and_purge_invalidate_cursor_without_hiding_new_phrases():
    m = EventReplayManager()
    emit(m)
    old = page(m)["cursor"]
    m.clear()
    emit(m, "after purge")
    p = page(m, old)
    assert p["gap"] == "reset" and p["events"][0]["data"]["text"] == "after purge"
    restarted = EventReplayManager()
    emit(restarted, "after restart")
    p2 = page(restarted, p["cursor"])
    assert p2["gap"] == "reset" and p2["events"][0]["data"]["text"] == "after restart"


def test_privacy_read_gate_and_failed_settings_are_fail_closed():
    settings = {"privacy_mode_enabled": False}
    m = EventReplayManager(settings_provider=lambda: settings)
    emit(m, "private")
    settings["privacy_mode_enabled"] = True
    assert page(m)["events"][0]["data"].get("redacted") is True

    def fail():
        raise OSError("settings unavailable")

    m._settings_provider = fail
    assert page(m)["events"][0]["data"].get("redacted") is True


def test_malformed_and_future_cursor_are_rejected_without_payload():
    m = EventReplayManager()
    emit(m, "secret")
    valid = page(m)["cursor"]
    for cursor in [
        "",
        "bad",
        "a:1",
        valid.split(":")[0] + ":9999999999999999999999",
        5,
        {},
        True,
    ]:
        result = page(m, cursor)
        assert result.get("ok") is False
        assert "events" not in result


def test_legacy_log_response_is_unchanged():
    m = EventReplayManager()
    emit(m)
    result = m.handle_get_event_log({"event_type": "live_subs.result"})
    assert set(result) == {"events", "count"}


def test_event_is_recorded_before_sse_subscriber_can_observe_hint():
    from backend.event_bus import EventBus

    m = EventReplayManager()
    bus = EventBus()
    bus._event_replay = m
    observed = []

    class Subscriber:
        def put_nowait(self, event):
            observed.append(page(m)["events"])

    subscriber = Subscriber()
    bus._subscribers.append(subscriber)
    bus.emit("live_subs.result", {"text": "ready"})
    assert observed and observed[0][0]["data"]["text"] == "ready"


def test_purge_during_privacy_check_cannot_return_pre_purge_snapshot():
    m = EventReplayManager()
    emit(m, "purged")

    def purge_then_allow():
        m.clear()
        return {"privacy_mode_enabled": False}

    m._settings_provider = purge_then_allow
    result = page(m)
    assert result.get("ok") is False
    assert "events" not in result


def test_dispatch_cursor_budget_is_bounded_and_separate_from_legacy_debug_reads():
    from backend.service import BackendService
    from backend.ipc_throttle import IPCThrottle

    # Только dispatcher: __init__ не вызывается, workers/хранилища не создаются.
    service = BackendService.__new__(BackendService)
    from types import SimpleNamespace

    service._settings_svc = SimpleNamespace(cached_settings=lambda: {})
    service._request_signer = None
    service._ipc_throttle = IPCThrottle(limits={"medium": 1, "light": 2})
    service._audit_logger = None
    manager = EventReplayManager()
    service._dispatch_table = {"get_event_log": manager.handle_get_event_log}

    def call(params):
        return service.handle_request(
            {"id": "synthetic", "method": "get_event_log", "params": params}
        )

    assert call({})["ok"] is True  # legacy medium budget исчерпан
    request = {"cursor_mode": True, "event_type": "live_subs.result", "limit": 200}
    assert call(request)["ok"] is True
    assert call(request)["ok"] is True
    assert call(request)["error"]["code"] == "rate_limit_exceeded"
    assert call({})["error"]["code"] == "rate_limit_exceeded"
    assert (
        call({"cursor_mode": True, "event_type": "stt.final"})["error"]["code"]
        == "rate_limit_exceeded"
    )


def test_cursor_snapshot_distinguishes_clear_from_new_backend_and_reports_privacy():
    settings = {"privacy_mode_enabled": False}
    m = EventReplayManager(settings_provider=lambda: settings)
    emit(m, "before purge")
    old = page(m)["cursor"]
    m.clear()
    cleared = page(m, old)
    assert cleared["gap"] == "reset" and cleared["reset_reason"] == "clear"
    restarted = page(EventReplayManager(), old)
    assert restarted["reset_reason"] == "restart"
    settings["privacy_mode_enabled"] = True
    assert page(m, cleared["cursor"])["privacy_mode"] is True


def test_purge_generation_survives_backend_restart_without_persisting_phrases(tmp_path):
    path = tmp_path / "events.ndjson"
    first = EventReplayManager(persist_path=path)
    emit(first, "before purge")
    before = page(first)
    first.close()
    second = EventReplayManager(persist_path=path)
    assert page(second)["generation"] == before["generation"]
    second.clear()
    after = page(second)
    assert after["generation"] != before["generation"]
    second.close()
    third = EventReplayManager(persist_path=path)
    try:
        assert page(third)["generation"] == after["generation"]
        assert third.get_events() == []
        marker = path.with_suffix(".generation")
        assert marker.read_text().strip() == after["generation"]
        assert marker.stat().st_size <= 33
        assert "before purge" not in marker.read_text()
    finally:
        third.close()


def test_generation_write_failure_reports_error_but_still_wipes_memory(
    tmp_path, monkeypatch
):
    import pytest
    import backend.event_replay as module

    m = EventReplayManager(persist_path=tmp_path / "events.ndjson")
    emit(m, "must disappear")

    def fail(*args, **kwargs):
        raise OSError("synthetic metadata write failure")

    monkeypatch.setattr(module, "atomic_write_text", fail)
    try:
        with pytest.raises(OSError):
            m.clear()
        assert m.get_events() == []
        assert page(m)["generation"] is None
    finally:
        m.close()
