"""id срабатывания связывает status.last_detection и wake_word_take_preroll (ревью B2).

Раньше take игнорировал params и отдавал звук ТЕКУЩЕГО слота: агент, увидевший срабатывание №1, мог получить аудио
срабатывания №2 (слот вытеснен). Теперь агент может передать ``detection_id``; чужой слот не отдаётся и не расходуется.
Вызов без ``detection_id`` ведёт себя как раньше (совместимость со старым агентом).
"""

import sys
import time
from types import SimpleNamespace

import numpy as np

from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.wake_word_preroll import PreRollBuffer

RATE = 16000
CHUNK = 1280


class _Done(BaseException):
    """Конец сценария теста; BaseException, чтобы `except Exception` цикла его не глотал."""


class _Stream:
    def __init__(self, adapter, chunks, on_read):
        self._adapter, self._chunks, self._on_read, self._reads = adapter, chunks, on_read, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, frames):
        self._reads += 1
        if self._reads > self._chunks:
            self._adapter._stop_event.set()
            raise _Done
        self._on_read(self._reads)
        return np.ones(frames, dtype=np.int16), False


def _adapter(tmp_path):
    cfg = {"audio_guarded_read_enabled": False}
    return OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: cfg.get(k, d),
                               read_privacy_revision=lambda: "rev1")


def _run(adapter, monkeypatch, chunks, on_read):
    adapter._oww = SimpleNamespace(predict=lambda c: {"hey": 0.9})  # срабатывает на каждом чанке
    adapter._on_detected = lambda name, score: None
    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(InputStream=lambda **kw: _Stream(adapter, chunks, on_read)))
    try:
        adapter._listen_loop(threshold=0.5, chunk_size=CHUNK, sample_rate=RATE, generation=adapter._generation)
    except _Done:
        pass


def _current_id(adapter):
    return adapter.handle_wake_word_status({})["last_detection"]["id"]


def test_status_last_detection_carries_a_growing_id(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    ids = []
    _run(adapter, monkeypatch, chunks=4, on_read=lambda n: ids.append(_current_id(adapter)) if n > 1 else None)
    assert all(isinstance(i, int) for i in ids)
    assert ids == sorted(set(ids)) and len(ids) == 3, "id растёт с каждым срабатыванием"


def test_take_with_stale_detection_id_is_superseded_and_keeps_the_current_slot(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    seen = {}

    def on_read(n):
        if n == 3:  # срабатывания на чанках 1 и 2 уже были: слот принадлежит второму
            stale, current = _current_id(adapter) - 1, _current_id(adapter)
            seen["stale"] = adapter.handle_wake_word_take_preroll({"detection_id": stale})
            seen["current"] = adapter.handle_wake_word_take_preroll({"detection_id": current})
            seen["again"] = adapter.handle_wake_word_take_preroll({"detection_id": current})

    _run(adapter, monkeypatch, chunks=4, on_read=on_read)
    assert seen["stale"]["available"] is False and seen["stale"]["reason"] == "superseded"
    assert "pcm_b64" not in seen["stale"]
    assert seen["current"]["available"] is True and seen["current"]["samples"] > 0, "слот не расходован чужим запросом"
    assert seen["again"]["available"] is False and seen["again"]["reason"] == "no_detection", "одноразово"


def test_take_without_detection_id_keeps_the_old_contract(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    seen = {}

    def on_read(n):
        if n == 3:
            seen["out"] = adapter.handle_wake_word_take_preroll({})

    _run(adapter, monkeypatch, chunks=4, on_read=on_read)
    assert seen["out"]["available"] is True and seen["out"]["samples"] > 0


def test_unknown_or_malformed_detection_id_never_hands_out_audio(tmp_path):
    adapter = _adapter(tmp_path)
    ring = PreRollBuffer(RATE * 8)
    ring.append(np.full(CHUNK, 3, dtype=np.int16))
    adapter._preroll_fire = {"ring": ring, "ts": time.monotonic(), "rev": "rev1", "anchor": 0, "rate": RATE, "id": 5}
    for bad in (4, 6, "5", "x", -1, 0):
        out = adapter.handle_wake_word_take_preroll({"detection_id": bad})
        assert out["available"] is False and out["reason"] == "superseded", bad
        assert adapter._preroll_fire is not None, "чужой запрос слот не расходует"
    ok = adapter.handle_wake_word_take_preroll({"detection_id": 5})
    assert ok["available"] is True
