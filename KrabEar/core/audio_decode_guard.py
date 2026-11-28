"""Чтение аудио с потолком ДЕКОДИРОВАННОГО PCM (F03, ревью #2329).

Пределы по байтам на диске (снимок 1 ГиБ, analyze_file 500 МБ) не ограничивают
память: сжатый файл раскрывается многократно. 30 минут стерео-тишины 48 кГц во
FLAC занимают 335 КБ, а декодируются в 659 МБ float32; пик RSS обработчиков
quality/silence/profile_noise был 1.4–2.4 ГБ (анализаторы делают ещё ~2–3
копии массива).

`read_audio_capped` проверяет число сэмплов (кадры × каналы) по заголовку ДО
декодирования и держит тот же потолок на фактически прочитанных кадрах (если
заголовок занижен). Отказ — `ValueError` с понятным текстом, без тихого None.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 128 Mi сэмплов = 512 МиБ float32. С копиями анализаторов (~3.6× по замеру)
# это ~1.8 ГБ пика. Покрывает ~2 ч 20 мин записи 16 кГц моно (типичная запись
# Ear), ~46 мин 48 кГц моно, ~23 мин 48 кГц стерео. Модульная константа, не
# пользовательская настройка.
MAX_DECODED_SAMPLES = 128 * 1024 * 1024


def _refuse(path: Path | str, samples: int, limit: int) -> ValueError:
    return ValueError(
        f"Аудио {Path(path).name}: {samples} декодированных сэмплов больше предела "
        f"{limit} (~{limit * 4 // (1024 * 1024)} МБ float32) — анализ отклонён"
    )


def read_audio_capped(
    path: Path | str,
    *,
    dtype: str = "float32",
    always_2d: bool = False,
    max_samples: int | None = None,
) -> tuple[Any, int]:
    """Аналог `sf.read(path, dtype=..., always_2d=...)` с потолком декодированного PCM.

    Raises:
        ValueError: заголовок или фактически прочитанные данные превышают предел.
        RuntimeError/soundfile.LibsndfileError: файл не читается (как у sf.read).
    """
    import soundfile as sf

    limit = MAX_DECODED_SAMPLES if max_samples is None else max_samples
    with sf.SoundFile(str(path)) as f:
        channels = max(int(f.channels), 1)
        max_frames = limit // channels
        declared = int(f.frames)
        if declared < 0 or declared > max_frames:
            raise _refuse(path, max(declared, 0) * channels, limit)
        # Просим на один кадр больше предела: soundfile отдаёт не больше
        # объявленного, но если вернёт больше — это ловится ниже, а память
        # уже ограничена max_frames + 1.
        data = f.read(max_frames + 1, dtype=dtype, always_2d=always_2d)
        got = len(data)
        if got > max_frames:
            raise _refuse(path, got * channels, limit)
        return data, int(f.samplerate)
