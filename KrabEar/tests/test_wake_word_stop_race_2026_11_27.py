"""stop() между детекцией и публикацией: после остановки срабатывание не вооружается и не публикуется (B4).

stop() отзывает pre-roll и последнюю детекцию синхронно, но поток прослушивания, уже ждущий на том же локе,
раньше успевал вооружить pre-roll и опубликовать детекцию ПОСЛЕ остановки. Тесты воспроизводят окно остановки
в двух местах: до вооружения и между вооружением и публикацией, плюс зомби старого поколения.
"""

import sys
from types import SimpleNamespace

from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.state_store import StateStore


class _Audio:
    def flatten(self):
        return self

    def any(self):
        return True


class _Stream:
    def __init__(self, adapter_ref):
        self._adapter_ref = adapter_ref
        self._reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    @property
    def read_available(self):
        return 8

    def read(self, frames):
        self._reads += 1
        if self._reads > 1:
            self._adapter_ref[0]._stop_event.set()  # ограничивает цикл при регрессии
        return _Audio(), False


def _make_adapter(tmp_path):
    store = StateStore(tmp_path / "profile")
    store.initialize_startup_plaintext_policy(new_profile=True)
    settings = {"audio_read_poll_sec": 0.001, "audio_stream_starve_sec": 0}
    ref = []
    adapter = OpenWakeWordAdapter(
        store.data_dir, settings_get=settings.get, read_privacy_revision=store.read_wake_privacy_revision,
    )
    ref.append(adapter)
    return adapter, ref


def _run_loop(adapter, ref, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **kw: _Stream(ref)))
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)


def test_stop_before_arming_does_not_arm_or_publish(tmp_path, monkeypatch):
    adapter, ref = _make_adapter(tmp_path)
    fired = []

    def predict(audio):
        # stop() успел отозвать всё и взвести событие, пока поток шёл от predict к вооружению
        adapter._stop_event.set()
        return {"hey": 0.9}

    adapter._oww = SimpleNamespace(predict=predict)
    adapter._on_detected = lambda name, score: fired.append(name)

    _run_loop(adapter, ref, monkeypatch)

    assert fired == []
    assert adapter._last_detection is None
    assert adapter.handle_wake_word_status({})["last_detection"] is None
    assert adapter._preroll_fire is None


def test_stop_between_arming_and_publish_does_not_publish(tmp_path, monkeypatch):
    adapter, ref = _make_adapter(tmp_path)
    fired = []
    adapter._oww = SimpleNamespace(predict=lambda audio: {"hey": 0.9})
    adapter._on_detected = lambda name, score: fired.append(name)

    original = adapter._record_detection

    def stop_then_record(*args, **kwargs):
        adapter._stop_event.set()  # stop() вклинился между вооружением pre-roll и публикацией
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "_record_detection", stop_then_record)
    _run_loop(adapter, ref, monkeypatch)

    assert fired == []
    assert adapter._last_detection is None
    assert adapter.handle_wake_word_status({})["last_detection"] is None
    assert adapter._preroll_fire is None  # finally цикла сбрасывает вооружённое срабатывание


def test_zombie_generation_cannot_publish_into_new_session(tmp_path):
    adapter, _ = _make_adapter(tmp_path)
    old_generation = adapter._generation
    adapter._generation += 1  # start() новой сессии

    assert adapter._record_detection("hey", 0.9, generation=old_generation) is False
    assert adapter._last_detection is None
    # актуальное поколение и вызов без поколения (прежний контракт) публикуют как раньше
    assert adapter._record_detection("hey", 0.9, generation=adapter._generation) is True
    adapter._last_detection = None
    assert adapter._record_detection("hey", 0.9) is True


def test_record_detection_after_stop_event_is_refused(tmp_path):
    adapter, _ = _make_adapter(tmp_path)
    adapter._stop_event.set()
    assert adapter._record_detection("hey", 0.9) is False
    assert adapter._last_detection is None


def test_normal_detection_still_published_when_not_stopped(tmp_path, monkeypatch):
    """Регрессия-страж: без остановки детекция по-прежнему публикуется и pre-roll вооружается."""
    adapter, ref = _make_adapter(tmp_path)
    fired = []
    seen = {}
    adapter._oww = SimpleNamespace(predict=lambda audio: {"hey": 0.9})

    def on_detected(name, score):
        fired.append(name)
        seen["armed"] = adapter._preroll_fire is not None
        seen["published"] = adapter._last_detection is not None
        adapter._stop_event.set()

    adapter._on_detected = on_detected
    _run_loop(adapter, ref, monkeypatch)

    assert fired == ["hey"]
    assert seen == {"armed": True, "published": True}
