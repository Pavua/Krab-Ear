"""test_event_bridge.py — backend/event_bridge.py::EventBridge
(spec docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.1).

Тестирует IPC-сторону в изоляции: инжектируемый post_fn (БЕЗ реальной сети —
жёсткое требование), drop-oldest deque, батчинг, backoff/смена состояния,
форма диагностики, disabled-killswitch, start/stop lifecycle, и (поправка
контролёра №1, 2026-07-07) stale-TTL при отправке — конверты старше
MAX_EVENT_AGE_SEC отбрасываются со счётчиком dropped_stale вместо отправки
задним числом после долгого даунтайма REST. Реальное REST-поведение (сеть,
/internal/event) покрыто отдельно в Задаче 4 (контракт) и Задаче 6
(двухпроцессный e2e) — НЕ дублируется здесь.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bridge import (  # noqa: E402
    EventBridge,
    QUEUE_MAXLEN,
    BATCH_MAX,
    BACKOFF_MIN_SEC,
    BACKOFF_MAX_SEC,
    MAX_EVENT_AGE_SEC,
    EVENT_BRIDGE_TOKEN_FILENAME,
    read_bridge_token,
)


def _fake_settings(enabled: bool = True, port: int = 5005) -> SimpleNamespace:
    return SimpleNamespace(EVENT_BRIDGE_ENABLED=enabled, REST_SERVER_PORT=port)


class EventBridgeUnitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    # -- on_event: неблокирующий, никогда не бросает исключения ----------------

    def test_on_event_appends_envelope(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir))
        bridge.on_event("stt.final", {"text": "x"})
        self.assertEqual(bridge.get_diagnostics()["queue_depth"], 1)

    def test_on_event_disabled_is_noop(self):
        bridge = EventBridge(settings=_fake_settings(enabled=False), data_dir=Path(self._tmpdir))
        bridge.on_event("stt.final", {"text": "x"})
        diag = bridge.get_diagnostics()
        self.assertEqual(diag["queue_depth"], 0)
        self.assertEqual(diag["state"], "disabled")
        self.assertFalse(diag["enabled"])

    def test_queue_drop_oldest_at_maxlen(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir))
        for i in range(QUEUE_MAXLEN + 10):
            bridge.on_event("x", {"i": i})
        diag = bridge.get_diagnostics()
        self.assertEqual(diag["queue_depth"], QUEUE_MAXLEN)
        self.assertEqual(diag["dropped"], 10)

    # -- sender: инжектируемый post_fn, без сети --------------------------------

    def test_drain_and_send_success_pops_batch_and_updates_counters(self):
        calls = []

        def fake_post(url, payload, token, timeout):
            calls.append((url, payload, token, timeout))
            return True

        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir), post_fn=fake_post)
        for i in range(BATCH_MAX + 5):
            bridge.on_event("x", {"i": i})
        bridge._token = "test-token"  # обходим файловый I/O в этом юнит-тесте
        bridge._drain_and_send()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1]["events"]), BATCH_MAX)
        diag = bridge.get_diagnostics()
        self.assertEqual(diag["sent"], BATCH_MAX)
        self.assertEqual(diag["queue_depth"], 5)  # остаток остаётся в очереди
        self.assertEqual(diag["state"], "up")

    def test_drain_and_send_failure_requeues_and_backs_off(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                             post_fn=lambda *a, **k: False)
        bridge._token = "test-token"
        bridge.on_event("x", {"i": 1})
        bridge._drain_and_send()

        diag = bridge.get_diagnostics()
        self.assertEqual(diag["state"], "down")
        self.assertEqual(diag["failed"], 1)
        self.assertEqual(diag["queue_depth"], 1, "неотправленный батч должен остаться в очереди, не быть выброшенным")
        self.assertEqual(bridge._current_backoff, BACKOFF_MIN_SEC * 2)

    def test_backoff_caps_at_max(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                             post_fn=lambda *a, **k: False)
        bridge._token = "test-token"
        bridge.on_event("x", {})
        for _ in range(10):
            bridge._next_retry_ts = 0.0  # форсируем обход backoff-гейта для прямого вызова
            bridge._drain_and_send()
        self.assertLessEqual(bridge._current_backoff, BACKOFF_MAX_SEC)

    def test_state_change_logged_once_not_per_event(self):
        post_results = iter([False, False, False, True])
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                             post_fn=lambda *a, **k: next(post_results))
        bridge._token = "test-token"
        with self.assertLogs("KrabEar.Backend.EventBridge", level="WARNING") as cm:
            for _ in range(3):
                bridge.on_event("x", {})
                bridge._next_retry_ts = 0.0
                bridge._drain_and_send()
        down_warnings = [m for m in cm.output if "недоступен" in m]
        self.assertEqual(len(down_warnings), 1, "ровно ОДИН WARN на 3 подряд неудачи — по смене состояния, не по событию")

    # -- stale-TTL при отправке (поправка контролёра №1, 2026-07-07) -----------

    def test_stale_events_dropped_fresh_events_sent(self):
        """После «даунтайма» конверты старше MAX_EVENT_AGE_SEC не уходят при
        восстановлении — только свежие. Дропнутые считаются в dropped_stale,
        а не в sent/failed (они никогда не были отправлены)."""
        sent_batches = []

        def fake_post(url, payload, token, timeout):
            sent_batches.append(payload["events"])
            return True

        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir), post_fn=fake_post)
        bridge._token = "test-token"

        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=MAX_EVENT_AGE_SEC + 5)).isoformat(
            timespec="seconds"
        )
        with bridge._lock:
            bridge._queue.append({"type": "old.stale", "ts": stale_ts, "data": {}})
        bridge.on_event("fresh.event", {"i": 1})

        bridge._drain_and_send()

        diag = bridge.get_diagnostics()
        self.assertEqual(diag["dropped_stale"], 1)
        self.assertEqual(diag["sent"], 1)
        self.assertEqual(diag["queue_depth"], 0)
        self.assertEqual(len(sent_batches), 1)
        sent_types = [e["type"] for e in sent_batches[0]]
        self.assertEqual(sent_types, ["fresh.event"], "стухший конверт не должен попасть в отправленный батч")

    def test_missing_or_invalid_ts_treated_as_fresh_fail_open(self):
        """Невалидный/отсутствующий ts при парсинге → конверт считается свежим
        (fail-open, не терять события из-за формата)."""
        sent_batches = []

        def fake_post(url, payload, token, timeout):
            sent_batches.append(payload["events"])
            return True

        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir), post_fn=fake_post)
        bridge._token = "test-token"
        with bridge._lock:
            bridge._queue.append({"type": "no.ts", "ts": "", "data": {}})
            bridge._queue.append({"type": "garbage.ts", "ts": "not-a-timestamp", "data": {}})

        bridge._drain_and_send()

        diag = bridge.get_diagnostics()
        self.assertEqual(diag["dropped_stale"], 0, "невалидный/пустой ts не должен считаться stale (fail-open)")
        self.assertEqual(diag["sent"], 2)
        self.assertEqual(len(sent_batches), 1)
        self.assertEqual(len(sent_batches[0]), 2)

    # -- форма диагностики -------------------------------------------------------

    def test_get_diagnostics_shape(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir))
        diag = bridge.get_diagnostics()
        for key in ("enabled", "state", "queue_depth", "sent", "dropped", "dropped_stale", "failed"):
            self.assertIn(key, diag)

    # -- lifecycle: start/stop ----------------------------------------------------

    def test_start_disabled_does_not_spawn_thread(self):
        bridge = EventBridge(settings=_fake_settings(enabled=False), data_dir=Path(self._tmpdir))
        bridge.start()
        self.assertIsNone(bridge._thread)

    def test_start_creates_token_file_with_0600(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                             post_fn=lambda *a, **k: True)
        bridge.start()
        try:
            token_path = Path(self._tmpdir) / EVENT_BRIDGE_TOKEN_FILENAME
            self.assertTrue(token_path.exists())
            mode = token_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            self.assertEqual(read_bridge_token(Path(self._tmpdir)), bridge._token)
        finally:
            bridge.stop()

    def test_read_bridge_token_returns_none_when_absent(self):
        self.assertIsNone(read_bridge_token(Path(self._tmpdir)))

    def test_stop_is_idempotent_and_joins_thread(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                             post_fn=lambda *a, **k: True)
        bridge.start()
        bridge.stop()
        bridge.stop()  # второй вызов не должен бросать
        self.assertFalse(bridge._thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
