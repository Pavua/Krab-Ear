"""Tests for translation.timeout error push (Phase B.2 F3)."""

from __future__ import annotations

import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator


def _make_translator(with_bus: bool = True) -> Translator:
    """Build a Translator with bypassed __init__ (no real model loading)."""
    t = Translator.__new__(Translator)
    t._pipelines = {}
    t._unavailable = set()
    t._cache = OrderedDict()
    t._cache_capacity = 500
    if with_bus:
        t._error_bus = MagicMock()
    return t


class TranslatorPushErrorHelperTests(unittest.TestCase):
    """Unit tests for Translator._push_error helper."""

    def test_no_bus_does_not_raise(self) -> None:
        """_push_error with no _error_bus set must not raise."""
        t = _make_translator(with_bus=False)
        t._push_error("translation.timeout", "test debug")  # must not raise

    def test_broken_bus_does_not_raise(self) -> None:
        """If error_bus.push itself throws, _push_error swallows the exception."""
        t = _make_translator()
        t._error_bus.push.side_effect = RuntimeError("bus broken")
        t._push_error("translation.timeout", "some debug info")  # must not raise

    def test_push_calls_bus_with_correct_code(self) -> None:
        t = _make_translator()
        t._push_error("translation.timeout", "debug msg")
        self.assertEqual(t._error_bus.push.call_count, 1)
        pushed = t._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "translation.timeout")
        self.assertEqual(pushed.component, "translation")
        self.assertEqual(pushed.severity, "warn")

    def test_push_message_debug_included(self) -> None:
        t = _make_translator()
        t._push_error("translation.timeout", "TimeoutError: pipeline timed out")
        pushed = t._error_bus.push.call_args[0][0]
        self.assertIn("TimeoutError", pushed.message_debug)


class TranslatorWithModelExceptionTests(unittest.TestCase):
    """_translate_with_model pushes translation.timeout on exception."""

    def test_exception_in_chunks_pushes_translation_timeout(self) -> None:
        """When _translate_text_chunks raises, translation.timeout is pushed."""
        t = _make_translator()
        # Pre-populate pipeline cache so we skip the build step
        t._pipelines[("Helsinki-NLP/opus-mt-ru-es", False)] = MagicMock()

        with patch.object(t, "_translate_text_chunks", side_effect=RuntimeError("model OOM")):
            result = t._translate_with_model(
                text="Привет",
                resolved_mode="ru_to_es",
                network_mode="offline_strict",
                translation_style="neutral",
                return_mode="ru_to_es",
            )

        self.assertEqual(result.status, "translate_error")
        self.assertEqual(t._error_bus.push.call_count, 1)
        pushed = t._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "translation.timeout")
        self.assertEqual(pushed.component, "translation")
        self.assertEqual(pushed.severity, "warn")

    def test_timeout_error_pushes(self) -> None:
        t = _make_translator()
        t._pipelines[("Helsinki-NLP/opus-mt-ru-es", False)] = MagicMock()

        with patch.object(t, "_translate_text_chunks", side_effect=TimeoutError("HF timeout")):
            result = t._translate_with_model(
                "Привет", "ru_to_es", "offline_strict", "neutral", "ru_to_es"
            )

        self.assertEqual(result.status, "translate_error")
        pushed = t._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "translation.timeout")
        self.assertIn("TimeoutError", pushed.message_debug)

    def test_no_push_on_success(self) -> None:
        """No error is pushed on successful translation."""
        t = _make_translator()
        t._pipelines[("Helsinki-NLP/opus-mt-ru-es", False)] = MagicMock()

        with patch.object(t, "_translate_text_chunks", return_value="Hola"):
            result = t._translate_with_model(
                "Привет", "ru_to_es", "offline_strict", "neutral", "ru_to_es"
            )

        self.assertEqual(result.status, "ok")
        t._error_bus.push.assert_not_called()

    def test_mode_included_in_debug_message(self) -> None:
        """The debug message includes the mode for diagnostics."""
        t = _make_translator()
        t._pipelines[("Helsinki-NLP/opus-mt-es-ru", False)] = MagicMock()

        with patch.object(t, "_translate_text_chunks", side_effect=ConnectionError("no network")):
            t._translate_with_model(
                "Hola", "es_to_ru", "offline_strict", "neutral", "es_to_ru"
            )

        pushed = t._error_bus.push.call_args[0][0]
        self.assertIn("es_to_ru", pushed.message_debug)

    def test_no_bus_exception_does_not_propagate(self) -> None:
        """Without error_bus, exception in translate still returns TranslationResult."""
        t = _make_translator(with_bus=False)
        t._pipelines[("Helsinki-NLP/opus-mt-ru-es", False)] = MagicMock()

        with patch.object(t, "_translate_text_chunks", side_effect=RuntimeError("crash")):
            result = t._translate_with_model(
                "Привет", "ru_to_es", "offline_strict", "neutral", "ru_to_es"
            )

        self.assertEqual(result.status, "translate_error")


if __name__ == "__main__":
    unittest.main()
