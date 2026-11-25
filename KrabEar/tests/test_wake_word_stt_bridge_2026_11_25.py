"""Распознаватель STT-споттера: своя копия GigaAM в памяти, аудио без файлов, mlx_lock коротко и без ожидания."""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from backend import wake_word_stt_bridge as bridge
from backend.stt_keyword_spotter import TranscriberUnavailable
from core.config import DEFAULT_SETTINGS

SRC = Path(bridge.__file__).read_text(encoding="utf-8")


class _Lock:
    def __init__(self, acquirable=True):
        self.acquirable, self.acquired, self.released, self.timeouts = acquirable, 0, 0, []

    def acquire(self, timeout=None):
        self.timeouts.append(timeout)
        if self.acquirable:
            self.acquired += 1
        return self.acquirable

    def release(self):
        self.released += 1


class _Inter:
    def __init__(self, exc=None):
        self.exc, self.kwargs = exc, None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def __enter__(self):
        if self.exc:
            raise self.exc
        return self

    def __exit__(self, *a):
        return False


def _make(text="Краб.", lock=None, inter=None, loader=None, infer=None, **kw):
    calls = {"load": 0, "infer": []}

    def default_loader(mode):
        calls["load"] += 1
        return ("model", "tokenizer")

    def default_infer(model, tokenizer, audio):
        calls["infer"].append(audio)
        return text

    lock = lock or _Lock()
    tr = bridge.GigaAmMemoryTranscriber(
        loader=loader or default_loader, infer=infer or default_infer,
        mlx_lock_fn=lambda: lock, inter_lock_fn=inter or _Inter(), release_cache=lambda: calls.setdefault("cleared", 0),
        **kw,
    )
    return tr, calls, lock


def test_model_is_loaded_lazily_once_and_released_on_demand():
    tr, calls, _ = _make()
    assert calls["load"] == 0 and not tr.is_loaded()  # режим off: память не тратим
    tr(np.ones(2000, dtype=np.int16))
    tr(np.ones(2000, dtype=np.int16))
    assert calls["load"] == 1 and tr.is_loaded()
    tr.release()
    assert not tr.is_loaded()
    tr(np.ones(2000, dtype=np.int16))
    assert calls["load"] == 2


def test_audio_goes_to_the_model_as_float32_array_in_memory():
    tr, calls, lock = _make()
    assert tr(np.array([0, 16384, -32768, 32767], dtype=np.int16)) == "Краб."
    audio = calls["infer"][0]
    assert audio.dtype == np.float32 and audio.ndim == 1
    assert audio[1] == 0.5 and audio[2] == -1.0  # ровно int16/32768, как load_audio gigaam_mlx
    assert lock.acquired == 1 and lock.released == 1


def test_mlx_lock_is_taken_briefly_and_busy_lock_skips_the_utterance():
    tr, calls, lock = _make(lock=_Lock(acquirable=False), lock_wait_sec=0.25)
    with pytest.raises(TranscriberUnavailable, match="mlx_busy"):
        tr(np.ones(2000, dtype=np.int16))
    assert lock.timeouts == [0.25] and calls["infer"] == [] and lock.released == 0


def test_inter_process_lock_timeout_means_busy_not_error():
    class MLXInterLockTimeout(Exception):
        pass

    tr, calls, _ = _make(inter=_Inter(exc=MLXInterLockTimeout("lock file busy")), lock_wait_sec=0.25)
    with pytest.raises(TranscriberUnavailable, match="mlx_busy"):
        tr(np.ones(2000, dtype=np.int16))
    assert calls["infer"] == []


def test_inter_process_lock_gets_the_same_short_timeout():
    inter = _Inter()
    tr, _, _ = _make(inter=inter, lock_wait_sec=0.25)
    tr(np.ones(2000, dtype=np.int16))
    assert inter.kwargs == {"timeout_sec": 0.25}


def test_real_mlx_lock_held_by_dictation_does_not_block_the_spotter():
    from core.mlx_lock import mlx_lock

    held, done = threading.Event(), threading.Event()

    def dictation():
        with mlx_lock():
            held.set()
            done.wait(5.0)

    t = threading.Thread(target=dictation, daemon=True)
    t.start()
    assert held.wait(2.0)
    calls = {"infer": 0}
    tr = bridge.GigaAmMemoryTranscriber(
        loader=lambda mode: ("m", "t"), infer=lambda m, t_, a: calls.__setitem__("infer", 1) or "краб",
        lock_wait_sec=0.1, release_cache=lambda: None,
    )
    start = time.monotonic()
    with pytest.raises(TranscriberUnavailable, match="mlx_busy"):
        tr(np.ones(2000, dtype=np.int16))
    assert time.monotonic() - start < 1.0 and calls["infer"] == 0
    done.set()
    t.join(2.0)
    assert tr(np.ones(2000, dtype=np.int16)) == "краб"  # лок свободен — работает


def test_infer_exception_does_not_leak_text_and_lock_is_released():
    def boom(model, tokenizer, audio):
        raise RuntimeError("секретный фрагмент текста краб")

    tr, _, lock = _make(infer=boom)
    with pytest.raises(TranscriberUnavailable) as info:
        tr(np.ones(2000, dtype=np.int16))
    assert "краб" not in str(info.value) and info.value.__cause__ is None
    assert lock.released == 1


