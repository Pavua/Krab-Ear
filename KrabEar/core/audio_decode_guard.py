"""Чтение аудио с потолком ДЕКОДИРОВАННОГО PCM (F03, ревью #2329).

Пределы по байтам на диске (снимок 1 ГиБ, analyze_file 500 МБ) не ограничивают
память: сжатый файл раскрывается многократно. 30 минут стерео-тишины 48 кГц во
FLAC занимают 335 КБ, а декодируются в 659 МБ float32; пик RSS обработчиков
quality/silence/profile_noise был 1.4–2.4 ГБ (анализаторы делают ещё ~2–3
копии массива).

`read_audio_capped` проверяет число сэмплов (кадры × каналы) по заголовку ДО
декодирования. Отказ — `ValueError` с понятным текстом, без тихого None.

Что держит память (замер 28.11 на soundfile 0.13.1 / libsndfile 1.2.2):
- Главная защита — проверка заголовка. Для seekable-файла (а через обработчики
  приходят только обычные файлы-снимки) soundfile сам режет чтение до
  `frames - tell()`, поэтому больше заявленного в заголовке не прочитать.
  Заниженный заголовок (WAV data-чанк, FLAC total_samples) отдаёт ровно
  заявленные кадры, остальное не декодируется: 20 минут под заголовком «1 с»
  дают 16000 кадров и ~0 МБ прироста RSS.
- Неизвестная длина приводит к отказу: FLAC с total_samples=0 libsndfile
  объявляет как SF_COUNT_MAX кадров. WAV с data=0xFFFFFFFF libsndfile
  обрезает до фактического размера файла, и предел применяется к нему.
- `read(max_frames + 1)` и проверка `got > max_frames` — страховка для
  не-seekable источников и нестандартных обёрток. Для seekable-файлов при
  нынешней проверке заголовка эта ветка недостижима. Она остаётся вторым
  рубежом, если проверку ослабят: у FLAC неизвестной длины libsndfile отдаёт
  столько кадров, сколько попросили (замер: read(32001) вернул 32001).

Известное ограничение: у не-seekable источника soundfile аллоцирует буфер на
весь запрошенный объём сразу, то есть до предела (~512 МиБ float32) ещё до
проверки. Через обработчики аналитики это недостижимо: они читают только
обычные файлы.
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
        ValueError: заголовок объявляет больше предела (или неизвестную длину);
            для не-seekable источника — если фактически прочитано больше.
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
        # Для seekable-файла soundfile сам режет запрос до заявленных кадров
        # (уже проверены ≤ max_frames), поэтому прочитано не больше заголовка.
        # Лишний кадр и проверка ниже — страховка для не-seekable источников и
        # нестандартных обёрток (см. докстринг модуля).
        data = f.read(max_frames + 1, dtype=dtype, always_2d=always_2d)
        got = len(data)
        if got > max_frames:
            raise _refuse(path, got * channels, limit)
        return data, int(f.samplerate)
