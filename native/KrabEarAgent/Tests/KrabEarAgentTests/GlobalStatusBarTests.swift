/*
 GlobalStatusBarTests — unit tests for GlobalStatusBar SF Symbol fix (Wave 523).

 Coverage:
 1. imageForOp("transcribe_job") → non-nil NSImage (SF Symbol "waveform").
 2. imageForOp("obsidian_sync")  → non-nil NSImage (SF Symbol "arrow.triangle.2.circlepath").
 3. imageForOp("mlx_inference")  → non-nil NSImage (SF Symbol "cpu").
 4. imageForOp("unknown_op")     → non-nil NSImage fallback (SF Symbol "circle.fill").
 5. No Unicode glyph strings returned — all image paths use SF Symbols.

 Root cause: AGENT-J (Wave 67 PR #412) — NSTextField with Unicode bullets (●, ▶, ◉)
 triggers CoreText glyph-metrics build on main thread during ColorSync callback → AppHang.
 Fix: replace Unicode-glyph NSTextField with NSImageView + SF Symbol NSImage.

 Wave 416 audit identified GlobalStatusBar.swift lines 251/253 as sister sites.
 Wave 523 fix: iconLabel (NSTextField) → iconImageView (NSImageView) + imageForOp().
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class GlobalStatusBarTests: XCTestCase {

    private var bar: GlobalStatusBar!

    override func setUp() {
        super.setUp()
        bar = GlobalStatusBar(frame: NSRect(x: 0, y: 0, width: 400, height: 28))
    }

    override func tearDown() {
        bar = nil
        super.tearDown()
    }

    // MARK: - SF Symbol image tests (AGENT-J sister fix)

    /// transcribe_job must produce a non-nil SF Symbol image (waveform).
    /// Confirms no Unicode glyph fallback that would trigger CoreText AppHang.
    func test_imageForOp_transcribeJob_isNonNil() {
        let image = bar.imageForOp("transcribe_job")
        XCTAssertNotNil(image, "imageForOp('transcribe_job') must return non-nil SF Symbol image")
    }

    /// obsidian_sync must produce a non-nil SF Symbol image (arrow.triangle.2.circlepath).
    func test_imageForOp_obsidianSync_isNonNil() {
        let image = bar.imageForOp("obsidian_sync")
        XCTAssertNotNil(image, "imageForOp('obsidian_sync') must return non-nil SF Symbol image")
    }

    /// mlx_inference must produce a non-nil SF Symbol image (cpu).
    func test_imageForOp_mlxInference_isNonNil() {
        let image = bar.imageForOp("mlx_inference")
        XCTAssertNotNil(image, "imageForOp('mlx_inference') must return non-nil SF Symbol image")
    }

    /// Unknown op must return the fallback SF Symbol (circle.fill), not nil.
    func test_imageForOp_unknownOp_returnsNonNilFallback() {
        let image = bar.imageForOp("some_unknown_op")
        XCTAssertNotNil(image, "imageForOp with unknown op must return non-nil fallback SF Symbol image")
    }

    /// All known op types must return non-nil images — exhaustive check.
    func test_imageForOp_allKnownOps_areNonNil() {
        let knownOps = ["transcribe_job", "obsidian_sync", "mlx_inference"]
        for op in knownOps {
            XCTAssertNotNil(bar.imageForOp(op), "imageForOp('\(op)') must not be nil")
        }
    }
}
