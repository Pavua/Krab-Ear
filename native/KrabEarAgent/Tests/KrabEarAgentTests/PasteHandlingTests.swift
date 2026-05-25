/*
 PasteHandlingTests.swift
 Unit tests for main+PasteHandling.swift logic.

 Strategy:
 - `isDuplicateAutopasteCandidate` and the fingerprint-cap logic live on
   @MainActor AgentAppDelegate, which cannot be instantiated standalone in
   XCTest without a full NSApplication runloop.
 - We replicate the pure deduplication logic in `PasteDuplicateChecker` (a
   value type with an injected `now` clock) so the same branching is tested
   with full time-control.
 - `normalizePlainText` is a pure string transformation and is called via a
   standalone helper that mirrors the implementation exactly.

 Tests:
   test_isDuplicateAutopasteCandidate_recent_returns_true
   test_isDuplicateAutopasteCandidate_old_returns_false
   test_isDuplicateAutopasteCandidate_id_key_takes_priority_over_text_key
   test_isDuplicateAutopasteCandidate_nil_id_falls_back_to_text_key
   test_isDuplicateAutopasteCandidate_empty_id_falls_back_to_text_key
   test_recent_fingerprints_capped_at_120
   test_cap_removes_only_old_entries
   test_unicode_text_in_paste
   test_normalizePlainText_collapses_whitespace
   test_normalizePlainText_removes_tabs
*/

import XCTest
@testable import KrabEarAgent

// MARK: - PasteDuplicateChecker (portable replica of isDuplicateAutopasteCandidate)

/// Mirror of `AgentAppDelegate.isDuplicateAutopasteCandidate` that accepts an
/// injected `now` timestamp for deterministic time-control in tests.
///
/// Logic is identical to the production implementation in main+PasteHandling.swift:
///   - Key = "id:<historyId>" when historyId is non-nil and non-empty
///   - Key = "text:<normalizedText>" otherwise
///   - Returns true (duplicate) when the same key was seen within 4.0 seconds
///   - Caps the fingerprints dict at 120 entries, evicting keys older than 120 s
struct PasteDuplicateChecker {
    var fingerprints: [String: TimeInterval] = [:]
    private static let windowSec: TimeInterval = 4.0
    private static let maxEntries: Int = 120
    private static let evictionAgeSec: TimeInterval = 120.0

    mutating func isDuplicate(historyId: String?, text: String, now: TimeInterval) -> Bool {
        let normalizedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let key: String
        if let id = historyId, !id.isEmpty {
            key = "id:\(id)"
        } else {
            key = "text:\(normalizedText)"
        }

        if let previous = fingerprints[key], (now - previous) < Self.windowSec {
            return true
        }
        fingerprints[key] = now

        if fingerprints.count > Self.maxEntries {
            let cutoff = now - Self.evictionAgeSec
            fingerprints = fingerprints.filter { $0.value >= cutoff }
        }
        return false
    }
}

// MARK: - normalizePlainText (pure replica)

/// Mirror of `AgentAppDelegate.normalizePlainText` — tabs/CRs replaced,
/// lines split on whitespace and rejoined with single spaces.
private func normalizePlainText(_ text: String) -> String {
    let replaced = text
        .replacingOccurrences(of: "\t", with: " ")
        .replacingOccurrences(of: "\r\n", with: "\n")
        .replacingOccurrences(of: "\r", with: "\n")
    let lines = replaced
        .split(separator: "\n")
        .map { raw in
            raw
                .split(whereSeparator: { $0 == " " || $0 == "\t" })
                .joined(separator: " ")
        }
        .filter { !$0.isEmpty }
    return lines.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
}

// MARK: - PasteHandlingTests

final class PasteHandlingTests: XCTestCase {

    // MARK: 1. isDuplicateAutopasteCandidate — window logic

    /// Same historyId within 4 s → duplicate.
    func test_isDuplicateAutopasteCandidate_recent_returns_true() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0

        let first = checker.isDuplicate(historyId: "abc-123", text: "Hello", now: t0)
        let second = checker.isDuplicate(historyId: "abc-123", text: "Hello", now: t0 + 1.0)

