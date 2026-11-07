"""BackendService.close() обязан завершать WebhookManager (2026-11-07).

ЧТО НАЙДЕНО: close() останавливает все фоновые потоки, но не зовёт
``WebhookManager.shutdown()``. Executor (4 непрерывных non-daemon воркера)
переживает close(), а EventBus-листенер после detach уже не шлёт новых задач —
но in-flight доставки с retry/sleep остаются без владельца.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from test_backend_service import FakeRecorder, FakeTranscriber, FakeTranslator  # noqa: E402


class ServiceCloseWebhookShutdownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        store.initialize_startup_plaintext_policy(new_profile=True)
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_close_shuts_down_webhook_manager(self) -> None:
        mgr = self.service._webhook_manager
        self.assertFalse(mgr._executor_shutdown)
        self.service.close()
        self.assertTrue(mgr._executor_shutdown, "close() не завершил WebhookManager")

    def test_close_is_idempotent_and_fire_after_close_is_safe(self) -> None:
        mgr = self.service._webhook_manager
        self.service.close()
        self.service.close()
        # Поздний emit после close не должен бросать из-за закрытого executor.
        mgr.register_webhook("http://127.0.0.1:1/x", [], allow_local=True)
        mgr.fire_webhook("recording.started", {})  # submit в закрытый executor не должен бросать


if __name__ == "__main__":
    unittest.main()
