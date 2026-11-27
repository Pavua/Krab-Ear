"""Оба движка wake word выключены на лету → микрофон освобождается, звук не копится в pre-roll (ревью B17)."""

import sys
from types import SimpleNamespace

import numpy as np

from backend import openwakeword_adapter as oa
from backend.openwakeword_adapter import OpenWakeWordAdapter

CHUNK = 1280


class _Done(BaseException):
    """Конец сценария теста; BaseException, чтобы `except Exception` цикла его не глотал."""


class _Stream:
    def __init__(self, on_read, max_reads=6):
        self._on_read, self.reads, self.closed, self._max = on_read, 0, False, max_reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def read(self, frames):
        self.reads += 1
        if self.reads > self._max:
            raise _Done  # страховка: цикл не вышел сам
        self._on_read(self.reads)
        return np.ones(frames, dtype=np.int16), False


def _run(adapter, stream, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **kw: stream))
    try:
        adapter._listen_loop(threshold=0.5, chunk_size=CHUNK, sample_rate=16000, generation=adapter._generation)
    except _Done:
        pass


def _adapter(tmp_path, cfg):
    full = {"audio_guarded_read_enabled": False}
    full.update(cfg)
    return OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: full.get(k, d)), full


def test_both_engines_switched_off_releases_microphone_and_stops_filling_ring(tmp_path, monkeypatch):
    adapter, cfg = _adapter(tmp_path, {})
    predicted, appended = [], []
    adapter._oww = SimpleNamespace(predict=lambda c: predicted.append(1) or {})
    real_append = oa.PreRollBuffer.append
    monkeypatch.setattr(oa.PreRollBuffer, "append",
                        lambda self, chunk: appended.append(1) or real_append(self, chunk))

    def on_read(n):
        if n == 3:  # на третьем чанке владелец выключил оба движка
            cfg["wake_word_oww_mode"] = "off"
            cfg["wake_word_stt_mode"] = "off"

    stream = _Stream(on_read)
    _run(adapter, stream, monkeypatch)

    assert stream.closed
    assert stream.reads == 3, "после off/off цикл не должен читать микрофон дальше"
    assert len(predicted) == 2, "чанк, прочитанный после выключения, не распознаётся"
    assert len(appended) == 2, "и не попадает в pre-roll-кольцо"


def test_one_engine_off_keeps_listening(tmp_path, monkeypatch):
    """Страж: выключен только openWakeWord, STT-споттер жив — цикл работает дальше, микрофон не отпускаем."""
    adapter, cfg = _adapter(tmp_path, {"wake_word_oww_mode": "off", "wake_word_stt_mode": "live"})
    calls = []

    class Stt:
        def predict(self, chunk):
            calls.append(1)
            return {"krab_stt": 0.0}

        def reset(self):
            pass

        def close(self, timeout=0.5):
            pass

        def stats(self):
            return {}

        def busy_for(self):
            return 0.0

        def last_unavailable_reason(self):
            return None

    adapter._oww = None
    adapter._stt = Stt()
    stream = _Stream(lambda n: None, max_reads=4)
    _run(adapter, stream, monkeypatch)

    assert len(calls) == 4, "STT live: все четыре чанка распознаются, выхода по off/off нет"


def test_both_off_from_the_start_never_appends_to_ring(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_oww_mode": "off", "wake_word_stt_mode": "off"})
    appended = []
    adapter._oww = SimpleNamespace(predict=lambda c: {})
    real_append = oa.PreRollBuffer.append
    monkeypatch.setattr(oa.PreRollBuffer, "append",
                        lambda self, chunk: appended.append(1) or real_append(self, chunk))
    stream = _Stream(lambda n: None)
    _run(adapter, stream, monkeypatch)
    assert stream.closed and stream.reads == 1 and appended == []
