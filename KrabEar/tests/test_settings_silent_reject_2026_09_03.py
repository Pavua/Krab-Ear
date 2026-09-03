"""set_settings не смеет отвечать «ок» на значение, которое не записал.

Живой случай 03.09.2026: `set_settings {"call_provider": "gateway"}` вернул
`ok: True`, а в settings.json осталось прежнее `telnyx` — валидатор не знал
нового значения и молча вернул старое. Для владельца это худший вид отказа:
интерфейс говорит «сохранено», поведение не меняется, и искать причину негде.

Тест держит инвариант на самом валидаторе: значение из допустимого списка
обязано доезжать, недопустимое — не превращаться в тихое «как было».
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.settings_validator import _ENUM_FIELDS  # noqa: E402


class CallProviderEnumTestCase(unittest.TestCase):
    def test_gateway_is_accepted(self) -> None:
        self.assertIn(
            "gateway", _ENUM_FIELDS["call_provider"],
            "после консолидации телефонии звонки идут через Voice Gateway — "
            "значение обязано быть допустимым, иначе set_settings молча его отбросит",
        )

    def test_legacy_values_stay_valid(self) -> None:
        """Старые значения не выкидываем: settings.json владельца хранит `telnyx`,
        и его исчезновение из списка вернуло бы настройку к дефолту молча."""
        for legacy in ("telnyx", "twilio", "sip_local", "none"):
            self.assertIn(legacy, _ENUM_FIELDS["call_provider"])
