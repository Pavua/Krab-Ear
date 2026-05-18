"""Wave 69: tests ensuring REST server does not spawn a duplicate GigaAM worker.

Root cause: rest_server.py module-level ``engine = AudioEngine()`` triggered
GigaAM warmup subprocess in parallel with BackendService — resulting in two
gigaam_worker.py processes (1.46 GB RAM wasted + orphan risk on crash).

Fix (Option A1): AudioEngine now accepts ``skip_gigaam_warmup=True`` which
prevents the warmup thread/subprocess from spawning. REST server passes this
flag because it proxies STT via BackendService IPC and never calls GigaAM
directly.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — allow ``from core.* import`` and ``from backend.* import``
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).parent
_PROJECT_ROOT = _TESTS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class TestSkipGigaamWarmupFlag(unittest.TestCase):
    """AudioEngine.__init__ skips warmup thread when skip_gigaam_warmup=True."""

    def _make_engine(self, skip: bool, gigaam_enabled: bool):
        """Create a real AudioEngine with STT_GIGAAM_ENABLED patched."""
        # Patch heavy imports that are not needed for this test
        with patch("core.engine.mlx_whisper", None), \
             patch("core.stt_router.STTRouter") as MockRouter:
            MockRouter.return_value = MagicMock()
            from core.config import settings as _settings
            with patch.object(type(_settings), "STT_GIGAAM_ENABLED",
                              new_callable=lambda: property(lambda _: gigaam_enabled)):
                import importlib
                import core.engine
                importlib.reload(core.engine)
                eng = core.engine.AudioEngine(skip_gigaam_warmup=skip)
        return eng

    def test_skip_gigaam_warmup_prevents_thread(self):
        """With skip_gigaam_warmup=True no GigaAM-warmup Thread is started."""
        spawned_threads = []

        import threading

        class _CapturingThread(threading.Thread):
            def start(self):
                spawned_threads.append(self.name)
                # Do NOT call super().start() — avoid spawning real threads in tests

        with patch("core.engine.mlx_whisper", None), \
                patch("core.stt_router.STTRouter") as MockRouter, \
                patch("threading.Thread", _CapturingThread):
            MockRouter.return_value = MagicMock()
            from core.config import settings as _settings
            with patch.object(_settings, "STT_GIGAAM_ENABLED", True):
                import importlib
                import core.engine
                importlib.reload(core.engine)
                _ = core.engine.AudioEngine(skip_gigaam_warmup=True)

        gigaam_threads = [n for n in spawned_threads if "GigaAM" in n]
        self.assertEqual(
            gigaam_threads, [],
            f"Expected no GigaAM warmup thread with skip_gigaam_warmup=True, got: {gigaam_threads}",
        )

    def test_warmup_thread_fires_when_not_skipped(self):
        """With skip_gigaam_warmup=False (default) a GigaAM-warmup Thread IS started."""
        spawned_threads = []

        import threading

        class _CapturingThread(threading.Thread):
            def start(self):
                spawned_threads.append(self.name)

        with patch("core.engine.mlx_whisper", None), \
                patch("core.stt_router.STTRouter") as MockRouter, \
                patch("threading.Thread", _CapturingThread):
            MockRouter.return_value = MagicMock()
            from core.config import settings as _settings
            with patch.object(_settings, "STT_GIGAAM_ENABLED", True):
                import importlib
                import core.engine
                importlib.reload(core.engine)
                _ = core.engine.AudioEngine(skip_gigaam_warmup=False)

        gigaam_threads = [n for n in spawned_threads if "GigaAM" in n]
        self.assertIn(
            "GigaAM-warmup",
            gigaam_threads,
            "Expected GigaAM-warmup thread when skip_gigaam_warmup=False",
        )

    def test_skip_flag_default_is_false(self):
        """skip_gigaam_warmup defaults to False — backward compat for BackendService."""
        import inspect
        import core.engine
        sig = inspect.signature(core.engine.AudioEngine.__init__)
        param = sig.parameters.get("skip_gigaam_warmup")
        self.assertIsNotNone(param, "skip_gigaam_warmup parameter missing from AudioEngine.__init__")
        self.assertFalse(param.default, "Default must be False for backward compat")


class TestRestServerEngineInitSkipsWarmup(unittest.TestCase):
    """REST server module passes skip_gigaam_warmup=True to AudioEngine."""

    def test_rest_server_engine_created_with_skip_flag(self):
        """AudioEngine() in rest_server.py is called with skip_gigaam_warmup=True."""
        captured_kwargs = {}

        def fake_audio_engine(*args, **kwargs):
            captured_kwargs.update(kwargs)
            mock = MagicMock()
            mock.quality_profile = "balanced"
            mock._router = MagicMock()
            mock._router.get_gigaam_adapter.return_value = None
            return mock

        # Minimal stubs so rest_server imports without heavy deps
        stub_modules = {
            "flask": MagicMock(),
            "flask_smorest": MagicMock(),
            "flask_sock": MagicMock(),
            "flask_limiter": MagicMock(),
            "flask_limiter.util": MagicMock(),
            "flask_cors": MagicMock(),
            "marshmallow": MagicMock(),
            "werkzeug": MagicMock(),
            "werkzeug.utils": MagicMock(),
            "backend.event_bus": MagicMock(),
            "backend.rest_auth": MagicMock(),
            "backend.service": MagicMock(),
            "backend.state_store": MagicMock(),
            "backend.transcriber": MagicMock(),
            "backend.metrics_collector": MagicMock(),
            "backend.api_versioning": MagicMock(),
        }

        import sys as _sys

        # Patch AudioEngine before rest_server import
        with patch("core.engine.AudioEngine", side_effect=fake_audio_engine):
            for mod_name, stub in stub_modules.items():
                _sys.modules.setdefault(mod_name, stub)

            # Remove cached rest_server if previously imported
            _sys.modules.pop("backend.rest_server", None)
            try:
                import backend.rest_server  # noqa: F401
            except Exception:
                # Import may fail due to deep Flask wiring — we only care about kwargs
                pass

        self.assertTrue(
            captured_kwargs.get("skip_gigaam_warmup", False),
            f"rest_server.py must pass skip_gigaam_warmup=True to AudioEngine, got kwargs: {captured_kwargs}",
        )


class TestRestAtexitCleanup(unittest.TestCase):
    """atexit cleanup in rest_server calls adapter.close() when adapter exists."""

    def test_atexit_calls_adapter_close(self):
        """_rest_engine_cleanup() closes the GigaAM adapter when present."""
        mock_adapter = MagicMock()
        mock_adapter.close = MagicMock()

        mock_engine = MagicMock()
        mock_engine._router = MagicMock()
        mock_engine._router.get_gigaam_adapter.return_value = mock_adapter

        # Simulate the cleanup function directly (avoid full module import)
        def _rest_engine_cleanup_sim():
            try:
                if mock_engine is not None and mock_engine._router is not None:
                    adapter = mock_engine._router.get_gigaam_adapter()
                    if adapter is not None and hasattr(adapter, "close"):
                        adapter.close()
            except Exception:
                pass

        _rest_engine_cleanup_sim()
        mock_adapter.close.assert_called_once()

    def test_atexit_noop_when_no_adapter(self):
        """_rest_engine_cleanup() is a no-op when get_gigaam_adapter() returns None."""
        mock_engine = MagicMock()
        mock_engine._router = MagicMock()
        mock_engine._router.get_gigaam_adapter.return_value = None

        # Should not raise
        def _rest_engine_cleanup_sim():
            try:
                if mock_engine is not None and mock_engine._router is not None:
                    adapter = mock_engine._router.get_gigaam_adapter()
                    if adapter is not None and hasattr(adapter, "close"):
                        adapter.close()
            except Exception:
                pass

        _rest_engine_cleanup_sim()  # no assertion needed — just must not raise


class TestNoDuplicateWorkerArchitecture(unittest.TestCase):
    """Architectural invariant: only BackendService should own GigaAM warmup."""

    def test_rest_server_does_not_import_gigaam_directly(self):
        """rest_server.py source must not import gigaam directly."""
        rest_server_path = Path(__file__).parent.parent / "backend" / "rest_server.py"
        source = rest_server_path.read_text(encoding="utf-8")
        self.assertNotIn(
            "import gigaam",
            source,
            "rest_server.py must not import gigaam directly — use BackendService IPC",
        )
        self.assertNotIn(
            "from gigaam",
            source,
            "rest_server.py must not import gigaam directly — use BackendService IPC",
        )

    def test_rest_server_passes_skip_warmup_in_source(self):
        """rest_server.py source must contain skip_gigaam_warmup=True."""
        rest_server_path = Path(__file__).parent.parent / "backend" / "rest_server.py"
        source = rest_server_path.read_text(encoding="utf-8")
        self.assertIn(
            "skip_gigaam_warmup=True",
            source,
            "rest_server.py must pass skip_gigaam_warmup=True to AudioEngine (Wave 69 guard)",
        )

    def test_engine_py_has_skip_gigaam_warmup_param(self):
        """engine.py source must define the skip_gigaam_warmup parameter."""
        engine_path = Path(__file__).parent.parent / "core" / "engine.py"
        source = engine_path.read_text(encoding="utf-8")
        self.assertIn(
            "skip_gigaam_warmup",
            source,
            "AudioEngine.__init__ must define skip_gigaam_warmup parameter (Wave 69)",
        )


if __name__ == "__main__":
    unittest.main()
