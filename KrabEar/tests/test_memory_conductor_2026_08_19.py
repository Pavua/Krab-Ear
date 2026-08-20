"""MemoryConductor — умеренная лестница выгрузки (спека v2.1 §5, план T5).

Контракты, закреплённые здесь:
- shadow логирует решения, но НИКОГДА не зовёт исполнителей;
- гистерезис давления: ровно 3 подряд тика (2 недостаточно);
- запись блокирует gigaam и brain; встреча и чужая brain-лиза блокируют brain;
- verify=None (неизвестно) НЕ жжёт cooldown; verify-успех жжёт;
- секвенс recording-start: rewriter грузится только ПОСЛЕ подтверждённой выгрузки brain;
- mlx.oom без подтверждения давлением → skipped_gate;
- C-POLICY-SOURCE: модуль лестницы не читает леджер (нет read_all).
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


def _settings(**over):
    base = {
        "memory_conductor_enabled": True,
        "memory_conductor_enforce": True,  # тесты лестницы гоняем в enforce
        "gigaam_idle_unload_sec": 600.0,
        "whisper_idle_unload_sec": 900.0,
        "rewriter_idle_unload_sec": 1800.0,
        "memory_pressure_streak_ticks": 3,
        "memory_evict_cooldown_sec": 600.0,
        "llm_brain_model": "qwen/qwen3.6-27b",
        "llm_model": "gemma-4-e4b-it-mlx",
        "llm_base_url": "http://localhost:1234/v1",
    }
    base.update(over)
    svc = MagicMock()
    svc.cached_settings.return_value = base
    return svc


def _mk(settings=None, *, pressure=0, recording=False, meeting=False,
        lease=None, gigaam_idle=0.0, stt_ts=None, model_loaded=False):
    """Кондуктор с фейками; исполнители — MagicMock'и, возвращающие успех."""
    c = MemoryConductor(
        settings_service=settings or _settings(),
        ledger=MagicMock(),
        is_recording=lambda: recording,
        is_meeting_active=lambda: meeting,
        pressure_fn=lambda: pressure,
        gigaam_close_if_idle=MagicMock(return_value=True),
        gigaam_idle_sec_fn=lambda: gigaam_idle,
        last_stt_activity_ts_fn=lambda: (stt_ts if stt_ts is not None
                                         else time.monotonic()),
        tick_sec=0.05,
        unload_model_fn=MagicMock(),
        load_model_fn=MagicMock(),
        model_loaded_fn=MagicMock(return_value=model_loaded),
        lease_holder_fn=lambda: lease,
        verify_timeout_sec=0.2,
        verify_poll_sec=0.02,
    )
    return c


class ShadowTest(unittest.TestCase):
    def test_shadow_never_calls_executors(self):
        c = _mk(_settings(memory_conductor_enforce=False),
                pressure=4, gigaam_idle=10_000.0, stt_ts=0.0)
        for _ in range(5):
            c.tick_once()
        c.gigaam_close_if_idle.assert_not_called()
        c.unload_model_fn.assert_not_called()
        d = c.get_diagnostics()
        self.assertIsNotNone(d.get("shadow_since"))
        self.assertGreater(
            sum(r.get("would", 0) for r in d["residents"].values()), 0,
            "shadow обязан ЛОГИРОВАТЬ решения, иначе неделя тени слепа",
        )

    def test_per_resident_enforce_or(self):
        s = _settings(memory_conductor_enforce=False,
                      memory_conductor_enforce_gigaam=True)
        c = _mk(s, gigaam_idle=10_000.0)
        c.tick_once()
        c.gigaam_close_if_idle.assert_called_once()


class HysteresisTest(unittest.TestCase):
    def test_two_ticks_not_enough_three_fire(self):
        c = _mk(pressure=4)
        c.tick_once()
        c.tick_once()
        c.unload_model_fn.assert_not_called()
        c.tick_once()
        c.unload_model_fn.assert_called_once()

    def test_streak_resets_on_calm_tick(self):
        calm = {"v": 4}
        c = _mk()
        c._pressure_fn = lambda: calm["v"]
        c.tick_once()
        c.tick_once()
        calm["v"] = 0
        c.tick_once()
        calm["v"] = 4
        c.tick_once()
        c.tick_once()
        c.unload_model_fn.assert_not_called()


class GatesTest(unittest.TestCase):
    def test_recording_blocks_gigaam_and_brain(self):
        c = _mk(pressure=4, recording=True, gigaam_idle=10_000.0)
        for _ in range(4):
            c.tick_once()
        c.gigaam_close_if_idle.assert_not_called()
        c.unload_model_fn.assert_not_called()

    def test_meeting_blocks_brain(self):
        c = _mk(pressure=4, meeting=True)
        for _ in range(4):
            c.tick_once()
        c.unload_model_fn.assert_not_called()

    def test_foreign_lease_blocks_brain_counted(self):
        c = _mk(pressure=4, lease={"owner": "krab", "pid": 1})
        for _ in range(4):
            c.tick_once()
        c.unload_model_fn.assert_not_called()
        self.assertGreater(c.get_diagnostics()["residents"]["brain"]["skipped_gate"], 0)


