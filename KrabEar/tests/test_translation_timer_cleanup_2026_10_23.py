"""Реальные Foundation timers и production deinit, без UI/IPC/capture."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/KrabEarAgent/Sources/KrabEarAgent/TranslationStreamView.swift"


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("swift"), "Нужен macOS Swift"
)
class TranslationTimerCleanupTest(unittest.TestCase):
    def test_real_timers_invalidated_after_main_or_background_release(self):
        source = SOURCE.read_text(encoding="utf-8")
        start = source.index("    deinit {")
        end = source.index("    // MARK: - Layout", start)
        destructor = source[start:end]
        program = (
            r"""import Foundation
import Dispatch

final class TaskLeaf { var cancelled = false; func cancel() { cancelled = true } }
final class Owner {
    var sseStreamTask: TaskLeaf?
    var captureTimer: Timer?
    var glossaryRefreshTimer: Timer?
"""
            + destructor
            + r"""
}
final class ReleaseBox: @unchecked Sendable { var owner: Owner? }
DispatchQueue.main.async {
    let box = ReleaseBox()
    box.owner = Owner()
    let task = TaskLeaf()
    let capture = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { _ in }
    let glossary = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { _ in }
    box.owner!.sseStreamTask = task
    box.owner!.captureTimer = capture
    box.owner!.glossaryRefreshTimer = glossary
    weak var released = box.owner
    let release = {
        box.owner = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
            guard released == nil, task.cancelled else {
                fputs("OWNER_OR_SSE_NOT_RELEASED\n", stderr); exit(2)
            }
            guard !capture.isValid, !glossary.isValid else {
                capture.invalidate(); glossary.invalidate()
                fputs("TIMER_LEAK: repeating timers survive owner release\n", stderr); exit(1)
            }
            print("TIMERS_RELEASED"); exit(0)
        }
    }
    if CommandLine.arguments.last == "background" {
        DispatchQueue.global().async(execute: release)
    } else {
        release()
    }
}
dispatchMain()
"""
        )
        with tempfile.TemporaryDirectory(prefix="ear-timer-cleanup-") as tmp:
            probe = Path(tmp) / "probe.swift"
            probe.write_text(program, encoding="utf-8")
            for mode in ("main", "background"):
                with self.subTest(release=mode):
                    result = subprocess.run(
                        [
                            shutil.which("swift"),
                            "-swift-version",
                            "5",
                            str(probe),
                            mode,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=45,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn("TIMERS_RELEASED", result.stdout)


if __name__ == "__main__":
    unittest.main()
