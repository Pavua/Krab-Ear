/*
 HistoryPanelHistoryFormatBytesTests — юнит-тесты pure helper'а
 `formatBytes` из +History.swift. Используется в onCompact/diagnostics
 для отображения размеров в человекочитаемом формате.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelHistoryFormatBytesTests: XCTestCase {

    // MARK: - Bytes (< 1024)

    func test_zero_returnsZeroBytes() {
        XCTAssertEqual(HistoryPanelController.formatBytes(0), "0 B")
    }

    func test_negative_clampedToZero() {
        XCTAssertEqual(HistoryPanelController.formatBytes(-100), "0 B")
    }

    func test_smallBytes() {
        XCTAssertEqual(HistoryPanelController.formatBytes(512), "512 B")
    }

    func test_byteBoundary_1023() {
        XCTAssertEqual(HistoryPanelController.formatBytes(1023), "1023 B")
    }

    // MARK: - Kilobytes (1024..1024^2-1)

    func test_oneKB_exactly() {
        XCTAssertEqual(HistoryPanelController.formatBytes(1024), "1.0 KB")
    }

    func test_kb_decimalPrecision() {
        // 1500 / 1024 ≈ 1.4648 → "1.5 KB"
        XCTAssertEqual(HistoryPanelController.formatBytes(1500), "1.5 KB")
    }

    func test_kb_largeValue() {
        // 100 KB
        XCTAssertEqual(HistoryPanelController.formatBytes(100 * 1024), "100.0 KB")
    }

    func test_kb_boundary() {
        // Just under 1 MB
        XCTAssertEqual(HistoryPanelController.formatBytes(1024 * 1024 - 1), "1024.0 KB")
    }

    // MARK: - Megabytes

    func test_oneMB_exactly() {
        XCTAssertEqual(HistoryPanelController.formatBytes(1024 * 1024), "1.0 MB")
    }

    func test_mb_decimalPrecision() {
        // 5.5 MB
        let bytes = Int(5.5 * 1024 * 1024)
        XCTAssertEqual(HistoryPanelController.formatBytes(bytes), "5.5 MB")
    }

    func test_mb_largeValue() {
        // 500 MB
        XCTAssertEqual(HistoryPanelController.formatBytes(500 * 1024 * 1024), "500.0 MB")
    }

    // MARK: - Gigabytes

    func test_oneGB_exactly() {
        XCTAssertEqual(HistoryPanelController.formatBytes(1024 * 1024 * 1024), "1.00 GB")
    }

    func test_gb_decimalPrecision() {
        // 2.5 GB
        let bytes = Int(2.5 * 1024 * 1024 * 1024)
        XCTAssertEqual(HistoryPanelController.formatBytes(bytes), "2.50 GB")
    }

    func test_gb_largeValue() {
        // 100 GB
        let bytes = 100 * 1024 * 1024 * 1024
        XCTAssertEqual(HistoryPanelController.formatBytes(bytes), "100.00 GB")
    }

    // MARK: - Format consistency

    func test_unitOrderConsistent() {
        // Bytes → KB → MB → GB transitions should be monotonic
        let values = [0, 512, 1024, 1024 * 1024, 1024 * 1024 * 1024]
        let outputs = values.map(HistoryPanelController.formatBytes)
        XCTAssertTrue(outputs[0].hasSuffix(" B"))
        XCTAssertTrue(outputs[1].hasSuffix(" B"))
        XCTAssertTrue(outputs[2].hasSuffix(" KB"))
        XCTAssertTrue(outputs[3].hasSuffix(" MB"))
        XCTAssertTrue(outputs[4].hasSuffix(" GB"))
    }

    func test_kbAndMbHaveOneDecimal() {
        // KB и MB используют %.1f
        let kb = HistoryPanelController.formatBytes(2048)
        let mb = HistoryPanelController.formatBytes(2 * 1024 * 1024)
        XCTAssertEqual(kb, "2.0 KB")
        XCTAssertEqual(mb, "2.0 MB")
    }

    func test_gbHasTwoDecimals() {
        // GB использует %.2f (бОльшие числа → больше precision)
        let gb = HistoryPanelController.formatBytes(2 * 1024 * 1024 * 1024)
        XCTAssertEqual(gb, "2.00 GB")
    }
}
