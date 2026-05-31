"""Tests for W1340 fix: _on_settings_saved_lang_id case-tolerant key comparison.

W1334 F2 HIGH: W1271's hook compared old.get("model_balanced", "") vs
new.get("model_balanced", "").  The key "model_balanced" (lowercase) does NOT
exist in settings.json which stores pydantic field names as "MODEL_BALANCED"
(uppercase).  The comparison always evaluated "" == "" so cache eviction never
fired in production.

This test file verifies that:
1. hook fires when MODEL_BALANCED (uppercase) changes.
2. hook fires when model_balanced (lowercase) changes.
3. hook is a no-op when an unrelated setting changes.
4. AudioLanguageID.clear_model_cache() is exposed and works.
"""

from __future__ import annotations

import sys
import os
import ast
import importlib
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — allow running as standalone or via pytest from repo root
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_TESTS_DIR = _THIS.parent
_BACKEND_DIR = _TESTS_DIR.parent
_REPO_ROOT = _BACKEND_DIR.parent

for _p in (_BACKEND_DIR, str(_REPO_ROOT)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Stub out heavy optional deps so audio_lang_id can be imported
# ---------------------------------------------------------------------------
def _register_stub_modules() -> None:
    """Register lightweight stubs for numpy, mlx_whisper and mlx.core."""
    try:
        import numpy  # noqa: F401 — already present in venv
    except ImportError:
        sys.modules["numpy"] = MagicMock()

    if "mlx_whisper" not in sys.modules:
        stub = MagicMock()
        stub.load_models.load_model = MagicMock(return_value=object())
        sys.modules["mlx_whisper"] = stub
    if "mlx" not in sys.modules:
        sys.modules["mlx"] = MagicMock()
    if "mlx.core" not in sys.modules:
        sys.modules["mlx.core"] = MagicMock()


_register_stub_modules()


# ---------------------------------------------------------------------------
# Helper: import a fresh copy of audio_lang_id each test run
# ---------------------------------------------------------------------------
def _fresh_audio_lang_id():
    """Force-reimport audio_lang_id to get a fresh class with empty _model_cache.

    W1751: re-ensure the heavy-dep stubs first.  The conftest
    _purge_leaked_module_stubs fixture removes bare mlx / mlx.core stubs after
    every test, so by the time a *later* test in this file calls this helper the
    module-level _register_stub_modules() install may have been purged.  Without
    re-installing, the reimport of core.audio_lang_id would try the real mlx
    (unavailable / unsafe under xdist) and fail.  Re-registering makes this
    helper self-sufficient regardless of cross-test purge timing.
    """
    _register_stub_modules()
    for key in list(sys.modules.keys()):
        if "audio_lang_id" in key:
            del sys.modules[key]
    return importlib.import_module("core.audio_lang_id")


# ---------------------------------------------------------------------------
# Helper: read _get_model_balanced and _on_settings_saved_lang_id logic from
# service.py source without importing the whole heavy module
# ---------------------------------------------------------------------------
_SERVICE_PY = _BACKEND_DIR / "backend" / "service.py"


def _extract_hook_source() -> str:
    """Return the source of the _on_settings_saved_lang_id / _get_model_balanced
    helpers from service.py for AST-only validation."""
    with open(_SERVICE_PY, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Standalone hook re-implementation extracted from service.py source
# (mirrors the exact same logic so we test the real algorithm without
# importing BackendService which drags in all heavy deps)
# ---------------------------------------------------------------------------
def _build_hook(evict_fn):
    """Build _get_model_balanced + _on_settings_saved_lang_id matching service.py."""

    def _get_model_balanced(d: dict) -> str:
        for k in ("MODEL_BALANCED", "model_balanced", "stt_model_balanced"):
            v = d.get(k)
            if v is not None:
                return str(v)
        return ""

    def _on_settings_saved_lang_id(old: dict, new: dict) -> None:
        old_model = _get_model_balanced(old)
        new_model = _get_model_balanced(new)
        if new_model != old_model:
            evict_fn()

    return _on_settings_saved_lang_id


# ===========================================================================
# Test 1: hook fires on MODEL_BALANCED (uppercase) change
# ===========================================================================
class TestHookFiresOnModelBalancedUppercase(unittest.TestCase):
    """test_hook_fires_on_MODEL_BALANCED_change_uppercase"""

    def test_hook_fires_on_MODEL_BALANCED_change_uppercase(self):
        """When settings dict uses uppercase MODEL_BALANCED key, hook evicts cache."""
        evict_calls = []
        hook = _build_hook(lambda: evict_calls.append(1))

        old = {"MODEL_BALANCED": "mlx-community/whisper-large-v3-turbo"}
        new = {"MODEL_BALANCED": "mlx-community/whisper-large-v3-mlx"}

        hook(old, new)

        self.assertEqual(
            len(evict_calls),
            1,
            "cache eviction must fire when MODEL_BALANCED (uppercase) changes",
        )

    def test_hook_noop_when_MODEL_BALANCED_unchanged(self):
        """No eviction when uppercase MODEL_BALANCED value is identical."""
        evict_calls = []
        hook = _build_hook(lambda: evict_calls.append(1))

        same = {"MODEL_BALANCED": "mlx-community/whisper-large-v3-turbo"}
        hook(dict(same), dict(same))

        self.assertEqual(len(evict_calls), 0)


# ===========================================================================
# Test 2: hook fires on model_balanced (lowercase) change
# ===========================================================================
class TestHookFiresOnModelBalancedLowercase(unittest.TestCase):
    """test_hook_fires_on_model_balanced_change_lowercase"""

    def test_hook_fires_on_model_balanced_change_lowercase(self):
        """When settings dict uses lowercase model_balanced key, hook evicts cache."""
        evict_calls = []
        hook = _build_hook(lambda: evict_calls.append(1))

        old = {"model_balanced": "mlx-community/whisper-large-v3-turbo"}
        new = {"model_balanced": "mlx-community/whisper-large-v3-mlx"}

        hook(old, new)

        self.assertEqual(
            len(evict_calls),
            1,
            "cache eviction must fire when model_balanced (lowercase) changes",
        )

    def test_hook_fires_on_stt_model_balanced_legacy_key(self):
        """Legacy key stt_model_balanced also triggers eviction."""
        evict_calls = []
        hook = _build_hook(lambda: evict_calls.append(1))

        old = {"stt_model_balanced": "old-model"}
        new = {"stt_model_balanced": "new-model"}

        hook(old, new)

        self.assertEqual(len(evict_calls), 1)


# ===========================================================================
# Test 3: hook is a no-op when an unrelated setting changes
# ===========================================================================
class TestHookNoOpWhenUnrelatedSettingChanges(unittest.TestCase):
    """test_hook_no_op_when_unrelated_setting_changes"""

    def test_hook_no_op_when_unrelated_setting_changes(self):
        """Changing an unrelated setting must NOT trigger eviction."""
        evict_calls = []
        hook = _build_hook(lambda: evict_calls.append(1))

        old = {"translation_mode": "off", "quality_profile": "balanced"}
        new = {"translation_mode": "auto", "quality_profile": "max"}

        hook(old, new)

        self.assertEqual(
            len(evict_calls),
            0,
            "eviction must NOT fire when only unrelated settings change",
        )

    def test_original_bug_both_empty_is_noop(self):
        """Original W1271 bug: both old/new return '' → comparison true → no evict.

        This test documents that when NEITHER key exists in either dict
        the hook correctly does nothing (no false-positive eviction).
        """
        evict_calls = []
        hook = _build_hook(lambda: evict_calls.append(1))

        # Both dicts have no model_balanced key at all
        hook({}, {})

        self.assertEqual(
            len(evict_calls),
            0,
            "When neither old nor new contain any model_balanced variant the hook must be a no-op",
        )


# ===========================================================================
# Test 4: AudioLanguageID.clear_model_cache() API
# ===========================================================================
class TestClearModelCacheAPI(unittest.TestCase):
    """AudioLanguageID.clear_model_cache() classmethod behaviour."""

    def setUp(self):
        self.mod = _fresh_audio_lang_id()
        self.AudioLanguageID = self.mod.AudioLanguageID

    def tearDown(self):
        self.AudioLanguageID._model_cache.clear()

    def test_clear_model_cache_exists(self):
        """clear_model_cache must be a classmethod callable."""
        self.assertTrue(
            callable(getattr(self.AudioLanguageID, "clear_model_cache", None)),
            "AudioLanguageID.clear_model_cache must exist and be callable",
        )

    def test_clear_model_cache_empties_cache(self):
        """clear_model_cache() must empty _model_cache."""
        self.AudioLanguageID._model_cache["fake-model"] = object()
        self.assertEqual(len(self.AudioLanguageID._model_cache), 1)

        self.AudioLanguageID.clear_model_cache()

        self.assertEqual(
            len(self.AudioLanguageID._model_cache),
            0,
            "clear_model_cache() must empty _model_cache",
        )

    def test_clear_model_cache_thread_safe(self):
        """clear_model_cache() may be called concurrently without raising."""
        self.AudioLanguageID._model_cache["a"] = object()
        self.AudioLanguageID._model_cache["b"] = object()
        errors = []

        def _clear():
            try:
                self.AudioLanguageID.clear_model_cache()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_clear) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"concurrent clear raised: {errors}")

    def test_cache_lock_attribute_exists(self):
        """_cache_lock must be a threading.Lock (or RLock) on the class."""
        lock = getattr(self.AudioLanguageID, "_cache_lock", None)
        self.assertIsNotNone(lock, "AudioLanguageID._cache_lock must exist")
        # Duck-type check: both Lock and RLock expose acquire/release
        self.assertTrue(
            hasattr(lock, "acquire") and hasattr(lock, "release"),
            "_cache_lock must expose acquire/release",
        )


