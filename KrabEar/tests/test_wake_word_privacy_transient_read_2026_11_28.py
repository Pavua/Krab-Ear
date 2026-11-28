"""Транзиентный сбой чтения privacy-ревизии не должен рвать захват wake word (инцидент 27.11 20:38).

Живая цепочка: агент на старте шлёт set_settings → эксклюзивный лок StateStore занят записью →
``read_wake_privacy_revision`` (shared, 0.5 с) падает ``StateStoreLockTimeout`` → адаптер трактовал
это как «политика сменилась» и выходил из цикла → ``Pa_StopStream`` в ``__exit__`` зависал →
heartbeat stale 31.7 с → thread_hung → wedged:true → агент перезапускал backend.

Контракт фикса: сбой ЧТЕНИЯ (исключение провайдера) повторяем с ограниченным backoff; пока ревизия
неизвестна, цикл стоит — ни чтения кадров, ни кольца, ни распознавания (fail-closed). Бюджет
исчерпан → прежнее закрытие захвата. Ответ провайдера «None» (privacy ON / битая политика) —
по-прежнему немедленное закрытие (см. test_wake_word_privacy_during_capture.py).
"""

import sys
import threading
import time
from types import SimpleNamespace

import pytest

import backend.openwakeword_adapter as oww_mod
from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.state_store import StateStore


class _Audio:
    def __init__(self, calls):
        self._calls = calls

    def flatten(self):
        self._calls.append("flatten")
        return self

    def any(self):
        return True


def _install_stream(monkeypatch, calls, on_read):
    class Stream:
        def __enter__(self):
            calls.append("opened")
            return self

        def __exit__(self, *exc):
            calls.append("closed")

        @property
        def read_available(self):
            return 8

        def read(self, frames):
            calls.append("read")
            on_read(calls.count("read"))
            return _Audio(calls), False

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **kw: Stream()))


def _settings():
    return {"audio_read_poll_sec": 0.001, "audio_stream_starve_sec": 0}.get


def _arm(adapter, calls):
    def predict(audio):
        calls.append("predict")
        return {}

    adapter._oww = SimpleNamespace(predict=predict)


def test_lock_contention_during_capture_keeps_stream_open(tmp_path, monkeypatch):
    """RED до фикса: чужой эксклюзивный лок (запись настроек) дольше 0.5 с закрывал захват."""
    calls = []
    store = StateStore(tmp_path / "profile")
    store.initialize_startup_plaintext_policy(new_profile=True)
    writer = StateStore(store.data_dir)
    held = threading.Event()
    release = threading.Event()

    def hold_exclusive_lock():
        with writer._lock():
            held.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold_exclusive_lock, daemon=True)

    def on_read(n):
        if n == 1:
            holder.start()
            assert held.wait(2.0)
            threading.Timer(0.9, release.set).start()  # дольше lock-timeout чтения (0.5 с)
        elif n >= 3:
            adapter._stop_event.set()

    adapter = OpenWakeWordAdapter(
        store.data_dir, settings_get=_settings(), read_privacy_revision=store.read_wake_privacy_revision,
    )
    _arm(adapter, calls)
    _install_stream(monkeypatch, calls, on_read)
    try:
        adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)
    finally:
        release.set()
        holder.join(2.0)

    assert calls.count("read") >= 3, calls  # цикл пережил конкуренцию за лок
    assert calls.count("predict") >= 2, calls
    assert calls[-1] == "closed"
    assert calls.count("closed") == 1


def test_no_audio_processed_while_revision_unknown(tmp_path, monkeypatch):
    """Пока чтение падает, цикл стоит: между сбоем и восстановлением нет read/predict."""
    calls = []
    failures = {"left": 0}

    def provider():
        if failures["left"] > 0:
            failures["left"] -= 1
            calls.append("revision-error")
            raise TimeoutError("lock busy")
        calls.append("revision-ok")
        return "rev-1"

    def on_read(n):
        if n == 1:
            failures["left"] = 3
        elif n >= 3:
            adapter._stop_event.set()

    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_FIRST_DELAY_SEC", 0.01)
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=_settings(), read_privacy_revision=provider)
    _arm(adapter, calls)
    _install_stream(monkeypatch, calls, on_read)
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    first_err = calls.index("revision-error")
    recovered = calls.index("revision-ok", first_err)
    assert calls[first_err:recovered] == ["revision-error"] * 3
    assert calls.count("read") >= 3
    assert calls[-1] == "closed"


