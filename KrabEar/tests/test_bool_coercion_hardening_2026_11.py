"""E5 п.4: «ложь» не должна читаться как True.

1. `_handle_configure_auto_export`: `bool(params["enabled"])` превращал строку "false" в True —
   авто-экспорт включался командой «выключить». Теперь общий `BackendService._coerce_bool`.
2. `SettingsValidator._coerce_bool`: `bool(float("nan"))` == True — NaN включал булев флаг
   (например cloud_rewriter_enabled, который отправляет текст наружу). Не конечное число — не bool.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402


class _SchedulerStub:
    _coerce_bool = staticmethod(BackendService._coerce_bool)
    _handle_configure_auto_export = BackendService._handle_configure_auto_export

    def __init__(self):
        self._export_scheduler = MagicMock()


class AutoExportEnabledCoercionTests(unittest.TestCase):
    def _enabled_passed(self, raw) -> bool:
        stub = _SchedulerStub()
        stub._handle_configure_auto_export({"enabled": raw})
        return stub._export_scheduler.configure.call_args.kwargs["enabled"]

    def test_string_false_variants_disable(self):
        for raw in ("false", "False", "off", "no", "0", 0, False):
            with self.subTest(raw=raw):
                self.assertFalse(self._enabled_passed(raw))

    def test_true_variants_and_default_enable(self):
        for raw in ("true", "on", "yes", "1", 1, True):
            with self.subTest(raw=raw):
                self.assertTrue(self._enabled_passed(raw))
        stub = _SchedulerStub()
        stub._handle_configure_auto_export({})
        self.assertTrue(stub._export_scheduler.configure.call_args.kwargs["enabled"])


class ValidatorNonFiniteBoolTests(unittest.TestCase):
    def test_nan_and_inf_do_not_enable_bool_flag(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                fixed = SettingsValidator().validate({"cloud_rewriter_enabled": bad}).fixed
                self.assertIs(fixed["cloud_rewriter_enabled"], False)

    def test_regular_numbers_still_coerce(self):
        self.assertIs(SettingsValidator._coerce_bool(1), True)
        self.assertIs(SettingsValidator._coerce_bool(0.0), False)
        self.assertIsNone(SettingsValidator._coerce_bool(float("nan")))
        self.assertTrue(math.isnan(float("nan")))


if __name__ == "__main__":
    unittest.main()
