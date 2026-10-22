"""ARC-регрессия настоящего lazy getter без запуска панели/capture/IPC.

Компилируем неизменённое тело production getter с узкими UI/capture stubs.
Так проверяется реальное владение closure, а не наличие строки weak в source.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+ApplyTheme+LiveTab.swift"
)


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("swift"),
    "Нужен macOS Swift/Objective-C ARC",
)
class TranslationViewOwnerARCTest(unittest.TestCase):
    def test_cached_view_releases_when_controller_releases(self):
        source = SOURCE.read_text(encoding="utf-8")
        start = source.index("    var translationStreamView: TranslationStreamView {")
        end = source.index("    /// Assemble Live Translation tab.", start)
        getter = source[start:end]
        program = (
            r"""import AppKit
import Foundation
import ObjectiveC

@MainActor final class TranslationStreamView: NSObject {
    var onToggleCapture: (() -> Void)?
    var isCapturing = false
    init(frame: NSRect) { super.init() }
}
@MainActor final class CaptureLeaf { var isCapturing = false }
@MainActor final class AgentAppDelegate: NSObject {
    let systemAudioCapture = CaptureLeaf()
    func toggleLiveSubsCaptureFromMenu() { fatalError("capture is outside ARC probe") }
}
@MainActor final class HistoryPanelController: NSObject {}
nonisolated(unsafe) private var translationStreamViewKey: UInt8 = 0
extension HistoryPanelController {
"""
            + getter
            + r"""
}
Task { @MainActor in
    weak var cached: TranslationStreamView?
    autoreleasepool {
        var owner: HistoryPanelController? = HistoryPanelController()
        let first = owner!.translationStreamView
        precondition(first === owner!.translationStreamView, "getter stopped caching")
        cached = first
        owner = nil
    }
    guard cached == nil else {
        fputs("ARC_LEAK: cached view retained after controller release\n", stderr)
        exit(1)
    }
    print("ARC_RELEASED")
    exit(0)
}
dispatchMain()
"""
        )
        with tempfile.TemporaryDirectory(prefix="ear-owner-arc-") as tmp:
            probe = Path(tmp) / "probe.swift"
            probe.write_text(program, encoding="utf-8")
            result = subprocess.run(
                [shutil.which("swift"), "-swift-version", "5", str(probe)],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ARC_RELEASED", result.stdout)


if __name__ == "__main__":
    unittest.main()
