"""Сенсор давления памяти: sysctl + своп/доступная RAM (инцидент 2026-09-05).

Живой инцидент 15:33–15:37: GigaAM SIGKILL, а MemoryConductor пропустил
релиф, потому что kern.memorystatus_vm_pressure_level=1 (гейт хотел >=2),
при этом swap ~19.5/20 ГБ. Darwin может сидеть на warning=1 бесконечно.

Контракт:
- level>=2 — distress без доп. улик (urgent/critical);
- level=1 + высокий своп (или эквивалент) — distress;
- level=1 + здоровая память — НЕ distress (мягкое предупреждение);
- enforce-флаги кондуктора по умолчанию остаются False.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.memory_conductor import MemoryConductor  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402
from core.mlx_memory_gate import (  # noqa: E402
    HostMemoryStats,
    is_memory_distress,
    parse_vm_swapusage,
)


_ENFORCE_KEYS = (
    "memory_conductor_enforce",
    "memory_conductor_enforce_gigaam",
    "memory_conductor_enforce_whisper",
    "memory_conductor_enforce_rewriter",
    "memory_conductor_enforce_brain",
    "memory_conductor_enforce_recording_sequence",
)


def _healthy() -> HostMemoryStats:
    return HostMemoryStats(swap_used_gb=0.4, swap_total_gb=20.0, available_gb=12.0)


def _swap_ceiling() -> HostMemoryStats:
    """Фикстура инцидента 2026-09-05: своп у потолка, RAM почти нет."""
    return HostMemoryStats(swap_used_gb=19.5, swap_total_gb=20.0, available_gb=0.4)


def _settings(**over):
    base = {
        "memory_conductor_enabled": True,
        "memory_conductor_enforce": False,
        "memory_conductor_enforce_brain": False,
        "gigaam_idle_unload_sec": 600.0,
        "whisper_idle_unload_sec": 900.0,
        "rewriter_idle_unload_sec": 1800.0,
        "memory_pressure_streak_ticks": 3,
        "memory_evict_cooldown_sec": 600.0,
        "llm_brain_model": "qwen/qwen3.6-27b",
        "llm_model": "gemma-4-e4b-it-mlx",
        "llm_base_url": "http://localhost:1234/v1",
        "mlx_oom_auto_unload_enabled": False,
    }
    base.update(over)
    svc = MagicMock()
    svc.cached_settings.return_value = base
    return svc


def _mk(*, pressure=0, host_stats=None, settings=None, recording=False):
    stats = host_stats if host_stats is not None else _healthy()
    return MemoryConductor(
        settings_service=settings or _settings(),
        ledger=MagicMock(),
        is_recording=lambda: recording,
        is_meeting_active=lambda: False,
        pressure_fn=lambda: pressure,
        host_stats_fn=lambda: stats,
        gigaam_close_if_idle=MagicMock(return_value=True),
        gigaam_idle_sec_fn=lambda: 0.0,
        last_stt_activity_ts_fn=lambda: time.monotonic(),
        tick_sec=0.05,
        unload_model_fn=MagicMock(),
        load_model_fn=MagicMock(),
        model_loaded_fn=MagicMock(return_value=False),
        lease_holder_fn=lambda: None,
        verify_timeout_sec=0.2,
        verify_poll_sec=0.02,
    )


class ParseSwapusageTests(unittest.TestCase):
    def test_parses_sysctl_n_megabytes(self) -> None:
        raw = "total = 20480.00M  used = 19968.00M  free = 512.00M  (encrypted)"
        used, total = parse_vm_swapusage(raw)
        self.assertAlmostEqual(total, 20.0, places=2)
        self.assertAlmostEqual(used, 19.5, places=2)

    def test_parses_prefixed_sysctl_line(self) -> None:
        raw = "vm.swapusage: total = 2.00G  used = 1.50G  free = 0.50G"
        used, total = parse_vm_swapusage(raw)
        self.assertAlmostEqual(total, 2.0, places=2)
        self.assertAlmostEqual(used, 1.5, places=2)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_vm_swapusage(""))
        self.assertIsNone(parse_vm_swapusage("not a swap line"))


class IsMemoryDistressTests(unittest.TestCase):
    def test_level_2_is_distress_without_stats(self) -> None:
        self.assertTrue(is_memory_distress(2, None))
        self.assertTrue(is_memory_distress(4, _healthy()))

    def test_level_1_plus_high_swap_is_distress(self) -> None:
        self.assertTrue(is_memory_distress(1, _swap_ceiling()))

    def test_level_1_healthy_memory_is_not_distress(self) -> None:
        self.assertFalse(is_memory_distress(1, _healthy()))

    def test_level_1_without_stats_is_not_distress(self) -> None:
        self.assertFalse(is_memory_distress(1, None))

    def test_level_0_even_with_high_swap_stays_conservative(self) -> None:
        """level=0 — не warning; высокий своп без sysctl-warning не открывает гейт."""
        self.assertFalse(is_memory_distress(0, _swap_ceiling()))

    def test_level_1_plus_critically_low_available_ram_is_distress(self) -> None:
        stats = HostMemoryStats(swap_used_gb=4.0, swap_total_gb=20.0, available_gb=0.4)
        self.assertTrue(is_memory_distress(1, stats))


class ConductorCorroborationTests(unittest.TestCase):
    def test_shadow_would_evict_brain_on_level_1_plus_high_swap(self) -> None:
        c = _mk(pressure=1, host_stats=_swap_ceiling())
        for _ in range(3):
            c.tick_once()
        d = c.get_diagnostics()
        self.assertGreaterEqual(d["pressure_streak"], 3)
        self.assertGreater(d["residents"]["brain"]["would"], 0)
        c.unload_model_fn.assert_not_called()

    def test_level_1_healthy_does_not_accumulate_streak(self) -> None:
        c = _mk(pressure=1, host_stats=_healthy())
        for _ in range(5):
            c.tick_once()
        d = c.get_diagnostics()
        self.assertEqual(d["pressure_streak"], 0)
        self.assertEqual(d["residents"]["brain"]["would"], 0)

    def test_oom_confirm_level_1_high_swap_is_distress(self) -> None:
        c = _mk(pressure=1, host_stats=_swap_ceiling())
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        c.wait_workers(1.0)
        d = c.get_diagnostics()
        self.assertGreater(d["residents"]["brain"]["would"], 0)
        self.assertEqual(d["residents"]["brain"]["skipped_gate"], 0)
        notes = " ".join(d["decisions"])
        self.assertNotIn("unconfirmed by pressure", notes)
        c.unload_model_fn.assert_not_called()

    def test_oom_confirm_level_1_healthy_still_skipped(self) -> None:
        c = _mk(pressure=1, host_stats=_healthy())
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        c.wait_workers(1.0)
        d = c.get_diagnostics()
        self.assertGreater(d["residents"]["brain"]["skipped_gate"], 0)
        self.assertEqual(d["residents"]["brain"]["would"], 0)
        c.unload_model_fn.assert_not_called()


class EnforceDefaultsStayOffTests(unittest.TestCase):
    def test_all_enforce_flags_false_in_default_settings(self) -> None:
        for key in _ENFORCE_KEYS:
            self.assertIn(key, DEFAULT_SETTINGS)
            self.assertIs(
                DEFAULT_SETTINGS[key],
                False,
                "%s must stay False (owner-only enforce)" % key,
            )


if __name__ == "__main__":
    unittest.main()
