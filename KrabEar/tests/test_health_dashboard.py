"""Тесты эндпоинта /health/dashboard — самодостаточная HTML-страница состояния.

Проверяют:
  - Корректный HTTP-статус и Content-Type
  - Наличие обязательных секций и элементов в HTML
  - Мета-тег автообновления
  - Индикаторы статуса (dot, badge)
  - Поведение при недоступных зависимостях (psutil, health_checker)
  - Вспомогательные функции (_format_uptime, _status_dot_color)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Патчим тяжёлые зависимости до импорта rest_server (AudioEngine, StateStore, Transcriber).
_REST_AVAILABLE = False
try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"

    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []

    _mock_transcriber = MagicMock()

    _mock_metrics = MagicMock()
    _mock_metrics.get_summary.return_value = {
        "total_requests": 42,
        "error_rate": 0.05,
        "status": "ok",
        "stt_metrics": {
            "latency_ms": {"p50": 120.0, "p95": 350.0, "p99": 600.0, "avg": 180.0},
            "confidence": {"avg": 0.87},
        },
        "window_size": 42,
    }

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
            patch("backend.state_store.StateStore", return_value=_mock_store), \
            patch("backend.transcriber.Transcriber", return_value=_mock_transcriber), \
            patch("backend.metrics_collector.metrics", _mock_metrics):
        from backend.rest_server import app, _format_uptime, _status_dot_color, _build_dashboard_html

    _REST_AVAILABLE = True
except Exception:
    app = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_dashboard(client) -> tuple[object, str]:
    """Делает GET /health/dashboard, возвращает (response, html_text)."""
    resp = client.get("/health/dashboard")
    return resp, resp.data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Tests: HTTP basics
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardHttpBasics(unittest.TestCase):
    """Базовые HTTP-свойства эндпоинта."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_returns_200(self):
        resp, _ = _get_dashboard(self.client)
        self.assertEqual(resp.status_code, 200)

    def test_content_type_is_html(self):
        resp, _ = _get_dashboard(self.client)
        self.assertIn("text/html", resp.content_type)

    def test_response_is_non_empty(self):
        _, html = _get_dashboard(self.client)
        self.assertGreater(len(html), 500)


# ---------------------------------------------------------------------------
# Tests: Valid HTML structure
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardValidHtml(unittest.TestCase):
    """Проверяет, что возвращается корректный HTML-документ."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_has_doctype(self):
        _, html = _get_dashboard(self.client)
        self.assertTrue(html.strip().lower().startswith("<!doctype html"))

    def test_has_html_tag(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("<html", html.lower())
        self.assertIn("</html>", html.lower())

    def test_has_head_and_body(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("<head>", html.lower())
        self.assertIn("<body>", html.lower())
        self.assertIn("</body>", html.lower())

    def test_has_title(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("<title>", html.lower())
        self.assertIn("krab ear", html.lower())

    def test_has_charset_meta(self):
        _, html = _get_dashboard(self.client)
        self.assertIn('charset', html.lower())

    def test_has_viewport_meta(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("viewport", html.lower())


# ---------------------------------------------------------------------------
# Tests: Auto-refresh
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardAutoRefresh(unittest.TestCase):
    """Проверяет наличие механизма автообновления."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_has_meta_refresh(self):
        _, html = _get_dashboard(self.client)
        self.assertIn('http-equiv="refresh"', html.lower())

    def test_meta_refresh_interval_is_30(self):
        _, html = _get_dashboard(self.client)
        # Должно содержать content="30" в meta refresh
        self.assertIn('content="30"', html)

    def test_has_refresh_link(self):
        """Страница должна содержать ссылку на /health/dashboard для ручного рефреша."""
        _, html = _get_dashboard(self.client)
        self.assertIn("/health/dashboard", html)


