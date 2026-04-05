"""Тесты обрезки хвоста в AudioRecorder.

Проверяем только пост-обработку stop(), без запуска реального микрофона.
"""

from __future__ import annotations

import time

import numpy as np

from backend.recorder import AudioRecorder


def _make_recorder_with_audio(sample_rate: int = 16000, samples: int = 1600) -> AudioRecorder:
    rec = AudioRecorder(sample_rate=sample_rate, channels=1)
    rec._is_recording = True  # noqa: SLF001 - тестируем внутренний stop() контракт.
    rec._started_at = time.monotonic() - 0.5  # noqa: SLF001
    rec._chunks = [np.ones((samples, 1), dtype=np.float32)]  # noqa: SLF001
    rec._thread = None  # noqa: SLF001
    return rec


def test_stop_trim_tail_reduces_samples() -> None:
    rec = _make_recorder_with_audio(samples=1600)
    audio, duration = rec.stop(trim_tail_ms=100)
    assert duration >= 0.0
    # 16000 Гц * 100мс = 1600 samples, значит после trim массив пустой.
    assert audio.size == 0


def test_stop_trim_tail_keeps_prefix() -> None:
    rec = _make_recorder_with_audio(samples=3200)
    audio, _ = rec.stop(trim_tail_ms=100)
    # 3200 - 1600 = 1600 samples
    assert audio.size == 1600

