"""SoftResourceLimits/NumberOfFiles в ``ai.krab.ear.backend.plist.template`` (Sentry KRAB-EAR-BACKEND-24).

Без явного ``SoftResourceLimits`` launchd-юнит наследует дефолтный soft-лимит
открытых файловых дескрипторов macOS (`launchctl limit maxfiles` = 256, живая
проверка 2026-08-08) — тонкий запас для backend-процесса, который держит
десятки file-backed NDJSON-хранилищ (history + 6 override-файлов + settings +
error_bus/translation_cache/webhook_manager/...) плюс IPC socket-соединения
плюс per-lock fd (``StateStore._lock()`` открывает fd на flock). Живой
инцидент: ``OSError: [Errno 24] Too many open files: '.../history.lock'`` под
system-wide overload (culprit ``backend.state_store in _lock``).

RED до правки: ключа ``SoftResourceLimits`` в шаблоне нет вовсе — тест падает
на ``KeyError``, а не молчаливым несовпадением.
"""

from __future__ import annotations

import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "KrabEar" / "launchagents" / "ai.krab.ear.backend.plist.template"

# macOS launchd default soft limit (`launchctl limit maxfiles`) — the floor
# this fix must clear with real headroom, not just nominally exceed.
_DEFAULT_LAUNCHD_SOFT_LIMIT = 256


def _load_template() -> dict:
    return plistlib.loads(TEMPLATE.read_bytes())


def test_soft_resource_limits_raises_number_of_files_above_launchd_default() -> None:
    plist = _load_template()

    assert "SoftResourceLimits" in plist, (
        "шаблон обязан задавать SoftResourceLimits — иначе backend наследует "
        f"дефолтный launchd soft-лимит ({_DEFAULT_LAUNCHD_SOFT_LIMIT} файлов), "
        "см. Sentry KRAB-EAR-BACKEND-24"
    )
    limits = plist["SoftResourceLimits"]
    assert "NumberOfFiles" in limits, "SoftResourceLimits обязан содержать NumberOfFiles"

    number_of_files = limits["NumberOfFiles"]
    assert isinstance(number_of_files, int), "NumberOfFiles обязан быть целым числом (<integer> в plist)"
    assert number_of_files > _DEFAULT_LAUNCHD_SOFT_LIMIT * 4, (
        f"NumberOfFiles={number_of_files} недостаточно выше дефолтного launchd "
        f"лимита ({_DEFAULT_LAUNCHD_SOFT_LIMIT}) — нужен реальный запас, не "
        "символическая надбавка"
    )
