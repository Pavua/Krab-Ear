"""Определение аппаратного профиля Mac для автокалибровки STT.

Читает chip/RAM/CPU через sysctl — только чтение, без сайд-эффектов.
Mock-friendly: inject ``_sysctl_reader`` callable в ``detect_hardware_profile()``.

Tier classification:
    low   < 16 GB unified memory
    mid   16–32 GB
    high  > 32 GB
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier thresholds (GB)
# ---------------------------------------------------------------------------

_TIER_LOW_MAX_GB = 16
_TIER_HIGH_MIN_GB = 32

TIER_LOW = "low"
TIER_MID = "mid"
TIER_HIGH = "high"


@dataclass
class HardwareProfile:
    """Аппаратный профиль текущей машины."""
    chip: str           # "Apple M1 Pro" / "Intel Core i9" / "unknown"
    ram_gb: int         # Unified/physical RAM in GB (rounded)
    cores: int          # Logical CPU cores
    is_apple_silicon: bool
    tier: str           # "low" | "mid" | "high"
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chip": self.chip,
            "ram_gb": self.ram_gb,
            "cores": self.cores,
            "is_apple_silicon": self.is_apple_silicon,
            "tier": self.tier,
        }


# ---------------------------------------------------------------------------
# Default sysctl reader (subprocess-based, works on macOS)
# ---------------------------------------------------------------------------

def _default_sysctl_reader(key: str) -> str:
    """Читает sysctl ключ и возвращает строку-значение; пустая строка при ошибке."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", key],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as exc:  # pragma: no cover  # noqa: BLE001
        logger.debug("sysctl %s failed: %s", key, exc)
    return ""


# ---------------------------------------------------------------------------
# Core detection logic (injectable for tests)
# ---------------------------------------------------------------------------

def _classify_tier(ram_gb: int) -> str:
    if ram_gb < _TIER_LOW_MAX_GB:
        return TIER_LOW
    if ram_gb <= _TIER_HIGH_MIN_GB:
        return TIER_MID
    return TIER_HIGH


def detect_hardware_profile(
    sysctl_reader: Callable[[str], str] | None = None,
) -> HardwareProfile:
    """Определяет аппаратный профиль Mac.

    Args:
        sysctl_reader: callable(key) -> str.  Если None — использует реальный
            subprocess sysctl.  Инжектируется в тестах для изоляции от OS.

    Returns:
        HardwareProfile с заполненными полями.  При успешном чтении sysctl
        tier определяется реальным объёмом RAM (1 GB → tier=low).
        При любой *исключительной* ошибке (sysctl недоступен, subprocess упал)
        возвращает консервативный default: tier=mid, ram_gb=16 —
        автокалибровка не должна ломать запуск.
    """
    reader = sysctl_reader or _default_sysctl_reader

    try:
        # RAM
        mem_bytes_str = reader("hw.memsize")
        mem_bytes = int(mem_bytes_str) if mem_bytes_str.isdigit() else 0
        ram_gb = max(1, mem_bytes // (1024 ** 3))

        # CPU cores
        ncpu_str = reader("hw.logicalcpu")
        cores = int(ncpu_str) if ncpu_str.isdigit() else 1

        # Chip / brand string
        # macOS: "machdep.cpu.brand_string" works on both Intel and Apple Silicon
        brand = reader("machdep.cpu.brand_string")
        # hw.model gives "MacBookPro18,1" style; brand_string is more readable
        hw_model = reader("hw.model")

        # Detect Apple Silicon: brand_string contains "Apple M" on M-series;
        # on Intel it contains "Intel".
        is_apple_silicon = "Apple" in brand and "Intel" not in brand
        # Fallback: hw.model starts with "Mac" and lacks "Intel" label
        if not brand:
            is_apple_silicon = bool(hw_model) and "Intel" not in hw_model

        chip = brand if brand else (hw_model if hw_model else "unknown")
        tier = _classify_tier(ram_gb)

        raw = {
            "hw.memsize": mem_bytes_str,
            "hw.logicalcpu": ncpu_str,
            "machdep.cpu.brand_string": brand,
            "hw.model": hw_model,
        }

        profile = HardwareProfile(
            chip=chip,
            ram_gb=ram_gb,
            cores=cores,
            is_apple_silicon=is_apple_silicon,
            tier=tier,
            raw=raw,
        )
        logger.debug(
            "hardware_profile: chip=%s ram_gb=%d cores=%d as=%s tier=%s",
            chip, ram_gb, cores, is_apple_silicon, tier,
        )
        return profile

    except Exception as exc:  # noqa: BLE001
        logger.warning("detect_hardware_profile failed, using safe default: %s", exc)
        return HardwareProfile(
            chip="unknown",
            ram_gb=16,
            cores=4,
            is_apple_silicon=False,
            tier=TIER_MID,
        )
