"""Гейт второго MLX Whisper-чекпоинта при давлении памяти.

Живой инцидент 2026-08-16: REST грузил turbo → large-v3-mlx → turbo снова
при confidence < 0.65, параллельно с LM Studio на 36 ГБ → SIGSEGV в
потоке whisper-large-v3-turbo. LM Studio наш mlx_inter_process_lock не знает.

Пробa: ``kern.memorystatus_vm_pressure_level`` (0=normal, 1=warn, 2=urgent, 4=critical).
Не Darwin / ошибка / мусор → None → не скип (Linux CI без сюрпризов).
Env ``KRAB_EAR_STT_SKIP_SECOND_MLX=1|0`` перекрывает пробу.

Живой инцидент 2026-09-05: Darwin сидел на warning=1 при swap ~19.5/20 ГБ.
``is_memory_distress`` подтверждает warning свопом/доступной RAM — level=1
один не считается distress (мягкое предупреждение).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

_PRESSURE_SYSCTL = "kern.memorystatus_vm_pressure_level"
_SWAP_SYSCTL = "vm.swapusage"
_ENV_FORCE = "KRAB_EAR_STT_SKIP_SECOND_MLX"

# Пороги подтверждения warning=1. Инцидент: 19.5/20 ГБ (~97%).
_SWAP_DISTRESS_RATIO = 0.85
_SWAP_DISTRESS_GB = 16.0
_AVAILABLE_DISTRESS_GB = 0.75

_SWAPUSAGE_RE = re.compile(
    r"total\s*=\s*([\d.]+)\s*([MG])B?\s+used\s*=\s*([\d.]+)\s*([MG])B?",
    re.IGNORECASE,
)


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


@dataclass(frozen=True)
class HostMemoryStats:
    """Снимок хоста для подтверждения memory distress. Без sysctl-уровня."""

    swap_used_gb: float
    swap_total_gb: float
    available_gb: float | None = None


def parse_vm_swapusage(raw: str) -> tuple[float, float] | None:
    """Разбор ``sysctl vm.swapusage``. Возвращает (used_gb, total_gb) или None."""
    if not raw:
        return None
    match = _SWAPUSAGE_RE.search(raw)
    if match is None:
        return None
    total = _to_gb(float(match.group(1)), match.group(2))
    used = _to_gb(float(match.group(3)), match.group(4))
    return (used, total)


def _to_gb(value: float, unit: str) -> float:
    if unit.upper() == "G":
        return value
    return value / 1024.0


def host_memory_stats() -> HostMemoryStats | None:
    """Своп + доступная RAM. Ошибка/не Darwin → None (нечем подтверждать warning)."""
    swap = _read_swapusage()
    if swap is None:
        return None
    used, total = swap
    return HostMemoryStats(
        swap_used_gb=used,
        swap_total_gb=total,
        available_gb=_read_available_gb(),
    )


def is_memory_distress(level: int, stats: HostMemoryStats | None) -> bool:
    """True, если машина в реальном memory distress.

    level>=2 (urgent/critical) — достаточно sysctl.
    level==1 (warning) — только вместе со свопом у потолка или почти нулевой
    доступной RAM. level==1 без улик — мягкое предупреждение, не OOM.
    """
    if level >= 2:
        return True
    if level < 1:
        return False
    if stats is None:
        return False
    if stats.swap_total_gb > 0 and (
        stats.swap_used_gb / stats.swap_total_gb
    ) >= _SWAP_DISTRESS_RATIO:
        return True
    if stats.swap_used_gb >= _SWAP_DISTRESS_GB:
        return True
    if (
        stats.available_gb is not None
        and stats.available_gb < _AVAILABLE_DISTRESS_GB
        and stats.swap_used_gb > 1.0
    ):
        return True
    return False


def _read_swapusage() -> tuple[float, float] | None:
    try:
        proc = subprocess.run(
            ["sysctl", "-n", _SWAP_SYSCTL],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_vm_swapusage((proc.stdout or "").strip())


def _read_available_gb() -> float | None:
    try:
        proc = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE") or 4096)
    except (OSError, ValueError, TypeError):
        page_size = 4096
    free_pages = 0
    inactive_pages = 0
    found_free = False
    for line in (proc.stdout or "").splitlines():
        if "Pages free" in line:
            free_pages = _first_int(line)
            found_free = True
        elif "Pages inactive" in line:
            inactive_pages = _first_int(line)
    if not found_free:
        return None
    return (free_pages + inactive_pages) * page_size / (1024 ** 3)


def _first_int(line: str) -> int:
    match = re.search(r"(\d+)", line.split(":", 1)[-1])
    return int(match.group(1)) if match else 0


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