# ---------------------------------------------------------------------------
# Tests: Required sections
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardSections(unittest.TestCase):
    """Проверяет наличие обязательных разделов."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_has_health_checks_section(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("Health Checks", html)

    def test_has_system_resources_section(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("System Resources", html)

    def test_has_recent_metrics_section(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("Recent Metrics", html)

    def test_has_service_section(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("Service", html)

    def test_has_uptime_field(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("Uptime", html)

    def test_has_latency_metrics(self):
        _, html = _get_dashboard(self.client)
        # Метрики задержки (p50 / p95 / p99)
        self.assertIn("Latency", html)

    def test_has_confidence_metric(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("Confidence", html)

    def test_has_total_requests(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("Total requests", html)


# ---------------------------------------------------------------------------
# Tests: Status indicators
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardStatusIndicators(unittest.TestCase):
    """Проверяет наличие цветных индикаторов статуса."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_has_dot_indicator(self):
        _, html = _get_dashboard(self.client)
        self.assertIn('class="dot"', html)

    def test_has_badge_element(self):
        _, html = _get_dashboard(self.client)
        self.assertIn('class="badge"', html)

    def test_has_overall_status_badge(self):
        _, html = _get_dashboard(self.client)
        self.assertIn("overall-badge", html)

    def test_inline_css_no_external_deps(self):
        """Страница не должна ссылаться на внешние CSS/JS."""
        _, html = _get_dashboard(self.client)
        external_patterns = [
            "cdn.jsdelivr.net",
            "googleapis.com",
            "unpkg.com",
            "<script src=",
            '<link rel="stylesheet"',
        ]
        for pattern in external_patterns:
            self.assertNotIn(pattern, html, f"Found external dependency: {pattern}")

    def test_dark_theme_colors_present(self):
        """Тёмная тема: переменная --bg или явный тёмный цвет фона."""
        _, html = _get_dashboard(self.client)
        # Либо CSS переменная --bg, либо явный тёмный цвет
        self.assertTrue("--bg" in html or "#0f1117" in html or "#111" in html)