def test_model_load_failure_is_unavailable_and_retried_next_time():
    state = {"n": 0}

    def flaky(mode):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("no weights")
        return ("m", "t")

    tr, _, _ = _make(loader=flaky)
    with pytest.raises(TranscriberUnavailable, match="model_load_failed"):
        tr(np.ones(2000, dtype=np.int16))
    assert tr(np.ones(2000, dtype=np.int16)) == "Краб."


def test_non_text_result_is_rejected():
    tr, _, _ = _make(infer=lambda m, t, a: 5)
    with pytest.raises(TranscriberUnavailable, match="not_text"):
        tr(np.ones(2000, dtype=np.int16))


def test_warmup_loads_and_runs_on_silence_and_swallows_unavailability():
    tr, calls, _ = _make()
    tr.warmup()
    assert calls["load"] == 1 and len(calls["infer"]) == 1 and not calls["infer"][0].any()
    busy, _, _ = _make(lock=_Lock(acquirable=False))
    busy.warmup()  # занят — молча пропускаем


def test_release_clears_mlx_cache_only_under_mlx_lock():
    cleared = []
    lock = _Lock()
    inter = _Inter()
    tr = bridge.GigaAmMemoryTranscriber(
        loader=lambda m: ("m", "t"), infer=lambda m, t, a: "x", mlx_lock_fn=lambda: lock, inter_lock_fn=inter,
        release_cache=lambda: cleared.append(lock.acquired - lock.released), lock_wait_sec=0.25,
    )
    tr(np.ones(100, dtype=np.int16))
    tr.release()
    assert not tr.is_loaded()
    assert cleared == [1]                       # кэш сброшен, пока лок удерживался
    assert lock.released == lock.acquired == 2  # инференс + выгрузка, лок отпущен
    assert inter.kwargs == {"timeout_sec": 0.25}


def test_release_with_busy_lock_drops_references_but_skips_cache_clear_and_does_not_wait():
    cleared = []
    tr = bridge.GigaAmMemoryTranscriber(
        loader=lambda m: ("m", "t"), infer=lambda m, t, a: "x", mlx_lock_fn=lambda: _Lock(acquirable=False),
        inter_lock_fn=_Inter(), release_cache=lambda: cleared.append(1), lock_wait_sec=0.05,
    )
    tr._model, tr._tokenizer = "m", "t"
    tr.release()
    assert not tr.is_loaded() and cleared == []


def test_release_never_clears_cache_while_dictation_holds_the_real_mlx_lock():
    from core.mlx_lock import mlx_lock

    cleared = []
    held, done = threading.Event(), threading.Event()

    def dictation():
        with mlx_lock():
            held.set()
            done.wait(5.0)

    t = threading.Thread(target=dictation, daemon=True)
    t.start()
    assert held.wait(2.0)
    tr = bridge.GigaAmMemoryTranscriber(
        loader=lambda m: ("m", "t"), infer=lambda m, t_, a: "x", lock_wait_sec=0.1, release_cache=lambda: cleared.append(1),
    )
    tr._model = "m"
    start = time.monotonic()
    tr.release()
    assert time.monotonic() - start < 1.0 and cleared == [] and not tr.is_loaded()
    done.set()
    t.join(2.0)


def test_warmup_started_before_release_does_not_bring_the_model_back():
    gate, entered = threading.Event(), threading.Event()

    def slow_loader(mode):
        entered.set()
        gate.wait(5.0)
        return ("m", "t")

    tr, calls, _ = _make(loader=slow_loader)
    worker = threading.Thread(target=tr.warmup, daemon=True)
    worker.start()
    assert entered.wait(2.0)
    tr.release()          # владелец выключил режим, пока модель ещё грузилась
    gate.set()
    worker.join(3.0)
    assert not tr.is_loaded() and calls["infer"] == []


def test_warm_model_is_not_warmed_again_after_adapter_restart():
    tr, calls, _ = _make()
    tr.warmup()
    tr.warmup()           # адаптер перезапускается после каждой диктовки
    assert len(calls["infer"]) == 1 and calls["load"] == 1
    tr.release()
    tr.warmup()           # после выгрузки прогрев нужен снова
    assert len(calls["infer"]) == 2 and calls["load"] == 2


def test_bridge_never_writes_audio_to_disk_or_spawns_processes_or_logs_text():
    tree = ast.parse(SRC)
    top_imports = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    top_imports |= {(n.module or "").split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom)}
    all_imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    all_imports |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not all_imports & {"tempfile", "subprocess", "wave", "socket", "sqlite3", "pathlib", "shutil"}
    assert "mlx" not in top_imports and "gigaam_mlx" not in top_imports  # импорт моделей только лениво
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            assert name not in {"open", "write_text", "write_bytes", "NamedTemporaryFile", "load_audio", "transcribe",
                                "unlink", "mkdir"}
            if name in {"debug", "info", "warning", "error", "exception"}:
                assert "text" not in ast.dump(n)


def test_settings_defaults_keep_old_behaviour_and_validator_rejects_garbage():
    from backend.settings_validator import SettingsValidator

    assert DEFAULT_SETTINGS["wake_word_oww_mode"] == "live"
    assert DEFAULT_SETTINGS["wake_word_stt_mode"] == "off"
    fixed = SettingsValidator().validate({"wake_word_oww_mode": "turbo", "wake_word_stt_mode": "x"}).fixed
    assert fixed["wake_word_oww_mode"] == "live" and fixed["wake_word_stt_mode"] == "off"
    ok = SettingsValidator().validate({"wake_word_oww_mode": "shadow", "wake_word_stt_mode": "live"}).fixed
    assert ok["wake_word_oww_mode"] == "shadow" and ok["wake_word_stt_mode"] == "live"
