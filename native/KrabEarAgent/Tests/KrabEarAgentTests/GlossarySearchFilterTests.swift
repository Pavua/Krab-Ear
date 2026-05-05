/*
 GlossarySearchFilterTests — XCTest suite for glossary search/filter logic.

 Tests the pure static function HistoryPanelController.filterGlossary(_:query:)
 which drives the client-side search field in the Live Translation tab.

 Coverage:
   - Empty query → all entries returned, sorted by key.
   - Non-empty query → only entries matching source OR target (case-insensitive).
   - Whitespace-only query → treated as empty (all entries returned).
   - Query matches only source → matched entry included.
   - Query matches only target → matched entry included.
   - No matches → empty array.
   - Result is sorted alphabetically by key regardless of filter.
*/

import XCTest
@testable import KrabEarAgent

final class GlossarySearchFilterTests: XCTestCase {

    // Sample glossary used across tests.
    private let sampleGlossary: [String: String] = [
        "инсульт":      "stroke",
        "давление":     "blood pressure",
        "сахар":        "sugar / glucose",
        "Привет":       "Hello",
        "кардиограмма": "ECG / EKG",
    ]

    // MARK: - Empty query

    func test_emptyQuery_returnsAllEntriesSortedByKey() {
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "")
        XCTAssertEqual(result.count, sampleGlossary.count)
        // Verify ascending sort by key
        let keys = result.map(\.key)
        XCTAssertEqual(keys, keys.sorted())
    }

    func test_whitespaceOnlyQuery_treatedAsEmpty() {
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "   ")
        XCTAssertEqual(result.count, sampleGlossary.count)
    }

    // MARK: - Source match

    func test_queryMatchesSource_caseInsensitive() {
        // "инсульт" should match when searching "ИНСУЛЬТ"
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "ИНСУЛЬТ")
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result.first?.key, "инсульт")
    }

    func test_queryMatchesSourcePartial() {
        // "давл" is a prefix of "давление"
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "давл")
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result.first?.key, "давление")
    }

    // MARK: - Target match

    func test_queryMatchesTarget_caseInsensitive() {
        // "STROKE" should match entry whose value is "stroke"
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "STROKE")
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result.first?.key, "инсульт")
    }

    func test_queryMatchesTargetPartial() {
        // "blood" should match "blood pressure"
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "blood")
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result.first?.key, "давление")
    }

    // MARK: - Multiple matches

    func test_queryMatchesMultiple() {
        // "e" appears in many targets: stroke, blood pressure, sugar/glucose, Hello, ECG/EKG
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "e")
        // At minimum stroke (инсульт) and blood pressure (давление) + glucose + Hello + ECG
        XCTAssertGreaterThanOrEqual(result.count, 3)
        // Result must still be sorted by key
        let keys = result.map(\.key)
        XCTAssertEqual(keys, keys.sorted())
    }

    // MARK: - No match

    func test_queryNoMatch_returnsEmpty() {
        let result = HistoryPanelController.filterGlossary(sampleGlossary, query: "xyzzy")
        XCTAssertTrue(result.isEmpty)
    }

    // MARK: - Empty glossary

    func test_emptyGlossary_returnsEmpty() {
        let result = HistoryPanelController.filterGlossary([:], query: "инсульт")
        XCTAssertTrue(result.isEmpty)
    }

    func test_emptyGlossaryEmptyQuery_returnsEmpty() {
        let result = HistoryPanelController.filterGlossary([:], query: "")
        XCTAssertTrue(result.isEmpty)
    }

    // MARK: - Sort stability

    func test_sortedAlphabetically() {
        let glossary: [String: String] = [
            "б": "B",
            "а": "A",
            "в": "C",
        ]
        let result = HistoryPanelController.filterGlossary(glossary, query: "")
        XCTAssertEqual(result.map(\.key), ["а", "б", "в"])
    }
}
