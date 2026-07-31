"""test_event_bridge_wiring.py — source-контракт: EventBridge реально ПОДКЛЮЧЁН
к жизненному циклу BackendService (класс бага setupErrorBus/setupHealthMonitor,
Swift-сторона 2026-07-05: collaborator существовал, но никогда не вызывался в
проде при 100% зелёных изолированных тестах). Механическая grep-проверка —
дополняет (не заменяет) end-to-end доказательство в scripts/e2e_event_bridge_smoke.py
(Задача 6) и scripts/audit_decorative_wiring.py --strict (CI guard).

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge_wiring.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SERVICE_SRC = (PROJECT_ROOT / "backend" / "service.py").read_text(encoding="utf-8")
_HEALTH_SRC = (PROJECT_ROOT / "backend" / "health_check_service.py").read_text(encoding="utf-8")


class EventBridgeWiringSourceContractTestCase(unittest.TestCase):
    def test_event_bridge_constructed_in_init(self):
        self.assertIn("self._event_bridge = EventBridge(", _SERVICE_SRC)

    def test_event_bridge_registered_as_listener(self):
        self.assertIn("event_bus.add_listener(self._event_bridge.on_event)", _SERVICE_SRC)

    def test_event_bridge_started(self):
        self.assertIn("self._event_bridge.start()", _SERVICE_SRC)

    def test_event_bridge_stopped_in_close(self):
        close_start = _SERVICE_SRC.index("def close(self)")
        # До конца метода, а не фиксированное окно: рост close() (runtime-
        # hardening 2026-07-20 добавил close_background_workers в начало)
        # выталкивал _event_bridge за границу среза → ложный RED.
        close_end = _SERVICE_SRC.index("\n    def ", close_start)
        close_body = _SERVICE_SRC[close_start:close_end]
        self.assertIn("_event_bridge", close_body)
        self.assertIn(".stop()", close_body)

    def test_event_bridge_passed_to_health_check_service(self):
        self.assertIn("event_bridge=self._event_bridge", _SERVICE_SRC)

    def test_health_check_service_exposes_event_bridge_in_diagnostics(self):
        self.assertIn('"event_bridge"', _HEALTH_SRC)

    # -- S3/Задача 6: динамическое подавление -----------------------------------

    def test_event_bridge_receives_rest_running_fn(self):
        """Мост получает аксессор владельца, а не читает рубильник сам —
        одноразовое чтение REST_IN_PROCESS_ENABLED в конструкторе больше
        недостаточно (сторож REST способен поднять сервер позже старта)."""
        self.assertIn("rest_running_fn=self._is_rest_inprocess_running", _SERVICE_SRC)

    def test_rest_inprocess_block_constructed_before_event_bridge(self):
        """Блок подъёма REST обязан идти ВЫШЕ создания моста — мосту нужен
        готовый self._rest_inprocess для рабочего аксессора (иначе первый же
        батч читал бы ещё не созданный атрибут)."""
        rest_idx = _SERVICE_SRC.index("self._rest_inprocess = None")
        bridge_idx = _SERVICE_SRC.index("self._event_bridge = EventBridge(")
        self.assertLess(
            rest_idx, bridge_idx,
            "self._rest_inprocess = None должен встречаться в источнике РАНЬШЕ, "
            "чем self._event_bridge = EventBridge(...)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
