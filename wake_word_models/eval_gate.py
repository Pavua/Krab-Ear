#!/usr/bin/env python3
"""eval_gate.py — гейт приёмки wake-word модели «Краб» ДО установки.

Меряет ONNX-модель openWakeWord на ОТЛОЖЕННЫХ клипах владельца и на синтетических
тишине/шуме, решает «принять / не принять» по порогам. Установку не делает.

Пороги (решение владельца 24.11, см. отчёт wake_word_models/report_krab_ru.md):
  * тишина и белый шум:    доля кадров со score > 0.5  <= 1%
  * речь (проверочные негативы владельца + golden): доля кадров <= 0.5%
  * проверочные негативы-клипы владельца: 0 срабатываний на клип
  * проверочные позитивы «Краб»: recall >= 90%

Чистая часть (``GateThresholds``, ``evaluate_gate``, ``format_report``) не требует numpy и
openwakeword — на ней тесты. В отчёт попадают только числа: ни имён клипов, ни путей к
личным файлам.

Запуск:
    PYTHONPATH=<work-dir>/site_extra python wake_word_models/eval_gate.py \\
        --model <work-dir>/artifacts/krab_ru_owner_v1.onnx --work-dir <work-dir> [--out report.md]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
FRAME = 1280          # 80 мс при 16 кГц — шаг openWakeWord
THRESHOLD = 0.5
REFRACTORY_FRAMES = 19  # ~1.5 с: повторные кадры одного срабатывания считаем одним


@dataclass(frozen=True)
class GateThresholds:
    silence_noise_frac: float = 0.01
    speech_frac: float = 0.005
    owner_neg_clip_hits: int = 0
    min_recall: float = 0.90
    min_pos_clips: int = 10  # меньше — гейт не информативен, это отказ, а не успех


def evaluate_gate(m: Mapping[str, float], thr: GateThresholds = GateThresholds()) -> tuple[bool, list[str]]:
    """``(принято, причины отказа)``. Не хватает метрики или пустой набор — это отказ."""
    failures: list[str] = []

    def need(key: str) -> float | None:
        value = m.get(key)
        if value is None:
            failures.append(f"нет метрики {key}")
        return value

    sil_frames, sil_over = need("silence_noise_frames"), need("silence_noise_over")
    sp_frames, sp_over = need("speech_frames"), need("speech_over")
    neg_clips, neg_hits = need("owner_neg_clips"), need("owner_neg_clip_hits")
    pos_clips, pos_hits = need("pos_clips"), need("pos_hits")
    if failures:
        return False, failures

    if sil_frames <= 0:
        failures.append("нет кадров тишины/шума для проверки")
    elif sil_over / sil_frames > thr.silence_noise_frac:
        failures.append(f"тишина/шум: {100 * sil_over / sil_frames:.2f}% кадров > {100 * thr.silence_noise_frac:.1f}%")
    if sp_frames <= 0:
        failures.append("нет кадров речи для проверки")
    elif sp_over / sp_frames > thr.speech_frac:
        failures.append(f"речь: {100 * sp_over / sp_frames:.2f}% кадров > {100 * thr.speech_frac:.1f}%")
    if neg_clips <= 0:
        failures.append("нет проверочных негативов владельца")
    elif neg_hits > thr.owner_neg_clip_hits:
        failures.append(f"негативы владельца: {int(neg_hits)} из {int(neg_clips)} клипов дали срабатывание")
    if pos_clips < thr.min_pos_clips:
        failures.append(f"проверочных позитивов {int(pos_clips)} < {thr.min_pos_clips}: проверка не информативна")
    elif pos_hits / pos_clips < thr.min_recall:
        failures.append(f"recall: {100 * pos_hits / pos_clips:.1f}% < {100 * thr.min_recall:.0f}%")
    return (not failures), failures


def format_report(m: Mapping[str, float], thr: GateThresholds, passed: bool, failures: list[str],
                  *, model_label: str) -> str:
    """Markdown только с числами (без имён клипов и путей)."""

    def pct(over: str, total: str) -> str:
        t = m.get(total) or 0
        return f"{100 * (m.get(over) or 0) / t:.2f}%" if t else "н/д"

    lines = [
        f"# Гейт приёмки wake-word модели {model_label}",
        "",
        f"**Итог: {'ПРИНЯТА' if passed else 'НЕ ПРИНЯТА'}**",
        "",
        "| Проверка | Результат | Порог |",
        "|---|---|---|",
        f"| Тишина и белый шум, доля кадров > {THRESHOLD} | {pct('silence_noise_over', 'silence_noise_frames')} "
        f"({int(m.get('silence_noise_frames', 0))} кадров) | <= {100 * thr.silence_noise_frac:.1f}% |",
        f"| Речь (проверочные негативы + golden), доля кадров | {pct('speech_over', 'speech_frames')} "
        f"({int(m.get('speech_frames', 0))} кадров) | <= {100 * thr.speech_frac:.1f}% |",
        f"| Проверочные негативы владельца со срабатыванием | {int(m.get('owner_neg_clip_hits', 0))} из "
        f"{int(m.get('owner_neg_clips', 0))} | <= {thr.owner_neg_clip_hits} |",
        f"| Recall «Краб» на проверочных позитивах | {int(m.get('pos_hits', 0))} из {int(m.get('pos_clips', 0))} "
        f"| >= {100 * thr.min_recall:.0f}% |",
        "",
    ]
    extras = [(k, m[k]) for k in ("pos_hits_gain_low", "pos_hits_gain_high", "stream_hours", "stream_triggers",
                                  "golden_all_frames", "golden_all_over") if k in m]
    if extras:
        lines.append("Справочно (не входит в гейт): " + ", ".join(f"{k}={v:g}" for k, v in extras))
        lines.append("")
    if failures:
        lines += ["Причины отказа:"] + [f"- {f}" for f in failures] + [""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Измерение (нужны numpy и openwakeword)
# ----------------------------------------------------------------------------

def _frame_scores(model: Any, key: str, pcm: Any) -> list[float]:
    model.reset()
    out: list[float] = []
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        out.append(float(model.predict(pcm[i:i + FRAME])[key]))
    return out


def _debounced_triggers(scores: list[float]) -> int:
    count = 0
    cooldown = 0
    for s in scores:
        if cooldown > 0:
            cooldown -= 1
            continue
        if s > THRESHOLD:
            count += 1
            cooldown = REFRACTORY_FRAMES
    return count


def measure(model_path: Path, work_dir: Path, golden_dir: Path | None, *, seed: int = 13) -> dict[str, float]:
    import numpy as np
    from openwakeword.model import Model

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import owner_data as od

    model = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
    key = list(model.models.keys())[0]
    rng = np.random.default_rng(seed)
    clips = Path(work_dir) / "owner_clips"
    m: dict[str, float] = {}

    # --- тишина и белый шум: 5 + 5 минут (сумма 10 мин), шум трёх уровней
    sec = 300
    silence = np.zeros(16000 * sec, dtype=np.int16)
    noise_parts = []
    for sigma in (0.002, 0.01, 0.04):
        noise_parts.append((rng.normal(0, sigma, 16000 * (sec // 3)) * 32767).clip(-32768, 32767).astype(np.int16))
    sil_scores = _frame_scores(model, key, silence)
    noise_scores = _frame_scores(model, key, np.concatenate(noise_parts))
    m["silence_noise_frames"] = len(sil_scores) + len(noise_scores)
    m["silence_noise_over"] = sum(s > THRESHOLD for s in sil_scores + noise_scores)

    # --- речь: проверочные негативы владельца (включая golden val), кадры и клипы
    neg_frames = neg_over = neg_clips = neg_hits = 0
    stream_scores: list[float] = []
    for path in sorted((clips / "neg_val").glob("*.wav")):
        pcm = np.array(od.read_wav_mono16(path), dtype=np.int16)
        scores = _frame_scores(model, key, pcm)
        neg_frames += len(scores)
        neg_over += sum(s > THRESHOLD for s in scores)
        neg_clips += 1
        neg_hits += any(s > THRESHOLD for s in scores)
        stream_scores += scores
    m["owner_neg_clips"], m["owner_neg_clip_hits"] = neg_clips, neg_hits
    m["speech_frames"], m["speech_over"] = neg_frames, neg_over

    # --- справочно: весь golden R2 (часть была в train — в гейт не входит)
    if golden_dir:
        g_frames = g_over = 0
        for path in od.list_golden_clips(Path(golden_dir)):
            scores = _frame_scores(model, key, np.array(od.read_wav_mono16(path), dtype=np.int16))
            g_frames += len(scores)
            g_over += sum(s > THRESHOLD for s in scores)
            stream_scores += scores
        m["golden_all_frames"], m["golden_all_over"] = g_frames, g_over

    # --- проверочные позитивы: клип с запасом тишины до и после; плюс усиление ×0.5 и ×2 справочно
    pos_paths = sorted((clips / "pos_val").glob("*.wav"))
    pad_head, pad_tail = np.zeros(16000, dtype=np.int16), np.zeros(8000, dtype=np.int16)
    hits = {1.0: 0, 0.5: 0, 2.0: 0}
    for path in pos_paths:
        pcm = np.array(od.read_wav_mono16(path), dtype=np.float32)
        for gain in hits:
            sig = np.concatenate([pad_head, (pcm * gain).clip(-32768, 32767).astype(np.int16), pad_tail])
            hits[gain] += any(s > THRESHOLD for s in _frame_scores(model, key, sig))
    m["pos_clips"], m["pos_hits"] = len(pos_paths), hits[1.0]
    m["pos_hits_gain_low"], m["pos_hits_gain_high"] = hits[0.5], hits[2.0]

    # --- справочно: срабатываний/час на сводном потоке (тишина + шум + речь)
    stream_scores = sil_scores + noise_scores + stream_scores
    m["stream_hours"] = round(len(stream_scores) * FRAME / 16000 / 3600, 4)
    m["stream_triggers"] = _debounced_triggers(stream_scores)
    return m


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Гейт приёмки wake-word модели «Краб» (без установки)")
    p.add_argument("--model", required=True, help="ONNX модель")
    p.add_argument("--work-dir", required=True, help="work-dir обучения на данных владельца (owner_clips/)")
    p.add_argument("--golden-dir", default=str(Path.home() / "Library/Application Support/KrabEar/golden"))
    p.add_argument("--out", default=None, help="Куда записать отчёт Markdown (числа, без имён клипов)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    model = Path(args.model).expanduser()
    work = Path(args.work_dir).expanduser()
    for need in ("neg_val", "pos_val"):
        if not (work / "owner_clips" / need).is_dir():
            print(f"нет {work / 'owner_clips' / need}: сначала --stage owner_features", file=sys.stderr)
            return 2
    metrics = measure(model, work, Path(args.golden_dir).expanduser() if args.golden_dir else None)
    thr = GateThresholds()
    passed, failures = evaluate_gate(metrics, thr)
    report = format_report(metrics, thr, passed, failures, model_label=model.stem)
    print(report)
    if args.out:
        Path(args.out).expanduser().write_text(report, encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
