"""Wave 153 — Sentry breadcrumb coverage tests.

Verifies that breadcrumbs are emitted from 5 hot paths with metadata-only
(no transcript text, no credentials).

Privacy contract enforced:
- NO transcript text in data dict
- NO API keys / tokens / credentials
- ONLY metadata: method names, duration_ms, counts, error types, booleans
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup so test can be run standalone
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _make_fake_sentry_sdk():
    """Return a minimal sentry_sdk stub with add_breadcrumb recorder."""
    fake = types.ModuleType("sentry_sdk")
    fake.init = MagicMock()
    fake.add_breadcrumb = MagicMock()
    fake.push_scope = MagicMock()
    fake.capture_exception = MagicMock()
    return fake


def _init_observability(fake_sdk):
    """Force _sentry_initialized=True with a fake SDK."""
    import backend.observability as mod
    mod._sentry_initialized = False
    with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
        mod.init_sentry("https://fake@sentry.io/123")
    # keep initialized flag set; inject the fake sdk for subsequent calls
    sys.modules["sentry_sdk"] = fake_sdk
    return mod


# ---------------------------------------------------------------------------
# 1. engine.py — AudioEngine.transcribe() breadcrumbs
# ---------------------------------------------------------------------------

class TestEngineBreadcrumbs(unittest.TestCase):
    """AudioEngine.transcribe() emits start/finish breadcrumbs (metadata only)."""

    def setUp(self):
        self.fake_sdk = _make_fake_sentry_sdk()
        _init_observability(self.fake_sdk)

    def tearDown(self):
        sys.modules.pop("sentry_sdk", None)
        import backend.observability as mod
        mod._sentry_initialized = False

    def _make_engine(self):
        """Construct a minimal AudioEngine stub."""
        import core.engine as engine_mod

        eng = engine_mod.AudioEngine.__new__(engine_mod.AudioEngine)
        # Minimal attribute stubs needed for transcribe()
        eng._resolve_language = lambda hint: hint or "ru"
        eng._stop_preview_worker = MagicMock()
        eng._llm_rewrite_allowed = MagicMock(return_value=False)
        eng._punctuation_pass_allowed = MagicMock(return_value=False)
        eng._maybe_run_diarization = MagicMock(return_value=None)
        eng._settings_get = MagicMock(return_value=False)
        eng._apply_vad_prefilter = MagicMock(return_value=None)
        eng._maybe_multipass_retry = MagicMock(side_effect=lambda *a, **k: a[3])  # pass-through
        eng._maybe_denoise = MagicMock(side_effect=lambda x: x)
        eng._confidence_calibrator = MagicMock()
        calibrated = MagicMock()
        calibrated.calibrated = 0.75
        calibrated.adjustments = []
        eng._confidence_calibrator.calibrate_detailed.return_value = calibrated
        eng._error_bus = None
        eng._push_error = MagicMock()
        eng.current_model = "mlx-whisper/medium"
        # Fake STT result
        fake_stt_result = {
            "text": "test",
            "segments": [],
            "engine": "mlx-whisper",
            "model_used": "mlx-whisper/medium",
            "language": "ru",
            "audio_duration_sec": 2.5,
        }
        eng._transcribe_with_fallback = MagicMock(return_value=fake_stt_result)
        eng._estimate_num_speakers = MagicMock(return_value=None)
        eng._build_speaker_context_prompt = MagicMock(return_value="")
        return eng

    def _call_transcribe_patched(self, eng):
        """Call transcribe with all external dependencies patched."""
        import numpy as np
        from core import config as cfg

        audio = np.zeros(8000, dtype=np.float32)

        with patch.object(cfg.settings, "STT_STREAMING_ENABLED", False), \
             patch.object(cfg.settings, "STT_VAD_PREFILTER_ENABLED", False), \
             patch.object(cfg.settings, "STT_MULTIPASS_ENABLED", False), \
             patch.object(cfg.settings, "DIARIZATION_ENABLED", False), \
             patch.object(cfg.settings, "STT_DENOISE_ENABLED", False), \
             patch.object(cfg.settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", False), \
             patch.object(cfg.settings, "NUMBER_NORMALIZATION_ENABLED", False), \
             patch.object(cfg.settings, "DATETIME_NORMALIZATION_ENABLED", False), \
             patch.object(cfg.settings, "SENSEVOICE_EMOTION_TO_HISTORY", False), \
             patch("core.engine.build_initial_prompt", return_value=""):
            from core.engine import TextUtils
            with patch.object(TextUtils, "cleanup_transcript", return_value="test"):
                result = eng.transcribe(audio, is_preview=False, lang_hint="ru")
        return result

    def test_transcribe_start_breadcrumb_emitted(self):
        eng = self._make_engine()
        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            self._call_transcribe_patched(eng)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "transcribe_start"]
        self.assertTrue(len(calls) >= 1, "transcribe_start breadcrumb not emitted")

    def test_transcribe_finish_breadcrumb_emitted(self):
        eng = self._make_engine()
        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            self._call_transcribe_patched(eng)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "transcribe_finish"]
        self.assertTrue(len(calls) >= 1, "transcribe_finish breadcrumb not emitted")

    def test_transcribe_start_data_has_no_text(self):
        """Breadcrumb data must NOT contain transcript text."""
        eng = self._make_engine()
        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            self._call_transcribe_patched(eng)

        for c in self.fake_sdk.add_breadcrumb.call_args_list:
            data = c.kwargs.get("data", {})
            self.assertNotIn("text", data, "breadcrumb data must not contain 'text' key")
            self.assertNotIn("transcript", data, "breadcrumb data must not contain 'transcript' key")

    def test_transcribe_start_data_has_metadata(self):
        eng = self._make_engine()
        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            self._call_transcribe_patched(eng)

        start_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                       if c.kwargs.get("message") == "transcribe_start"]
        self.assertTrue(len(start_calls) >= 1)
        data = start_calls[0].kwargs.get("data", {})
        self.assertIn("lang_hint", data)
        self.assertIn("is_preview", data)

    def test_transcribe_finish_data_has_duration_ms(self):
        eng = self._make_engine()
        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            self._call_transcribe_patched(eng)

        finish_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                        if c.kwargs.get("message") == "transcribe_finish"]
        self.assertTrue(len(finish_calls) >= 1)
        data = finish_calls[0].kwargs.get("data", {})
        self.assertIn("duration_ms", data)
        self.assertIn("confidence", data)


# ---------------------------------------------------------------------------
# 2. llm_rewriter.py — LLMRewriter._rewrite_impl() breadcrumbs
# ---------------------------------------------------------------------------

class TestLLMRewriterBreadcrumbs(unittest.TestCase):
    """LLMRewriter._rewrite_impl() emits start/finish breadcrumbs."""

    def setUp(self):
        self.fake_sdk = _make_fake_sentry_sdk()
        _init_observability(self.fake_sdk)

    def tearDown(self):
        sys.modules.pop("sentry_sdk", None)
        import backend.observability as mod
        mod._sentry_initialized = False

    def _make_rewriter(self):
        from backend.llm_rewriter import LLMRewriter
        return LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="",
            model="test-model",
        )

    def test_rewrite_start_breadcrumb_when_circuit_open(self):
        """Even when circuit is open, rewrite_start is still emitted."""
        rw = self._make_rewriter()
        # Force circuit open
        rw._circuit._state = __import__("backend.llm_rewriter", fromlist=["CircuitState"]).CircuitState.OPEN
        rw._circuit._opened_at = __import__("time").monotonic()

        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            result = rw.rewrite("hello world test input")

        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "circuit_open")
        start_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                       if c.kwargs.get("message") == "rewrite_start"]
        self.assertTrue(len(start_calls) >= 1, "rewrite_start not emitted")

    def test_rewrite_skipped_breadcrumb_when_circuit_open(self):
        from backend.llm_rewriter import CircuitState
        import time as _time
        rw = self._make_rewriter()
        rw._circuit._state = CircuitState.OPEN
        rw._circuit._opened_at = _time.monotonic()

        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            rw.rewrite("hello world")

        skip_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                      if c.kwargs.get("message") == "rewrite_skipped"]
        self.assertTrue(len(skip_calls) >= 1)
        data = skip_calls[0].kwargs.get("data", {})
        self.assertEqual(data.get("reason"), "circuit_open")

    def test_rewrite_finish_breadcrumb_on_success(self):
        """On HTTP 200 success, rewrite_finish is emitted with ok=True."""
        rw = self._make_rewriter()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello world corrected."}}]
        }

        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            with patch.object(rw._session, "post", return_value=fake_resp):
                result = rw.rewrite("Hello world corrected")

        self.assertTrue(result.ok)
        finish_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                        if c.kwargs.get("message") == "rewrite_finish"]
        self.assertTrue(len(finish_calls) >= 1, "rewrite_finish not emitted")
        data = finish_calls[0].kwargs.get("data", {})
        self.assertTrue(data.get("ok"))
        self.assertIn("latency_ms", data)

    def test_breadcrumb_data_contains_no_text(self):
        """No breadcrumb must contain the input or output text."""
        rw = self._make_rewriter()
        input_text = "my private dictated sentence no credentials here"

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "My private dictated sentence."}}]
        }

        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            with patch.object(rw._session, "post", return_value=fake_resp):
                rw.rewrite(input_text)

        for c in self.fake_sdk.add_breadcrumb.call_args_list:
            data = c.kwargs.get("data", {})
            for key, val in data.items():
                if isinstance(val, str):
                    self.assertNotIn("private", val)
                    self.assertNotIn("credential", val)

    def test_breadcrumb_data_has_model_not_api_key(self):
        """Breadcrumb data includes model name but never api_key."""
        from backend.llm_rewriter import CircuitState
        import time as _time
        rw = self._make_rewriter()
        rw._api_key = "secret-key-12345"  # set a key
        rw._circuit._state = CircuitState.OPEN
        rw._circuit._opened_at = _time.monotonic()

        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            rw.rewrite("some text")

        for c in self.fake_sdk.add_breadcrumb.call_args_list:
            data = c.kwargs.get("data", {})
            for val in data.values():
                if isinstance(val, str):
                    self.assertNotIn("secret-key", val)

    def test_chatbot_response_breadcrumb_emitted(self):
        """chatbot_response failure path emits rewrite_finish with ok=False."""
        rw = self._make_rewriter()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "Извините, я не могу это сделать."}}]
        }

        with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
            with patch.object(rw._session, "post", return_value=fake_resp):
                result = rw.rewrite("some long enough text that passes length guard here")

        self.assertFalse(result.ok)
        finish_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                        if c.kwargs.get("message") == "rewrite_finish"
                        and not c.kwargs.get("data", {}).get("ok")]
        self.assertTrue(len(finish_calls) >= 1, "rewrite_finish(ok=False) not emitted for chatbot")


# ---------------------------------------------------------------------------
# 3. state_store.py — StateStore.compact_with_stats() breadcrumbs
# ---------------------------------------------------------------------------

class TestStateStoreCompactBreadcrumbs(unittest.TestCase):
    """compact_with_stats() emits compact_start and compact_finish breadcrumbs."""

    def setUp(self):
        self.fake_sdk = _make_fake_sentry_sdk()
        _init_observability(self.fake_sdk)

    def tearDown(self):
        sys.modules.pop("sentry_sdk", None)
        import backend.observability as mod
        mod._sentry_initialized = False

    def test_compact_start_breadcrumb_emitted(self):
        from backend.state_store import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                store.compact_with_stats()

        start_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                       if c.kwargs.get("message") == "compact_start"]
        self.assertTrue(len(start_calls) >= 1, "compact_start not emitted")

    def test_compact_finish_breadcrumb_emitted(self):
        from backend.state_store import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                store.compact_with_stats()

        finish_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                        if c.kwargs.get("message") == "compact_finish"]
        self.assertTrue(len(finish_calls) >= 1, "compact_finish not emitted")

    def test_compact_finish_data_has_items_compacted(self):
        from backend.state_store import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                store.compact_with_stats()

        finish_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                        if c.kwargs.get("message") == "compact_finish"]
        self.assertTrue(len(finish_calls) >= 1)
        data = finish_calls[0].kwargs.get("data", {})
        self.assertIn("items_compacted", data)
        self.assertIn("reclaimed_bytes", data)

    def test_compact_breadcrumb_data_has_no_text(self):
        """Compact breadcrumbs must not contain any history text."""
        from backend.state_store import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            # Add an item with text to ensure it doesn't leak
            store.add_history_item("private transcript text should never appear")
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                store.compact_with_stats()

        for c in self.fake_sdk.add_breadcrumb.call_args_list:
            if c.kwargs.get("category") == "history":
                data = c.kwargs.get("data", {})
                for val in data.values():
                    if isinstance(val, str):
                        self.assertNotIn("private", val)
                        self.assertNotIn("transcript", val)

    def test_compact_start_data_has_before_counts(self):
        from backend.state_store import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                store.compact_with_stats()

        start_calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                       if c.kwargs.get("message") == "compact_start"]
        data = start_calls[0].kwargs.get("data", {})
        self.assertIn("active_count", data)
        self.assertIn("total_bytes", data)


# ---------------------------------------------------------------------------
# 4. audit_logger.py — AuditLogger.log_request() breadcrumbs
# ---------------------------------------------------------------------------

class TestAuditLoggerBreadcrumbs(unittest.TestCase):
    """AuditLogger.log_request() emits an ipc_request breadcrumb."""

    def setUp(self):
        self.fake_sdk = _make_fake_sentry_sdk()
        _init_observability(self.fake_sdk)

    def tearDown(self):
        sys.modules.pop("sentry_sdk", None)
        import backend.observability as mod
        mod._sentry_initialized = False

    def test_ipc_request_breadcrumb_emitted(self):
        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("get_history", {}, {"ok": True}, 12.5)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "ipc_request"]
        self.assertTrue(len(calls) >= 1, "ipc_request breadcrumb not emitted")

    def test_breadcrumb_data_has_method_name(self):
        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("stop_recording", {}, {"ok": True}, 55.0)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "ipc_request"]
        self.assertTrue(len(calls) >= 1)
        data = calls[0].kwargs.get("data", {})
        self.assertEqual(data.get("method"), "stop_recording")

    def test_breadcrumb_data_has_duration_ms(self):
        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("get_history", {}, {"ok": False}, 88.3)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "ipc_request"]
        data = calls[0].kwargs.get("data", {})
        self.assertIn("duration_ms", data)
        self.assertAlmostEqual(data["duration_ms"], 88.3, places=1)

    def test_breadcrumb_data_has_ok_flag(self):
        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("transcribe_file", {}, {"ok": False}, 10.0)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "ipc_request"]
        data = calls[0].kwargs.get("data", {})
        self.assertFalse(data.get("ok"))

    def test_breadcrumb_data_has_no_params_values(self):
        """Params values (potentially sensitive) must not appear in breadcrumb."""
        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            params = {"api_key": "supersecret", "text": "private input"}
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("set_settings", params, {"ok": True}, 5.0)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "ipc_request"]
        data = calls[0].kwargs.get("data", {})
        for val in data.values():
            if isinstance(val, str):
                self.assertNotIn("supersecret", val)
                self.assertNotIn("private", val)

    def test_no_breadcrumb_when_sentry_not_initialized(self):
        """When Sentry is not initialized, add_breadcrumb must not be called."""
        import backend.observability as mod
        mod._sentry_initialized = False  # Reset

        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("ping", {}, {"ok": True}, 1.0)

        self.fake_sdk.add_breadcrumb.assert_not_called()

    def test_category_is_audit(self):
        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("ping", {}, {"ok": True}, 1.0)

        calls = [c for c in self.fake_sdk.add_breadcrumb.call_args_list
                 if c.kwargs.get("message") == "ipc_request"]
        self.assertTrue(len(calls) >= 1)
        self.assertEqual(calls[0].kwargs.get("category"), "audit")


# ---------------------------------------------------------------------------
# Privacy audit — cross-cutting test
# ---------------------------------------------------------------------------

class TestBreadcrumbPrivacyContract(unittest.TestCase):
    """Cross-cutting: no breadcrumb in any module contains forbidden keys."""

    FORBIDDEN_KEYS = {"text", "transcript", "api_key", "token", "password",
                      "auth", "secret", "dsn", "credential"}

    def setUp(self):
        self.fake_sdk = _make_fake_sentry_sdk()
        _init_observability(self.fake_sdk)

    def tearDown(self):
        sys.modules.pop("sentry_sdk", None)
        import backend.observability as mod
        mod._sentry_initialized = False

    def _assert_no_forbidden_keys(self):
        for c in self.fake_sdk.add_breadcrumb.call_args_list:
            data = c.kwargs.get("data", {})
            for key in data:
                self.assertNotIn(key.lower(), self.FORBIDDEN_KEYS,
                                 f"Forbidden key '{key}' found in breadcrumb data")

    def test_audit_logger_no_forbidden_keys(self):
        from backend.audit_logger import AuditLogger
        with tempfile.TemporaryDirectory() as tmp:
            al = AuditLogger(tmp)
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                al.log_request("test_method", {"password": "secret"}, {"ok": True}, 1.0)
        self._assert_no_forbidden_keys()

    def test_state_store_no_forbidden_keys(self):
        from backend.state_store import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            with patch.dict(sys.modules, {"sentry_sdk": self.fake_sdk}):
                store.compact_with_stats()
        self._assert_no_forbidden_keys()


if __name__ == "__main__":
    unittest.main()