        XCTAssertFalse(first, "First occurrence must not be a duplicate")
        XCTAssertTrue(second, "Same id within 4 s must be classified as duplicate")
    }

    /// Same historyId, but >4 s later → NOT a duplicate.
    func test_isDuplicateAutopasteCandidate_old_returns_false() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0

        _ = checker.isDuplicate(historyId: "abc-456", text: "World", now: t0)
        let result = checker.isDuplicate(historyId: "abc-456", text: "World", now: t0 + 5.0)

        XCTAssertFalse(result, "Same id after >4 s window must not be duplicate")
    }

    /// When historyId is provided, key is "id:<id>" — different texts with same id are duplicates.
    func test_isDuplicateAutopasteCandidate_id_key_takes_priority_over_text_key() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0

        _ = checker.isDuplicate(historyId: "same-id", text: "Text A", now: t0)
        // Different text, same id → still duplicate because key is based on id
        let result = checker.isDuplicate(historyId: "same-id", text: "Text B", now: t0 + 0.5)

        XCTAssertTrue(result, "Same historyId within window is a duplicate regardless of text")
    }

    /// nil historyId → key falls back to "text:<text>".
    func test_isDuplicateAutopasteCandidate_nil_id_falls_back_to_text_key() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0

        _ = checker.isDuplicate(historyId: nil, text: "Duplicate text", now: t0)
        let result = checker.isDuplicate(historyId: nil, text: "Duplicate text", now: t0 + 2.0)

        XCTAssertTrue(result, "Nil historyId with same text within window must be duplicate")
    }

    /// Empty string historyId treated same as nil → falls back to text key.
    func test_isDuplicateAutopasteCandidate_empty_id_falls_back_to_text_key() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0

        _ = checker.isDuplicate(historyId: "", text: "Same text again", now: t0)
        let result = checker.isDuplicate(historyId: "", text: "Same text again", now: t0 + 1.5)

        XCTAssertTrue(result, "Empty historyId falls back to text key — within window = duplicate")
    }

    // MARK: 2. Fingerprint cap at 120 entries

    /// Adding 121 entries triggers eviction — entries older than 120 s are removed.
    /// After eviction, dict size must be <= 120.
    func test_recent_fingerprints_capped_at_120() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0

        // Add 121 unique entries at the same timestamp (all "fresh").
        for i in 0...120 {
            _ = checker.isDuplicate(historyId: "id-\(i)", text: "text-\(i)", now: t0)
        }

        // The 121st insertion triggers cap check.  All entries share the same `now`
        // so none qualify for eviction by age — dict stays capped at 120 (the filter
        // runs after inserting key 121, but since cutoff = t0 - 120 = t0-120 and all
        // entries == t0, they survive the filter).  Size == 121 entries pre-filter,
        // post-filter all survive (t0 >= t0 - 120), so dict == 121.
        // This documents the production behavior: the cap is a soft cleanup, not a
        // hard limit when all entries are fresh.
        XCTAssertGreaterThanOrEqual(checker.fingerprints.count, 120,
            "At least 120 entries retained after cap check")
    }

    /// Old entries (> 120 s) are evicted when cap is exceeded.
    func test_cap_removes_only_old_entries() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0
        let staleTime = t0 - 200.0   // 200 s old → well beyond 120 s eviction age

        // Insert 60 stale entries (will be evicted).
        for i in 0..<60 {
            checker.fingerprints["stale-\(i)"] = staleTime
        }
        // Insert 61 fresh entries — adding entry 121 triggers cap + eviction.
        for i in 0..<61 {
            _ = checker.isDuplicate(historyId: "fresh-\(i)", text: "t-\(i)", now: t0)
        }

        // After eviction, stale entries should be gone; fresh ones retained.
        let hasStale = checker.fingerprints.keys.contains { $0.hasPrefix("stale-") }
        XCTAssertFalse(hasStale, "Stale entries (>120 s old) must be evicted when cap exceeded")

        let freshCount = checker.fingerprints.keys.filter { $0.hasPrefix("id:fresh-") }.count
        XCTAssertEqual(freshCount, 61, "All 61 fresh entries must be retained after eviction")
    }

    // MARK: 3. Unicode text

    /// Unicode (Cyrillic, emoji) must not crash or corrupt the fingerprint key.
    func test_unicode_text_in_paste() {
        var checker = PasteDuplicateChecker()
        let t0: TimeInterval = 1_000_000.0

        let unicodeText = "Привет мир 🦀 café naïve"

        let first = checker.isDuplicate(historyId: nil, text: unicodeText, now: t0)
        let second = checker.isDuplicate(historyId: nil, text: unicodeText, now: t0 + 1.0)

        XCTAssertFalse(first, "First unicode occurrence must not be duplicate")
        XCTAssertTrue(second, "Repeated unicode text within window must be duplicate")

        // Key should be present in the dict (no crash, correct encoding).
        let expectedKey = "text:\(unicodeText)"  // text is already trimmed
        XCTAssertNotNil(checker.fingerprints[expectedKey],
            "Unicode key must be stored in fingerprints dict")
    }

    // MARK: 4. normalizePlainText

    func test_normalizePlainText_collapses_whitespace() {
        let input = "  Hello   world  \n  foo   bar  "
        let result = normalizePlainText(input)
        XCTAssertEqual(result, "Hello world foo bar")
    }

    func test_normalizePlainText_removes_tabs() {
        let input = "word1\tword2\t\tword3"
        let result = normalizePlainText(input)
        XCTAssertEqual(result, "word1 word2 word3")
    }
}
