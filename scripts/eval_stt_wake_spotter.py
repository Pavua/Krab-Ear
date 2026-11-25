#!/usr/bin/env python3
"""Оценка поиска слова «Краб» по распознаванию речи на личном датасете владельца (ДО включения в приложение).

Каждый клип датасета (`scripts/record_wake_dataset.py`) подаётся в `SttKeywordModel` как непрерывный поток
(секунда тишины до и после), считается, сработала ли модель. В отчёт попадают ТОЛЬКО числа по блокам: ни имён
файлов, ни транскриптов (записи — биометрия владельца, см. память reference-wake-dataset-owner-voice).

Приёмка (решение владельца 24.11): recall >= 85% на позитивах и 0 ложных на негативах. Исключение — НЕ целым блоком,
а по клипу: негативный клип, в подсказке (`prompt` manifest) которого есть слово на «краб» («Краб-салат», «Крабы»),
считается отдельной строкой `<блок>:содержит_слово`; остальные клипы того же блока («Грабли», «Крап», «Карп»)
остаются обычными негативами и обязаны не срабатывать.

Запуск (из корня репозитория, нужны mlx и gigaam-mlx из .venv_krab_ear; микрофон не используется):
    PYTHONPATH=KrabEar .venv_krab_ear/bin/python scripts/eval_stt_wake_spotter.py [--dataset DIR] [--seed 5]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import wave
from collections import defaultdict
from pathlib import Path
from typing import Mapping

DEFAULT_DATASET = Path.home() / "Library" / "Application Support" / "KrabEar" / "wake_dataset"
MIN_RECALL = 0.85
# Суффикс блока для негативных клипов, где по подсказке звучит слово «краб» — срабатывание там не ложное.
CONTAINS_WORD_SUFFIX = ":содержит_слово"


def block_label(block: str, prompt: str) -> str:
    """Блок результата: клип с подсказкой, содержащей слово на «краб», выделяется в отдельную строку."""
    words = re.sub(r"[^\w\s]", " ", prompt.lower().replace("ё", "е")).split()
    return block + CONTAINS_WORD_SUFFIX if any(w.startswith("краб") for w in words) else block


def summarize(results: Mapping[tuple[str, str], tuple[int, int]]) -> dict[str, float]:
    """``{(label, block): (всего, сработало)}`` → итоговые числа. Чистая функция (тесты без аудио)."""
    pos_total = sum(t for (label, _), (t, _) in results.items() if label == "pos")
    pos_hit = sum(h for (label, _), (_, h) in results.items() if label == "pos")
    neg_total = sum(t for (label, b), (t, _) in results.items() if label == "neg" and not b.endswith(CONTAINS_WORD_SUFFIX))
    neg_fire = sum(h for (label, b), (_, h) in results.items() if label == "neg" and not b.endswith(CONTAINS_WORD_SUFFIX))
    word_total = sum(t for (label, b), (t, _) in results.items() if label == "neg" and b.endswith(CONTAINS_WORD_SUFFIX))
    word_fire = sum(h for (label, b), (_, h) in results.items() if label == "neg" and b.endswith(CONTAINS_WORD_SUFFIX))
    return {
        "pos_total": pos_total, "pos_hit": pos_hit,
        "recall": (pos_hit / pos_total) if pos_total else 0.0,
        "neg_total": neg_total, "neg_fire": neg_fire,
        "word_clips": word_total, "word_clip_fires": word_fire,
    }


def accepted(summary: Mapping[str, float]) -> bool:
    return summary["pos_total"] > 0 and summary["neg_total"] > 0 and summary["recall"] >= MIN_RECALL and summary["neg_fire"] == 0


def format_report(results: Mapping[tuple[str, str], tuple[int, int]], summary: Mapping[str, float]) -> str:
    lines = [f"# Оценка поиска «Краб» по распознаванию речи: {'ПРИНЯТО' if accepted(summary) else 'НЕ ПРИНЯТО'}", "",
             "| Блок | Сработало / всего |", "|---|---|"]
    for (label, block), (total, hit) in sorted(results.items()):
        lines.append(f"| {label} {block} | {hit} / {total} |")
    lines += ["", f"Recall: {int(summary['pos_hit'])} из {int(summary['pos_total'])} = {100 * summary['recall']:.1f}% (порог {100 * MIN_RECALL:.0f}%)",
              f"Ложные на негативах без слова «краб»: {int(summary['neg_fire'])} из {int(summary['neg_total'])} (порог 0)",
              f"Клипы, где слово «краб» звучит само: сработало {int(summary['word_clip_fires'])} из {int(summary['word_clips'])} (не ложные)", ""]
    return "\n".join(lines)


def _read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("ожидался WAV 16 кГц моно 16 бит")
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--seed", type=int, default=5)
    args = parser.parse_args(argv)
    import numpy as np

    from backend import stt_keyword_spotter as sk
    from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter

    adapter = GigaAMMLXAdapter(mode="v3_e2e_rnnt")

    def transcribe(audio):
        try:
            return adapter.transcribe(audio.astype(np.float32) / 32768.0, sample_rate=16000)["text"]
        except Exception:  # noqa: BLE001 — пустой ответ GigaAM на короткий клип = «не распознано»
            return ""

    model = sk.SttKeywordModel(transcribe, cooldown_sec=0.0, max_per_minute=100_000)
    rows = [json.loads(line) for line in (args.dataset / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    random.Random(args.seed).shuffle(rows)
    frame = sk.FRAME_SAMPLES
    stats: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        path = args.dataset / Path(row["file"]).name  # только имя файла: manifest не должен уводить из каталога
        audio = _read_wav(path)
        signal = np.concatenate([np.zeros(16000, np.int16), audio, np.zeros(16000, np.int16)])
        signal = signal[: len(signal) // frame * frame]
        fired = False
        for i in range(0, len(signal), frame):
            fired |= model.predict(signal[i:i + frame])[sk.DEFAULT_NAME] >= 0.5
            if (i // frame) % 8 == 0:
                model.drain(10)
        model.drain(10)
        fired |= model.predict(np.zeros(frame, np.int16))[sk.DEFAULT_NAME] >= 0.5
        block = block_label(row["block"], row.get("prompt", "")) if row["label"] == "neg" else row["block"]
        entry = stats[(row["label"], block)]
        entry[0] += 1
        entry[1] += int(fired)
    model.close()
    results = {k: (v[0], v[1]) for k, v in stats.items()}
    summary = summarize(results)
    print(format_report(results, summary))
    return 0 if accepted(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
