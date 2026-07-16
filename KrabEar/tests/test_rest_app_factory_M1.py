"""M1 (спека 2026-07-16-m-series-rest-merge-design §3): фабрика + deps-прокси.

Импорт rest_server — по канону категории A: патчим тяжёлые конструкторы ВОКРУГ
импорта (см. test_rest_smoke.py). Обратимых sys.modules-стабов не используем.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_REST_AVAILABLE = False
try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.load_settings.return_value = {}
    _mock_transcriber = MagicMock()

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
            patch("backend.state_store.StateStore", return_value=_mock_store), \
            patch("backend.transcriber.Transcriber", return_value=_mock_transcriber):
        import backend.rest_server as rs
    _REST_AVAILABLE = True
except Exception:  # pragma: no cover - защитный skip как в test_rest_smoke.py
    rs = None


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class ModuleGlobalsDepsTest(unittest.TestCase):
    """Страж категории B: патч module-атрибута виден через deps-прокси."""

    def test_proxy_reads_live_module_attribute(self):
        fake_store = MagicMock(name="patched_store")
        with patch.object(rs, "store", fake_store):
            self.assertIs(rs._MODULE_DEPS.store, fake_store)
        self.assertIsNot(rs._MODULE_DEPS.store, fake_store)

    def test_deps_helper_falls_back_without_app_context(self):
        # Прямые вызовы хендлеров без request-контекста (канон reload-тестов,
        # см. CLAUDE.md про self.rs.ws_stream(...)) должны получать module-глобалы.
        self.assertIs(rs._deps(), rs._MODULE_DEPS)

    def test_static_deps_is_plain_container(self):
        deps = rs.StaticDeps(
            engine="e", store="s", transcriber="tr", translator="tl",
            tts_service="tts", metrics="m", event_bus="b", sse_stream="ss",
        )
        self.assertEqual(deps.store, "s")
        self.assertEqual(deps.sse_stream, "ss")


def _fresh_static_deps():
    eng = MagicMock()
    eng.quality_profile = "balanced"
    st = MagicMock()
    st.load_vocabulary.return_value = []
    st.load_settings.return_value = {}
    m = MagicMock()
    m.get_summary.return_value = {"total_requests": 0, "error_rate": 0, "status": "waiting_data"}
    return rs.StaticDeps(
        engine=eng, store=st, transcriber=MagicMock(), translator=MagicMock(),
        tts_service=MagicMock(), metrics=m, event_bus=MagicMock(), sse_stream=MagicMock(),
    )


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class CreateAppFactoryTest(unittest.TestCase):
    def test_two_apps_are_independent(self):
        d1, d2 = _fresh_static_deps(), _fresh_static_deps()
        app1, app2 = rs.create_app(d1), rs.create_app(d2)
        self.assertIsNot(app1, app2)
        self.assertIs(app1.config["REST_DEPS"], d1)
        self.assertIs(app2.config["REST_DEPS"], d2)

    @unittest.expectedFailure
    def test_factory_app_health_uses_injected_engine(self):
        # M1 Task 3 снимет маркер: до свипа хендлеров /health читает
        # module-глобал `engine`, не инжектированный deps.engine.
        deps = _fresh_static_deps()
        deps.engine.quality_profile = "max"
        client = rs.create_app(deps).test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["profile"], "max")

    def test_module_level_app_still_exists_and_serves(self):
        resp = rs.app.test_client().get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_module_level_aliases_preserved(self):
        for name in ("app", "sock", "api", "limiter", "ws_stream",
                     "store", "engine", "transcriber", "translator", "tts_service"):
            self.assertTrue(hasattr(rs, name), f"module-алиас {name} пропал")


if __name__ == "__main__":
    unittest.main()
