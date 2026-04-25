/*
 ConfidenceIndicatorTests — тесты функции confidenceColor(for:).

 Подход: вызываем module-level free function напрямую через @testable import.
 NSColor.systemGreen/.systemOrange/.systemRed/.clear не требуют живого NSApp,
 поэтому тесты работают полностью headless (swift test, CI).
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class ConfidenceIndicatorTests: XCTestCase {

    // MARK: - confidenceColor(for:) — цветовые пороги

    func test_confidenceColor_highConfidence_returnsSuccess() {
        let color = confidenceColor(for: 0.9)
        XCTAssertEqual(color, KrabEarTheme.Colors.success,
            "confidence >= 0.85 должен возвращать Colors.success (systemGreen)")
    }

    func test_confidenceColor_exactHighThreshold_returnsSuccess() {
        let color = confidenceColor(for: 0.85)
        XCTAssertEqual(color, KrabEarTheme.Colors.success,
            "confidence == 0.85 (граница) должен возвращать Colors.success")
    }

    func test_confidenceColor_mediumConfidence_returnsWarning() {
        let color = confidenceColor(for: 0.7)
        XCTAssertEqual(color, KrabEarTheme.Colors.warning,
            "0.65 <= confidence < 0.85 должен возвращать Colors.warning (systemOrange)")
    }

    func test_confidenceColor_exactLowThreshold_returnsWarning() {
        let color = confidenceColor(for: 0.65)
        XCTAssertEqual(color, KrabEarTheme.Colors.warning,
            "confidence == 0.65 (нижняя граница warning) должен возвращать Colors.warning")
    }

    func test_confidenceColor_lowConfidence_returnsError() {
        let color = confidenceColor(for: 0.5)
        XCTAssertEqual(color, KrabEarTheme.Colors.error,
            "confidence < 0.65 должен возвращать Colors.error (systemRed)")
    }

    func test_confidenceColor_zeroConfidence_returnsError() {
        let color = confidenceColor(for: 0.0)
        XCTAssertEqual(color, KrabEarTheme.Colors.error,
            "confidence == 0.0 должен возвращать Colors.error")
    }

    func test_confidenceColor_nilConfidence_returnsClear() {
        let color = confidenceColor(for: nil)
        XCTAssertEqual(color, NSColor.clear,
            "nil confidence (импорт без метаданных) должен возвращать .clear — индикатор скрыт")
    }

    // MARK: - HistoryItem — парсинг confidence из payload

    func test_historyItem_parsesDoubleConfidence() {
        let payload: [String: Any] = [
            "id": "test-1",
            "ts": "2026-01-01T00:00:00Z",
            "text": "Привет",
            "confidence": 0.92,
        ]
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertEqual(item!.confidence!, 0.92, accuracy: 0.0001)
    }

    func test_historyItem_parsesFloatConfidence() {
        let payload: [String: Any] = [
            "id": "test-2",
            "ts": "2026-01-01T00:00:00Z",
            "text": "Hola",
            "confidence": Float(0.73),
        ]
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertEqual(item!.confidence!, 0.73, accuracy: 0.001)
    }

    func test_historyItem_missingConfidence_isNil() {
        let payload: [String: Any] = [
            "id": "test-3",
            "ts": "2026-01-01T00:00:00Z",
            "text": "Sin confianza",
        ]
        let item = HistoryItem(payload: payload)
        XCTAssertNotNil(item)
        XCTAssertNil(item?.confidence,
            "Отсутствующее поле confidence должно давать nil (импорт без метаданных)")
    }
}