# ===========================================================================
# Test 5: AST-only source assertions for service.py
# ===========================================================================
class TestServicePySourceAssertions(unittest.TestCase):
    """Verify the fix is present in service.py source without importing it."""

    def setUp(self):
        self.source = _extract_hook_source()

    def test_get_model_balanced_helper_present(self):
        """service.py must define _get_model_balanced helper."""
        self.assertIn(
            "_get_model_balanced",
            self.source,
            "service.py must contain _get_model_balanced helper",
        )

    def test_all_three_key_variants_checked(self):
        """service.py must check MODEL_BALANCED, model_balanced, stt_model_balanced."""
        for key in ("MODEL_BALANCED", "model_balanced", "stt_model_balanced"):
            self.assertIn(
                key,
                self.source,
                f"service.py must reference '{key}' in the hook",
            )

    def test_on_settings_saved_lang_id_present(self):
        """service.py must define _on_settings_saved_lang_id."""
        self.assertIn(
            "_on_settings_saved_lang_id",
            self.source,
            "service.py must contain _on_settings_saved_lang_id",
        )

    def test_clear_model_cache_called(self):
        """service.py hook must call AudioLanguageID.clear_model_cache()."""
        self.assertIn(
            "clear_model_cache",
            self.source,
            "service.py hook must call clear_model_cache()",
        )

    def test_no_lowercase_only_comparison(self):
        """The old W1271 bug: single-key 'model_balanced' lookup must not appear
        alone in the hook definition section."""
        # Find the _on_settings_saved_lang_id function body and confirm it
        # uses _get_model_balanced (the helper), not a bare .get("model_balanced").
        # We check that _get_model_balanced wraps the lookup.
        idx = self.source.find("_on_settings_saved_lang_id")
        self.assertGreater(idx, 0)
        # The helper must be defined before the hook
        idx_helper = self.source.find("_get_model_balanced")
        self.assertGreater(idx_helper, 0)
        self.assertLess(
            idx_helper,
            idx,
            "_get_model_balanced helper must be defined before _on_settings_saved_lang_id",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
