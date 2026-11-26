"""Pre-roll: добивающие правки по независимому ревью (ChatGPT Extra High 26.11, находки B1, 6, 19).

* B1: остановка слушателя синхронно отзывает ещё не забранное срабатывание (раньше — только в finally цикла,
  то есть после закрытия потока; при зависшем join звук оставался доступен).
* 6: take по истёкшему срабатыванию НЕ чистит кольцо, которым ещё пользуется живая сессия (там может уже ждать
  звук следующего срабатывания).
* 19: кольцо держит ёмкость строго: пустой чанк не копится, чанк больше ёмкости обрезается до последних отсчётов.
"""

from __future__ import annotations

import time

import numpy as np

from backend import openwakeword_adapter as oa
from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.wake_word_preroll import PreRollBuffer

RATE = 16000


def _adapter(tmp_path):
    return OpenWakeWordAdapter(tmp_path, settings_get=lambda k, d: d, read_privacy_revision=lambda: "rev1",
                               stt_transcribe=lambda _s: "")


def _arm(adapter, ring, age=0.0):
    adapter._preroll_fire = {"ring": ring, "ts": time.monotonic() - age, "rev": "rev1", "anchor": 0, "rate": RATE}


def test_stop_revokes_an_untaken_fire_synchronously(tmp_path):
    adapter = _adapter(tmp_path)
    ring = PreRollBuffer(RATE * 8)
    ring.append(np.ones(1280, dtype=np.int16))
    _arm(adapter, ring)
    adapter.stop()
    out = adapter.handle_wake_word_take_preroll({})
    assert out["available"] is False and out["reason"] == "session_ended"
    assert "pcm_b64" not in out


def test_take_after_stop_does_not_hand_out_audio_even_with_a_live_ring(tmp_path):
    adapter = _adapter(tmp_path)
    ring = PreRollBuffer(RATE * 8)
    ring.append(np.full(1280, 7, dtype=np.int16))
    _arm(adapter, ring)
    adapter.stop()
    assert adapter._preroll_fire is None


def test_expired_take_keeps_the_ring_of_the_live_session(tmp_path):
    adapter = _adapter(tmp_path)
    ring = PreRollBuffer(RATE * 8)
    ring.append(np.full(1280, 5, dtype=np.int16))
    _arm(adapter, ring, age=oa.PREROLL_TTL_SEC + 1)
    out = adapter.handle_wake_word_take_preroll({})
    assert out["available"] is False and out["reason"] == "expired"
    assert len(ring.slice_from(0, RATE * 8)) == 2 * 1280, "кольцо живой сессии не очищено"


def test_privacy_refusal_still_clears_the_ring(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._read_privacy_revision = lambda: "rev2"
    ring = PreRollBuffer(RATE * 8)
    ring.append(np.full(1280, 5, dtype=np.int16))
    _arm(adapter, ring)
    out = adapter.handle_wake_word_take_preroll({})
    assert out["available"] is False and out["reason"] == "privacy"
    assert ring.slice_from(0, RATE * 8) == b"", "звук, снятый под старой политикой, не остаётся"


def test_ring_ignores_empty_chunks_and_does_not_grow():
    ring = PreRollBuffer(100)
    for _ in range(1000):
        ring.append(np.zeros(0, dtype=np.int16))
    assert ring.total == 0 and len(ring._chunks) == 0
    ring.append(np.arange(10, dtype=np.int16))
    for _ in range(1000):
        ring.append(np.zeros(0, dtype=np.int16))
    assert len(ring._chunks) == 1 and ring.total == 10


def test_ring_trims_an_oversized_chunk_to_its_capacity_keeping_positions_consistent():
    ring = PreRollBuffer(10)
    end = ring.append(np.arange(25, dtype=np.int16))
    assert end == 25
    got = np.frombuffer(ring.slice_from(0, 100), dtype="<i2")
    assert list(got) == list(range(15, 25)), "хранятся последние 10 отсчётов"
    assert list(np.frombuffer(ring.slice_from(20, 100), dtype="<i2")) == [20, 21, 22, 23, 24], "позиции абсолютные"
    assert ring.total == 25
