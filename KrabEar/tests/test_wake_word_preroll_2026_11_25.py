"""Pre-roll после срабатывания «Краб»: звук между словом и стартом записи агента не теряется.

Живая проверка 25.11.2026: от конца слова до записи проходит ~1,3–1,7 с (хвост VAD, распознавание, опрос агента,
старт сессии) — первые слова команды терялись. Backend держит короткое кольцо PCM и отдаёт агенту звук с момента
«после слова» одним одноразовым IPC-вызовом ``wake_word_take_preroll``.
"""

from __future__ import annotations

import base64
import math
import sys
from types import SimpleNamespace

import numpy as np

from backend import openwakeword_adapter as oa
from backend import stt_keyword_spotter as sk
from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.wake_word_preroll import PreRollBuffer

CHUNK = 1280
RATE = 16000


# ----------------------------------------------------------------------------- кольцо
def _pcm(values) -> np.ndarray:
    return np.asarray(values, dtype=np.int16)


def test_ring_returns_everything_from_the_anchor_to_the_end():
    ring = PreRollBuffer(max_samples=10_000)
    ring.append(_pcm(range(0, 100)))
    ring.append(_pcm(range(100, 200)))
    assert ring.total == 200
    out = np.frombuffer(ring.slice_from(150, max_samples=1000), dtype=np.int16)
    assert out.tolist() == list(range(150, 200))


def test_ring_forgets_old_audio_and_clamps_an_old_anchor():
    ring = PreRollBuffer(max_samples=150)
    for start in (0, 100, 200):
        ring.append(_pcm(range(start, start + 100)))
    assert ring.total == 300
    out = np.frombuffer(ring.slice_from(0, max_samples=1000), dtype=np.int16)
    assert len(out) <= 250 and out[-1] == 299 and out[0] >= 50  # старое забыто, якорь подтянут к началу кольца


def test_ring_caps_the_answer_keeping_the_start_after_the_anchor():
    ring = PreRollBuffer(max_samples=10_000)
    ring.append(_pcm(range(0, 1000)))
    out = np.frombuffer(ring.slice_from(100, max_samples=50), dtype=np.int16)
    assert out.tolist() == list(range(100, 150))  # начало команды важнее хвоста


def test_ring_anchor_in_the_future_or_negative_is_safe():
    ring = PreRollBuffer(max_samples=1000)
    ring.append(_pcm(range(100)))
    assert ring.slice_from(500, max_samples=100) == b""
    assert len(ring.slice_from(-50, max_samples=1000)) == 200  # отрицательный якорь = с начала


def test_ring_clear_drops_audio_but_keeps_counting():
    ring = PreRollBuffer(max_samples=1000)
    ring.append(_pcm(range(100)))
    ring.clear()
    assert ring.slice_from(0, max_samples=1000) == b""
    ring.append(_pcm(range(10)))
    assert ring.total == 110


def test_ring_accepts_plain_lists_and_rejects_nothing():
    ring = PreRollBuffer(max_samples=1000)
    assert ring.append(list(range(20))) == 20


# ----------------------------------------------------------------------------- модель: якорь срабатывания
def _tone(frames: int, amp: int = 3000) -> list[int]:
    return [int(amp * math.sin(2 * math.pi * 220 * i / sk.SAMPLE_RATE)) for i in range(sk.FRAME_SAMPLES * frames)]


def _silence(frames: int) -> list[int]:
    return [0] * (sk.FRAME_SAMPLES * frames)


def _drive(model, burst):
    """Подаёт кадры; после каждого ждёт воркер. Возвращает (номер кадра срабатывания, back в сэмплах)."""
    fired_at = back = None
    frames = len(burst) // sk.FRAME_SAMPLES
    for n in range(frames):
        out = model.predict(burst[n * sk.FRAME_SAMPLES:(n + 1) * sk.FRAME_SAMPLES])
        model.drain(timeout=2.0)
        if out["krab_stt"] >= 0.5 and fired_at is None:
            fired_at, back = n + 1, model.fire_preroll_back()
    return fired_at, back


