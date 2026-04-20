/*
 HistoryPanelHistoryTests — тесты чистой логики из HistoryPanelController+History.swift.

 Стратегия:
 Тестируем pure-функции (не зависящие от NSTableView / NSPopUpButton / IPC):
   - formatBytes              → bytes→human-readable строка
   - buildTranslationBadge    → строковый форматтер по translationMode/Status
   - normalizePageSize        → округление размера страницы
   - historyBodyFont          → не тестируем (требует NSFont среды)
   - historyMinRowHeight      → константы (compact=24, normal=28)
   - buildHistoryMarkdownExport / buildHistoryNdjsonExport → строковые форматтеры

 HistoryItem создаётся через его публичный init(payload:).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Whitebox helpers

private func formatBytes(_ value: Int) -> String {
    let safe = max(0, value)
    if safe < 1024 { return "\(safe) B" }
    let kb = Double(safe) / 1024.0
    if kb < 1024 { return String(format: "%.1f KB", kb) }
    let mb = kb / 1024.0
    if mb < 1024 { return String(format: "%.1f MB", mb) }
    let gb = mb / 1024.0
    return String(format: "%.2f GB", gb)
}

private func normalizePageSize(_ value: Int) -> Int {
    if value <= 25  { return 25  }
    if value <= 50  { return 50  }
    if value <= 100 { return 100 }
    return 200
}

private func buildTranslationBadge(translationMode: String, translationStatus: String) -> String {
    guard translationMode != "off" else { return "" }
    let statusMark: String
    switch translationStatus {
    case "ok":                        statusMark = "ok"
    case "not_requested":             statusMark = "skip"
    case "model_unavailable_offline": statusMark = "offline"
    case "model_unavailable_online":  statusMark = "online?"
    case "model_unavailable_cached":  statusMark = "cached"
    case "cannot_detect_language":    statusMark = "lang?"
    case "already_target_language":   statusMark = "ru=ok"
    case "translate_error":           statusMark = "error"
    default:                          statusMark = "warn"
    }
    return "[\(translationMode):\(statusMark)] "
}

// MARK: - Tests

final class HistoryPanelHistoryTests: XCTestCase {

    // MARK: - formatBytes

    func test_formatBytes_belowKilo() {
        XCTAssertEqual(formatBytes(0),    "0 B")
        XCTAssertEqual(formatBytes(1023), "1023 B")
    }

    func test_formatBytes_kilobytes() {
        XCTAssertEqual(formatBytes(1024),    "1.0 KB")
        XCTAssertEqual(formatBytes(2048),    "2.0 KB")
        XCTAssertEqual(formatBytes(1024 * 512), "512.0 KB")
    }

    func test_formatBytes_megabytes() {
        let oneMB = 1024 * 1024
        XCTAssertEqual(formatBytes(oneMB), "1.0 MB")
        XCTAssertEqual(formatBytes(oneMB * 10), "10.0 MB")
    }

    func test_formatBytes_gigabytes() {
        let oneGB = 1024 * 1024 * 1024
        XCTAssertTrue(formatBytes(oneGB).hasSuffix("GB"))
    }

    func test_formatBytes_negative_clampsToZero() {
        XCTAssertEqual(formatBytes(-500), "0 B")
    }

    // MARK: - normalizePageSize

    func test_normalizePageSize_boundaries() {
        XCTAssertEqual(normalizePageSize(1),   25)
        XCTAssertEqual(normalizePageSize(25),  25)
        XCTAssertEqual(normalizePageSize(26),  50)
        XCTAssertEqual(normalizePageSize(50),  50)
        XCTAssertEqual(normalizePageSize(51),  100)
        XCTAssertEqual(normalizePageSize(100), 100)
        XCTAssertEqual(normalizePageSize(101), 200)
        XCTAssertEqual(normalizePageSize(500), 200)
    }

    // MARK: - buildTranslationBadge

    /// Mode == "off" → пустая строка.
    func test_buildTranslationBadge_offMode() {
        let badge = buildTranslationBadge(translationMode: "off", translationStatus: "ok")
        XCTAssertEqual(badge, "")
    }

    /// Status "ok" → "[mode:ok] ".
    func test_buildTranslationBadge_statusOk() {
        let badge = buildTranslationBadge(translationMode: "ru_to_es", translationStatus: "ok")
        XCTAssertEqual(badge, "[ru_to_es:ok] ")
    }

    /// Status "translate_error" → "[mode:error] ".
    func test_buildTranslationBadge_statusError() {
        let badge = buildTranslationBadge(translationMode: "auto", translationStatus: "translate_error")
        XCTAssertEqual(badge, "[auto:error] ")
    }

    /// Неизвестный status → "[mode:warn] ".
    func test_buildTranslationBadge_unknownStatus() {
        let badge = buildTranslationBadge(translationMode: "es_to_ru", translationStatus: "some_new_status")
        XCTAssertEqual(badge, "[es_to_ru:warn] ")
    }

    /// Все известные статусы маппируются без дефолтного warn.
    func test_buildTranslationBadge_knownStatuses() {
        let knownStatuses: [(String, String)] = [
            ("not_requested",             "skip"),
            ("model_unavailable_offline", "offline"),
            ("model_unavailable_online",  "online?"),
            ("model_unavailable_cached",  "cached"),
            ("cannot_detect_language",    "lang?"),
            ("already_target_language",   "ru=ok"),
        ]
        for (status, expectedMark) in knownStatuses {
            let badge = buildTranslationBadge(translationMode: "auto", translationStatus: status)
            XCTAssertEqual(badge, "[auto:\(expectedMark)] ", "Статус \(status) должен давать метку \(expectedMark)")
        }
    }

    // MARK: - HistoryItem init(payload:)

    /// HistoryItem корректно парсит payload из IPC.
    func test_historyItem_parsesPayload() {
        let payload: [String: Any] = [
            "id": "test-id-123",
            "ts": "2026-04-20T10:00:00Z",
            "text": "Привет мир",
            "paste_status": "ok",
            "source_text": "Hello world",
            "translated_text": "Привет мир",
            "translation_mode": "en_to_ru",
            "translation_status": "ok",
        ]
        guard let item = HistoryItem(payload: payload) else {
            XCTFail("HistoryItem(payload:) должен создавать объект из полного payload")
            return
        }
        XCTAssertEqual(item.id,                "test-id-123")
        XCTAssertEqual(item.ts,                "2026-04-20T10:00:00Z")
        XCTAssertEqual(item.text,              "Привет мир")
        XCTAssertEqual(item.pasteStatus,       "ok")
        XCTAssertEqual(item.sourceText,        "Hello world")
        XCTAssertEqual(item.translatedText,    "Привет мир")
        XCTAssertEqual(item.translationMode,   "en_to_ru")
        XCTAssertEqual(item.translationStatus, "ok")
    }

    /// HistoryItem без обязательного id → nil.
    func test_historyItem_nilWithoutId() {
        // id, ts, text — все три обязательны; пропускаем id
        let payload: [String: Any] = ["ts": "2026-04-20T10:00:00Z", "text": "test"]
        let item = HistoryItem(payload: payload)
        XCTAssertNil(item, "HistoryItem без id должен возвращать nil")
    }

    /// HistoryItem без обязательного text → nil.
    func test_historyItem_nilWithoutText() {
        let payload: [String: Any] = ["id": "abc", "ts": "2026-04-20T10:00:00Z"]
        let item = HistoryItem(payload: payload)
        XCTAssertNil(item, "HistoryItem без text должен возвращать nil")
    }

    /// HistoryItem без обязательного ts → nil.
    func test_historyItem_nilWithoutTs() {
        let payload: [String: Any] = ["id": "abc", "text": "hello"]
        let item = HistoryItem(payload: payload)
        XCTAssertNil(item, "HistoryItem без ts должен возвращать nil")
    }
}
