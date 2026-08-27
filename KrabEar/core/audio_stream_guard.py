"""Guarded read для PortAudio-потоков: прерываемое ожидание кадров.

Спека: ``docs/superpowers/specs/2026-08-23-portaudio-unkillable-read-design.md``.

🔴 Корень класса (доказан sample зависшего процесса 2026-08-22): вызов
``stream.read(n)`` — это ``Pa_ReadStream``, реализованный как ``usleep``-цикл
БЕЗ таймаута. Он спит, пока ring buffer не наберёт ``n`` кадров. Если CoreAudio
не запустил IO-поток для стрима (в проде наблюдалось: ноль ``IOThread``/``HAL_``
тредов в процессе при успешном ``Pa_StartStream``), наполнять буфер некому —
чтение висит вечно. Цикл вида ``while not stop_event.is_set(): stream.read(...)``
проверяет событие ТОЛЬКО между чтениями, поэтому зависшее чтение делает поток
неубиваемым: ``join()`` не прерывает C-вызов.

Лекарство здесь: ждать кадры опросом ``read_available`` короткими шагами с
проверкой ``stop_event`` на каждом, и звать ``read()`` только когда кадры точно
есть. Дополнительной нагрузки это не создаёт: блокирующее чтение PortAudio и так
реализовано опросом со сном.

Модуль намеренно не импортирует ``sounddevice`` — работает с любым duck-type
объектом, у которого есть ``read_available`` (ubuntu-CI живёт без PortAudio).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

# Дефолты; вызывающий может переопределить (значения из настроек — см.
# DEFAULT_SETTINGS/_RANGE_FIELDS, там же клампы).
DEFAULT_POLL_SEC = 0.05
DEFAULT_STARVE_SEC = 3.0


class StreamStarved(RuntimeError):
    """Стрим не отдаёт кадры дольше порога и это НЕ легитимная пауза.

    Целевой случай — «мёртвый с рождения» стрим: открыт успешно, но IO-поток
    CoreAudio не запущен, поэтому кадров не будет никогда. Вызывающий обязан
    выйти из цикла (не лечить себя сам — владельцы перезапуска сессии внешние,
    см. §4.3 спеки) и пометить причину, чтобы сторож довёл её до эскалации.
    """


def _available_frames(stream: Any) -> int | None:
    """Сколько кадров готово. ``None`` — считать голоданием, не «гейт выключен».

    🔴 ``sounddevice`` бросает ``PortAudioError`` из ``read_available`` при
    отрицательном коде. Трактовать это как «не знаю, читаю блокирующе» нельзя:
    fail-open вернул бы ровно тот неубиваемый ``read()``, ради которого написан
    модуль (спека §4.4).
    """
    try:
        value = stream.read_available
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        # Не настоящее целое (подделанный стрим в тесте, экзотический бэкенд) —
        # честно считаем неизвестностью, а не разрешением на блокирующий read.
        return None
    return value


def wait_for_frames(
    stream: Any,
    frames: int,
    *,
    stop_event: threading.Event,
    poll_sec: float = DEFAULT_POLL_SEC,
    starve_sec: float = DEFAULT_STARVE_SEC,
    is_recording: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Ждёт ``frames`` доступных кадров, оставаясь прерываемым.

    Returns:
        True — кадры есть, ``stream.read(frames)`` не заблокируется.
        False — попросили остановиться (``stop_event``).

    Raises:
        StreamStarved: кадров нет дольше ``starve_sec`` и запись не идёт.
    """
    poll = max(0.001, float(poll_sec))      # 🔴 wait(timeout<=0) = CPU-spin
    starve = max(0.0, float(starve_sec))
    starving_since: float | None = None

    while True:
        if stop_event.is_set():
            return False

        available = _available_frames(stream)
        if available is not None and available >= frames:
            return True

        # Легитимные источники голодания сбрасывают таймер. Главный —
        # активная запись: meeting НЕ снимает wake-word слушатель, и адаптер
        # голодает всю встречу штатно (амендмент C2). Сработавший здесь
        # детектор открыл бы второй входной тап под записью — инцидент F6.
        if _recording_now(is_recording):
            starving_since = None
        else:
            now = clock()
            if starving_since is None:
                starving_since = now
            elif starve > 0 and (now - starving_since) >= starve:
                raise StreamStarved(
                    f"нет кадров дольше {starve:.1f}s "
                    f"(доступно={available!r}) — стрим не поставляет аудио"
                )

        if stop_event.wait(poll):
            return False


def _recording_now(is_recording: Callable[[], bool] | None) -> bool:
    """Идёт ли запись. Сбой датчика — fail-safe «идёт» (спека §4.2).

    Ложное «идёт» отложит детект до следующей проверки; ложное «нет» открыло бы
    второй микрофонный тап под живой диктовкой — цена ошибок несимметрична.
    """
    if is_recording is None:
        return False
    try:
        return bool(is_recording())
    except Exception:
        return True
