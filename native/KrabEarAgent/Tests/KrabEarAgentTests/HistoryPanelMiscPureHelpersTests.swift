/*
 HistoryPanelMiscPureHelpersTests — юнит-тесты двух pure helpers:
 1. `buildTranslationBadge(_ item: HistoryItem)` из +History.swift
 2. `formatDuration(_ seconds: Double)` из +LiveTranslation.swift
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelMiscPureHelpersTests: XCTestCase {

    // MARK: - Helpers

    private func _item(mode: String, status: String) -> HistoryItem {
        let payload: [String: Any] = [
            "id": "test-id",
            "ts": "2026-04-25T10:00:00Z",
            "text": "тест",
            "translation_mode": mode,
            "translation_status": status,
        ]
        return HistoryItem(payload: payload)!
    }

    // MARK: - buildTranslationBadge

    func test_badge_translationOff_returnsEmpty() {
        let item = _item(mode: "off", status: "ok")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "")
    }

    func test_badge_okStatus() {
        let item = _item(mode: "ru_to_es", status: "ok")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[ru_to_es:ok] ")
    }

    func test_badge_notRequested_skipMark() {
        let item = _item(mode: "auto", status: "not_requested")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[auto:skip] ")
    }

    func test_badge_modelUnavailableOffline() {
        let item = _item(mode: "es_to_ru", status: "model_unavailable_offline")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[es_to_ru:offline] ")
    }

    func test_badge_modelUnavailableOnline() {
        let item = _item(mode: "auto", status: "model_unavailable_online")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[auto:online?] ")
    }

    func test_badge_modelUnavailableCached() {
        let item = _item(mode: "auto", status: "model_unavailable_cached")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[auto:cached] ")
    }

    func test_badge_cannotDetectLanguage() {
        let item = _item(mode: "auto", status: "cannot_detect_language")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[auto:lang?] ")
    }

    func test_badge_alreadyTargetLanguage() {
        let item = _item(mode: "auto", status: "already_target_language")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[auto:ru=ok] ")
    }

    func test_badge_translateError() {
        let item = _item(mode: "ru_to_es", status: "translate_error")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[ru_to_es:error] ")
    }

    func test_badge_unknownStatus_warnFallback() {
        let item = _item(mode: "auto", status: "some_random_status")
        XCTAssertEqual(HistoryPanelController.buildTranslationBadge(item), "[auto:warn] ")
    }

    // MARK: - formatDuration

    func test_duration_zero() {
        XCTAssertEqual(HistoryPanelController.formatDuration(0), "00:00")
    }

    func test_duration_negative_clampedToZero() {
        XCTAssertEqual(HistoryPanelController.formatDuration(-5), "00:00")
    }

    func test_duration_secondsOnly() {
        XCTAssertEqual(HistoryPanelController.formatDuration(45), "00:45")
    }

    func test_duration_oneMinute() {
        XCTAssertEqual(HistoryPanelController.formatDuration(60), "01:00")
    }

    func test_duration_minutesAndSeconds() {
        XCTAssertEqual(HistoryPanelController.formatDuration(125), "02:05")
    }

    func test_duration_doubleRounding() {
        // 59.6 → rounded → 60 → "01:00"
        XCTAssertEqual(HistoryPanelController.formatDuration(59.6), "01:00")
        // 59.4 → rounded → 59 → "00:59"
        XCTAssertEqual(HistoryPanelController.formatDuration(59.4), "00:59")
    }

    func test_duration_largeValue() {
        // 1 час
        XCTAssertEqual(HistoryPanelController.formatDuration(3600), "60:00")
    }

    func test_duration_alwaysTwoDigits() {
        // 5 секунд → "00:05" (padded), не "0:5"
        XCTAssertEqual(HistoryPanelController.formatDuration(5), "00:05")
        XCTAssertEqual(HistoryPanelController.formatDuration(125), "02:05")
    }
}
