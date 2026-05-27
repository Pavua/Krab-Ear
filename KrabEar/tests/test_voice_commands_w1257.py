"""Tests for W1251 F3+F5 fixes in VoiceCommandProcessor.

F3: Duplicate `nueva línea` entry removed from _ES_COMMANDS.
F5: capitalize_next silently consumed at end-of-text — now logs warning and
    preserves accumulated text.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_voice_commands_w1257.py -v
"""

import logging
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.voice_commands import (  # noqa: E402
    VoiceCommandProcessor,
    _ES_COMMANDS,
)


def _make_proc(enabled: bool = True, languages=None) -> VoiceCommandProcessor:
    if languages is None:
        languages = ["ru", "es", "en"]
    settings = {
        "voice_commands_enabled": enabled,
        "voice_commands_languages": languages,
    }
    return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))


class TestEsNuevaLineaNoDuplicate(unittest.TestCase):
    """F3: _ES_COMMANDS must not contain duplicate 'nueva línea' entries."""

    def test_es_nueva_linea_no_duplicate(self):
        """Only one 'nueva línea' entry should exist in _ES_COMMANDS."""
        nueva_linea_entries = [
            (p, a, v)
            for p, a, v in _ES_COMMANDS
            if p == "nueva línea"
        ]
        self.assertEqual(
            len(nueva_linea_entries),
            1,
            msg=f"Expected exactly 1 'nueva línea' entry, found {len(nueva_linea_entries)}: {nueva_linea_entries}",
        )

    def test_es_nueva_linea_inserts_newline(self):
        """'nueva línea' command still works correctly after dedup."""
        proc = _make_proc()
        result = proc.process("primera línea nueva línea segunda línea", language="es")
        self.assertEqual(result, "primera línea\nsegunda línea")


class TestCapitalizeNextAtEndOfText(unittest.TestCase):
    """F5: capitalize_next at end-of-text must log warning and preserve text."""

    def test_capitalize_next_at_end_logs_warning(self):
        """Calling 'большая буква' with no following word emits a logger.info warning."""
        proc = _make_proc()
        with self.assertLogs("KrabEar.VoiceCommands", level=logging.INFO) as cm:
            proc.process("большая буква", language="ru")
        # At least one log record must mention the end-of-text condition
        self.assertTrue(
            any("capitalize_next at end-of-text" in msg for msg in cm.output),
            msg=f"Expected warning about capitalize_next at end-of-text, got: {cm.output}",
        )

    def test_capitalize_next_at_end_preserves_text(self):
        """capitalize_next at end-of-text must NOT drop accumulated text."""
        proc = _make_proc()
        # "привет большая буква" — "привет" is accumulated before the modifier,
        # then end-of-text: should return the accumulated text, not empty string.
        result = proc.process("привет большая буква", language="ru")
        self.assertNotEqual(result, "", msg="Text before capitalize_next must be preserved")
        self.assertIn("привет", result.lower())

    def test_capitalize_next_standalone_returns_empty_not_crash(self):
        """capitalize_next alone (nothing before or after) returns empty, logs warning."""
        proc = _make_proc()
        with self.assertLogs("KrabEar.VoiceCommands", level=logging.INFO) as cm:
            result = proc.process("большая буква", language="ru")
        # No text to preserve — result is empty (or whitespace-only)
        self.assertEqual(result.strip(), "")
        self.assertTrue(
            any("capitalize_next at end-of-text" in msg for msg in cm.output),
        )

    def test_capitalize_next_mid_text_still_works(self):
        """capitalize_next in the middle of text still capitalizes the next word."""
        proc = _make_proc()
        result = proc.process("привет большая буква мир", language="ru")
        self.assertEqual(result, "привет Мир")

    def test_en_capitalize_next_at_end_preserves_text(self):
        """capitalize next at end-of-text in English also preserves accumulated text."""
        proc = _make_proc()
        result = proc.process("hello capitalize next", language="en")
        self.assertIn("hello", result.lower())
        self.assertNotEqual(result, "")

    def test_en_capitalize_next_at_end_logs_warning(self):
        """capitalize next at end-of-text (EN) emits logger.info warning."""
        proc = _make_proc()
        with self.assertLogs("KrabEar.VoiceCommands", level=logging.INFO) as cm:
            proc.process("hello capitalize next", language="en")
        self.assertTrue(
            any("capitalize_next at end-of-text" in msg for msg in cm.output),
            msg=f"Expected end-of-text warning, got: {cm.output}",
        )


if __name__ == "__main__":
    unittest.main()
