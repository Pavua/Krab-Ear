"""Wave 658 — AGENT-J Unicode regression test.

Сканирует .swift файлы в native/KrabEarAgent/Sources/ на наличие опасных
Unicode-глифов внутри NSTextField(labelWithString: и NSAttributedString(string:.
Именно такие глифы вызвали AGENT-J CoreText hang (Wave 67, PR #412).

Два теста:
  1. clean tree passes  — производственное дерево без нарушений.
  2. bad fixture fails  — детектор обнаруживает нарушения в специальном fixture.
"""

import re
import sys
import textwrap
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SWIFT_SOURCES = _REPO_ROOT / "native" / "KrabEarAgent" / "Sources"

# ---------------------------------------------------------------------------
# Dangerous glyphs — CoreText hang triggers in NSTextField label strings.
# Keep in sync with Wave 67 / AGENT-J post-mortem.
# ---------------------------------------------------------------------------
DANGEROUS_GLYPHS: str = "●○◉•▶◀⇄▲▼★✕✓⏱"

# Match NSTextField(labelWithString: or NSAttributedString(string:
# then capture the first quoted string on that line.
_LABEL_RE = re.compile(
    r'NS(?:TextField\(labelWithString:|AttributedString\(string:)'
    r'[^"\']*["\']([^"\']*)["\']'
)


def _find_violations(sources_root: Path) -> list[tuple[Path, int, str]]:
    """Return (file, lineno, stripped_line) for every violation found."""
    hits: list[tuple[Path, int, str]] = []
    if not sources_root.exists():
        return hits
    for swift_file in sorted(sources_root.rglob("*.swift")):
        for lineno, raw in enumerate(
            swift_file.read_text(errors="replace").splitlines(), 1
        ):
            for m in _LABEL_RE.finditer(raw):
                if any(ch in m.group(1) for ch in DANGEROUS_GLYPHS):
                    hits.append((swift_file, lineno, raw.strip()))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoUnicodeGlyphsWave658(unittest.TestCase):
    """AGENT-J regression guard — Wave 658."""

    def test_clean_tree_has_no_violations(self) -> None:
        """Production source tree must contain zero dangerous glyphs in label strings."""
        violations = _find_violations(_SWIFT_SOURCES)
        if violations:
            detail = "\n".join(
                f"  {v[0].relative_to(_REPO_ROOT)}:{v[1]}  {v[2]}"
                for v in violations
            )
            self.fail(
                f"AGENT-J regression: {len(violations)} dangerous Unicode glyph(s) "
                f"found in NSTextField/NSAttributedString label string(s).\n"
                f"Replace with SF Symbols (NSImage(systemSymbolName:)).\n\n"
                f"Violations:\n{detail}"
            )

    def test_bad_fixture_is_detected(self) -> None:
        """Detector must flag dangerous glyphs in a synthetic bad fixture."""
        import tempfile

        fixture = textwrap.dedent("""\
            // Wave 658 fixture — intentionally bad
            let dot = NSTextField(labelWithString: "● Active")
            let attr = NSAttributedString(string: "Status ▶ running")
            let clean = NSTextField(labelWithString: "No glyph here — OK")
            let sf = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
        """)

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Sources"
            src.mkdir()
            (src / "BadGlyphFixture.swift").write_text(fixture)
            violations = _find_violations(src)

        self.assertEqual(
            len(violations),
            2,
            f"Expected exactly 2 violations from fixture, got {len(violations)}: {violations}",
        )
        lines = [v[2] for v in violations]
        self.assertTrue(any("●" in ln for ln in lines), "Bullet ● must be detected")
        self.assertTrue(any("▶" in ln for ln in lines), "Triangle ▶ must be detected")


if __name__ == "__main__":
    unittest.main()
