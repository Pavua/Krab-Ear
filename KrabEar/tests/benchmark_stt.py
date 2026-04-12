#!/usr/bin/env python3
"""STT бенчмарк — замеряет latency транскрибации на синтетическом аудио.

Генерирует короткий WAV-файл (sine wave, 3 секунды, 16kHz) и прогоняет
AudioEngine.transcribe() в N итерациях. Выводит avg/p95/max latency.

Использование:
    PYTHONPATH=$(pwd)/KrabEar python KrabEar/tests/benchmark_stt.py
    PYTHONPATH=$(pwd)/KrabEar python KrabEar/tests/benchmark_stt.py --iterations 5
"""

import os
import sys
import time
import tempfile
import argparse

# Add project root to path (KrabEar/ dir)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

SAMPLE_RATE = 16000
DURATION_SEC = 3


def generate_test_wav(duration_sec: int = DURATION_SEC, sample_rate: int = SAMPLE_RATE) -> str:
    """Генерирует синтетический WAV-файл с тоном 440 Hz.

    Возвращает путь к временному файлу (вызывающая сторона отвечает за удаление).
    """
    import numpy as np

    # Lazy import scipy to keep module-level load fast
    try:
        from scipy.io import wavfile as _wf  # type: ignore
        _use_scipy = True
    except ImportError:
        _use_scipy = False

    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    # Mix 440 Hz tone + 880 Hz overtone for a slightly more speech-like signal
    audio = 0.6 * np.sin(2 * np.pi * 440 * t) + 0.4 * np.sin(2 * np.pi * 880 * t)
    audio_int16 = (audio * 32767).astype(np.int16)

    tmp = tempfile.NamedTemporaryFile(
        prefix="krab_ear_bench_", suffix=".wav", delete=False
    )
    tmp_path = tmp.name
    tmp.close()

    if _use_scipy:
        _wf.write(tmp_path, sample_rate, audio_int16)
    else:
        # Pure stdlib fallback — write minimal WAV header manually
        import struct, wave
        with wave.open(tmp_path, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio_int16.tobytes())

    return tmp_path


def _percentile(sorted_values: list, p: float) -> float:
    """Вычисляет p-й перцентиль из отсортированного списка."""
    if not sorted_values:
        return 0.0
    idx = (len(sorted_values) - 1) * p / 100
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def benchmark_stt(iterations: int = 3) -> None:
    """Запускает STT бенчмарк на синтетическом аудио."""

    print("Krab Ear STT Benchmark")
    print(f"Iterations : {iterations}")
    print(f"Audio      : {DURATION_SEC}s sine wave @ {SAMPLE_RATE} Hz (synthetic)")
    print()

    # --- Lazy import heavy dependencies ---
    print("Загрузка AudioEngine (может занять несколько секунд при первом запуске)...")
    load_start = time.monotonic()
    from core.engine import AudioEngine  # type: ignore
    engine = AudioEngine()
    load_ms = int((time.monotonic() - load_start) * 1000)
    print(f"AudioEngine готов за {load_ms} ms\n")

    # --- Generate test WAV once ---
    wav_path = generate_test_wav()
    print(f"Тестовый WAV: {wav_path}")
    wav_size_kb = os.path.getsize(wav_path) / 1024
    print(f"Размер файла : {wav_size_kb:.1f} KB\n")

    latencies_ms: list[float] = []
    results: list[dict] = []

    print(f"{'#':>3}  {'Latency':>10}  {'Текст (обрезан до 60 симв.)' }")
    print(f"{'─'*3}  {'─'*10}  {'─'*60}")

    try:
        for i in range(iterations):
            iter_start = time.monotonic()
            try:
                result = engine.transcribe(
                    audio_data=wav_path,
                    cleanup_profile="soft",
                    is_preview=False,
                    domain="casual",
                    lang_hint="ru",
                )
                latency_ms = (time.monotonic() - iter_start) * 1000
                text = result.get("text", "").strip() if isinstance(result, dict) else str(result).strip()
                tag = "COLD" if i == 0 else "WARM"
                display_text = (text[:57] + "...") if len(text) > 60 else text or "(пусто)"
                print(f"{i+1:>3}  [{tag}] {latency_ms:>7.0f} ms  {display_text}")
            except Exception as exc:
                latency_ms = (time.monotonic() - iter_start) * 1000
                tag = "COLD" if i == 0 else "WARM"
                print(f"{i+1:>3}  [{tag}] {latency_ms:>7.0f} ms  ERROR: {exc}")
                text = f"ERROR: {exc}"

            latencies_ms.append(latency_ms)
            results.append({"iter": i + 1, "latency_ms": latency_ms, "text": text})

    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

    # --- Stats ---
    if not latencies_ms:
        print("\nНет данных для статистики.")
        return

    sorted_lat = sorted(latencies_ms)
    avg_ms = sum(latencies_ms) / len(latencies_ms)
    p95_ms = _percentile(sorted_lat, 95)
    max_ms = max(latencies_ms)
    cold_ms = latencies_ms[0]
    warm_latencies = latencies_ms[1:] if len(latencies_ms) > 1 else latencies_ms
    warm_avg_ms = sum(warm_latencies) / len(warm_latencies)

    print(f"\n{'='*50}")
    print("  РЕЗУЛЬТАТЫ")
    print(f"{'='*50}")
    print(f"  Cold (1-я итерация)  : {cold_ms:>8.0f} ms")
    print(f"  Warm avg (2+)        : {warm_avg_ms:>8.0f} ms")
    print(f"  Avg (все итерации)   : {avg_ms:>8.0f} ms")
    print(f"  p95                  : {p95_ms:>8.0f} ms")
    print(f"  Max                  : {max_ms:>8.0f} ms")
    print(f"  AudioEngine load     : {load_ms:>8} ms")
    print(f"{'='*50}")

    # RTF (Real-Time Factor): latency / audio_duration — <1.0 is real-time capable
    audio_duration_ms = DURATION_SEC * 1000
    warm_rtf = warm_avg_ms / audio_duration_ms
    print(f"\n  Real-Time Factor (warm avg / {DURATION_SEC}s audio): {warm_rtf:.2f}x")
    rtf_label = "real-time OK" if warm_rtf <= 1.0 else "SLOWER than real-time"
    print(f"  ({rtf_label})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Krab Ear STT latency benchmark")
    parser.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=3,
        help="Количество итераций (default: 3)",
    )
    args = parser.parse_args()
    benchmark_stt(iterations=args.iterations)


if __name__ == "__main__":
    main()
