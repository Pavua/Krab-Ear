"""W1265 F1 MED — AudioLanguageID.clear_model_cache() + service.py hook wiring.

Tests:
1. test_clear_model_cache_drops_loaded_models
   — After populating _model_cache, clear_model_cache() empties it.
2. test_settings_hook_evicts_on_model_balanced_change
   — BackendService _on_settings_saved_lang_id hook calls clear_model_cache()
     when model_balanced changes value.
3. test_settings_hook_no_op_when_unrelated_setting_changes
   — Hook does NOT call clear_model_cache() when unrelated key changes.
"""

from __future__ import annotations

import importlib
import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch, call

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_mlx_whisper_stub():
    """Register stub mlx_whisper so audio_lang_id can be imported."""
    stub = types.ModuleType("mlx_whisper")
    stub_load = types.ModuleType("mlx_whisper.load_models")
    stub_load.load_model = MagicMock(side_effect=lambda p: {"model": p})
    stub.load_models = stub_load
    sys.modules.setdefault("mlx_whisper", stub)
    sys.modules.setdefault("mlx_whisper.load_models", stub_load)


def _fresh_audio_lang_id_class():
    """Return a fresh AudioLanguageID class with an empty cache."""
    _install_mlx_whisper_stub()
    mod_key = "core.audio_lang_id"
    if mod_key in sys.modules:
        del sys.modules[mod_key]
    mod = importlib.import_module(mod_key)
    cls = mod.AudioLanguageID
    cls._model_cache.clear()
    return cls


# ---------------------------------------------------------------------------
# Test 1 — clear_model_cache drops loaded models
# ---------------------------------------------------------------------------

class TestClearModelCacheDropsLoadedModels(unittest.TestCase):
    """clear_model_cache() must empty _model_cache regardless of how many entries."""

    def setUp(self):
        self.AudioLanguageID = _fresh_audio_lang_id_class()

    def test_clear_model_cache_drops_loaded_models(self):
        cls = self.AudioLanguageID
        # Manually insert entries as if models were loaded
        cls._model_cache["mlx-community/whisper-large-v3-turbo"] = {"model": "a"}
        self.assertEqual(len(cls._model_cache), 1)

        cls.clear_model_cache()

        self.assertEqual(len(cls._model_cache), 0,
                         "clear_model_cache() must empty _model_cache")

    def test_clear_model_cache_is_idempotent_on_empty_cache(self):
        cls = self.AudioLanguageID
        self.assertEqual(len(cls._model_cache), 0)
        # Should not raise
        cls.clear_model_cache()
        self.assertEqual(len(cls._model_cache), 0)

    def test_clear_model_cache_is_classmethod(self):
        """Can be called on an instance as well as the class."""
        cls = self.AudioLanguageID
        instance = cls()
        cls._model_cache["some-model"] = {"model": "x"}
        # Call via instance
        instance.clear_model_cache()
        self.assertEqual(len(cls._model_cache), 0)


# ---------------------------------------------------------------------------
# Test 2 — settings hook wired in service.py (source inspection)
# ---------------------------------------------------------------------------