def test_keyword_only_utterance_anchors_after_the_word():
    model = sk.SttKeywordModel(lambda _s: "краб", name="krab_stt", cooldown_sec=0.0)
    burst = _silence(10) + _tone(6) + _silence(12)  # слово, потом тишина: в ней начнётся команда
    fired_at, back = _drive(model, burst)
    assert fired_at is not None and back is not None
    # Звук ПОСЛЕ слова: ни одного отсчёта самого «Краб» в хвосте; запас — не больше двух кадров тишины до конца слова.
    word_end = (10 + 6 + sk.TRAIL_FRAMES) * sk.FRAME_SAMPLES  # реплика заканчивается через TRAIL_FRAMES тишины
    now = fired_at * sk.FRAME_SAMPLES
    assert back == now - (word_end - 2 * sk.FRAME_SAMPLES)
    model.close()


def test_multiword_utterance_anchors_at_the_start_so_the_command_in_it_survives():
    model = sk.SttKeywordModel(lambda _s: "краб открой историю", name="krab_stt", cooldown_sec=0.0)
    burst = _silence(10) + _tone(12) + _silence(12)
    fired_at, back = _drive(model, burst)
    assert fired_at is not None and back is not None
    # Реплика целиком (с пре-роллом тишины в начале): команда внутри неё не теряется.
    utter_len = (sk.PRE_ROLL_FRAMES + 12 + sk.TRAIL_FRAMES) * sk.FRAME_SAMPLES
    word_end = (10 + 12 + sk.TRAIL_FRAMES) * sk.FRAME_SAMPLES
    assert back == fired_at * sk.FRAME_SAMPLES - (word_end - utter_len)
    model.close()


def test_fire_preroll_back_is_single_use_and_none_without_fire():
    model = sk.SttKeywordModel(lambda _s: "краб", name="krab_stt", cooldown_sec=0.0)
    assert model.fire_preroll_back() is None
    burst = _silence(10) + _tone(6) + _silence(12)
    _, back = _drive(model, burst)
    assert back is not None and model.fire_preroll_back() is None
    model.close()


# ----------------------------------------------------------------------------- адаптер
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
        return np.full(frames, self._reads, dtype=np.int16), False  # значение сэмпла = номер чанка (для сверки)


class _FakeStt:
    def __init__(self, fire_on=(), back=0):
        self.fire_on, self.calls, self.back, self._pending = set(fire_on), 0, back, None

    def predict(self, chunk):
        self.calls += 1
        fire = self.calls in self.fire_on
        if fire:
            self._pending = self.back
        return {oa.STT_MODEL_NAME: 1.0 if fire else 0.0}

    def fire_preroll_back(self):
        back, self._pending = self._pending, None
        return back

    def reset(self):
        pass

    def close(self, timeout=0.5):
        pass

    def stats(self):
        return {"utterances": 0}

    def busy_for(self):
        return 0.0

    def last_unavailable_reason(self):
        return None


def _adapter(tmp_path, settings=None):
    cfg = {"audio_guarded_read_enabled": False, "wake_word_stt_mode": "live", "wake_word_oww_mode": "off"}
    cfg.update(settings or {})
    return OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: cfg.get(k, d),
                               read_privacy_revision=lambda: "rev1",
                               stt_transcribe=lambda _s: ""), cfg


def _run(adapter, monkeypatch, chunks, stt, on_read=None, oww=None):
    adapter._stt = stt
    adapter._oww = oww
    adapter._on_detected = lambda name, score: None
    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(InputStream=lambda **kw: _Stream(adapter, chunks, on_read)))
    adapter._listen_loop(threshold=0.5, chunk_size=CHUNK, sample_rate=RATE, generation=adapter._generation)


def _take(adapter):
    return adapter.handle_wake_word_take_preroll({})


def test_take_returns_audio_since_the_anchor_while_the_loop_is_alive(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path)
    taken = {}

    def on_read(n):
        if n == 6:  # после срабатывания на 3-м чанке цикл ещё жив: агент забирает звук
            taken.update(_take(adapter))

    # якорь = 2 чанка назад от момента срабатывания → звук с 2-го чанка включительно до текущего (3-го)... и до 5-го
    _run(adapter, monkeypatch, chunks=8, stt=_FakeStt(fire_on={3}, back=2 * CHUNK), on_read=on_read)
    assert taken["ok"] and taken["available"]
    assert taken["sample_rate"] == RATE and taken["format"] == "pcm_s16le_mono_b64"
    pcm = np.frombuffer(base64.b64decode(taken["pcm_b64"]), dtype="<i2")
    assert taken["samples"] == len(pcm)
    # чанк с номером k заполнен значением k; взяли на чтении №6 → в кольце чанки 1..5; якорь: после чанка 3 минус 2 чанка
    assert pcm[0] == 2 and pcm[-1] == 5 and len(pcm) == 4 * CHUNK


