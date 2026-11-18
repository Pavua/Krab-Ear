"""Смена политики во время ожидания, чтения и распознавания wake word."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.openwakeword_adapter import OpenWakeWordAdapter


@pytest.mark.parametrize("blocked", [True, None], ids=["privacy-on", "unknown"])
@pytest.mark.parametrize("phase", ["wait", "ready", "read", "unguarded-read", "flatten", "predict", "record", "callback"])
def test_policy_change_closes_capture_at_next_boundary(tmp_path, monkeypatch, blocked, phase):
    privacy = False
    calls = []

    def change_policy(at):
        nonlocal privacy
        if phase == at:
            privacy = blocked

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
            change_policy("read")
            change_policy("unguarded-read")
            return Audio(), False

    settings = {
        "audio_read_poll_sec": 0.001,
        "audio_stream_starve_sec": 0,
        "audio_guarded_read_enabled": phase != "unguarded-read",
    }
    adapter = OpenWakeWordAdapter(
        tmp_path, settings_get=settings.get, read_privacy=lambda: privacy,
        is_recording=lambda: "poll" in calls and phase == "wait",
    )

    def predict(audio):
        calls.append("predict")
        change_policy("predict")
        return {"first": 0.9, "second": 0.8}

    def callback(name, score):
        calls.append(name)
        change_policy("callback")

    adapter._oww = SimpleNamespace(predict=predict)
    adapter._on_detected = callback
    record_detection = adapter._record_detection

    def record_and_check(name, score):
        record_detection(name, score)
        change_policy("record")

    record = MagicMock(side_effect=record_and_check)
    monkeypatch.setattr(adapter, "_record_detection", record)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **kw: Stream()))
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    assert calls[-1] == "closed"
    if phase == "wait":
        assert calls.count("poll") == 1
    assert calls.count("read") == int(phase not in {"wait", "ready"})
    assert calls.count("flatten") == int(phase in {"flatten", "predict", "record", "callback"})
    assert calls.count("predict") == int(phase in {"predict", "record", "callback"})
    assert record.call_count == int(phase in {"record", "callback"})
    assert calls.count("first") == int(phase == "callback")
    assert "second" not in calls
    assert not adapter.heartbeat()["starvation_active"]
