"""KE05C-26, KE05D-20: строка "false" не становится True; записи истории не пропадают.

``bool("false") is True``: строковое значение в настройках включало enforce-политику
выгрузки моделей (MemoryConductor), а в HistoryItem.from_dict флаги ``is_protected`` /
``favorite`` читались наоборот. Кроме того ``float(10**500)`` и ``int("abc")`` бросали
исключения в from_dict — запись молча выпадала из истории (``_iter_history_items_unlocked``
глотает исключение и пропускает запись).
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.memory_conductor import MemoryConductor  # noqa: E402
from backend.models import HistoryItem  # noqa: E402
from backend.strict_bool import parse_bool  # noqa: E402

_TMP = tempfile.TemporaryDirectory(prefix="ear-strict-bool-")


class ParseBool(unittest.TestCase):
    def test_real_bools_and_numbers(self):
        self.assertIs(parse_bool(True, False), True)
        self.assertIs(parse_bool(False, True), False)
        self.assertIs(parse_bool(1, False), True)
        self.assertIs(parse_bool(0, True), False)

    def test_strings(self):
        for v in ("true", "True", " TRUE ", "1", "yes", "on"):
            self.assertIs(parse_bool(v, False), True, v)
        for v in ("false", "False", " FALSE ", "0", "no", "off", ""):
            self.assertIs(parse_bool(v, True), False, v)

    def test_unrecognized_returns_default(self):
        self.assertIs(parse_bool("maybe", True), True)
        self.assertIs(parse_bool("maybe", False), False)
        self.assertIs(parse_bool(None, True), True)
        self.assertIs(parse_bool([1], False), False)
        self.assertIs(parse_bool(float("nan"), True), True)
        self.assertIs(parse_bool(2, False), False)


def _conductor(settings: dict) -> MemoryConductor:
    svc = MagicMock()
    svc.cached_settings.return_value = settings
    return MemoryConductor(
        settings_service=svc,
        ledger=MagicMock(),
        is_recording=lambda: False,
        is_meeting_active=lambda: False,
        pressure_fn=lambda: 0,
        host_stats_fn=lambda: None,
        gigaam_close_if_idle=MagicMock(),
        gigaam_idle_sec_fn=lambda: 0.0,
        last_stt_activity_ts_fn=lambda: 0.0,
        tick_sec=0.05,
        unload_model_fn=MagicMock(),
        load_model_fn=MagicMock(),
        model_loaded_fn=MagicMock(return_value=False),
        models_share_loaded_instance_fn=MagicMock(return_value=False),
        lease_holder_fn=lambda: None,
        shadow_marker_path=Path(_TMP.name) / "shadow.json",
    )


class ConductorEnforceFlags(unittest.TestCase):
    def test_string_false_does_not_enable_enforce(self):
        c = _conductor({"memory_conductor_enforce": "false", "memory_conductor_enforce_brain": "False"})
        self.assertFalse(c.enforce_for("brain"))
        self.assertFalse(c.enforce_for("gigaam"))

    def test_string_true_enables_enforce(self):
        self.assertTrue(_conductor({"memory_conductor_enforce": "true"}).enforce_for("brain"))

    def test_real_bool_unchanged(self):
        self.assertTrue(_conductor({"memory_conductor_enforce": True}).enforce_for("gigaam"))
        self.assertFalse(_conductor({}).enforce_for("gigaam"))

    def test_string_false_does_not_keep_conductor_enabled(self):
        # _get("memory_conductor_enabled", True) с type(default)("false") читался как True.
        c = _conductor({"memory_conductor_enabled": "false"})
        self.assertFalse(c._get("memory_conductor_enabled", True))


class HistoryItemFromDict(unittest.TestCase):
    _BASE = {"id": "i1", "ts": "2026-01-01T00:00:00+00:00", "text": "t"}

    def _item(self, **extra) -> HistoryItem:
        return HistoryItem.from_dict({**self._BASE, **extra})

    def test_string_false_flags_are_false(self):
        it = self._item(favorite="false", llm_applied="false", is_protected="false", privacy_mode="false")
        self.assertFalse(it.favorite)
        self.assertFalse(it.llm_applied)
        self.assertFalse(it.is_protected)
        self.assertFalse(it.privacy_mode)

    def test_string_true_flags_are_true(self):
        it = self._item(favorite="true", is_protected="1")
        self.assertTrue(it.favorite)
        self.assertTrue(it.is_protected)

    def test_unrecognized_protection_flags_fail_closed(self):
        # непонятное значение: не снимаем защиту и не теряем пометку приватности
        it = self._item(is_protected="???", privacy_mode="???")
        self.assertTrue(it.is_protected)
        self.assertTrue(it.privacy_mode)

    def test_invalid_latency_does_not_drop_the_record(self):
        for bad in ("abc", [1], {"x": 1}, float("inf"), float("nan"), 10**500):
            it = self._item(llm_latency_ms=bad)
            self.assertEqual(it.llm_latency_ms, 0, repr(bad))

    def test_valid_latency_kept(self):
        self.assertEqual(self._item(llm_latency_ms="42").llm_latency_ms, 42)
        self.assertEqual(self._item(llm_latency_ms=17.9).llm_latency_ms, 17)

    def test_huge_numbers_do_not_drop_the_record(self):
        it = self._item(confidence=10**500, audio_duration_sec=10**500)
        self.assertEqual(it.confidence, 0.0)
        self.assertEqual(it.audio_duration_sec, 0.0)

    def test_create_rejects_nonfinite_confidence(self):
        it = HistoryItem.create(text="x", paste_status="ok", confidence=float("nan"))
        self.assertTrue(it.confidence is None or math.isfinite(it.confidence))
        self.assertEqual(it.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
