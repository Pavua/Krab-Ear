"""Интеграционный тест: active_requests инкрементится во время IPC dispatch."""

import unittest
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_metrics import HealthMetrics


class FakeService:
    """Минимальный сервис, имитирующий dispatch с tracking."""

    def __init__(self) -> None:
        self.health_metrics = HealthMetrics()
        self.observed_active = 0

    def handle(self, method: str) -> int:
        with self.health_metrics.track_request():
            self.observed_active = self.health_metrics.active_requests()
            time.sleep(0.05)
        return self.observed_active


class HealthMetricsIntegrationTestCase(unittest.TestCase):
    def test_active_requests_visible_during_dispatch(self):
        service = FakeService()
        result = service.handle("ping")
        self.assertGreaterEqual(result, 1)
        # После handle() — снова 0
        self.assertEqual(service.health_metrics.active_requests(), 0)

    def test_concurrent_requests_increment_correctly(self):
        service = FakeService()
        threads = [
            threading.Thread(target=service.handle, args=(f"m{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(service.health_metrics.active_requests(), 0)


if __name__ == "__main__":
    unittest.main()
