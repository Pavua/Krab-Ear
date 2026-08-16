#!/usr/bin/env python3
"""Офлайн-замер аномалии длительности VAD vs chunker vs WAV (W2b).

Не ходит в прод-сокет, не гоняет GigaAM/MLX, не kickstart.

Usage:
    PYTHONPATH=KrabEar python scripts/measure_duration_anomaly.py /path/to.wav
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _measure(wav_path: Path) -> dict[str, float]:
    import soundfile as sf

    from core.engine import AudioEngine

    info = sf.info(str(wav_path))
    sample_rate = int(info.samplerate) if info.samplerate else 0
    wav_duration_sec = (
        float(info.frames / info.samplerate) if info.samplerate else 0.0
    )
    audio, loaded_sr = sf.read(str(wav_path), dtype="float32")
    sr = int(loaded_sr) if loaded_sr else sample_rate
    # Тот же расчёт, что engine.py VAD pre-filter: len(audio) / sample_rate.
    vad_total_sec = float(len(audio) / sr) if sr > 0 else 0.0
    mono = AudioEngine._resample_audio_to_mono_16k(audio, sr)
    chunker_duration_sec = float(len(mono) / 16000.0)
    return {
        "wav_duration_sec": wav_duration_sec,
        "vad_total_sec": vad_total_sec,
        "chunker_duration_sec": chunker_duration_sec,
        "delta_chunker_minus_vad": chunker_duration_sec - vad_total_sec,
        "delta_chunker_minus_wav": chunker_duration_sec - wav_duration_sec,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: measure_duration_anomaly.py /path/to.wav",
            file=sys.stderr,
        )
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"файл не найден: {path}", file=sys.stderr)
        return 2
    print(json.dumps(_measure(path), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
