"""Wave 222: _push_error Sentry guard tests.

Verifies that when error_bus.push raises internally, capture_exception is called
instead of silently swallowing, and that the guard itself never propagates.

CONSTRAINT: no mlx_whisper / model loading. All collaborators mocked.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers to build lightweight stub instances that bypass __init__ but expose
# _push_error and _error_bus.
# ---------------------------------------------------------------------------

def _make_engine_stub() -> object:
    from core.engine import AudioEngine
    obj = AudioEngine.__new__(AudioEngine)
    obj.current_model = "mlx-community/whisper-base-mlx"
    obj.quality_profile = "balanced"
    return obj


def _make_state_store_stub(tmp_path: Path) -> object:
    from backend.state_store import StateStore
    obj = StateStore.__new__(StateStore)
    obj.data_dir = tmp_path
    return obj


def _make_vocabulary_store_stub(tmp_path: Path) -> object:
    from backend.vocabulary_store import VocabularyStore
    obj = VocabularyStore.__new__(VocabularyStore)
    obj.data_dir = tmp_path
    obj.path = tmp_path / "vocabulary.json"
    return obj


def _make_llm_rewriter_stub() -> object:
    from backend.llm_rewriter import LLMRewriter
    obj = LLMRewriter.__new__(LLMRewriter)
    obj._model = "test-model"
    obj._base_url = "http://localhost:1234/v1"
    return obj


def _make_translator_stub() -> object:
    from backend.translator import Translator
    obj = Translator.__new__(Translator)
    return obj


_STUB_FACTORIES = {
    "engine": _make_engine_stub,
    "llm_rewriter": _make_llm_rewriter_stub,
    "translator": _make_translator_stub,
}


def _make_valid_error_bus_push():
    """Return a mock error_bus whose push() succeeds normally."""
    bus = MagicMock()
    bus.push = MagicMock()
    return bus


def _make_failing_error_bus_push(exc: Exception):
    """Return a mock error_bus whose push() raises exc."""
    bus = MagicMock()
    bus.push = MagicMock(side_effect=exc)
    return bus


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNormalPushNoSentryCapture(unittest.TestCase):
    """When push succeeds, capture_exception must NOT be called."""

    def test_engine_normal_push_no_sentry(self):
        obj = _make_engine_stub()
        obj._error_bus = _make_valid_error_bus_push()

        with patch("backend.observability.capture_exception") as mock_cap:
            with patch("backend.error_codes.ERROR_REGISTRY", {"stt.load_fail": {
                "severity": "error",
                "user_msg_ru": "STT ошибка",
                "actionable": False,
                "action_id": None,
            }}):
                obj._push_error("stt.load_fail", "unit test")

        mock_cap.assert_not_called()
        obj._error_bus.push.assert_called_once()

    def test_llm_rewriter_normal_push_no_sentry(self):
        obj = _make_llm_rewriter_stub()
        obj._error_bus = _make_valid_error_bus_push()

        with patch("backend.observability.capture_exception") as mock_cap:
            with patch("backend.error_codes.ERROR_REGISTRY", {}):
                obj._push_error("rewriter.timeout", "test msg")

        mock_cap.assert_not_called()

    def test_translator_normal_push_no_sentry(self):
        obj = _make_translator_stub()
        obj._error_bus = _make_valid_error_bus_push()

        with patch("backend.observability.capture_exception") as mock_cap:
            with patch("backend.error_codes.ERROR_REGISTRY", {}):
                obj._push_error("translation.unavailable", "test msg")

        mock_cap.assert_not_called()


class TestInternalExceptionCallsCaptureException(unittest.TestCase):
    """When push() raises, capture_exception must be called with the exception."""

    def _assert_capture_called_on_push_failure(self, obj):
        exc = RuntimeError("bus internal failure")
        obj._error_bus = _make_failing_error_bus_push(exc)

        captured = []

        def fake_capture_exception(e, component=None):
            captured.append((e, component))

        with patch("backend.observability.capture_exception", side_effect=fake_capture_exception):
            with patch("backend.error_codes.ERROR_REGISTRY", {}):
                # Must not raise
                obj._push_error("stt.load_fail", "debug info")

        self.assertEqual(len(captured), 1, "capture_exception should be called once")
        self.assertIs(captured[0][0], exc)
        # wave805 replaced sentinel with component names
        self.assertIsNotNone(captured[0][1], "component kwarg must be passed")

    def test_engine_push_failure_calls_capture(self):
        self._assert_capture_called_on_push_failure(_make_engine_stub())

    def test_llm_rewriter_push_failure_calls_capture(self):
        self._assert_capture_called_on_push_failure(_make_llm_rewriter_stub())

    def test_translator_push_failure_calls_capture(self):
        self._assert_capture_called_on_push_failure(_make_translator_stub())


class TestCaptureExceptionItselfFailingNoPropagation(unittest.TestCase):
    """When capture_exception itself raises, _push_error must still not propagate."""

    def test_engine_sentry_import_failure_no_raise(self):
        obj = _make_engine_stub()
        exc = RuntimeError("bus failure")
        obj._error_bus = _make_failing_error_bus_push(exc)

        def boom(*a, **kw):
            raise ImportError("sentry_sdk not installed")

        with patch("backend.observability.capture_exception", side_effect=boom):
            with patch("backend.error_codes.ERROR_REGISTRY", {}):
                # Must not raise even when capture_exception itself fails
                obj._push_error("stt.load_fail", "debug info")

    def test_llm_rewriter_sentry_import_failure_no_raise(self):
        obj = _make_llm_rewriter_stub()
        exc = ValueError("bad state")
        obj._error_bus = _make_failing_error_bus_push(exc)

        def boom(*a, **kw):
            raise RuntimeError("Sentry broken")

        with patch("backend.observability.capture_exception", side_effect=boom):
            with patch("backend.error_codes.ERROR_REGISTRY", {}):
                obj._push_error("rewriter.timeout", "debug")

    def test_translator_sentry_import_failure_no_raise(self):
        obj = _make_translator_stub()
        exc = TypeError("unexpected type")
        obj._error_bus = _make_failing_error_bus_push(exc)

        def boom(*a, **kw):
            raise RuntimeError("Sentry unavailable")

        with patch("backend.observability.capture_exception", side_effect=boom):
            with patch("backend.error_codes.ERROR_REGISTRY", {}):
                obj._push_error("translation.unavailable", "debug")


class TestPerModuleHelpersAllGuarded(unittest.TestCase):
    """Parametric: all 6 modules expose a _push_error with the Sentry guard."""

    def _verify_sentry_guard(self, obj):
        """Assert that push failure triggers capture_exception and doesn't raise.

        We patch error_bus.push directly on the mock, so KrabError construction
        succeeds but the final bus.push() call raises. This cleanly isolates the
        guard path regardless of component Literal validation.
        """
        exc = RuntimeError("injected push failure")
        obj._error_bus = _make_failing_error_bus_push(exc)

        captured = []

        def fake_cap(e, component=None):
            captured.append(e)

        # Use a valid code so KrabError construction doesn't fail before push()
        with patch("backend.observability.capture_exception", side_effect=fake_cap):
            with patch("backend.error_codes.ERROR_REGISTRY", {
                "stt.load_fail": {
                    "severity": "error",
                    "user_msg_ru": "STT ошибка",
                    "actionable": False,
                    "action_id": None,
                }
            }):
                obj._push_error("stt.load_fail", "debug text")

        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0], exc)

    def test_engine_guarded(self):
        self._verify_sentry_guard(_make_engine_stub())

    def test_llm_rewriter_guarded(self):
        self._verify_sentry_guard(_make_llm_rewriter_stub())

    def test_translator_guarded(self):
        self._verify_sentry_guard(_make_translator_stub())

    def test_state_store_guarded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._verify_sentry_guard(_make_state_store_stub(Path(tmp)))

    def test_vocabulary_store_guarded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._verify_sentry_guard(_make_vocabulary_store_stub(Path(tmp)))

class TestNoErrorBusNoSentry(unittest.TestCase):
    """When _error_bus is None (not injected), _push_error returns early — no Sentry."""

    def test_engine_no_bus_no_sentry(self):
        obj = _make_engine_stub()
        # no _error_bus attribute at all

        with patch("backend.observability.capture_exception") as mock_cap:
            obj._push_error("stt.load_fail", "debug")

        mock_cap.assert_not_called()

    def test_translator_no_bus_no_sentry(self):
        obj = _make_translator_stub()

        with patch("backend.observability.capture_exception") as mock_cap:
            obj._push_error("translation.unavailable", "debug")

        mock_cap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
