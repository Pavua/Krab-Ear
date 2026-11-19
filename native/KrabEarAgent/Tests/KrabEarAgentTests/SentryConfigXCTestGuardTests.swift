/*
 SentryConfigXCTestGuardTests — Sentry SDK не стартует внутри XCTest-бандла.

 Живой шум (KRAB-EAR-AGENT-1C, 2026-11-18): test-runner `com.apple.dt.xctest.tool`
 поднимал Sentry с боевым release/environment и репортил sleep внутри теста как
 App Hang продакшена. Гард: под XCTest `SentryConfig.initialize` — no-op,
 даже если DSN задан.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class SentryConfigXCTestGuardTests: XCTestCase {

    func test_isRunningUnderXCTest_true_when_configuration_path_present() {
        XCTAssertTrue(SentryConfig.isRunningUnderXCTest(
            environment: ["XCTestConfigurationFilePath": "/tmp/x.xctestconfiguration"],
            xctestClassLoaded: false))
    }

    func test_isRunningUnderXCTest_true_when_xctest_class_loaded() {
        XCTAssertTrue(SentryConfig.isRunningUnderXCTest(
            environment: [:], xctestClassLoaded: true))
    }

    func test_isRunningUnderXCTest_false_for_plain_app_process() {
        XCTAssertFalse(SentryConfig.isRunningUnderXCTest(
            environment: ["HOME": "/Users/x"], xctestClassLoaded: false))
    }

    func test_initialize_does_not_activate_sentry_under_xctest() {
        // Под XCTest DSN задан, но SDK обязан остаться выключенным.
        SentryConfig.initialize(dsn: "https://public@o0.ingest.sentry.io/1")
        XCTAssertFalse(SentryConfig.isActive)
    }
}
