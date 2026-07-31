"""S3/Задача 5 (+ R2-фикс 4, финальное ревью): посчитанный бюджет останова
REST укладывается в ExitTimeOut.

Тест ТОЛЬКО арифметический: сумма ВСЕХ именованных констант, лежащих на
последовательном пути teardown между SIGTERM и выходом процесса, не должна
превышать ``ExitTimeOut`` из шаблона backend-плиста. Реальный риск (STT-запрос
в дренаже, скорость диска при компактировании под нагрузкой) этот тест НЕ
проверяет — он покрывается смоком SIGTERM под нагрузкой (S3/Задача 10), а не
арифметикой.

R2-фикс 4: до этой правки сумма считала только 3 слагаемых
(``_IPC_DRAIN_BUDGET_SEC`` + ``SHUTDOWN_JOIN_TIMEOUT_SEC`` +
``REST_DRAIN_BUDGET_SEC`` = 15.0, РОВНО ExitTimeOut=15 — нулевой запас) и
МОЛЧА пропускала три других слагаемых того же последовательного пути,
добавленных этой же волной:

    _shutdown_backend (service.py), ДО IPC-дренажа:
        RestWatchdog.stop() -> thread.join(timeout=STOP_JOIN_TIMEOUT_SEC)   [rest_watchdog.py]

    BackendService.close() (service.py), ПОСЛЕ IPC-дренажа:
        EventBridge.stop() -> thread.join(timeout=STOP_JOIN_TIMEOUT_SEC)    [event_bridge.py]
        InProcessRestServer.stop() внутри себя ТАКЖЕ ждёт барьер входа в
        serve_forever() (SERVE_ENTER_TIMEOUT_SEC) ПЕРЕД REST_DRAIN_BUDGET_SEC/
        SHUTDOWN_JOIN_TIMEOUT_SEC, которые уже считались.

Все шесть слагаемых лежат на ОДНОМ последовательном пути (см. порядок вызовов
в _shutdown_backend/BackendService.close(), service.py) — сумма именно
складывается, не перекрывается параллельно.
"""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import _IPC_DRAIN_BUDGET_SEC  # noqa: E402
from backend.rest_inprocess import (  # noqa: E402
    REST_DRAIN_BUDGET_SEC,
    SERVE_ENTER_TIMEOUT_SEC,
    SHUTDOWN_JOIN_TIMEOUT_SEC,
)
from backend.rest_watchdog import STOP_JOIN_TIMEOUT_SEC as _WATCHDOG_STOP_JOIN_TIMEOUT_SEC  # noqa: E402
from backend.event_bridge import STOP_JOIN_TIMEOUT_SEC as _EVENT_BRIDGE_STOP_JOIN_TIMEOUT_SEC  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "KrabEar" / "launchagents" / "ai.krab.ear.backend.plist.template"


def _exit_timeout() -> float:
    # plistlib не понимает XML-комментарии со style-doctype без валидного XML —
    # см. test_backend_plist_data_dir_parity_S3.py для того же паттерна.
    plist = plistlib.loads(TEMPLATE.read_bytes())
    return float(plist["ExitTimeOut"])


def test_rest_drain_budget_fits_remaining_exit_timeout() -> None:
    components = {
        "IPC drain (_IPC_DRAIN_BUDGET_SEC)": _IPC_DRAIN_BUDGET_SEC,
        "RestWatchdog.stop() join": _WATCHDOG_STOP_JOIN_TIMEOUT_SEC,
        "REST drain (REST_DRAIN_BUDGET_SEC)": REST_DRAIN_BUDGET_SEC,
        "REST serve-enter barrier (SERVE_ENTER_TIMEOUT_SEC)": SERVE_ENTER_TIMEOUT_SEC,
        "REST thread join (SHUTDOWN_JOIN_TIMEOUT_SEC)": SHUTDOWN_JOIN_TIMEOUT_SEC,
        "EventBridge.stop() join": _EVENT_BRIDGE_STOP_JOIN_TIMEOUT_SEC,
    }
    total = sum(components.values())
    exit_timeout = _exit_timeout()
    breakdown = " + ".join(f"{name}={value}" for name, value in components.items())
    assert total <= exit_timeout, (
        f"{total}с ({breakdown}) не помещается в ExitTimeOut={exit_timeout}с "
        "шаблона плиста — либо подними ExitTimeOut в шаблоне (и объясни "
        "новое значение комментарием), либо уменьши один из бюджетов"
    )
