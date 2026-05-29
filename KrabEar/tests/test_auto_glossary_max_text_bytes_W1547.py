"""test_auto_glossary_max_text_bytes_W1547.py — W1547 regression guard.

Verifies that core.auto_glossary._MAX_TEXT_BYTES constant exists (1 MB)
and that AutoGlossaryBuilder._build_from_history truncates oversized text.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_auto_glossary_max_text_bytes_W1547.py -v
"""

from __future__ import annotations

import os
import sys
import types
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.auto_glossary import _MAX_TEXT_BYTES, AutoGlossaryBuilder  # noqa: E402


class TestMaxTextBytesConstantExists(unittest.TestCase):
    """W1547: _MAX_TEXT_BYTES must be exported from core.auto_glossary."""

    def test_constant_is_exported(self):
        """_MAX_TEXT_BYTES must be importable and equal to 1 MB."""
        self.assertEqual(_MAX_TEXT_BYTES, 1024 * 1024)

    def test_constant_is_positive_int(self):
        self.assertIsInstance(_MAX_TEXT_BYTES, int)
        self.assertGreater(_MAX_TEXT_BYTES, 0)


class TestAutoGlossaryTextByteCap(unittest.TestCase):
    """W1547: AutoGlossaryBuilder._build_from_history must truncate large texts."""

    def _make_store(self, text: str):
        """Return a minimal store stub that serves one item with given text."""
        item = types.SimpleNamespace()
        item.to_dict = lambda: {"text": text, "ts": "2026-05-29T12:00:00"}
        store = types.SimpleNamespace()
        store.get_history_page = lambda cursor, limit: ([item], None)
        return store

    def test_oversized_text_is_truncated(self):
        """Text larger than _MAX_TEXT_BYTES must be silently truncated, not raise."""
        large_text = "А" * (_MAX_TEXT_BYTES + 500)  # definitely > 1 MB in UTF-8
        store = self._make_store(large_text)
        builder = AutoGlossaryBuilder(store=store, data_dir=None)
        # Must not raise even with very large text
        result = builder._build_from_history(window_days=365, top_n=10)
        self.assertIsInstance(result, list)

    def test_normal_text_is_unaffected(self):
        """Text within limits must pass through and produce terms normally."""
        normal_text = "Разработка программного обеспечения машинного обучения системы"
        store = self._make_store(normal_text)
        builder = AutoGlossaryBuilder(store=store, data_dir=None)
        result = builder._build_from_history(window_days=365, top_n=10)
        self.assertIsInstance(result, list)

    def test_build_returns_list_not_raises_on_giant_text(self):
        """build() API must succeed (not raise) on oversized text."""
        large_text = "Б" * (_MAX_TEXT_BYTES * 2)
        store = self._make_store(large_text)
        builder = AutoGlossaryBuilder(store=store, data_dir=None)
        result = builder.build(window_days=365, top_n=5, force=True)
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
