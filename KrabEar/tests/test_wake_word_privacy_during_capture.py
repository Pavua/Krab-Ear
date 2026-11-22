"""Смена политики во время ожидания, чтения и распознавания wake word."""

import sys
from types import SimpleNamespace

import pytest

from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.state_store import StateStore


@pytest.mark.parametrize("blocked", [True, None, "roundtrip"], ids=["privacy-on", "unknown", "on-off"])
@pytest.mark.parametrize("phase", ["wait", "ready", "read", "unguarded-read", "flatten", "predict", "predict-empty", "record", "callback"])
def test_policy_change_closes_capture_at_next_boundary(tmp_path, monkeypatch, blocked, phase):
    calls = []
    published = []
    store = StateStore(tmp_path / "profile")
    store.initialize_startup_plaintext_policy(new_profile=True)
    writer = StateStore(store.data_dir)

    def change_policy(at):
        if phase != at:
            return
        if blocked is None:
            writer.settings_path.unlink(missing_ok=True)
            return
        writer.save_settings({"privacy_mode_enabled": True})
        if blocked == "roundtrip":
            writer.save_settings({"privacy_mode_enabled": False})

    class Audio:
        def flatten(self):
            calls.append("flatten")
            change_policy("flatten")
            return self

        def any(self):
            return True

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append("closed")

        @property
        def read_available(self):
            calls.append("poll")
            if phase == "wait" and calls.count("poll") <= 2:
                # Даже при записи и выключенном starvation timeout нужен выход.
                # Третий poll возвращает кадры, чтобы регрессия не подвесила тест.
                change_policy("wait")
                return 0
            change_policy("ready")
            return 8

        def read(self, frames):
            calls.append("read")
            if calls.count("read") > 1:
                adapter._stop_event.set()  # ограничивает старый цикл при регрессии
            change_policy("read")
            change_policy("unguarded-read")
            return Audio(), False

    settings = {
        "audio_read_poll_sec": 0.001,
        "audio_stream_starve_sec": 0,
        "audio_guarded_read_enabled": phase != "unguarded-read",
    }
    adapter = OpenWakeWordAdapter(
        store.data_dir, settings_get=settings.get, read_privacy_revision=store.read_wake_privacy_revision,
        is_recording=lambda: "poll" in calls and phase == "wait",
    )

    def predict(audio):
        calls.append("predict")
        if phase == "predict-empty":
            change_policy(phase)
            return {}
        change_policy("predict")
        return {"first": 0.9, "second": 0.8}

    def callback(name, score):
        calls.append(name)
        change_policy("callback")
        if phase == "callback":
            published.append(adapter.handle_wake_word_status({})["last_detection"])

    adapter._oww = SimpleNamespace(predict=predict)
    adapter._on_detected = callback
    record_detection = adapter._record_detection

    def record_and_check(name, score, **kwargs):
        recorded = record_detection(name, score, **kwargs)
        if recorded:
            calls.append("recorded")
            change_policy("record")
        return recorded

    monkeypatch.setattr(adapter, "_record_detection", record_and_check)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **kw: Stream()))
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    assert calls[-1] == "closed"
    if phase in {"wait", "predict-empty"}:
        assert calls.count("poll") == 1
    assert calls.count("read") == int(phase not in {"wait", "ready"})
    assert calls.count("flatten") == int(phase in {"flatten", "predict", "predict-empty", "record", "callback"})
    assert calls.count("predict") == int(phase in {"predict", "predict-empty", "record", "callback"})
    assert calls.count("recorded") == int(phase in {"record", "callback"})
    assert calls.count("first") == int(phase == "callback")
    assert all(hit is None for hit in published)
    assert "second" not in calls
    assert not adapter.heartbeat()["starvation_active"]


def test_wake_revision_matches_strict_policy_without_fingerprinting_each_poll(tmp_path, monkeypatch):
    from unittest.mock import Mock

    store = StateStore(tmp_path / "profile")
    store.initialize_startup_plaintext_policy(new_profile=True)
    expected = store.read_plaintext_policy_snapshot().internal_revision
    assert expected is not None
    fingerprint = Mock(side_effect=AssertionError("unexpected content hash"))
    monkeypatch.setattr("backend.state_store.hashlib.sha256", fingerprint)
    assert store.read_wake_privacy_revision() == expected
    assert store.read_wake_privacy_revision() == expected
    store.settings_path.write_text("{}", encoding="utf-8")
    assert store.read_wake_privacy_revision() is None
    fingerprint.assert_not_called()
