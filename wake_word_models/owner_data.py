"""owner_data.py — чистые (stdlib-only) helpers для обучения на голосе владельца.

Вход: каталог записи ``scripts/record_wake_dataset.py`` (``manifest.jsonl`` + WAV 16 кГц
моно) и golden R2 (``ru_*.wav``/``es_*.wav``/``en_*.wav``). Здесь только разбор, деление
на train/val ПО КЛИПАМ, вырезание активного участка у позитивов и нарезка окон у
негативов. Тяжёлые вещи (аугментация, фичи, torch) живут в ``train_krab.py`` лениво.

Файл обязан импортироваться без numpy/torch/openwakeword (как и ``train_krab.py``):
юнит-тесты гоняются на окружении без тренировочного стека.

Приватность: функции не пишут и не логируют имена клипов/пути личных файлов выше
уровня счётчиков; запись WAV — только в рабочий каталог, который выбирает вызывающий.
"""

from __future__ import annotations

import array
import json
import math
import random
import re
import wave
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

SAMPLE_RATE = 16000
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.wav$")
_GOLDEN_NAME = re.compile(r"^(ru|es|en)_\d+\.wav$")


class OwnerDataError(ValueError):
    """Данные владельца не годятся (формат, имя, метка)."""


@dataclass(frozen=True)
class OwnerClip:
    path: Path
    label: str  # "pos" | "neg"
    block: str


def _safe_join(root: Path, name: str) -> Path:
    """Имя файла из manifest — не путь: без разделителей, ``..`` и симлинков наружу."""
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        raise OwnerDataError("manifest: недопустимое имя файла")
    path = root / name
    if path.is_symlink():
        raise OwnerDataError("manifest: симлинк вместо файла")
    return path


def load_owner_manifest(dataset_dir: Path) -> list[OwnerClip]:
    """Читает ``manifest.jsonl``; файлы, которых нет на диске, пропускаются (счётчик — вызывающему).

    Дубли по имени файла берутся последней строкой (скрипт записи переписывает клип).
    """
    root = Path(dataset_dir)
    manifest = root / "manifest.jsonl"
    if not manifest.is_file():
        raise OwnerDataError("manifest.jsonl не найден")
    latest: dict[str, dict] = {}
    for lineno, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise OwnerDataError(f"manifest: строка {lineno} не JSON") from exc
        if not isinstance(row, dict):
            raise OwnerDataError(f"manifest: строка {lineno} не объект")
        latest[str(row.get("file"))] = row
    clips: list[OwnerClip] = []
    for name, row in latest.items():
        label = row.get("label")
        if label not in ("pos", "neg"):
            raise OwnerDataError("manifest: метка должна быть pos или neg")
        if row.get("rate") != SAMPLE_RATE:
            raise OwnerDataError("manifest: частота не 16 кГц")
        path = _safe_join(root, name)
        if not path.is_file():
            continue
        block = row.get("block")
        clips.append(OwnerClip(path=path, label=str(label), block=str(block) if block else "unknown"))
    return sorted(clips, key=lambda c: (c.block, c.path.name))


def list_golden_clips(golden_dir: Path) -> list[Path]:
    """Только ``ru_/es_/en_NNN.wav``: служебные файлы того же каталога не берём."""
    root = Path(golden_dir)
    if not root.is_dir():
        return []
    out = [p for p in root.iterdir() if _GOLDEN_NAME.match(p.name) and p.is_file() and not p.is_symlink()]
    return sorted(out)


def split_clips(clips: Sequence[OwnerClip], *, val_ratio: float = 0.2,
                seed: int = 13) -> tuple[list[OwnerClip], list[OwnerClip]]:
    """Деление train/val СТРОГО по клипам, стратифицированно по блоку.

    Каждый блок с >= 2 клипами отдаёт в val минимум один (иначе val не покрывает
    «шёпот»/«далеко» и т.п.); блок из одного клипа целиком уходит в train. Один клип
    не попадает в обе части. Детерминировано по seed.
    """
    if not 0.0 < val_ratio < 1.0:
        raise OwnerDataError("val_ratio должен быть в (0, 1)")
    by_block: dict[str, list[OwnerClip]] = defaultdict(list)
    for clip in clips:
        by_block[clip.block].append(clip)
    train: list[OwnerClip] = []
    val: list[OwnerClip] = []
    for block in sorted(by_block):
        group = sorted(by_block[block], key=lambda c: c.path.name)
        random.Random(f"{seed}:{block}").shuffle(group)
        if len(group) < 2:
            train.extend(group)
            continue
        n_val = max(1, int(round(len(group) * val_ratio)))
        n_val = min(n_val, len(group) - 1)
        val.extend(group[:n_val])
        train.extend(group[n_val:])
    return train, val


