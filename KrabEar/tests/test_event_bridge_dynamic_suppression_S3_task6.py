"""S3/Задача 6: EventBridge подавляется ДИНАМИЧЕСКИ работающим in-process REST.

Заменяет устаревший контракт test_event_bridge_offline_when_inprocess_M2.py
(мост читал REST_IN_PROCESS_ENABLED один раз в конструкторе и выключался
навсегда). Одноразового решения недостаточно: сторож REST (отдельная задача
волны) способен поднять in-process REST ПОЗЖЕ старта — если подавление
вычислено один раз, мост остался бы живым при работающем REST и начал бы
постить события в СОБСТВЕННЫЙ процесс, `emit_envelope` доставил бы их
подписчикам ВТОРОЙ раз в обход push-листенеров (event_bus.py:210-217).

Три ситуации из таблицы спеки Р4:
- REST не слушает (rest_running_fn -> False) -> мост работает как раньше.
- REST слушает (rest_running_fn -> True) -> мост подавлен (state=suppressed,
  очередь не копится).
- rest_running_fn отсутствует (None, standalone/тесты) -> мост работает.

Плюс: подавление меняется между двумя батчами БЕЗ пересоздания моста, и
обязательный тест на отсутствие двойной доставки через настоящий EventBus.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge_dynamic_suppression_S3_task6.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bridge import EventBridge  # noqa: E402
from backend.event_bus import EventBus  # noqa: E402


def _fake_settings(enabled: bool = True, port: int = 5005) -> SimpleNamespace:
    return SimpleNamespace(EVENT_BRIDGE_ENABLED=enabled, REST_SERVER_PORT=port)


class EventBridgeDynamicSuppressionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # -- три ситуации из таблицы спеки Р4 --------------------------------------

    def test_rest_not_listening_bridge_queues_events(self):
        """REST не слушает (running=False) -> мост копит события как раньше."""
        bridge = EventBridge(
            settings=_fake_settings(), data_dir=self.data_dir,
            rest_running_fn=lambda: False,
        )
        bridge.on_event("krab_error", {"code": "test.code"})
        self.assertEqual(bridge.get_diagnostics()["queue_depth"], 1)

    def test_rest_listening_bridge_suppressed_and_drops_no_batch(self):
        """REST слушает (running=True) -> мост подавлен, события не копятся."""
        bridge = EventBridge(
            settings=_fake_settings(), data_dir=self.data_dir,
            rest_running_fn=lambda: True,
        )
        bridge.on_event("krab_error", {"code": "test.code"})
        diag = bridge.get_diagnostics()
        self.assertEqual(diag["queue_depth"], 0)
        # enabled (killswitch) остаётся True — подавление НЕ то же самое, что
        # EVENT_BRIDGE_ENABLED=False.
        self.assertTrue(diag["enabled"])

    def test_none_accessor_bridge_works(self):
        """Аксессора нет (standalone/тесты) -> трактуется как "не слушает"."""
        bridge = EventBridge(settings=_fake_settings(), data_dir=self.data_dir, rest_running_fn=None)
        bridge.on_event("krab_error", {"code": "test.code"})
        self.assertEqual(bridge.get_diagnostics()["queue_depth"], 1)

    def test_suppressed_state_distinct_from_disabled(self):
        """Killswitch выключен -> state='disabled'. REST слушает -> state='suppressed'.
        Канарейка обязана различать эти два состояния (плана пункт 4)."""
        disabled_bridge = EventBridge(
            settings=_fake_settings(enabled=False), data_dir=self.data_dir,
            rest_running_fn=lambda: False,
        )
        self.assertEqual(disabled_bridge.get_diagnostics()["state"], "disabled")

        suppressed_bridge = EventBridge(
            settings=_fake_settings(enabled=True), data_dir=self.data_dir,
            rest_running_fn=lambda: True,
        )
        suppressed_bridge._token = "test-token"
        suppressed_bridge._tick()
        self.assertEqual(suppressed_bridge.get_diagnostics()["state"], "suppressed")

    # -- динамика: меняется МЕЖДУ батчами без пересоздания моста ----------------

    def test_suppression_toggles_dynamically_without_recreating_bridge(self):
        flag = {"running": False}
        sent_batches = []

        def fake_post(url, payload, token, timeout):
            sent_batches.append(payload["events"])
            return True

        bridge = EventBridge(
            settings=_fake_settings(), data_dir=self.data_dir, post_fn=fake_post,
            rest_running_fn=lambda: flag["running"],
        )
        bridge._token = "test-token"

        # REST ещё не слушает -> событие копится и уходит.
        bridge.on_event("first", {})
        bridge._tick()
        self.assertEqual(bridge.get_diagnostics()["state"], "up")
        self.assertEqual(len(sent_batches), 1)

        # REST поднялся ПОЗЖЕ старта (тот самый сторож из плана волны) -> мост
        # подавлен на ТОМ ЖЕ объекте, без пересоздания.
        flag["running"] = True
        bridge.on_event("second", {})  # не должно попасть в очередь
        bridge._tick()
        self.assertEqual(bridge.get_diagnostics()["state"], "suppressed")
        self.assertEqual(bridge.get_diagnostics()["queue_depth"], 0)
        self.assertEqual(len(sent_batches), 1, "пока REST слушает — новых POST быть не должно")

        # REST снова не слушает -> мост разжат и опять работает.
        flag["running"] = False
        bridge.on_event("third", {})
        bridge._tick()
        self.assertEqual(len(sent_batches), 2)
        self.assertEqual(sent_batches[1][0]["type"], "third")

    # -- 🔴 обязательный тест: РОВНО один раз, не >= 1 ---------------------------

    def test_event_delivered_exactly_once_when_suppressed(self):
        """Поднимаем настоящую EventBus, вешаем мост листенером в подавленном
        состоянии, эмитим событие -> подписчик получает его РОВНО один раз.

        Условие именно ==1: >=1 не отличило бы дубль (мост, ошибочно
        отправивший конверт на /internal/event, который вернулся бы в ту же
        шину через emit_envelope) от штатной единственной доставки самим
        emit()."""
        bus = EventBus()
        subscriber_queue = bus.subscribe()
        bridge = EventBridge(
            settings=_fake_settings(), data_dir=self.data_dir,
            rest_running_fn=lambda: True,  # in-process REST слушает -> подавлен
        )
        bus.add_listener(bridge.on_event)

        bus.emit("krab_error", {"code": "test.code"})

        # Мост не поставил событие к себе в очередь (подавлен) — соответственно
        # ничего не отправил бы на REST при следующем _tick().
        self.assertEqual(bridge.get_diagnostics()["queue_depth"], 0)

        delivered = []
        while not subscriber_queue.empty():
            delivered.append(subscriber_queue.get_nowait())
        matching = [e for e in delivered if e is not None and e.get("type") == "krab_error"]
        self.assertEqual(len(matching), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
