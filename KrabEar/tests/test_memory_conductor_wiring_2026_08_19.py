"""Source-контракты проводки MemoryConductor (план T7).

Класс setupErrorBus: компонент может быть написан, покрыт тестами и НИКОГДА не
вызван из реального старта. Эти тесты читают исходники и закрепляют факт
проводки, а не поведение.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVICE = (PROJECT_ROOT / "backend" / "service.py").read_text()
RECORDING = (PROJECT_ROOT / "backend" / "recording_core_service.py").read_text()
CONFIG = (PROJECT_ROOT / "core" / "config.py").read_text()
VALIDATOR = (PROJECT_ROOT / "backend" / "settings_validator.py").read_text()


class ServiceWiringTest(unittest.TestCase):
    def test_conductor_constructed_and_started(self):
        self.assertIn("MemoryConductor(", SERVICE)
        self.assertIn("self._memory_conductor.start()", SERVICE)

    def test_conductor_is_the_single_oom_listener(self):
        self.assertIn("add_listener(self._memory_conductor.handle_oom_event)", SERVICE)
        self.assertNotIn("OomAutoRelief", SERVICE,
                         "single-tap: старый листенер обязан быть снят целиком")

    def test_old_module_deleted(self):
        self.assertFalse((PROJECT_ROOT / "backend" / "oom_auto_relief.py").exists(),
                         "мёртвый модуль поймает audit_dead_extracted_modules")

    def test_conductor_stopped_on_close(self):
        self.assertIn("self._memory_conductor.stop()", SERVICE)

    def test_get_memory_ledger_dispatched(self):
        self.assertIn('"get_memory_ledger"', SERVICE)


class RecordingCoreWiringTest(unittest.TestCase):
    def test_start_keeps_legacy_unload_fallback(self):
        """🔴 H2: shadow-неделя не смеет отключить живую выгрузку 19 ГБ."""
        self.assertIn('enforce_for("recording_sequence")', RECORDING)
        self.assertIn("unload_model_async(base_url, brain_model)", RECORDING,
                      "легаси-путь обязан остаться как else-ветка")

    def test_stop_reload_gated_by_conductor(self):
        self.assertIn("reload_brain_allowed()", RECORDING)


class SettingsWiringTest(unittest.TestCase):
    def test_defaults_registered(self):
        for key in ("memory_conductor_enabled", "memory_conductor_enforce",
                    "memory_conductor_enforce_brain", "gigaam_idle_unload_sec",
                    "whisper_idle_unload_sec", "rewriter_idle_unload_sec",
                    "memory_pressure_streak_ticks", "memory_evict_cooldown_sec"):
            self.assertIn(f'"{key}"', CONFIG, key)

    def test_range_bounds_registered(self):
        for key in ("gigaam_idle_unload_sec", "whisper_idle_unload_sec",
                    "rewriter_idle_unload_sec", "memory_pressure_streak_ticks",
                    "memory_evict_cooldown_sec"):
            self.assertIn(f'"{key}"', VALIDATOR, key)


if __name__ == "__main__":
    unittest.main()