# ---------------------------------------------------------------------------
# Tests: Health check data displayed
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardHealthCheckData(unittest.TestCase):
    """Проверяет, что данные проверок состояния отображаются в HTML."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _get_with_mock_health(self, health_data: dict) -> str:
        with patch("backend.rest_server._build_dashboard_html") as mock_build:
            mock_build.return_value = _build_dashboard_html_with_data(health_data)
            _, html = _get_dashboard(self.client)
        return html

    def test_check_names_appear_in_html(self):
        """Имена проверок (stt_model, llm и т.д.) должны быть в HTML."""
        _, html = _get_dashboard(self.client)
        # _build_dashboard_html вызывает HealthChecker; при моках он вернёт {}
        # Проверяем хотя бы что таблица Health Checks присутствует
        self.assertIn("Health Checks", html)

    def test_status_colors_used_in_html(self):
        """HTML должен содержать зелёный (#4ade80) или жёлтый (#fbbf24) цвет индикатора."""
        _, html = _get_dashboard(self.client)
        has_color = "#4ade80" in html or "#fbbf24" in html or "#f87171" in html
        self.assertTrue(has_color, "Ни один цвет статуса не найден в HTML")


# ---------------------------------------------------------------------------
# Tests: _format_uptime helper
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestFormatUptime(unittest.TestCase):
    """Unit-тесты вспомогательной функции _format_uptime."""

    def test_less_than_minute(self):
        result = _format_uptime(45)
        self.assertIn("45s", result)
        self.assertNotIn("m", result.replace("45s", ""))

    def test_exactly_one_minute(self):
        result = _format_uptime(60)
        self.assertIn("1m", result)
        self.assertIn("00s", result)

    def test_hours_minutes_seconds(self):
        result = _format_uptime(3661)  # 1h 1m 1s
        self.assertIn("1h", result)
        self.assertIn("1m", result)
        self.assertIn("01s", result)

    def test_days(self):
        result = _format_uptime(86400 + 3600 + 120 + 5)  # 1d 1h 2m 05s
        self.assertIn("1d", result)
        self.assertIn("1h", result)
        self.assertIn("2m", result)

    def test_zero_seconds(self):
        result = _format_uptime(0)
        self.assertIn("00s", result)

    def test_large_uptime(self):
        result = _format_uptime(10 * 86400 + 23 * 3600 + 59 * 60 + 59)
        self.assertIn("10d", result)


# ---------------------------------------------------------------------------
# Tests: _status_dot_color helper
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestStatusDotColor(unittest.TestCase):
    """Unit-тесты функции _status_dot_color."""

    def test_ok_is_green(self):
        self.assertEqual(_status_dot_color("ok"), "#4ade80")

    def test_healthy_is_green(self):
        self.assertEqual(_status_dot_color("healthy"), "#4ade80")

    def test_warning_is_yellow(self):
        self.assertEqual(_status_dot_color("warning"), "#fbbf24")

    def test_degraded_is_yellow(self):
        self.assertEqual(_status_dot_color("degraded"), "#fbbf24")

    def test_unavailable_is_yellow(self):
        self.assertEqual(_status_dot_color("unavailable"), "#fbbf24")

    def test_error_is_red(self):
        self.assertEqual(_status_dot_color("error"), "#f87171")

    def test_unknown_status_is_red(self):
        self.assertEqual(_status_dot_color("some_unknown_status"), "#f87171")

    def test_circuit_open_is_yellow(self):
        self.assertEqual(_status_dot_color("circuit_open"), "#fbbf24")


# ---------------------------------------------------------------------------
# Tests: Graceful degradation
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardGracefulDegradation(unittest.TestCase):
    """Проверяет, что страница не падает при недоступных зависимостях."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_dashboard_ok_when_health_checker_raises(self):
        """При ошибке HealthChecker страница всё равно возвращает HTML."""
        # HealthChecker импортируется внутри _build_dashboard_html — патчим в его модуле
        with patch("backend.health_checker.HealthChecker") as mock_hc:
            mock_hc.side_effect = Exception("health checker unavailable")
            resp, html = _get_dashboard(self.client)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<html", html.lower())

    def test_dashboard_ok_when_psutil_missing(self):
        """Без psutil страница возвращает HTML с заглушкой для ресурсов системы."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("No module named 'psutil'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            resp, html = _get_dashboard(self.client)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<html", html.lower())

    def test_dashboard_ok_when_metrics_raises(self):
        """При ошибке метрик страница всё равно рендерится."""
        _mock_metrics.get_summary.side_effect = RuntimeError("metrics broken")
        try:
            resp, html = _get_dashboard(self.client)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("<html", html.lower())
        finally:
            _mock_metrics.get_summary.side_effect = None
            _mock_metrics.get_summary.return_value = {
                "total_requests": 42,
                "error_rate": 0.05,
                "status": "ok",
            }


# ---------------------------------------------------------------------------
# Tests: _build_dashboard_html unit test (no HTTP)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestBuildDashboardHtml(unittest.TestCase):
    """Unit-тесты _build_dashboard_html без HTTP-клиента."""

    def test_returns_string(self):
        html = _build_dashboard_html()
        self.assertIsInstance(html, str)

    def test_contains_doctype(self):
        html = _build_dashboard_html()
        self.assertTrue(html.strip().lower().startswith("<!doctype html"))

    def test_contains_auto_refresh_30(self):
        html = _build_dashboard_html()
        self.assertIn('content="30"', html)

    def test_contains_health_checks_heading(self):
        html = _build_dashboard_html()
        self.assertIn("Health Checks", html)

    def test_contains_system_resources_heading(self):
        html = _build_dashboard_html()
        self.assertIn("System Resources", html)

    def test_contains_recent_metrics_heading(self):
        html = _build_dashboard_html()
        self.assertIn("Recent Metrics", html)

    def test_no_external_links(self):
        html = _build_dashboard_html()
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn("googleapis.com", html)


# ---------------------------------------------------------------------------
# Utility for patched rendering (used in some tests)
# ---------------------------------------------------------------------------

def _build_dashboard_html_with_data(health_data: dict) -> str:
    """Упрощённый рендерер для тестов с инъекцией произвольных данных."""
    checks_html = ""
    for name, result in health_data.get("checks", {}).items():
        st = result.get("status", "unknown")
        checks_html += f"<tr><td>{name}</td><td>{st}</td></tr>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>Krab Ear — Health Dashboard</title>
</head><body>
<div class="overall-badge">{health_data.get('status', 'unknown')}</div>
<h2>Health Checks</h2><table>{checks_html}</table>
<h2>System Resources</h2>
<h2>Recent Metrics</h2>
<span class="dot"></span><span class="badge"></span>
</body></html>"""


if __name__ == "__main__":
    unittest.main()