def test_take_is_single_use(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path)
    results = []
    _run(adapter, monkeypatch, chunks=6, stt=_FakeStt(fire_on={2}, back=CHUNK),
         on_read=lambda n: results.append(_take(adapter)) if n in (4, 5) else None)
    assert results[0]["available"] is True
    assert results[1]["available"] is False and results[1]["reason"] == "no_detection"


def test_take_without_any_detection_is_unavailable(tmp_path):
    adapter, _ = _adapter(tmp_path)
    out = _take(adapter)
    assert out["ok"] is True and out["available"] is False and out["reason"] == "no_detection"
    assert "pcm_b64" not in out


def test_take_after_the_loop_exit_has_nothing_to_give(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path)
    _run(adapter, monkeypatch, chunks=5, stt=_FakeStt(fire_on={2}, back=CHUNK))
    out = _take(adapter)
    assert out["available"] is False  # цикл вышел → кольцо с PCM сброшено, ничего не висит в памяти


def test_take_expires_after_the_ttl(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path)
    out = {}

    def on_read(n):
        if n == 5:
            adapter._preroll_fire["ts"] -= oa.PREROLL_TTL_SEC + 1
            out.update(_take(adapter))

    _run(adapter, monkeypatch, chunks=6, stt=_FakeStt(fire_on={2}, back=CHUNK), on_read=on_read)
    assert out["available"] is False and out["reason"] == "expired"


def test_take_refuses_when_the_privacy_revision_changed(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path)
    revision = {"v": "rev1"}
    adapter._read_privacy_revision = lambda: revision["v"]
    out = {}

    def on_read(n):
        if n == 5:
            revision["v"] = "rev2"
            out.update(_take(adapter))

    _run(adapter, monkeypatch, chunks=6, stt=_FakeStt(fire_on={2}, back=CHUNK), on_read=on_read)
    assert out["available"] is False and out["reason"] == "privacy"


def test_shadow_fire_does_not_arm_the_preroll(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_stt_mode": "shadow"})
    out = {}
    _run(adapter, monkeypatch, chunks=6, stt=_FakeStt(fire_on={2}, back=CHUNK),
         on_read=lambda n: out.update(_take(adapter)) if n == 5 else None)
    assert out["available"] is False  # «в тени» никого не будит, значит агенту отдавать нечего


def test_oww_fire_uses_a_short_fixed_lookback(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path, {"wake_word_stt_mode": "off", "wake_word_oww_mode": "live"})
    calls = {"n": 0}

    def predict(chunk):
        calls["n"] += 1
        return {"krab_ru": 0.9 if calls["n"] == 9 else 0.0}

    out = {}
    _run(adapter, monkeypatch, chunks=12, stt=None, oww=SimpleNamespace(predict=predict),
         on_read=lambda n: out.update(_take(adapter)) if n == 11 else None)
    assert out["available"]
    pcm = np.frombuffer(base64.b64decode(out["pcm_b64"]), dtype="<i2")
    # срабатывание на 9-м чанке; взяли на чтении №11 (в кольце чанки 1..10): звук начинается на PREROLL_OWW_BACK_SEC
    # раньше конца 9-го чанка и идёт до конца 10-го
    assert pcm[-1] == 10
    assert len(pcm) == 10 * CHUNK - (9 * CHUNK - int(oa.PREROLL_OWW_BACK_SEC * RATE))


def test_answer_is_capped_to_the_max_duration(tmp_path, monkeypatch):
    adapter, _ = _adapter(tmp_path)
    out = {}
    big = int((oa.PREROLL_MAX_SEC + 3) * RATE)
    _run(adapter, monkeypatch, chunks=6, stt=_FakeStt(fire_on={2}, back=big),
         on_read=lambda n: out.update(_take(adapter)) if n == 5 else None)
    assert out["available"] and out["samples"] <= int(oa.PREROLL_MAX_SEC * RATE)


def test_dispatch_table_exposes_the_method():
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "backend" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "wake_word_take_preroll":
                    found = ast.unparse(v) == "self._oww_adapter.handle_wake_word_take_preroll"
    assert found
