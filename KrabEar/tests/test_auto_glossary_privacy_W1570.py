"""Тесты W1570 — privacy guards в AutoGlossaryBuilder.

Покрывает:
  N1: invalidate() не пишет на диск когда privacy_mode активен.
  N2: build() сбрасывает кэш загруженный до включения privacy_mode.
  N2b: build() возвращает [] с кэшем когда privacy_mode активен.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.auto_glossary import AutoGlossaryBuilder, AUTO_GLOSSARY_CACHE_FILE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal stub StateStore."""

    def __init__(self, items=None):
        self._items = items or []

    def get_history_page(self, cursor=None, limit=500):
        return self._items, None


def _make_builder_with_privacy(tmp_dir: Path, privacy_mode: bool) -> AutoGlossaryBuilder:
    """Build AutoGlossaryBuilder whose settings_provider returns privacy_mode."""
    settings = {"privacy_mode": privacy_mode}
    return AutoGlossaryBuilder(
        store=_FakeStore(),
        data_dir=tmp_dir,
        settings_provider=lambda: settings,
    )


def _make_builder_toggle(tmp_dir: Path, toggle_box: list) -> AutoGlossaryBuilder:
    """Build AutoGlossaryBuilder whose privacy_mode is read from toggle_box[0]."""
    return AutoGlossaryBuilder(
        store=_FakeStore(),
        data_dir=tmp_dir,
        settings_provider=lambda: {"privacy_mode": toggle_box[0]},
    )


# ---------------------------------------------------------------------------
# N1: invalidate() must not write disk in privacy mode
# ---------------------------------------------------------------------------

class TestInvalidateNoDiskWriteInPrivacyMode(unittest.TestCase):
    """W1570 N1 — invalidate() skips _save_cache_to_disk() when privacy_mode."""

    def test_invalidate_no_disk_write_in_privacy_mode(self) -> None:
        """No auto_glossary.json must be created by invalidate() in privacy mode."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            builder = _make_builder_with_privacy(tmp_path, privacy_mode=True)
            # Pre-seed an in-memory cache so invalidate has something to reset.
            builder._cache = ["SomeTerm", "AnotherTerm"]
            builder._cache_built_at = time.time()

            builder.invalidate()

            cache_file = tmp_path / AUTO_GLOSSARY_CACHE_FILE
            self.assertFalse(
                cache_file.exists(),
                msg="invalidate() must NOT write cache file when privacy_mode is active",
            )

    def test_invalidate_writes_disk_when_privacy_off(self) -> None:
        """invalidate() SHOULD write the cache file when privacy_mode is off."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            builder = _make_builder_with_privacy(tmp_path, privacy_mode=False)
            builder._cache = ["PublicTerm"]
            builder._cache_built_at = time.time()

            builder.invalidate()

            cache_file = tmp_path / AUTO_GLOSSARY_CACHE_FILE
            # File should exist (even if empty / terms=[])
            self.assertTrue(
                cache_file.exists(),
                msg="invalidate() MUST write cache file when privacy_mode is off",
            )

    def test_invalidate_clears_memory_in_privacy_mode(self) -> None:
        """Cache must be cleared from memory even in privacy mode."""
        with tempfile.TemporaryDirectory() as tmp:
            builder = _make_builder_with_privacy(Path(tmp), privacy_mode=True)
            builder._cache = ["SomeTerm"]
            builder._cache_built_at = time.time()

            builder.invalidate()

            self.assertEqual(builder._cache, [])
            self.assertEqual(builder._cache_built_at, 0.0)


# ---------------------------------------------------------------------------
# N2: stale pre-privacy cache cleared after toggle
# ---------------------------------------------------------------------------

