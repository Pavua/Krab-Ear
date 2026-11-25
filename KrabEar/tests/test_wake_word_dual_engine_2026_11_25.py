"""Два движка wake word на одном потоке микрофона: openWakeWord и STT-споттер, режимы off/shadow/live."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from backend import openwakeword_adapter as oa
from backend.openwakeword_adapter import OpenWakeWordAdapter

CHUNK = 1280


class _Stream:
    def __init__(self, adapter, chunks, on_read=None):
        self._adapter, self._chunks, self._reads, self._on_read = adapter, chunks, 0, on_read

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, frames):
        self._reads += 1
        if self._on_read is not None:
            self._on_read(self._reads)
        if self._reads >= self._chunks:
            self._adapter._stop_event.set()
        return np.ones(frames, dtype=np.int16), False


class _FakeStt:
    """Дублёр SttKeywordModel: срабатывает на заданных номерах чанков."""

    def __init__(self, fire_on=()):
        self.fire_on, self.calls, self.resets, self.closed = set(fire_on), 0, 0, False

    def predict(self, chunk):
        self.calls += 1
        return {oa.STT_MODEL_NAME: 1.0 if self.calls in self.fire_on else 0.0}

    def reset(self):
        self.resets += 1

    def close(self, timeout=0.5):
        self.closed = True

    def stats(self):
        return {"utterances": 0}

    def busy_for(self):
        return 0.0


def _adapter(tmp_path, settings=None, stt_transcribe=None):
    cfg = {"audio_guarded_read_enabled": False}
    cfg.update(settings or {})
    return OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: cfg.get(k, d), stt_transcribe=stt_transcribe), cfg


def _run(adapter, monkeypatch, chunks=6, on_read=None):
    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(InputStream=lambda **kw: _Stream(adapter, chunks, on_read)))
    adapter._listen_loop(threshold=0.5, chunk_size=CHUNK, sample_rate=16000, generation=adapter._generation)


def _arm(adapter, oww=None, stt=None):
    adapter._oww = oww
    adapter._stt = stt
    callbacks = []
    adapter.published_models = []

    def on_detected(name, score):
        callbacks.append((name, score))
        # _last_detection сбрасывается при выходе цикла — снимок берём в момент срабатывания
        adapter.published_models.append((adapter._last_detection or {}).get("model"))

    adapter._on_detected = on_detected
    return callbacks


def _oww(score=0.9):
    return SimpleNamespace(predict=lambda chunk: {"krab_ru": score})


def test_defaults_keep_old_behaviour_only_oww_live(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path)
    assert adapter.engine_mode(oa.ENGINE_OWW) == "live"
    assert adapter.engine_mode(oa.ENGINE_STT) == "off"
    cb = _arm(adapter, oww=_oww())
    _run(adapter, monkeypatch, chunks=3)
    assert len(cb) == 3 and adapter._engine_stats[oa.ENGINE_OWW]["fires_live"] == 3


def test_shadow_counts_but_never_wakes_or_publishes(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_oww_mode": "shadow", "wake_word_stt_mode": "live"})
    stt = _FakeStt(fire_on={2})
    cb = _arm(adapter, oww=_oww(), stt=stt)
    _run(adapter, monkeypatch, chunks=4)
    stats = adapter._engine_stats
    assert stats[oa.ENGINE_OWW]["fires_shadow"] == 4 and stats[oa.ENGINE_OWW]["fires_live"] == 0
    assert len(stats[oa.ENGINE_OWW]["shadow_ts"]) == 4
    assert stats[oa.ENGINE_STT]["fires_live"] == 1
    # будит только живой STT, и в last_detection имя именно STT-движка
    assert cb == [(oa.STT_MODEL_NAME, 1.0)]
    assert adapter.published_models == [oa.STT_MODEL_NAME]


def test_shadow_never_calls_record_detection(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_oww_mode": "shadow"})
    cb = _arm(adapter, oww=_oww())
    seen = []
    monkeypatch.setattr(adapter, "_record_detection", lambda *a, **k: seen.append(a) or True)
    _run(adapter, monkeypatch, chunks=3)
    assert seen == [] and cb == [] and adapter._last_detection is None


def test_off_engine_is_not_even_asked(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_oww_mode": "off", "wake_word_stt_mode": "live"})
    asked = []
    oww = SimpleNamespace(predict=lambda c: asked.append(1) or {"krab_ru": 0.99})
    stt = _FakeStt()
    _arm(adapter, oww=oww, stt=stt)
    _run(adapter, monkeypatch, chunks=3)
    assert asked == [] and stt.calls == 3


def test_mode_switch_applies_without_restart_and_resets_stt_on_enable(tmp_path, monkeypatch):
    adapter, cfg = _adapter(tmp_path, {"wake_word_stt_mode": "off"})
    stt = _FakeStt(fire_on={3})
    cb = _arm(adapter, oww=None, stt=stt)
    adapter._oww = SimpleNamespace(predict=lambda c: {"krab_ru": 0.0})

    def on_read(n):
        if n == 3:
            cfg["wake_word_stt_mode"] = "live"  # владелец включил в панели посреди сессии

    _run(adapter, monkeypatch, chunks=6, on_read=on_read)
    assert stt.calls == 4            # чанки 3..6 (читаем режим ПОСЛЕ чтения)
    assert stt.resets == 1           # включение на лету сбрасывает сегментатор один раз
    assert [name for name, _ in cb] == [oa.STT_MODEL_NAME]


def test_garbage_or_failing_settings_fall_back_to_safe_defaults(tmp_path):
    adapter, cfg = _adapter(tmp_path, {"wake_word_oww_mode": "turbo", "wake_word_stt_mode": 5})
    assert adapter.engine_mode(oa.ENGINE_OWW) == "live" and adapter.engine_mode(oa.ENGINE_STT) == "off"

    def boom(key, default):
        raise RuntimeError("settings down")

    broken = OpenWakeWordAdapter(tmp_path, settings_get=boom)
    assert broken.engine_mode(oa.ENGINE_OWW) == "live" and broken.engine_mode(oa.ENGINE_STT) == "off"


def test_start_with_both_engines_off_is_refused(tmp_path):
    adapter, _ = _adapter(tmp_path, {"wake_word_oww_mode": "off", "wake_word_stt_mode": "off"},
                          stt_transcribe=lambda a: "")
    with pytest.raises(RuntimeError):
        adapter.start("hey_jarvis", lambda n, s: None)


def test_start_stt_only_does_not_need_openwakeword_library(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_oww_mode": "off", "wake_word_stt_mode": "live"},
                          stt_transcribe=lambda a: "")
    adapter._oww_available = False
    loaded = []
    monkeypatch.setattr(adapter, "_load_model", lambda *a, **k: loaded.append(1))
    monkeypatch.setattr(adapter, "_listen_loop", lambda **kw: None)
    adapter.start("hey_jarvis", lambda n, s: None)
    assert loaded == [] and adapter._oww is None and adapter._stt is not None
    adapter.stop()
    assert adapter._stt is None


def test_stt_engine_needs_transcriber_and_standard_chunk(tmp_path, monkeypatch):
    # без транскрибера режим stt трактуется как недоступный; с нестандартным чанком споттер не создаётся
    a1, _ = _adapter(tmp_path, {"wake_word_stt_mode": "live"})
    a1._oww_available = True  # на CI-раннере openwakeword не установлен
    monkeypatch.setattr(a1, "_load_model", lambda *a, **k: SimpleNamespace(predict=lambda c: {}))
    monkeypatch.setattr(a1, "_listen_loop", lambda **kw: None)
    a1.start("hey_jarvis", lambda n, s: None)
    assert a1._stt is None
    a1.stop()
    a2, _ = _adapter(tmp_path, {"wake_word_stt_mode": "live"}, stt_transcribe=lambda a: "")
    a2._oww_available = True
    monkeypatch.setattr(a2, "_load_model", lambda *a, **k: SimpleNamespace(predict=lambda c: {}))
    monkeypatch.setattr(a2, "_listen_loop", lambda **kw: None)
    a2.start("hey_jarvis", lambda n, s: None, chunk_size=1024)
    assert a2._stt is None
    a2.stop()


def test_stop_closes_stt_and_status_exposes_numbers_only(tmp_path):
    adapter, _ = _adapter(tmp_path, {"wake_word_stt_mode": "shadow"})
    stt = _FakeStt()
    adapter._stt = stt
    adapter._note_fire(oa.ENGINE_STT, "shadow")
    status = adapter.handle_wake_word_status({})
    engines = status["engines"]
    assert engines[oa.ENGINE_STT]["mode"] == "shadow" and engines[oa.ENGINE_STT]["fires_shadow"] == 1
    assert engines[oa.ENGINE_STT]["transcribing_for_sec"] == 0.0
    assert engines[oa.ENGINE_OWW]["mode"] == "live"
    flat = repr(status)
    assert "shadow_ts" not in flat  # метки времени отдаём отдельным отчётом, а не в status
    adapter.stop()
    assert stt.closed and adapter._stt is None


def test_stt_failure_in_predict_does_not_kill_the_oww_engine(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_stt_mode": "live"})

    class Boom(_FakeStt):
        def predict(self, chunk):
            raise RuntimeError("spotter broken")

    cb = _arm(adapter, oww=_oww(), stt=Boom())
    _run(adapter, monkeypatch, chunks=3)
    assert len(cb) == 3                                   # openWakeWord продолжает будить все 3 чанка
    assert adapter._consecutive_stream_failures == 0      # цикл не упал и микрофон не считается сломанным
    assert adapter.engine_report()[oa.ENGINE_STT]["predict_errors"] == 3