class CooldownVerifyTest(unittest.TestCase):
    def test_verified_success_burns_cooldown(self):
        c = _mk(pressure=4, model_loaded=False)  # после unload список пуст → успех
        for _ in range(3):
            c.tick_once()
        c.wait_workers(2.0)
        self.assertEqual(c.unload_model_fn.call_count, 1)
        for _ in range(3):
            c.tick_once()
        c.wait_workers(2.0)
        self.assertEqual(c.unload_model_fn.call_count, 1, "cooldown не удержал")

    def test_verify_unknown_does_not_burn_cooldown(self):
        c = _mk(pressure=4)
        c.model_loaded_fn.return_value = None  # неизвестно
        for _ in range(3):
            c.tick_once()
        c.wait_workers(2.0)
        n1 = c.unload_model_fn.call_count
        self.assertEqual(n1, 1)
        for _ in range(3):
            c.tick_once()
        c.wait_workers(2.0)
        self.assertEqual(c.unload_model_fn.call_count, 2,
                         "unknown-исход не должен жечь cooldown")


class SequenceTest(unittest.TestCase):
    def test_rewriter_loads_only_after_brain_unload_verified(self):
        order = []
        c = _mk()
        c.unload_model_fn.side_effect = lambda *a, **k: order.append("unload")
        c.load_model_fn.side_effect = lambda *a, **k: order.append("load")
        c.model_loaded_fn.side_effect = lambda *a, **k: order.append("verify") or False
        c.on_recording_start()
        c.wait_workers(2.0)
        self.assertEqual(order[0], "unload")
        self.assertIn("verify", order)
        self.assertEqual(order[-1], "load")

    def test_sequence_shadow_gated(self):
        c = _mk(_settings(memory_conductor_enforce=False))
        c.on_recording_start()
        c.wait_workers(1.0)
        c.unload_model_fn.assert_not_called()
        c.load_model_fn.assert_not_called()


class OomTriggerTest(unittest.TestCase):
    def test_oom_without_pressure_is_skipped_gate(self):
        c = _mk(pressure=0)
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        c.wait_workers(1.0)
        c.unload_model_fn.assert_not_called()
        self.assertGreater(c.get_diagnostics()["residents"]["brain"]["skipped_gate"], 0)

    def test_oom_with_pressure_fires(self):
        c = _mk(pressure=4, model_loaded=False)
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        c.wait_workers(2.0)
        c.unload_model_fn.assert_called_once()

    def test_handler_returns_immediately_and_never_raises(self):
        c = _mk(pressure=4)
        c.unload_model_fn.side_effect = lambda *a, **k: time.sleep(1.0)
        t0 = time.monotonic()
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        self.assertLess(time.monotonic() - t0, 0.3)
        broken = MemoryConductor(
            settings_service=None, ledger=None, is_recording=None,
            is_meeting_active=None, pressure_fn=None,
            gigaam_close_if_idle=None, gigaam_idle_sec_fn=None,
            last_stt_activity_ts_fn=None,
        )
        broken.handle_oom_event("krab_error", {"code": "mlx.oom"})  # не бросает
        c.wait_workers(3.0)


class LegacyOomReliefTest(unittest.TestCase):
    """🔴 H1 финального гейта: прежний OOM-релиф был боевым по умолчанию —
    shadow-неделя не смеет молча снять взведённую страховочную сеть."""

    def test_shadow_with_legacy_flag_still_evicts_on_confirmed_oom(self):
        c = _mk(_settings(memory_conductor_enforce=False,
                          mlx_oom_auto_unload_enabled=True),
                pressure=4, model_loaded=False)
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        c.wait_workers(2.0)
        c.unload_model_fn.assert_called_once()

    def test_shadow_with_legacy_flag_off_only_counts_would(self):
        c = _mk(_settings(memory_conductor_enforce=False,
                          mlx_oom_auto_unload_enabled=False),
                pressure=4)
        c.handle_oom_event("krab_error", {"code": "mlx.oom"})
        c.wait_workers(2.0)
        c.unload_model_fn.assert_not_called()
        self.assertGreater(c.get_diagnostics()["residents"]["brain"]["would"], 0)


class ReloadGateTest(unittest.TestCase):
    def test_reload_allowed_true_in_shadow_with_would_log(self):
        c = _mk(_settings(memory_conductor_enforce=False), pressure=4)
        for _ in range(3):
            c.tick_once()
        self.assertTrue(c.reload_brain_allowed())
        self.assertGreater(c.get_diagnostics()["would_skip_brain_reload"], 0)

    def test_reload_blocked_under_enforced_streak(self):
        c = _mk(pressure=4, model_loaded=False)
        for _ in range(3):
            c.tick_once()
        self.assertFalse(c.reload_brain_allowed())
        calm = _mk(pressure=0)
        self.assertTrue(calm.reload_brain_allowed())


class LivenessAndPolicySourceTest(unittest.TestCase):
    def test_diagnostics_liveness_fields(self):
        c = _mk()
        c.tick_once()
        d = c.get_diagnostics()
        for key in ("thread_alive", "last_tick_ts", "pressure_streak", "residents"):
            self.assertIn(key, d)

    def test_ladder_module_never_reads_ledger(self):
        src = (PROJECT_ROOT / "backend" / "memory_conductor.py").read_text()
        self.assertNotIn(".read_all(", src,
                         "C-POLICY-SOURCE: политика читает только in-process состояние")


if __name__ == "__main__":
    unittest.main()
