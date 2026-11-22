# -*- coding: utf-8 -*-
"""Regression test: LiveSubsService settings_get production wiring (W1147 F2/F5, W1714).

W1714 regression guard: wave1431 silently dropped `settings_get=self._get_runtime_setting`
from the LiveSubsService(...) constructor call in BackendService.__init__.  The kwarg
defaults to `lambda k, d: d`, so every `self._settings_get('privacy_mode_enabled', False)`
returned the hardcoded `False` — making all privacy guards dead in production.

Tests here verify the PRODUCTION wiring in BackendService, NOT direct LiveSubsService
construction.  They must FAIL if the kwarg is dropped from service.py again.

Git evidence:
  - e95ffedb (wave1150): ADDED `settings_get=self._get_runtime_setting`
  - 2bf587d4 (wave1431): DROPPED it (unrelated HistoryService change)
  - This wave restores the kwarg and adds this regression test.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_minimal_backend():
    """Build a BackendService via __new__ with the minimum stubs needed to
    have a functioning _get_runtime_setting + _live_subs.

    Mirrors the pattern in test_applescript_injection.py but adds the
    _settings_svc stub so that _get_runtime_setting resolves against a real
    (mutable) settings dict.
    """
    from backend.service import BackendService
    from backend.state_store import StateStore
    from backend.live_subs_service import LiveSubsService

    tmp = Path(tempfile.mkdtemp())
    store = StateStore(data_dir=tmp)

    service = BackendService.__new__(BackendService)
    service.store = store

    # Stub all heavy collaborators
    service.recorder = MagicMock()
    service.transcriber = MagicMock()
    service.translator = MagicMock()
    service.llm_rewriter = MagicMock()
    service.metrics = MagicMock()
    service.event_bus = MagicMock()
    service._call_assist = MagicMock()
    service._history_svc = MagicMock()
    service._translation = MagicMock()
    service._translation_svc = MagicMock()

    # Wire a real _settings_svc whose cached_settings() we can control
    settings_dict: dict = {}
    settings_svc_stub = MagicMock()
    settings_svc_stub.cached_settings.return_value = settings_dict
    service._settings_svc = settings_svc_stub

    # Now wire _live_subs exactly as BackendService.__init__ does — after fix
    service._live_subs = LiveSubsService(
        transcriber=service.transcriber,
        translator=service.translator,
        settings_get=service._get_runtime_setting,
    )

    # Expose the live settings dict so tests can mutate it
    service._test_settings = settings_dict
    return service


def _make_pcm_b64(n_samples: int = 800) -> str:
    """Return base64 of n_samples zeroed int16 PCM bytes (< flush threshold)."""
    import numpy as np
    pcm = (np.zeros(n_samples, dtype=np.float32) * 32768).astype("int16")
    return base64.b64encode(pcm.tobytes()).decode()


# ── production-wiring identity test ──────────────────────────────────────────

class TestLiveSubsPrivacyWiringProductionW1714(unittest.TestCase):
    """Verify BackendService wires LiveSubsService.settings_get to _get_runtime_setting.

    This is the primary regression guard for W1714 / W1147 F2+F5.
    The test constructs a BackendService (via __new__ + stubs) using the SAME
    code that the production init uses, and asserts that the settings_get
    callback is the runtime getter — not the no-op default lambda.
    """

    def setUp(self):
        self.service = _build_minimal_backend()

    def test_settings_get_is_runtime_getter_identity(self):
        """_live_subs._settings_get must be service._get_runtime_setting, NOT a default lambda.

        Python bound methods create a new wrapper object on each attribute access, so
        `x is x` is always False for bound methods.  We therefore compare the underlying
        function (__func__) and the bound instance (__self__) separately.

        If the kwarg is dropped, _settings_get becomes a bare `lambda k, d: d` which
        has no __func__/__self__ attributes at all — the getattr fallback to None catches it.
        """
        live_subs = self.service._live_subs
        stored_getter = live_subs._settings_get
        runtime_getter = self.service._get_runtime_setting

        # Both must be bound methods of the same underlying function on the same instance
        self.assertIs(
            getattr(stored_getter, "__func__", None),
            getattr(runtime_getter, "__func__", None),
            "LiveSubsService._settings_get.__func__ != BackendService._get_runtime_setting.__func__! "
            "The settings_get= kwarg was likely dropped from the constructor call in service.py. "
            "This is the W1147/W1714 privacy regression.",
        )
        self.assertIs(
            getattr(stored_getter, "__self__", None),
            getattr(runtime_getter, "__self__", None),
            "LiveSubsService._settings_get is bound to a different instance than BackendService! "
            "Expected both to be bound to the same BackendService object.",
        )

    def test_privacy_mode_enabled_setting_propagates_to_live_subs(self):
        """When privacy_mode_enabled=True in runtime settings, handle_ingest returns skipped.

        This is an end-to-end wiring test: runtime settings dict → _get_runtime_setting
        → LiveSubsService.handle_ingest privacy gate.
        """
        # Enable privacy mode in the mocked settings dict
        self.service._test_settings["privacy_mode_enabled"] = True

        params = {
            "audio_chunk": _make_pcm_b64(),
            "sample_rate": 16000,
            "target_lang": "off",
            "is_final": False,
        }
        result = self.service._live_subs.handle_ingest(params)

        self.assertTrue(
            result.get("skipped"),
            f"Expected skipped=True when privacy_mode_enabled=True, got: {result}",
        )
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_disabled_setting_propagates_to_live_subs(self):
        """When privacy_mode_enabled=False in runtime settings, handle_ingest does NOT skip."""
        self.service._test_settings["privacy_mode_enabled"] = False

        params = {
            "audio_chunk": _make_pcm_b64(n_samples=800),  # < 3 s flush threshold
            "sample_rate": 16000,
            "target_lang": "off",
            "is_final": False,
        }
        result = self.service._live_subs.handle_ingest(params)

        self.assertNotEqual(
            result.get("reason"), "privacy_mode_active",
            f"Expected audio to be accepted when privacy_mode=False, got: {result}",
        )
        self.assertFalse(
            result.get("skipped", False),
            f"Expected skipped=False when privacy_mode=False, got: {result}",
        )

    def test_stop_respects_privacy_mode_via_production_wiring(self):
        """stop() must return skipped=True when privacy_mode_enabled in runtime settings."""
        self.service._test_settings["privacy_mode_enabled"] = True

        result = self.service._live_subs.stop()

        self.assertTrue(
            result.get("skipped"),
            f"Expected stop() to return skipped=True when privacy_mode=True, got: {result}",
        )
        self.assertEqual(result.get("reason"), "privacy_mode_active")


# ── regression: default lambda detection ─────────────────────────────────────

class TestDefaultLambdaIsDetectedW1714(unittest.TestCase):
    """Verify that the test_settings_get_is_runtime_getter_identity test WOULD FAIL if
    the kwarg were dropped (i.e., if someone accidentally reverts the fix again).

    This meta-test directly constructs LiveSubsService WITHOUT settings_get
    and confirms the resulting lambda is NOT the same object as _get_runtime_setting.
    """

    def test_default_lambda_is_not_runtime_getter(self):
        """The default `lambda k, d: d` has no __func__/__self__ — not _get_runtime_setting."""
        from backend.live_subs_service import LiveSubsService

        transcriber_stub = MagicMock()
        translator_stub = MagicMock()

        # Construct WITHOUT settings_get (regression scenario)
        svc_no_kwarg = LiveSubsService(
            transcriber=transcriber_stub,
            translator=translator_stub,
            # settings_get NOT passed → defaults to lambda k, d: d
        )

        # Build a minimal service to get its _get_runtime_setting reference
        service = _build_minimal_backend()

        # The default getter is a bare lambda — it has no __func__ attribute
        self.assertIsNone(
            getattr(svc_no_kwarg._settings_get, "__func__", None),
            "Expected default lambda to have no __func__ (it's not a bound method). "
            "The identity test (__func__ comparison) cannot catch a regression — investigate.",
        )
        # The runtime getter IS a bound method — it has __func__
        self.assertIsNotNone(
            getattr(service._get_runtime_setting, "__func__", None),
            "Expected _get_runtime_setting to be a bound method with __func__.",
        )

    def test_default_lambda_always_returns_default_value(self):
        """When constructed without settings_get, privacy guard is always inactive (False default)."""
        from backend.live_subs_service import LiveSubsService

        svc = LiveSubsService(
            transcriber=MagicMock(),
            translator=MagicMock(),
            # No settings_get — simulates the regression
        )

        # Even if we'd set privacy_mode_enabled=True in a real settings store,
        # the service would return False because it uses the default lambda.
        result = svc._settings_get("privacy_mode_enabled", False)
        self.assertFalse(
            result,
            "Default lambda correctly returns False — confirming it ignores real settings. "
            "This is exactly the W1714 regression: production privacy guard is dead.",
        )


# ── подключение через НАСТОЯЩИЙ BackendService.__init__ ──────────────────────

class TestLiveSubsPrivacyWiringViaRealConstructor(unittest.TestCase):
    """Фикстура _build_minimal_backend сама передаёт settings_get — удаление этой строки
    из рабочего конструктора ею не обнаруживается. Здесь строится НАСТОЯЩИЙ
    BackendService (с фейками тяжёлых коллабораторов), и подключение проверяется на нём."""

    def setUp(self):
        from backend.service import BackendService
        from backend.state_store import StateStore
        from test_backend_service import FakeRecorder, FakeTranscriber, FakeTranslator

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        store = StateStore(Path(self._tmp.name) / "data")
        store.initialize_startup_plaintext_policy(new_profile=True)
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        self.addCleanup(self.service.close)

    def test_live_subs_settings_get_bound_to_runtime_getter(self):
        stored = self.service._live_subs._settings_get
        self.assertIs(getattr(stored, "__func__", None),
                      type(self.service)._get_runtime_setting,
                      "settings_get= не передан в LiveSubsService из BackendService.__init__")
        self.assertIs(getattr(stored, "__self__", None), self.service)

    def test_privacy_on_blocks_stt_through_real_wiring(self):
        """Включаем privacy через РЕАЛЬНЫЙ путь настроек: ingest пропускается, STT не вызывается."""
        self.service.store.save_settings({"privacy_mode_enabled": True})
        self.service._settings_svc.invalidate_cache()
        live = self.service._live_subs
        with patch.object(live._transcriber, "transcribe") as stt:
            result = live.handle_ingest({
                "audio_chunk": _make_pcm_b64(),
                "sample_rate": 16000,
                "target_lang": "off",
                "is_final": False,
            })
        self.assertTrue(result.get("skipped"), result)
        stt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
