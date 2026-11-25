"""Два движка wake word на одном потоке микрофона: openWakeWord и STT-споттер, режимы off/shadow/live."""

from __future__ import annotations

import sys
import threading
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

    def last_unavailable_reason(self):
        return None


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


def test_both_engines_live_in_one_chunk_wake_once_but_count_both(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_oww_mode": "live", "wake_word_stt_mode": "live"})
    cb = _arm(adapter, oww=_oww(), stt=_FakeStt(fire_on={1, 2}))
    _run(adapter, monkeypatch, chunks=2)
    assert len(cb) == 2                                   # по одному пробуждению на чанк, не по два
    stats = adapter._engine_stats
    assert stats[oa.ENGINE_OWW]["fires_live"] == 2 and stats[oa.ENGINE_STT]["fires_live"] == 2


class _Transcriber:
    def __init__(self):
        self.warmed, self.released = threading.Event(), 0

    def __call__(self, samples):
        return ""

    def warmup(self):
        self.warmed.set()

    def release(self):
        self.released += 1


def test_enabling_stt_warms_the_model_up_in_background_and_disabling_releases_it(tmp_path, monkeypatch):
    tr = _Transcriber()
    adapter, cfg = _adapter(tmp_path, {"wake_word_stt_mode": "live"}, stt_transcribe=tr)
    adapter._oww_available = True
    monkeypatch.setattr(adapter, "_load_model", lambda *a, **k: SimpleNamespace(predict=lambda c: {}))
    monkeypatch.setattr(adapter, "_listen_loop", lambda **kw: None)
    adapter.start("hey_jarvis", lambda n, s: None)
    assert tr.warmed.wait(2.0) and tr.released == 0
    adapter.stop()
    cfg["wake_word_stt_mode"] = "off"
    adapter.start("hey_jarvis", lambda n, s: None)
    assert tr.released == 1 and adapter._stt is None      # режим off: модель выгружена, споттер не создан
    adapter.stop()


def test_status_shows_unavailable_reason_instead_of_a_silent_zero(tmp_path):
    from backend.stt_keyword_spotter import SttKeywordModel, TranscriberUnavailable

    def busy(_samples):
        raise TranscriberUnavailable("mlx_busy")

    spotter = SttKeywordModel(busy, name=oa.STT_MODEL_NAME, cooldown_sec=0.0)
    adapter, _ = _adapter(tmp_path, {"wake_word_stt_mode": "live"})
    adapter._stt = spotter
    from backend.stt_keyword_spotter import FRAME_SAMPLES
    burst = [0] * (FRAME_SAMPLES * 10) + [3000, -3000] * (FRAME_SAMPLES * 3) + [0] * (FRAME_SAMPLES * 8)
    for i in range(0, len(burst) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        spotter.predict(burst[i:i + FRAME_SAMPLES])
        spotter.drain(2.0)
    engines = adapter.handle_wake_word_status({})["engines"]
    assert engines[oa.ENGINE_STT]["spotter"]["unavailable"] >= 1
    assert engines[oa.ENGINE_STT]["unavailable_reason"] == "mlx_busy"
    spotter.close()


def test_switching_stt_off_at_runtime_releases_model_in_background_and_on_warms_up_again(tmp_path, monkeypatch):
    tr = _Transcriber()
    released = threading.Event()
    tr.release = lambda: released.set()
    adapter, cfg = _adapter(tmp_path, {"wake_word_stt_mode": "live"}, stt_transcribe=tr)
    adapter._oww = SimpleNamespace(predict=lambda c: {"krab_ru": 0.0})
    adapter._stt = _FakeStt()

    def on_read(n):
        if n == 2:
            cfg["wake_word_stt_mode"] = "off"
        if n == 4:
            cfg["wake_word_stt_mode"] = "live"

    adapter._on_detected = lambda n, s: None
    _run(adapter, monkeypatch, chunks=6, on_read=on_read)
    assert released.wait(2.0)      # выключили на лету — модель выгружена без рестарта адаптера
    assert tr.warmed.wait(2.0)     # включили обратно — прогрев запущен снова


def test_pause_for_recording_keeps_the_model_but_final_stop_releases_it(tmp_path):
    tr = _Transcriber()
    released = threading.Event()
    tr.release = lambda: released.set()
    recording = {"on": True}
    cfg = {"wake_word_stt_mode": "live", "audio_guarded_read_enabled": False}
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: cfg.get(k, d), stt_transcribe=tr,
                                  is_recording=lambda: recording["on"])
    adapter.stop()                                  # пауза под диктовку
    assert not released.wait(0.3)
    recording["on"] = False
    adapter.stop()                                  # обычная остановка при включённом wake word
    assert not released.wait(0.3)
    cfg["wake_word_enabled"] = False
    adapter.stop()                                  # wake word выключен — остановка «насовсем»
    assert released.wait(2.0)


def test_stop_never_starts_a_thread_when_no_model_was_ever_loaded(tmp_path, monkeypatch):
    # close() backend идёт через stop(); граница rest_stageb_boundary запрещает неучтённые потоки на этом пути
    class Real(_Transcriber):
        def is_loaded(self):
            return False

    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    adapter, cfg = _adapter(tmp_path, {"wake_word_enabled": False}, stt_transcribe=Real())
    assert adapter.stop() is True
    assert not [name for name in started if name == "SttSpotterRelease"]


def test_stop_survives_thread_start_failure(tmp_path, monkeypatch):
    class Loaded(_Transcriber):
        def is_loaded(self):
            return True

    def boom(self):
        raise PermissionError("unreviewed thread start")

    monkeypatch.setattr(threading.Thread, "start", boom)
    adapter, _ = _adapter(tmp_path, {"wake_word_enabled": False}, stt_transcribe=Loaded())
    assert adapter.stop() is True          # исключение выгрузки не должно ломать остановку/закрытие


def test_shutdown_never_releases_the_model_even_on_final_stop(tmp_path, monkeypatch):
    class Loaded(_Transcriber):
        def is_loaded(self):
            return True

    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    adapter, _ = _adapter(tmp_path, {"wake_word_enabled": False}, stt_transcribe=Loaded())
    adapter.mark_shutdown()
    assert adapter.stop() is True
    assert "SttSpotterRelease" not in started


def test_service_close_marks_shutdown_before_stopping_the_adapter():
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("backend", "service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    close = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "close"
                 and "wake_word_adapter" in (ast.get_source_segment(src, n) or ""))
    calls = [n for n in ast.walk(close) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "mark_shutdown"]
    assert calls, "close() обязан ВЫЗВАТЬ mark_shutdown (а не только прочитать атрибут)"
    assert calls[0].lineno < next(n.lineno for n in ast.walk(close) if isinstance(n, ast.Call)
                                  and getattr(n.func, "attr", None) == "stop"
                                  and getattr(getattr(n.func, "value", None), "id", None) == "wake_word_adapter")