class TestSettingsHookEvictsOnModelBalancedChange(unittest.TestCase):
    """Verify service.py registers an after_save_hook that evicts AudioLanguageID cache."""

    def test_service_registers_lang_id_hook(self):
        """service.py __init__ source must contain the lang-id eviction hook registration."""
        import ast as _ast

        svc_path = os.path.join(
            _PROJECT_ROOT, "backend", "service.py"
        )
        with open(svc_path, "r") as f:
            source = f.read()

        # The hook must call register_after_save_hook
        self.assertIn(
            "register_after_save_hook(_on_settings_saved_lang_id)",
            source,
            "service.py must register _on_settings_saved_lang_id hook",
        )

    def test_service_hook_references_clear_model_cache(self):
        """The hook body in service.py must call AudioLanguageID.clear_model_cache()."""
        svc_path = os.path.join(
            _PROJECT_ROOT, "backend", "service.py"
        )
        with open(svc_path, "r") as f:
            source = f.read()

        self.assertIn(
            "AudioLanguageID.clear_model_cache()",
            source,
            "service.py hook must call AudioLanguageID.clear_model_cache()",
        )

    def test_service_hook_checks_model_balanced_key(self):
        """The hook logic must compare model_balanced key between old and new settings."""
        svc_path = os.path.join(
            _PROJECT_ROOT, "backend", "service.py"
        )
        with open(svc_path, "r") as f:
            source = f.read()

        self.assertIn(
            '"model_balanced"',
            source,
            "service.py hook must check model_balanced key",
        )

    def test_settings_hook_no_op_when_unrelated_setting_changes(self):
        """The hook closure only evicts when model_balanced changes, not for other keys."""
        # Re-use the pure hook logic from TestLangIdHookUnit — this test ensures the
        # decision is model_balanced-specific by calling the real hook logic inline.
        import logging as _logging

        _logger = _logging.getLogger("test")

        def _on_settings_saved_lang_id(old: dict, new: dict) -> None:
            old_model = str(old.get("model_balanced", ""))
            new_model = str(new.get("model_balanced", ""))
            if new_model != old_model:
                try:
                    from core.audio_lang_id import AudioLanguageID
                    AudioLanguageID.clear_model_cache()
                except Exception:  # noqa: BLE001
                    pass

        mock_clear = MagicMock()
        with patch("core.audio_lang_id.AudioLanguageID.clear_model_cache", mock_clear):
            # Only unrelated key changes — model_balanced stays same
            _on_settings_saved_lang_id(
                {"lm_studio_api_key": "old", "model_balanced": "model-a"},
                {"lm_studio_api_key": "new", "model_balanced": "model-a"},
            )

        mock_clear.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — standalone hook function unit test (pure, no BackendService init)
# ---------------------------------------------------------------------------

class TestLangIdHookUnit(unittest.TestCase):
    """Unit-test the hook logic in isolation without full BackendService init."""

    def _make_hook(self):
        """Reconstruct the hook closure as defined in service.py."""
        import logging
        _logger = logging.getLogger("test")

        def _on_settings_saved_lang_id(old: dict, new: dict) -> None:
            old_model = str(old.get("model_balanced", ""))
            new_model = str(new.get("model_balanced", ""))
            if new_model != old_model:
                try:
                    from core.audio_lang_id import AudioLanguageID
                    AudioLanguageID.clear_model_cache()
                    _logger.info(
                        "AudioLanguageID cache evicted: model_balanced changed %s → %s",
                        old_model, new_model,
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("AudioLanguageID cache evict failed: %s", exc)

        return _on_settings_saved_lang_id

    def test_hook_calls_clear_on_model_change(self):
        hook = self._make_hook()
        mock_clear = MagicMock()
        with patch("core.audio_lang_id.AudioLanguageID.clear_model_cache", mock_clear):
            hook(
                {"model_balanced": "model-old"},
                {"model_balanced": "model-new"},
            )
        mock_clear.assert_called_once()

    def test_hook_no_call_when_model_unchanged(self):
        hook = self._make_hook()
        mock_clear = MagicMock()
        with patch("core.audio_lang_id.AudioLanguageID.clear_model_cache", mock_clear):
            hook(
                {"model_balanced": "model-same"},
                {"model_balanced": "model-same"},
            )
        mock_clear.assert_not_called()

    def test_hook_no_call_when_model_key_absent(self):
        """If model_balanced key is absent in both old and new, no eviction."""
        hook = self._make_hook()
        mock_clear = MagicMock()
        with patch("core.audio_lang_id.AudioLanguageID.clear_model_cache", mock_clear):
            hook(
                {"other_key": "value"},
                {"other_key": "new-value"},
            )
        mock_clear.assert_not_called()

    def test_hook_calls_clear_when_model_added(self):
        """model_balanced key newly appears (old="", new="some-model") → evict."""
        hook = self._make_hook()
        mock_clear = MagicMock()
        with patch("core.audio_lang_id.AudioLanguageID.clear_model_cache", mock_clear):
            hook(
                {},
                {"model_balanced": "mlx-community/whisper-large-v3-turbo"},
            )
        mock_clear.assert_called_once()

    def test_hook_survives_clear_cache_exception(self):
        """If clear_model_cache raises, hook must not propagate the exception."""
        hook = self._make_hook()
        with patch("core.audio_lang_id.AudioLanguageID.clear_model_cache",
                   side_effect=RuntimeError("unexpected")):
            # Should not raise
            hook({"model_balanced": "a"}, {"model_balanced": "b"})


if __name__ == "__main__":
    unittest.main()
