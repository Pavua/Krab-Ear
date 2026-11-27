"""Гонки выгрузки модели STT-споттера (ревью B18).

1) остановка «насовсем» посреди загрузки пропускала выгрузку (is_loaded() ещё False) — модель оставалась в памяти;
2) поток прогрева старого поколения обнулял свежезагруженную модель нового прогрева.
"""

from __future__ import annotations

import threading

import numpy as np

from backend import wake_word_stt_bridge as bridge
from backend.openwakeword_adapter import OpenWakeWordAdapter


class _Lock:
    def acquire(self, timeout=None):
        return True

    def release(self):
        pass


class _Inter:
    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _bridge(loader):
    return bridge.GigaAmMemoryTranscriber(
        loader=loader, infer=lambda m, t, a: "x", mlx_lock_fn=lambda: _Lock(), inter_lock_fn=_Inter(),
        release_cache=lambda: None, lock_wait_sec=0.05,
    )


def test_is_loading_is_true_only_while_the_model_is_being_loaded():
    gate, entered = threading.Event(), threading.Event()

    def slow(mode):
        entered.set()
        gate.wait(5.0)
        return ("m", "t")

    tr = _bridge(slow)
    assert not tr.is_loading()
    worker = threading.Thread(target=tr.warmup, daemon=True)
    worker.start()
    assert entered.wait(2.0)
    assert tr.is_loading() and not tr.is_loaded()
    gate.set()
    worker.join(3.0)
    assert not tr.is_loading() and tr.is_loaded()


def test_failed_load_does_not_leave_the_loading_flag_set():
    def boom(mode):
        raise RuntimeError("no weights")

    tr = _bridge(boom)
    tr.warmup()
    assert not tr.is_loading()


def test_final_stop_during_model_load_still_schedules_the_release(tmp_path):
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: d, read_privacy_revision=lambda: "r")
    released = threading.Event()

    class Loading:
        def is_loaded(self):
            return False  # загрузка идёт, ссылки на модель ещё нет

        def is_loading(self):
            return True

        def release(self):
            released.set()

    adapter._stt_transcribe = Loading()
    adapter._release_stt_model_async()
    assert released.wait(2.0), "выгрузка при идущей загрузке не пропускается"


def test_never_loaded_model_still_spawns_no_release_thread(tmp_path):
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: d, read_privacy_revision=lambda: "r")
    released = threading.Event()

    class Idle:
        def is_loaded(self):
            return False

        def is_loading(self):
            return False

        def release(self):
            released.set()

    adapter._stt_transcribe = Idle()
    adapter._release_stt_model_async()
    assert not released.wait(0.3)


def _interleave(tr, runner):
    """Вклинивает release() и прогрев нового поколения внутрь вызова runner'а — после загрузки, до проверки поколения."""
    real = tr._ensure_model
    state = {"done": False}

    def wrapped():
        result = real()
        if not state["done"]:
            state["done"] = True
            tr.release()                      # владелец выключил режим
            tr._loader = lambda mode: ("m2", "t2")
            tr.warmup()                       # и снова включил: новый прогрев загрузил СВОЮ модель
        return result

    tr._ensure_model = wrapped
    try:
        runner()
    finally:
        tr._ensure_model = real


def test_stale_warmup_does_not_wipe_the_model_a_newer_warmup_loaded():
    tr = _bridge(lambda mode: ("m1", "t1"))
    _interleave(tr, tr.warmup)
    assert tr.is_loaded() and tr._model == "m2"


def test_stale_utterance_does_not_wipe_the_model_a_newer_warmup_loaded():
    tr = _bridge(lambda mode: ("m1", "t1"))
    _interleave(tr, lambda: _expect_released(tr))
    assert tr.is_loaded() and tr._model == "m2"


def _expect_released(tr):
    try:
        tr(np.ones(100, dtype=np.int16))
    except bridge.TranscriberUnavailable as exc:
        assert str(exc) == "released"
    else:
        raise AssertionError("реплика старого поколения должна получить released")
