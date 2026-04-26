/*
 HistoryPanelSemanticSearchExtractIDTests — юнит-тесты regex-парсера ID prefix
 из строки результата semantic search (PR feat/semantic-search-click-to-item).

 Тестируемая функция: `HistoryPanelController.extractItemIDPrefix(from:) -> String?`

 Pattern format:
   "  <число>. [<%>%] <hex prefix>…  <preview>"

 Используется в double-click handler `handleSemanticResultsDoubleClick`:
 пользователь делает 2× click на строке результата → парсим prefix → ищем
 запись в self.items → переходим к ней в History tab.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelSemanticSearchExtractIDTests: XCTestCase {

    // MARK: - Valid lines (success path)

    func test_extracts_basic_format() {
        let line = "1. [82%] abc12345…  preview text"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abc12345")
    }

    func test_extracts_with_leading_whitespace() {
        let line = "    1. [82%] abc12345…  preview"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abc12345")
    }

    func test_extracts_uppercase_hex() {
        let line = "5. [99%] DEADBEEF…  что-то"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "DEADBEEF")
    }

    func test_extracts_mixed_case_hex() {
        let line = "10. [50%] aB3dEf12…  text"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "aB3dEf12")
    }

    func test_extracts_short_hex_4_chars_minimum() {
        let line = "1. [10%] abcd…  preview"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abcd")
    }

    func test_extracts_long_hex_full_uuid_prefix() {
        let line = "1. [75%] 0123456789abcdef…  preview"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "0123456789abcdef")
    }

    func test_extracts_double_digit_index() {
        let line = "42. [33%] cafe1234…  preview"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "cafe1234")
    }

    func test_extracts_with_three_digit_score() {
        let line = "1. [100%] abc12345…  text"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abc12345")
    }

    func test_extracts_with_zero_score() {
        let line = "1. [0%] abc12345…  text"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abc12345")
    }

    // MARK: - Invalid lines (returns nil)

    func test_returns_nil_for_empty_string() {
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: ""))
    }

    func test_returns_nil_for_random_text() {
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: "просто текст"))
    }

    func test_returns_nil_for_header_line() {
        let header = "Режим: semantic  ·  результатов: 5"
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: header))
    }

    func test_returns_nil_for_no_score_brackets() {
        let line = "1. abc12345…  preview"
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: line))
    }

    func test_returns_nil_for_no_ellipsis() {
        let line = "1. [82%] abc12345  preview"
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: line))
    }

    func test_returns_nil_for_non_hex_chars_in_prefix() {
        // 'g' и 'z' не hex
        let line = "1. [82%] xyzghijk…  preview"
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: line))
    }

    func test_returns_nil_for_too_short_hex_3_chars() {
        // Минимум по regex — 4 char
        let line = "1. [82%] abc…  preview"
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: line))
    }

    func test_returns_nil_for_index_without_dot() {
        let line = "1 [82%] abc12345…  preview"
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: line))
    }

    func test_returns_nil_for_score_without_percent() {
        let line = "1. [82] abc12345…  preview"
        XCTAssertNil(HistoryPanelController.extractItemIDPrefix(from: line))
    }

    // MARK: - Edge cases

    func test_handles_unicode_in_preview() {
        // Unicode после prefix — не должен мешать парсингу.
        let line = "1. [82%] abc12345…  Привет мир 🌍"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abc12345")
    }

    func test_handles_no_preview_text() {
        let line = "1. [82%] abc12345…"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abc12345")
    }

    func test_handles_extra_whitespace_around_brackets() {
        // Extra spaces — все ещё matches.
        let line = "  1.  [82%]   abc12345…  text"
        XCTAssertEqual(HistoryPanelController.extractItemIDPrefix(from: line), "abc12345")
    }
}
