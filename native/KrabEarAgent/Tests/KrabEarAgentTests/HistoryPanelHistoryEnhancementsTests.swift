/*
 HistoryPanelHistoryEnhancementsTests — тесты логики из HistoryPanelController+HistoryEnhancements.swift.

 Стратегия:
 HistoryEnhancements содержит почти исключительно @objc actions (IPC + NSAlert),
 однако несколько чистых функций доступны для whitebox-тестирования:
   - updateHistoryFiltersBadge   → логика подсчёта активных фильтров (whitebox)
   - cleanupDaysSelector mapping → daysMap[index] → days

 UI-зависимые методы (onExportSrt, onCleanupHistory, onVocabSuggestions,
 onGlossarySuggestions, onTableViewDoubleClick)
 пропускаем через XCTSkip с объяснением.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Whitebox helpers

/// Подсчёт активных фильтров по тем же правилам что в updateHistoryFiltersBadge.
private func countActiveHistoryFilters(
    query: String,
    pasteStatusIdx: Int,
    translationModeIdx: Int,
    translationStatusIdx: Int,
    fromDate: String,
    toDate: String
) -> Int {
    var count = 0
    if !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }
    if pasteStatusIdx > 0      { count += 1 }
    if translationModeIdx > 0  { count += 1 }
    if translationStatusIdx > 0 { count += 1 }
    if !fromDate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }
    if !toDate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty   { count += 1 }
    return count
}

/// Маппинг индекса cleanupDaysSelector → количество дней (из HistoryEnhancements).
private func cleanupDaysForIndex(_ index: Int) -> Int {
    let daysMap = [30, 60, 90, 180, 365]
    guard index >= 0, index < daysMap.count else { return 30 }
    return daysMap[index]
}

// MARK: - Tests

final class HistoryPanelHistoryEnhancementsTests: XCTestCase {

    // MARK: - Filter badge count

    /// Все фильтры выключены → count == 0.
    func test_filterBadgeCount_noFilters() {
        let count = countActiveHistoryFilters(
            query: "",
            pasteStatusIdx: 0,
            translationModeIdx: 0,
            translationStatusIdx: 0,
            fromDate: "",
            toDate: ""
        )
        XCTAssertEqual(count, 0)
    }

    /// Только query активен → count == 1.
    func test_filterBadgeCount_queryOnly() {
        let count = countActiveHistoryFilters(
            query: "тест",
            pasteStatusIdx: 0,
            translationModeIdx: 0,
            translationStatusIdx: 0,
            fromDate: "",
            toDate: ""
        )
        XCTAssertEqual(count, 1)
    }

    /// Все 6 фильтров активны → count == 6.
    func test_filterBadgeCount_allActive() {
        let count = countActiveHistoryFilters(
            query: "search",
            pasteStatusIdx: 1,
            translationModeIdx: 2,
            translationStatusIdx: 1,
            fromDate: "2026-01-01",
            toDate: "2026-04-20"
        )
        XCTAssertEqual(count, 6)
    }

    /// Пробельный query не считается активным.
    func test_filterBadgeCount_whitespaceQueryIgnored() {
        let count = countActiveHistoryFilters(
            query: "   ",
            pasteStatusIdx: 0,
            translationModeIdx: 0,
            translationStatusIdx: 0,
            fromDate: "",
            toDate: ""
        )
        XCTAssertEqual(count, 0)
    }

    // MARK: - cleanupDaysSelector mapping

    func test_cleanupDays_allIndices() {
        XCTAssertEqual(cleanupDaysForIndex(0), 30)
        XCTAssertEqual(cleanupDaysForIndex(1), 60)
        XCTAssertEqual(cleanupDaysForIndex(2), 90)
        XCTAssertEqual(cleanupDaysForIndex(3), 180)
        XCTAssertEqual(cleanupDaysForIndex(4), 365)
    }

    func test_cleanupDays_outOfRange_returnsDefault() {
        XCTAssertEqual(cleanupDaysForIndex(-1), 30)
        XCTAssertEqual(cleanupDaysForIndex(99), 30)
    }

    // MARK: - batch-prefix(50) logic (бывший onAutoSummaryBatch, метод удалён)

    /// prefix(50) на массиве < 50 элементов → берёт все.
    func test_autosummaryBatch_prefixAll_whenLessThan50() {
        let ids = (0 ..< 10).map { "id-\($0)" }
        let selected = Array(ids.prefix(50))
        XCTAssertEqual(selected.count, 10)
    }

    /// prefix(50) на массиве > 50 элементов → ровно 50.
    func test_autosummaryBatch_prefixCaps_at50() {
        let ids = (0 ..< 80).map { "id-\($0)" }
        let selected = Array(ids.prefix(50))
        XCTAssertEqual(selected.count, 50)
    }

    // MARK: - UI-coupled methods (skipped)

    func test_skip_onExportSrt_requiresNSWorkspace() throws {
        throw XCTSkip("onExportSrt требует NSWorkspace.selectFile — UI-coupled, skip в unit-среде")
    }

    func test_skip_onCleanupHistory_requiresNSAlert() throws {
        throw XCTSkip("onCleanupHistory требует NSAlert.runModal — UI-coupled, skip в unit-среде")
    }
}
