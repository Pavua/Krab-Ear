#!/usr/bin/env python3
"""Живой смок C2b (macOS, ручной гейт; НЕ для CI — требует pyannote/torch/say).

Синтетическая «встреча двух голосов» (say -v Milena / -v Yuri) →
AudioEngine.diarize_window на реальном pipeline → LiveSpeakerTracker →
ожидаем РОВНО 2 спикеров после сшивки двух окон.

Запуск из корня репо:
  PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python scripts/e2e_speakers_smoke.py
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "KrabEar"))

MILENA = [
    "Коллеги, начнём встречу. Сегодня обсуждаем план релиза на следующую неделю.",
    "Решение такое: релиз переносим на четверг, тестирование начинаем завтра.",
    "Запиши задачу: подготовить черновик документации до среды.",
]
YURI = [
    "Да, согласен. Ещё нужно решить вопрос с дизайном плавающей панели.",
    "Принято. Я возьму на себя задачу по настройке сервера сборки.",
    "Спасибо всем, хорошая встреча. До связи.",
]


def build_wavs(tmp: Path) -> list[Path]:
    parts = []
    for i, (m, y) in enumerate(zip(MILENA, YURI)):
        for voice, text in (("Milena", m), ("Yuri", y)):
            aiff = tmp / f"{voice}_{i}.aiff"
            subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
            parts.append(aiff)
    lst = tmp / "list.txt"
    wavs = []
    for p in parts:
        wav = p.with_suffix(".wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p),
                        "-ar", "16000", "-ac", "1", str(wav)], check=True)
        wavs.append(wav)
    lst.write_text("".join(f"file '{w}'\n" for w in wavs))
    full = tmp / "meeting.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-ar", "16000", "-ac", "1", str(full)], check=True)
    half1, half2 = tmp / "w1.wav", tmp / "w2.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(full),
                    "-t", "30", str(half1)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(full),
                    "-ss", "30", str(half2)], check=True)
    return [half1, half2]


def main() -> int:
    from core.engine import AudioEngine
    from backend.meeting_session_service import LiveSpeakerTracker

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        windows = build_wavs(tmp)
        engine = AudioEngine()
        tracker = LiveSpeakerTracker(threshold=0.72)
        for w in windows:
            t0 = time.monotonic()
            result = engine.diarize_window(str(w))
            print(f"{w.name}: {len(result['segments'])} сегм., "
                  f"{len(result['speaker_embeddings'])} эмб., "
                  f"{time.monotonic() - t0:.1f}с")
            tracker.ingest(result["segments"], result["speaker_embeddings"],
                           now_ts=time.time())
        snap = tracker.snapshot()
        print("Спикеры после сшивки:", snap)
        if len(snap) != 2:
            print(f"FAIL: ожидали 2 спикеров, получили {len(snap)}")
            return 1
        print("OK: ровно 2 спикера, сшивка между окнами работает")
        return 0


if __name__ == "__main__":
    sys.exit(main())