def test_persistent_read_failure_still_closes_capture(tmp_path, monkeypatch):
    """Бюджет повторов исчерпан → прежнее fail-closed закрытие, без распознавания."""
    calls = []
    state = {"broken": False}

    def provider():
        if state["broken"]:
            raise TimeoutError("lock busy")
        return "rev-1"

    def on_read(n):
        state["broken"] = True
        if n > 3:
            adapter._stop_event.set()  # страховка от зависания теста при регрессии

    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_BUDGET_SEC", 0.05)
    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_FIRST_DELAY_SEC", 0.01)
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=_settings(), read_privacy_revision=provider)
    _arm(adapter, calls)
    _install_stream(monkeypatch, calls, on_read)
    started = time.monotonic()
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    assert time.monotonic() - started < 2.0
    assert calls.count("read") == 1
    assert "predict" not in calls
    assert calls[-1] == "closed"
    assert adapter.active_model() is None  # сессия очищена: watchdog видит паузу, не мёртвую сессию


def test_transient_failure_before_open_retries_then_opens(tmp_path, monkeypatch):
    """До открытия тапа: сбой чтения повторяем, микрофон открываем только при известной ревизии."""
    calls = []
    failures = {"left": 2}

    def provider():
        if failures["left"] > 0:
            failures["left"] -= 1
            calls.append("revision-error")
            raise TimeoutError("lock busy")
        calls.append("revision-ok")
        return "rev-1"

    def on_read(n):
        adapter._stop_event.set()

    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_FIRST_DELAY_SEC", 0.01)
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=_settings(), read_privacy_revision=provider)
    _arm(adapter, calls)
    _install_stream(monkeypatch, calls, on_read)
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    assert calls[:3] == ["revision-error", "revision-error", "revision-ok"]
    assert calls.index("opened") > calls.index("revision-ok")
    assert calls.count("read") == 1


def test_persistent_failure_before_open_never_opens(tmp_path, monkeypatch):
    calls = []

    def provider():
        raise TimeoutError("lock busy")

    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_BUDGET_SEC", 0.05)
    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_FIRST_DELAY_SEC", 0.01)
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=_settings(), read_privacy_revision=provider)
    _arm(adapter, calls)
    _install_stream(monkeypatch, calls, lambda n: None)
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    assert "opened" not in calls


def test_stop_during_retry_exits_promptly(tmp_path, monkeypatch):
    """stop() во время повторов не ждёт бюджет: join слушателя укладывается в штатный таймаут."""
    calls = []
    state = {"broken": False}

    def provider():
        if state["broken"]:
            raise TimeoutError("lock busy")
        return "rev-1"

    def on_read(n):
        state["broken"] = True
        threading.Timer(0.1, adapter._stop_event.set).start()

    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_BUDGET_SEC", 30.0)
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=_settings(), read_privacy_revision=provider)
    _arm(adapter, calls)
    _install_stream(monkeypatch, calls, on_read)
    started = time.monotonic()
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    assert time.monotonic() - started < 2.0
    assert "predict" not in calls
    assert calls[-1] == "closed"


@pytest.mark.parametrize("value", [None])
def test_provider_none_is_not_retried(tmp_path, monkeypatch, value):
    """«None» от провайдера — это решение политики (ON/битая), а не сбой чтения: закрываем сразу."""
    calls = []
    state = {"on": False}

    def provider():
        calls.append("revision")
        return value if state["on"] else "rev-1"

    def on_read(n):
        state["on"] = True
        if n > 3:
            adapter._stop_event.set()

    monkeypatch.setattr(oww_mod, "_PRIVACY_READ_RETRY_BUDGET_SEC", 30.0)
    adapter = OpenWakeWordAdapter(tmp_path, settings_get=_settings(), read_privacy_revision=provider)
    _arm(adapter, calls)
    _install_stream(monkeypatch, calls, on_read)
    started = time.monotonic()
    adapter._listen_loop(threshold=0.5, chunk_size=8, sample_rate=16000, generation=adapter._generation)

    assert time.monotonic() - started < 1.0
    assert calls.count("read") == 1
    assert "predict" not in calls
