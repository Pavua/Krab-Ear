"""KE05D-22: ``_yaml_scalar`` не отдаёт без кавычек значения, которые YAML читает не как строку.

``true``/``null``/``no`` (код языка norwegian!)/``123``/``2026-01-01`` в frontmatter превращались
в bool/None/int/date — строковые теги и id теряли тип при чтении Obsidian'ом.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obsidian_sync import _yaml_scalar  # noqa: E402

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - PyYAML необязателен
    yaml = None

_AMBIGUOUS = [
    "true",
    "True",
    "FALSE",
    "yes",
    "No",
    "on",
    "OFF",
    "y",
    "N",
    "null",
    "Null",
    "~",
    "123",
    "-5",
    "+7",
    "1.5",
    ".5",
    "1e3",
    "0x1f",
    "0o17",
    "0b101",
    "2026-01-01",
    "12:30",
    ".inf",
    ".NaN",
    "nan",
    "inf",
]
_PLAIN = ["ru", "en", "meeting", "Транскрипция 2026-01-01", "abc12345", "tag-with-dash"]


class YamlScalarKeepsStrings(unittest.TestCase):
    def test_ambiguous_values_are_quoted(self):
        for v in _AMBIGUOUS:
            out = _yaml_scalar(v)
            self.assertTrue(out.startswith('"') and out.endswith('"'), f"{v!r} -> {out}")

    def test_plain_words_stay_readable(self):
        for v in _PLAIN:
            self.assertEqual(_yaml_scalar(v), v, v)

    @unittest.skipIf(yaml is None, "PyYAML не установлен")
    def test_roundtrip_is_always_a_string(self):
        for v in _AMBIGUOUS + _PLAIN + ["a: b", 'q"uote', "x\ny"]:
            loaded = yaml.safe_load(f"k: {_yaml_scalar(v)}")["k"]
            self.assertIsInstance(loaded, str, f"{v!r} -> {loaded!r}")
            self.assertEqual(loaded, v)

    def test_existing_quoting_behaviour_unchanged(self):
        self.assertEqual(_yaml_scalar(""), '""')
        self.assertEqual(_yaml_scalar("a: b"), '"a: b"')
        self.assertEqual(_yaml_scalar('he said "x"'), '"he said \\"x\\""')


if __name__ == "__main__":
    unittest.main()
