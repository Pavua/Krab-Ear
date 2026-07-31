"""S3/Задача 5: посчитанный бюджет дренажа REST укладывается в ExitTimeOut.

Тест ТОЛЬКО арифметический: сумма именованных констант
(``_IPC_DRAIN_BUDGET_SEC`` + ``SHUTDOWN_JOIN_TIMEOUT_SEC`` +
``REST_DRAIN_BUDGET_SEC``) не должна превышать ``ExitTimeOut`` из шаблона
backend-плиста. Реальный риск (STT-запрос в дренаже, скорость диска при
компактировании под нагрузкой) этот тест НЕ проверяет — он покрывается
смоком SIGTERM под нагрузкой (S3/Задача 10), а не арифметикой.
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
    SHUTDOWN_JOIN_TIMEOUT_SEC,
)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "KrabEar" / "launchagents" / "ai.krab.ear.backend.plist.template"


def _exit_timeout() -> float:
    # plistlib не понимает XML-комментарии со style-doctype без валидного XML —
    # см. test_backend_plist_data_dir_parity_S3.py для того же паттерна.
    plist = plistlib.loads(TEMPLATE.read_bytes())
    return float(plist["ExitTimeOut"])


def test_rest_drain_budget_fits_remaining_exit_timeout() -> None:
    total = _IPC_DRAIN_BUDGET_SEC + SHUTDOWN_JOIN_TIMEOUT_SEC + REST_DRAIN_BUDGET_SEC
    exit_timeout = _exit_timeout()
    assert total <= exit_timeout, (
        f"{total}с (IPC {_IPC_DRAIN_BUDGET_SEC} + join "
        f"{SHUTDOWN_JOIN_TIMEOUT_SEC} + REST {REST_DRAIN_BUDGET_SEC}) не "
        f"помещается в ExitTimeOut={exit_timeout}с шаблона плиста — либо "
        "подними ExitTimeOut в шаблоне (и объясни новое значение "
        "комментарием), либо уменьши один из бюджетов"
    )
