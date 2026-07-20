/*
 BackendToastShowTests — AGENT-M regression tests (Wave 266).

 Покрытие:
 1. show() не блокирует main thread (layout <16ms после prewarm; порядок окна — no-op).
 2. Повторный show() до dismiss быстрее первого (glyph cache hit).
 3. show() с Unicode/emoji строкой не крашит и не зависает.
 4. Конкурентные show() сериализуются через MainActor — только один панель активна.
 5. prewarmPanel() не создаёт второй NSPanel при повторном вызове.
 6. show() без предварительного prewarm тоже работает (lazy create fallback).
 7. dismissTimer отменяется при повторном show() до истечения timeout.
 8. После fadeOut заказывается скрытие панели (поведенческий паритет).

 Паттерны:
 - Каждый тест получает отдельный BackendToast и записывающий no-op порядка окон.
 - Время измеряется через CFAbsoluteTimeGetCurrent() до/после show().
 - Тестам не нужен NSScreen.main: позиционирование остаётся настоящим, порядок окна — no-op.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class BackendToastShowTests: XCTestCase {

    private var panelOrdering: RecordingPanelOrdering!

    override func setUp() async throws {
        try await super.setUp()
        panelOrdering = RecordingPanelOrdering()
    }

    override func tearDown() async throws {
        panelOrdering = nil
        try await super.tearDown()
    }

    // MARK: - Helpers

    /// Возвращает BackendToast с прогретой панелью.
    private func makeWarmToast() -> BackendToast {
        let toast = BackendToast(panelOrdering: panelOrdering)
        toast.prewarmPanel()
        // Сам prewarm обязан заказать show/hide, но проверка show() считает отдельно.
        panelOrdering.reset()
        return toast
    }

    // MARK: - 1. show() не блокирует main thread после prewarm

    func test_show_does_not_block_main_thread_after_prewarm() {
        let toast = makeWarmToast()

        let start = CFAbsoluteTimeGetCurrent()
        toast.show("Backend перезапущен", duration: 0.1)
        let elapsed = CFAbsoluteTimeGetCurrent() - start

        // После prewarm show() должен занимать <16ms (один frame budget).
        // На реальном Apple Silicon это обычно <1ms; даём запас для CI VM.
        XCTAssertLessThan(elapsed, 0.016,
            "show() blocked main thread for \(elapsed * 1000)ms (>16ms) — AGENT-M regression!")
        XCTAssertEqual(panelOrdering.orderFrontCallCount, 1)
    }

    // MARK: - 2. Повторный show() быстрее первого (glyph cache hit)

    func test_show_subsequent_calls_fast() {
        let toast = makeWarmToast()

        // Первый show() — прогревает path.
        let t0 = CFAbsoluteTimeGetCurrent()
        toast.show("Первый", duration: 0.05)
        let firstElapsed = CFAbsoluteTimeGetCurrent() - t0

        // Второй show() — должен быть ≤ первого (не хуже).
        let t1 = CFAbsoluteTimeGetCurrent()
        toast.show("Второй — быстрее", duration: 0.05)
        let secondElapsed = CFAbsoluteTimeGetCurrent() - t1

        XCTAssertLessThanOrEqual(secondElapsed, max(firstElapsed, 0.005),
            "Second show() (\(secondElapsed * 1000)ms) must not be slower than first (\(firstElapsed * 1000)ms)")
    }

    // MARK: - 3. show() с Unicode/emoji не крашит

    func test_show_handles_unicode_message() {
        let toast = makeWarmToast()

        let unicodeMessages = [
            "⚠ Backend не запускается — открой логи",
            "FATAL: \u{1F4A5} Critical error \u{2192} restart",
            "Транскрипция завершена ✓ (0.8s)",
            "Перезапуск через 15с...",
            String(repeating: "Длинное сообщение ", count: 20), // обрезается lineBreakMode
        ]

        for message in unicodeMessages {
            // Не должно крашиться или зависать.
            toast.show(message, duration: 0.05)
        }
    }

    // MARK: - 4. Конкурентные show() сериализуются через MainActor

    func test_concurrent_show_serialized() async {
        let toast = makeWarmToast()

        // Запускаем несколько show() через async Tasks — все должны выполниться
        // последовательно на MainActor без data race.
        await withTaskGroup(of: Void.self) { group in
            for i in 0..<10 {
                group.addTask { @MainActor in
                    toast.show("Message \(i)", duration: 0.05)
                }
            }
        }

        // После всех calls панель либо видима (последний show) либо в процессе fadeOut.
        // Главное — не было crash и singleton в консистентном состоянии.
        XCTAssertNotNil(toast.panel, "Panel should still exist after concurrent shows")
    }

    // MARK: - 5. prewarmPanel() не создаёт второй NSPanel

    func test_prewarm_idempotent() {
        let toast = BackendToast(panelOrdering: panelOrdering)
        toast.prewarmPanel()
        let panelAfterFirst = toast.panel

        toast.prewarmPanel() // повторный вызов
        let panelAfterSecond = toast.panel

        XCTAssertTrue(panelAfterFirst === panelAfterSecond,
            "prewarmPanel() must not replace existing panel on second call")
        XCTAssertEqual(panelOrdering.orderFrontRegardlessCallCount, 1)
        XCTAssertEqual(panelOrdering.orderOutCallCount, 1)
    }

    // MARK: - 6. show() без prewarm (lazy fallback)

    func test_show_without_prewarm_does_not_crash() {
        let toast = BackendToast(panelOrdering: panelOrdering)
        toast.show("Ленивое создание", duration: 0.05)
        XCTAssertNotNil(toast.panel, "Panel should be created lazily on first show/prewarm")
        XCTAssertEqual(panelOrdering.orderFrontCallCount, 1)
    }

    // MARK: - 7. dismissTimer отменяется при повторном show()

    func test_dismiss_timer_reset_on_repeat_show() async throws {
        let toast = makeWarmToast()

        toast.show("Первый", duration: 0.3)
        // Небольшая пауза
        try await Task.sleep(nanoseconds: 100_000_000) // 0.1s

        // Второй show() должен сбросить таймер
        toast.show("Второй", duration: 5.0)

        // Ждём 0.5s — если бы первый таймер не был отменён, панель бы скрылась через 0.3s
        let waitExp = expectation(description: "wait 0.5s after reset")
        Task {
            try await Task.sleep(nanoseconds: 500_000_000)
            waitExp.fulfill()
        }
        await fulfillment(of: [waitExp], timeout: 2.0)

        // Первый таймер не должен успеть заказать скрытие после замены на 5 секунд.
        XCTAssertEqual(
            panelOrdering.orderOutCallCount, 0,
            "Первый dismiss-таймер должен быть отменён повторным show()"
        )
    }

    // MARK: - 8. Панель скрывается после fadeOut (behavioral parity)

    func test_panel_hidden_after_fade() async throws {
        let toast = makeWarmToast()

        toast.show("Исчезнет через 0.3s", duration: 0.3)
        XCTAssertEqual(panelOrdering.orderFrontCallCount, 1)

        // Ждём fadeOut завершения: 0.3s duration + 0.25s анимация + буфер
        let hideExp = expectation(description: "panel hidden after fade")
        Task {
            try await Task.sleep(nanoseconds: 800_000_000) // 0.8s
            hideExp.fulfill()
        }
        await fulfillment(of: [hideExp], timeout: 3.0)

        XCTAssertEqual(panelOrdering.orderOutCallCount, 1,
            "После duration + fade должна быть заказана ровно одна команда скрытия")
    }
}

// MARK: - BackendToast testability extension

extension BackendToast {
    /// Возвращает созданную панель через reflection, не расширяя production API.
    var panel: NSPanel? {
        // Private storage недоступен из тестового модуля даже с @testable import.
        let mirror = Mirror(reflecting: self)
        for child in mirror.children {
            if child.label == "panel" { return child.value as? NSPanel }
        }
        return nil
    }
}
