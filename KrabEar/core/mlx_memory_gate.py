"""Гейт второго MLX Whisper-чекпоинта при давлении памяти.

Живой инцидент 2026-08-16: REST грузил turbo → large-v3-mlx → turbo снова
при confidence < 0.65, параллельно с LM Studio на 36 ГБ → SIGSEGV в
потоке whisper-large-v3-turbo. LM Studio наш mlx_inter_process_lock не знает.

Пробa: ``kern.memorystatus_vm_pressure_level`` (0=normal, 1=warn, 2=urgent, 4=critical).
Не Darwin / ошибка / мусор → None → не скип (Linux CI без сюрпризов).
Env ``KRAB_EAR_STT_SKIP_SECOND_MLX=1|0`` перекрывает пробу.
"""
from __future__ import annotations

import os
import subprocess

_PRESSURE_SYSCTL = "kern.memorystatus_vm_pressure_level"
_ENV_FORCE = "KRAB_EAR_STT_SKIP_SECOND_MLX"


def vm_pressure_level() -> int | None:
    """macOS jetsam-уровень или None, если проба недоступна."""
    try:
        proc = subprocess.run(
            ["sysctl", "-n", _PRESSURE_SYSCTL],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def should_skip_second_mlx_checkpoint() -> bool:
    """True → не грузить второй MLX-чекпоинт в этом процессе."""
    force = os.environ.get(_ENV_FORCE, "").strip().lower()
    if force in ("1", "true", "yes"):
        return True
    if force in ("0", "false", "no"):
        return False
    level = vm_pressure_level()
    if level is None:
        return False
    return level >= 1