def read_wav_mono16(path: Path) -> list[int]:
    """Сэмплы int16 моно 16 кГц; иной формат — ошибка, а не молчаливое преобразование."""
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != SAMPLE_RATE:
            raise OwnerDataError("ожидается WAV 16 кГц моно 16 бит")
        raw = wav.readframes(wav.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return list(samples)


def write_wav_mono16(path: Path, samples: Sequence[int]) -> None:
    """Атомарная запись (tmp + replace): оборванная запись не оставляет битый клип."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    data = array.array("h", [max(-32768, min(32767, int(s))) for s in samples])
    with wave.open(str(tmp), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(data.tobytes())
    tmp.replace(path)


def _frame_rms(samples: Sequence[int], frame: int) -> list[float]:
    out: list[float] = []
    for start in range(0, len(samples) - frame + 1, frame):
        chunk = samples[start:start + frame]
        out.append(math.sqrt(sum(s * s for s in chunk) / frame) / 32768.0)
    return out


def active_region(samples: Sequence[int], *, frame_ms: int = 25, rel_thr: float = 0.25,
                  abs_thr: float = 0.01, pad_ms: int = 120) -> tuple[int, int] | None:
    """Границы речи (сэмплы): кадры выше max(abs_thr, rel_thr * пик огибающей) плюс запас.

    ``None`` — речи нет (тишина/тихий клип): такой позитив обучать нельзя.
    """
    frame = SAMPLE_RATE * frame_ms // 1000
    env = _frame_rms(samples, frame)
    if not env:
        return None
    thr = max(abs_thr, rel_thr * max(env))
    idx = [i for i, e in enumerate(env) if e > thr]
    if not idx:
        return None
    pad = SAMPLE_RATE * pad_ms // 1000
    start = max(0, idx[0] * frame - pad)
    end = min(len(samples), (idx[-1] + 1) * frame + pad)
    return start, end


def trim_positive(samples: Sequence[int], *, max_len: int = 28000) -> list[int] | None:
    """Позитив → активный участок с запасом (augment_clips дополняет слева нулями до total_length).

    Длиннее ``max_len`` режем с конца: слово-триггер говорится в конце фразы, а запас справа
    (до 200 мс) augment_clips добавляет сам. ``None`` — речи нет.
    """
    region = active_region(samples)
    if region is None:
        return None
    start, end = region
    clip = list(samples[start:end])
    if len(clip) > max_len:
        clip = clip[-max_len:]
    return clip


def crop_windows(samples: Sequence[int], *, window: int = 32000, hop: int = 8000) -> list[list[int]]:
    """Окна фиксированной длины для негативов; короче окна — один клип как есть (если не пуст)."""
    if not samples:
        return []
    if len(samples) <= window:
        return [list(samples)]
    out = []
    for start in range(0, len(samples) - window + 1, hop):
        out.append(list(samples[start:start + window]))
    tail_start = len(samples) - window
    if (tail_start % hop) != 0:
        out.append(list(samples[tail_start:]))
    return out


def short_segments(samples: Sequence[int], *, count: int, min_len: int = 4800, max_len: int = 12800,
                   seed: int = 13) -> list[list[int]]:
    """Короткие отрезки речи (0.3–0.8 с) — негативы ТОГО ЖЕ вида, что позитив «Краб».

    ``augment_clips`` дополняет короткие клипы слева нулями до окна, то есть ставит их в КОНЕЦ окна.
    Позитив — короткий всплеск речи в конце окна; если негативы только длинные сплошные окна, модель
    учит «всплеск речи после паузы», а не слово, и срабатывает на любой речи. Короткие отрезки обычной
    речи в том же положении снимают этот сдвиг. Отрезок целиком внутри активного участка, без тишины.
    """
    region = active_region(samples, pad_ms=0)
    if region is None or count <= 0:
        return []
    start, end = region
    rng = random.Random(seed)
    out: list[list[int]] = []
    for _ in range(count):
        length = rng.randint(min_len, max_len)
        if end - start <= length:
            seg = list(samples[start:end])
        else:
            first = rng.randint(start, end - length)
            seg = list(samples[first:first + length])
        if seg and active_region(seg) is not None:
            out.append(seg)
    return out


def prepare_owner_clips(clips: Iterable[OwnerClip], out_dir: Path, *, prefix: str,
                        short_per_clip: int = 0,
                        short_block_prefixes: Sequence[str] = ()) -> dict[str, int]:
    """Пишет подготовленные клипы в ``out_dir`` под обезличенными именами ``prefix_NNNN.wav``.

    Позитивы — вырезанный активный участок (тихие/пустые пропускаются), негативы — окна; для негативов
    из блоков ``short_block_prefixes`` дополнительно ``short_per_clip`` коротких отрезков речи
    (``short_segments``). Возвращает счётчики: ``in``, ``out``, ``skipped_silent``, ``segments``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counters = {"in": 0, "out": 0, "skipped_silent": 0, "segments": 0}
    n = 0
    for clip in clips:
        counters["in"] += 1
        samples = read_wav_mono16(clip.path)
        if clip.label == "pos":
            trimmed = trim_positive(samples)
            pieces = [trimmed] if trimmed else []
            if not pieces:
                counters["skipped_silent"] += 1
        else:
            pieces = crop_windows(samples)
            if short_per_clip > 0 and clip.block.startswith(tuple(short_block_prefixes)):
                segs = short_segments(samples, count=short_per_clip, seed=n + 1)
                counters["segments"] += len(segs)
                pieces = pieces + segs
        for piece in pieces:
            write_wav_mono16(out_dir / f"{prefix}_{n:04d}.wav", piece)
            n += 1
            counters["out"] += 1
    return counters
