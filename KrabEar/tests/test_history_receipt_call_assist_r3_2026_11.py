"""R3 шаг 1 (2026-11-07): идемпотентная запись Call Assist timeline в историю.

Разведка: ``handle_timeline_to_history`` не имеет ни state-гарда, ни ключа —
повтор после потерянного ответа (или повторный клик после «Не удалось
сохранить») пишет вторую запись. ``add_history_item`` всегда генерирует новый
uuid4, ``is_idempotent`` в проде не вызывается никем.

Решение: клиентский ``request_id`` → детерминированный id записи (uuid5).
Проверка и запись — под одним локом стора. Удалённый id (tombstone/реестр
purge) не воскрешается.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


def _store(tmp: str) -> StateStore:
    store = StateStore(Path(tmp) / "data")
    store.initialize_startup_plaintext_policy(new_profile=True)
    return store


class _TimelineGateway(VoiceGatewayClient):
    def get(self, voice_gateway_url, api_key, path):
        if "/export" in path:
            return {"ok": True, "payload": {"content": "line1\nline2"}}
        return {"ok": True, "payload": {}}

    def post(self, voice_gateway_url, api_key, path, payload):
        return {"ok": True, "payload": {}}


class _Recorder:
    is_recording = False


class StoreReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = _store(self.tmp.name)

    def _active_count(self) -> int:
        return self.store.count_active_items()

    def test_same_key_writes_once(self):
        first, outcome1 = self.store.add_history_item_once("req-1", text="a")
        second, outcome2 = self.store.add_history_item_once("req-1", text="a")
        self.assertEqual((outcome1, outcome2), ("created", "existing"))
        self.assertEqual(first.id, second.id)
        self.assertEqual(self._active_count(), 1)

    def test_different_keys_write_separately(self):
        a, _ = self.store.add_history_item_once("req-1", text="a")
        b, _ = self.store.add_history_item_once("req-2", text="b")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self._active_count(), 2)

    def test_deleted_receipt_is_not_resurrected(self):
        item, _ = self.store.add_history_item_once("req-1", text="a")
        self.assertTrue(self.store.delete_history_item(item.id))
        again, outcome = self.store.add_history_item_once("req-1", text="a")
        self.assertIsNone(again)
        self.assertEqual(outcome, "deleted")
        self.assertEqual(self._active_count(), 0)

    def test_blank_key_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add_history_item_once("  ", text="a")


class TimelineToHistoryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = _store(self.tmp.name)
        self.svc = CallAssistService(
            store=self.store, recorder=_Recorder(), transcriber=object(),
            gateway=_TimelineGateway(),
        )
        self.svc._state["gateway_session_id"] = "gw_test_session"
        self.svc._state["active"] = True

    def test_retry_with_same_request_id_returns_same_item(self):
        r1 = self.svc.handle_timeline_to_history({"format": "md", "request_id": "click-1"})
        r2 = self.svc.handle_timeline_to_history({"format": "md", "request_id": "click-1"})
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(r1["history_id"], r2["history_id"])
        self.assertEqual((r1["receipt"], r2["receipt"]), ("created", "existing"))
        self.assertEqual(self.store.count_active_items(), 1)

    def test_without_request_id_behaviour_unchanged(self):
        r1 = self.svc.handle_timeline_to_history({"format": "md"})
        r2 = self.svc.handle_timeline_to_history({"format": "md"})
        self.assertNotEqual(r1["history_id"], r2["history_id"])
        self.assertNotIn("receipt", r1)

    def test_retry_after_delete_reports_deleted_without_write(self):
        r1 = self.svc.handle_timeline_to_history({"format": "md", "request_id": "click-1"})
        self.store.delete_history_item(r1["history_id"])
        r2 = self.svc.handle_timeline_to_history({"format": "md", "request_id": "click-1"})
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["receipt"], "deleted")
        self.assertIsNone(r2["history_id"])
        self.assertEqual(self.store.count_active_items(), 0)

    def test_existing_without_readable_row_still_returns_id(self):
        # Ревью: id активен, но строка не нашлась — не ложный ok:false.
        r1 = self.svc.handle_timeline_to_history({"format": "md", "request_id": "click-9"})
        orig = self.store.add_history_item_once
        self.store.add_history_item_once = lambda key, **f: (None, "existing")
        try:
            r2 = self.svc.handle_timeline_to_history({"format": "md", "request_id": "click-9"})
        finally:
            self.store.add_history_item_once = orig
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["history_id"], r1["history_id"])
        self.assertEqual(r2["receipt"], "existing")


if __name__ == "__main__":
    unittest.main()
