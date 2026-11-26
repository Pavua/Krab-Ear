import XCTest
@testable import KrabEarAgent

/// G3-P04-12/13: UI приватности не рисует неподтверждённые нули и не показывает
/// внутренние детали ошибок. Данные вымышленные.
final class PrivacyUIHonestyTests: XCTestCase {

    func test_dashboardMissingSectionsAreUnknownNotZero() throws {
        let data = try XCTUnwrap(PrivacyDashboardData(result: ["ok": true]))
        XCTAssertNil(data.itemCount)
        XCTAssertNil(data.transcriptsCount)
        XCTAssertNil(data.totalDataMb)
        XCTAssertNil(data.autoPurgeEnabled)
        XCTAssertNil(data.autoPurgeRetentionDays)
        XCTAssertNil(data.auditTotalEvents)
        XCTAssertEqual(HistoryPanelController.pdCountText(data.itemCount), "Неизвестно")
        XCTAssertEqual(HistoryPanelController.pdMbText(data.totalDataMb), "Неизвестно")
        XCTAssertEqual(HistoryPanelController.pdRetentionText(enabled: data.autoPurgeEnabled,
                                                              days: data.autoPurgeRetentionDays), "Неизвестно")
    }

    func test_dashboardKnownValuesIncludingRealZeroAreShown() throws {
        let data = try XCTUnwrap(PrivacyDashboardData(result: [
            "ok": true,
            "storage": ["item_count": 0, "transcripts_count": 3, "total_data_mb": 0],
            "retention": ["auto_purge_enabled": false, "auto_purge_retention_days": 90],
            "audit": ["total_events": 0],
        ]))
        XCTAssertEqual(HistoryPanelController.pdCountText(data.itemCount), "0", "реальный ноль остаётся нулём")
        XCTAssertEqual(HistoryPanelController.pdCountText(data.transcriptsCount), "3")
        XCTAssertEqual(HistoryPanelController.pdMbText(data.totalDataMb), "0.0 МБ")
        XCTAssertEqual(HistoryPanelController.pdRetentionText(enabled: false, days: 90), "Выкл")
        XCTAssertEqual(HistoryPanelController.pdRetentionText(enabled: true, days: 30), "Вкл · 30 дн.")
        XCTAssertEqual(HistoryPanelController.pdRetentionText(enabled: true, days: nil), "Вкл")
        XCTAssertEqual(HistoryPanelController.pdCountText(data.auditTotalEvents), "0")
    }

    func test_wrongTypedFieldsAreUnknown() throws {
        let data = try XCTUnwrap(PrivacyDashboardData(result: [
            "ok": true, "storage": ["item_count": "12", "total_data_mb": "x"], "retention": ["auto_purge_enabled": "yes"],
        ]))
        XCTAssertNil(data.itemCount)
        XCTAssertNil(data.totalDataMb)
        XCTAssertNil(data.autoPurgeEnabled)
    }

    func test_jsonBoolIsNotANumber() throws {
        let data = try XCTUnwrap(PrivacyDashboardData(result: [
            "ok": true, "storage": ["total_data_mb": true],
        ]))
        XCTAssertNil(data.totalDataMb, "true не должно читаться как 1.0 МБ")
        XCTAssertEqual(HistoryPanelController.pdOptionalDouble(NSNumber(value: 2.5)), 2.5)
        XCTAssertEqual(HistoryPanelController.pdOptionalDouble(3), 3.0)
    }

    func test_auditViewerErrorTextHasNoInternalDetails() {
        struct LeakyError: LocalizedError {
            var errorDescription: String? { "open /Users/pablito/secret/path.ndjson failed" }
        }
        let text = PrivacyAuditViewerWindowController.safeErrorText(LeakyError())
        XCTAssertFalse(text.contains("/Users"))
        XCTAssertFalse(text.contains("secret"))
        XCTAssertTrue(text.contains("LeakyError"))
    }
}
