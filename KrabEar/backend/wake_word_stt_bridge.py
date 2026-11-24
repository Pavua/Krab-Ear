"""Мост «STT-споттер слова-пробуждения → уже загруженный GigaAM-воркер backend».

Споттер (``stt_keyword_spotter.SttKeywordModel``) принимает снаружи функцию ``transcribe(audio) -> str``. Эта функция
НЕ создаёт вторую копию модели и НЕ ждёт: она закрепляет тот же живой GigaAM-воркер, что и телефонный профиль Voice
Gateway (``STTRouter.reserve_gigaam_call``: без lazy-create, без ожидания).

Инварианты:
  * воркер занят (звонок, диктовка, прогрев) или не загружен → ``TranscriberUnavailable``: реплика пропускается и
    считается в статистике, споттер никого не задерживает;
  * распознавание ограничено дедлайном (``deadline_sec``, по умолчанию 1.5 с): диктовка, начавшаяся сразу после
    реплики, ждёт воркер не дольше дедлайна (адаптер при старте записи останавливается, новые реплики не берутся);
  * текст возвращается только вызывающему и нигде не сохраняется и не логируется.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from backend.stt_keyword_spotter import TranscriberUnavailable

logger = logging.getLogger("KrabEar.Backend.WakeWordSttBridge")

DEFAULT_DEADLINE_SEC = 1.5
SAMPLE_RATE = 16000


def make_router_transcriber(
    get_router: Callable[[], Optional[Any]], *, deadline_sec: float = DEFAULT_DEADLINE_SEC,
) -> Callable[[Any], str]:
    """``get_router()`` вызывается на КАЖДОЙ реплике: роутер может быть пересоздан, а на старте backend ещё не готов."""

    def transcribe(samples: Any) -> str:
        import numpy as np

        router = get_router()
        if router is None:
            raise TranscriberUnavailable("no_router")
        try:
            lease, status = router.reserve_gigaam_call()
        except Exception as exc:  # noqa: BLE001 — роутер закрывается/перенастраивается: реплика пропускается
            raise TranscriberUnavailable("reserve_failed") from exc
        if lease is None:
            raise TranscriberUnavailable(str(status))
        audio = np.asarray(samples, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            result = lease.run(audio, SAMPLE_RATE, time.monotonic() + deadline_sec)
        except Exception as exc:  # noqa: BLE001 — исключение может нести фрагмент текста: наружу только тип
            logger.warning("WakeWordSttBridge: распознавание не удалось (%s)", type(exc).__name__)
            raise TranscriberUnavailable("run_failed") from None
        finally:
            try:
                lease.release()
            except Exception:  # noqa: BLE001
                logger.warning("WakeWordSttBridge: release lease не удался")
        if not isinstance(result, dict) or result.get("status") != "ok" or not isinstance(result.get("text"), str):
            raise TranscriberUnavailable("not_ok")
        return result["text"]

    return transcribe