class TestStaleCacheClearedAfterPrivacyToggle(unittest.TestCase):
    """W1570 N2 — build() clears stale cache when privacy_mode is toggled on."""

    def test_stale_cache_cleared_after_privacy_toggle(self) -> None:
        """Cache built before privacy_mode=True is wiped on next build() call."""
        with tempfile.TemporaryDirectory() as tmp:
            toggle = [False]  # start with privacy off
            builder = _make_builder_toggle(Path(tmp), toggle)

            # Manually inject a "pre-privacy" cache as if it were freshly loaded.
            builder._cache = ["PrePrivacyTerm"]
            builder._cache_built_at = time.time()  # appears valid (not expired)

            # Now toggle privacy mode ON
            toggle[0] = True

            result = builder.build()

            self.assertEqual(result, [], "build() must return [] in privacy mode")
            self.assertEqual(
                builder._cache, [],
                msg="Stale pre-privacy cache must be cleared from memory",
            )
            self.assertEqual(builder._cache_built_at, 0.0)

    def test_build_returns_empty_in_privacy_mode_even_with_loaded_cache(self) -> None:
        """build() returns [] and clears cache even if _is_cache_valid() would be True."""
        with tempfile.TemporaryDirectory() as tmp:
            toggle = [True]  # privacy mode active from start
            builder = _make_builder_toggle(Path(tmp), toggle)

            # Inject stale cache that would pass _is_cache_valid() check
            builder._cache = ["ShouldBeHidden"]
            builder._cache_built_at = time.time()

            result = builder.build()

            self.assertEqual(result, [])
            self.assertEqual(builder._cache, [])

    def test_build_uses_cache_after_privacy_toggle_off(self) -> None:
        """After privacy mode is disabled build() can return cached terms again."""
        with tempfile.TemporaryDirectory() as tmp:
            toggle = [True]
            builder = _make_builder_toggle(Path(tmp), toggle)

            # With privacy on, build clears and returns empty
            builder._cache = ["ShouldBeCleared"]
            builder._cache_built_at = time.time()
            result = builder.build()
            self.assertEqual(result, [])

            # Turn privacy off — now build() should attempt fresh build
            toggle[0] = False
            # Cache is empty and built_at=0 so _is_cache_valid() is False;
            # build() will try store but store has no items → []
            result2 = builder.build()
            self.assertEqual(result2, [])  # store is empty, OK

    def test_no_disk_write_from_build_in_privacy_mode(self) -> None:
        """build() must not write cache to disk when privacy_mode is active."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            builder = _make_builder_with_privacy(tmp_path, privacy_mode=True)

            # Inject pre-privacy cache
            builder._cache = ["PrePrivacyTerm"]
            builder._cache_built_at = time.time()

            builder.build()

            cache_file = tmp_path / AUTO_GLOSSARY_CACHE_FILE
            self.assertFalse(
                cache_file.exists(),
                msg="build() must NOT write cache file when privacy_mode is active",
            )


# ---------------------------------------------------------------------------
# Regression: existing privacy guard in build() still works
# ---------------------------------------------------------------------------

class TestExistingPrivacyGuardBuildRegressionW1570(unittest.TestCase):
    """Ensure W1570 changes don't break pre-existing privacy guard in build()."""

    def test_build_fresh_empty_store_privacy_on(self) -> None:
        """build() with empty store + privacy_mode returns [] without disk writes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            builder = _make_builder_with_privacy(tmp_path, privacy_mode=True)

            result = builder.build()

            self.assertEqual(result, [])
            self.assertFalse((tmp_path / AUTO_GLOSSARY_CACHE_FILE).exists())

    def test_invalidate_then_build_privacy_on_no_disk(self) -> None:
        """Calling invalidate() then build() in privacy mode leaves no disk artifact."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            builder = _make_builder_with_privacy(tmp_path, privacy_mode=True)
            builder._cache = ["Term"]
            builder._cache_built_at = time.time()

            builder.invalidate()
            builder.build()

            self.assertFalse((tmp_path / AUTO_GLOSSARY_CACHE_FILE).exists())


if __name__ == "__main__":
    unittest.main()
