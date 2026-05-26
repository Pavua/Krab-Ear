"""Wave 621 — AGENT-J Unicode regression test.

Сканирует все .swift файлы в native/KrabEarAgent/Sources/ на наличие
опасных Unicode-глифов внутри NSTextField(labelWithString: и
NSAttributedString(string: — именно такие глифы вызвали AGENT-J CoreText hang.

Wave 67 (PR #412): ● Unicode → SF Symbol 'circle.fill' — зафиксировано как root cause.
Этот тест не допускает регрессию.
"""

import sys
import re
import textwrap
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Repo root: two levels up from this file (KrabEar/tests/ → KrabEar/ → repo)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SWIFT_SOURCES = _REPO_ROOT / "native" / "KrabEarAgent" / "Sources"

# ---------------------------------------------------------------------------
# Dangerous glyphs — known CoreText hang triggers in NSTextField label strings
# Keep in sync with Wave 67 / AGENT-J post-mortem.
# ---------------------------------------------------------------------------
DANGEROUS_GLYPHS: str = "●○◉•▶◀⇄▲▼★✕✓⏱"

# Regex: match NSTextField(labelWithString: or NSAttributedString(string:
# followed (on the same line) by a quoted string containing a dangerous glyph.
_LABEL_PATTERN = re.compile(
    r'NS(?:TextField\(labelWithString:|AttributedString\(string:)'
    r'[^"\']*["\']([^"\']*)["\']'
)


def _find_violations(sources_root: Path) -> list[tuple[Path, int, str]]:
    """Return list of (file, lineno, matched_line) for every violation."""
    violations: list[tuple[Path, int, str]] = []
    if not sources_root.exists():
        return violations
    for swift_file in sorted(sources_root.rglob("*.swift")):
        for lineno, line in enumerate(swift_file.read_text(errors="replace").splitlines(), 1):
            for m in _LABEL_PATTERN.finditer(line):
                string_content = m.group(1)
                if any(ch in string_content for ch in DANGEROUS_GLYPHS):
                    violations.append((swift_file, lineno, line.strip()))
    return violations


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestNoUnicodeGlyphsInLabelStrings(unittest.TestCase):
    """Убеждаемся, что в NSTextField/NSAttributedString label строках нет опасных глифов."""

    def test_clean_tree_has_no_violations(self) -> None:
        """Производственное дерево не должно содержать опасных глифов в label-строках."""
        violations = _find_violations(_SWIFT_SOURCES)
        if violations:
            lines = "\n".join(
                f"  {v[0].relative_to(_REPO_ROOT)}:{v[1]}  {v[2]}"
                for v in violations
            )
            self.fail(
                f"Found {len(violations)} dangerous Unicode glyph(s) in NSTextField/"
                f"NSAttributedString label string(s).\n"
                f"Use SF Symbols (e.g. NSImage(systemSymbolName:)) instead.\n\n"
                f"Violations:\n{lines}"
            )

    def test_injection_fixture_is_detected(self) -> None:
        """Проверка: детектор находит нарушение в искусственном fixture-файле."""
        import tempfile, os

        fixture_code = textwrap.dedent("""\
            // Wave 621 fixture — intentionally bad
            let label = NSTextField(labelWithString: "● Active")
            let attr = NSAttributedString(string: "Status ▶ running")
            let clean = NSTextField(labelWithString: "No glyph here — OK")
            let sfSymbol = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
        """)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Mirror the expected directory structure
            src_dir = Path(tmpdir) / "Sources"
            src_dir.mkdir()
            fixture = src_dir / "BadGlyphFixture.swift"
            fixture.write_text(fixture_code)

            violations = _find_violations(src_dir)

        # Expect exactly 2 violations (the two bad lines), not the clean ones
        self.assertEqual(
            len(violations),
            2,
            f"Expected 2 violations in fixture, got {len(violations)}: {violations}",
        )
        matched_lines = [v[2] for v in violations]
        self.assertTrue(
            any("●" in ln for ln in matched_lines),
            "Bullet ● should be detected",
        )
        self.assertTrue(
            any("▶" in ln for ln in matched_lines),
            "Triangle ▶ should be detected",
        )


if __name__ == "__main__":
    unittest.main()
