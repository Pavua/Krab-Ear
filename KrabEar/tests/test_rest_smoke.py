"""Smoke-тесты REST API Krab Ear.

Проверяют структуру ответов HTTP-эндпоинтов без реального аудио/ML.
Пропускаются если зависимости REST-сервера недоступны.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# rest_server.py создаёт AudioEngine() на уровне модуля, что требует ML-зависимостей.
# Патчим тяжёлые объекты ДО импорта модуля, чтобы тесты работали без mlx-whisper/pyannote.
_REST_AVAILABLE = False
try:
    # Проверяем базовые зависимости (flask, pydantic, etc.)
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"

    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []

    _mock_transcriber = MagicMock()

    _mock_metrics = MagicMock()
    _mock_metrics.get_summary.return_value = {
        "total_requests": 0,
        "error_rate": 0,
        "status": "waiting_data",
    }

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
         patch("backend.state_store.StateStore", return_value=_mock_store), \
         patch("backend.transcriber.Transcriber", return_value=_mock_transcriber), \
         patch("backend.metrics_collector.metrics", _mock_metrics):
        from backend.rest_server import app
    _REST_AVAILABLE = True
except Exception:
    app = None  # type: ignore[assignment]


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class HealthEndpointTest(unittest.TestCase):
    """Тесты эндпоинта /health."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_json_structure(self):
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertIn("status", data)
        self.assertIn("service", data)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "krab-ear")

    def test_health_has_profile(self):
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertIn("profile", data)

    def test_health_content_type(self):
        resp = self.client.get("/health")
        self.assertIn("application/json", resp.content_type)


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class MetricsEndpointTest(unittest.TestCase):
    """Тесты эндпоинта /metrics."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_metrics_returns_200(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)

    def test_metrics_json_structure(self):
        resp = self.client.get("/metrics")
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("total_requests", data)

    def test_metrics_content_type(self):
        resp = self.client.get("/metrics")
        self.assertIn("application/json", resp.content_type)


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class VocabularyEndpointTest(unittest.TestCase):
    """Тесты эндпоинта /v1/vocabulary."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_vocabulary_get_returns_200(self):
        resp = self.client.get("/v1/vocabulary")
        self.assertEqual(resp.status_code, 200)

    def test_vocabulary_get_json_structure(self):
        resp = self.client.get("/v1/vocabulary")
        data = resp.get_json()
        self.assertIn("words", data)
        self.assertIsInstance(data["words"], list)

    def test_vocabulary_content_type(self):
        resp = self.client.get("/v1/vocabulary")
        self.assertIn("application/json", resp.content_type)


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeEndpointTest(unittest.TestCase):
    """Тесты валидации эндпоинта /v1/stt/transcribe (без реальной транскрибации)."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_transcribe_no_file_returns_400(self):
        resp = self.client.post("/v1/stt/transcribe")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("error", data)


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class EventsEndpointTest(unittest.TestCase):
    """Тесты эндпоинта /v1/events (SSE stream)."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_events_returns_sse_content_type(self):
        resp = self.client.get("/v1/events")
        self.assertIn("text/event-stream", resp.content_type)


if __name__ == "__main__":
    unittest.main()
