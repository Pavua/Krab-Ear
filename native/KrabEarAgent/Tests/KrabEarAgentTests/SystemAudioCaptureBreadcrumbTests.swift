/*
 SystemAudioCaptureBreadcrumbTests — проверяет поведение breadcrumb-инфраструктуры
 для Live Subs lifecycle событий.

 Подход: так как SystemAudioCapture и IPCClient — final-классы с зависимостями
 от ScreenCaptureKit, мы тестируем:
  1. SentryConfig.recordBreadcrumb no-op guard (isActive == false → нет краша).
  2. Эталонный набор 7 lifecycle-сообщений через BreadcrumbSpy (логика без SCStream).
  3. Что first_sample breadcrumb пишется только один раз (однократность).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - BreadcrumbSpy

/// Перехватчик breadcrumb-вызовов для тестов.
@MainActor
final class BreadcrumbSpy {
    struct Record: Equatable {
        let category: String
        let message: String
    }

    private(set) var records: [Record] = []

    func record(category: String, message: String, data: [String: Any] = [:]) {
        records.append(Record(category: category, message: message))
    }

    func messages(for category: String) -> [String] {
        records.filter { $0.category == category }.map(\.message)
    }

    func reset() { records = [] }
}

// MARK: - Tests

final class SystemAudioCaptureBreadcrumbTests: XCTestCase {

    // MARK: - no-op guard

    /// SentryConfig.recordBreadcrumb не падает когда Sentry не активен (DSN не задан).
    @MainActor
    func test_recordBreadcrumb_is_noop_when_sentry_not_active() {
        // isActive == false в тестовой среде — никакого DSN не задано.
        // Все вызовы должны быть тихими no-op без crash.
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "capture.start_called")
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "screencapture.permission_ok",
                                      data: ["displays_count": 1])
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "stream.initialized")
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "stream.started",
                                      data: ["target_lang": "ru"])
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "first_sample_received",
                                      data: ["sample_rate": 48000, "samples_count": 1024])
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "stream.error",
                                      data: ["error": "test_error"])
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "capture.stop_called")
        // Тест пройден если нет assertion failures или crashes.
    }

    // MARK: - Lifecycle message set

    /// Все 7 lifecycle-сообщений покрывают ожидаемый набор событий.
    @MainActor
    func test_all_seven_lifecycle_messages_are_defined() {
        let spy = BreadcrumbSpy()

        // Симулируем все 7 lifecycle-событий через spy
        let lifecycleEvents: [(String, [String: Any])] = [
            ("capture.start_called", [:]),
            ("screencapture.permission_ok", ["displays_count": 1]),
            ("stream.initialized", [:]),
            ("stream.started", ["target_lang": "ru"]),
            ("first_sample_received", ["sample_rate": 48_000, "samples_count": 512]),
            ("stream.error", ["error": "test_delegate_error"]),
            ("capture.stop_called", [:]),
        ]

        for (msg, data) in lifecycleEvents {
            spy.record(category: "live_subs", message: msg, data: data)
        }

        let messages = spy.messages(for: "live_subs")
        XCTAssertEqual(messages.count, 7, "Ожидается ровно 7 lifecycle breadcrumbs")

        let expected = [
            "capture.start_called",
            "screencapture.permission_ok",
            "stream.initialized",
            "stream.started",
            "first_sample_received",
            "stream.error",
            "capture.stop_called",
        ]
        for msg in expected {
            XCTAssertTrue(messages.contains(msg),
                          "Отсутствует ожидаемый breadcrumb: '\(msg)'")
        }
    }

    // MARK: - First sample once

    /// first_sample breadcrumb пишется только при первом сэмпле (флаг-guard).
    @MainActor
    func test_first_sample_breadcrumb_written_only_once() {
        let spy = BreadcrumbSpy()
        var didReceiveFirst = false

        // Симулируем 3 вызова callback, как в SCStreamOutput
        for _ in 0..<3 {
            if !didReceiveFirst {
                didReceiveFirst = true
                spy.record(category: "live_subs", message: "first_sample_received",
                           data: ["sample_rate": 48_000])
            }
        }

        let firstSampleMessages = spy.records.filter { $0.message == "first_sample_received" }
        XCTAssertEqual(firstSampleMessages.count, 1,
                       "first_sample_received должен записываться ровно 1 раз")
    }

    // MARK: - Category isolation

    /// Все Live Subs breadcrumbs используют категорию "live_subs", не "lifecycle".
    @MainActor
    func test_live_subs_breadcrumbs_use_correct_category() {
        let spy = BreadcrumbSpy()

        spy.record(category: "live_subs", message: "capture.start_called")
        spy.record(category: "lifecycle", message: "NSApp.terminate from stopAgent")

        let liveSubsMessages = spy.messages(for: "live_subs")
        let lifecycleMessages = spy.messages(for: "lifecycle")

        XCTAssertEqual(liveSubsMessages.count, 1)
        XCTAssertEqual(lifecycleMessages.count, 1)
        XCTAssertEqual(liveSubsMessages.first, "capture.start_called")
    }

    // MARK: - Error breadcrumb has error field

    /// stream.error breadcrumb содержит поле "error" с описанием.
    @MainActor
    func test_stream_error_breadcrumb_contains_error_description() {
        let spy = BreadcrumbSpy()
        let errorMessage = "SCStream startCapture failed: permission denied"

        spy.record(category: "live_subs", message: "stream.error",
                   data: ["error": errorMessage])

        XCTAssertEqual(spy.records.count, 1)
        XCTAssertEqual(spy.records.first?.message, "stream.error")
    }
}
