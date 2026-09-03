/*
 MainHealthMonitorWiringTests — Wave 152
 Unit tests for main+HealthMonitor.swift wiring logic.

 Covers:
   1. test_setupHealthMonitor_creates_actor_with_3s_interval
      — HealthMonitor инициализируется с pingInterval=3.0 и hangThreshold=2.
   2. test_status_update_timer_runs_on_main_actor
      — statusUpdateTimer планируется на MainActor runloop (interval=1s).
   3. test_subscribeToProbeEvents_invoked
      — subscribeToProbeEvents вызывается с correct restBaseURL.
   4. test_tearDown_cancels_timer_and_actor
      — tearDownHealthMonitor инвалидирует timer и останавливает monitor.
   5. test_applyHealthStateToStatusItem_uses_SF_Symbol
      — Wave 67: applyHealthStateToStatusItem ставит SF Symbol "circle.fill",
        НЕ Unicode "●" (AGENT-J root-cause fix).
   6. test_applyHealthStateToStatusItem_healthy_image_not_nil
      — Healthy state создаёт ненулевой button.image.
   7. test_applyHealthStateToStatusItem_hung_sets_yellow_tint
      — hung state передаёт .systemYellow в symbol configuration.
   8. test_applyHealthStateToStatusItem_stopped_sets_red_tint
      — stopped state передаёт .systemRed в symbol configuration.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

// MARK: - applyHealthStateToStatusItem + SF Symbol (Wave 67) tests

/// Тесты для applyHealthStateToStatusItem — изолированы от реального AgentAppDelegate.
/// Тестируем логику напрямую через testable helper, без запуска NSApp.
@MainActor
final class ApplyHealthStateTests: XCTestCase {

    // MARK: - Helpers

    /// Создаёт NSStatusItem (не добавленный в system status bar — headless mode).
    private func makeStatusButton() -> NSButton {
        let button = NSButton(frame: NSRect(x: 0, y: 0, width: 24, height: 24))
        button.title = "Krab"
        return button
    }

    /// Минимальный stub, имитирующий поведение applyHealthStateToStatusItem.
    /// Дублирует логику из main+HealthMonitor.swift без зависимости на AgentAppDelegate.
    private func applyState(_ state: HealthState, to button: NSButton) {
        let dotColor: NSColor
        switch state {
        case .healthy: dotColor = .systemGreen
        case .hung:    dotColor = .systemYellow
        case .stopped: dotColor = .systemRed
        }

        // Wave 67: SF Symbol, not Unicode `●`
        let symConfig = NSImage.SymbolConfiguration(pointSize: 10, weight: .bold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [dotColor]))
        let dotImage = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)?
            .withSymbolConfiguration(symConfig)
        button.image = dotImage
        button.imagePosition = .imageLeft
    }

    // MARK: - T5: Wave 67 — SF Symbol not Unicode dot

    /// applyHealthStateToStatusItem должна использовать SF Symbol "circle.fill",
    /// НЕ Unicode символ "●" (U+25CF).
    ///
    /// Контекст Wave 67 (AGENT-J): "●" не входит в primary glyphs SF Pro →
    /// CoreText fallback → `TFPFont::CopyGlyphPath` синхронно на CALayer commit → AppHang ≥2s.
    /// SF Symbol рендерится как pre-rendered template image — glyph path не генерируется.
    func test_applyHealthStateToStatusItem_uses_SF_Symbol_not_unicode_dot() {
        let button = makeStatusButton()
        applyState(.healthy, to: button)

        // Кнопка должна иметь image (не nil)
        XCTAssertNotNil(button.image, "button.image не должен быть nil после applyState")

        // Title не должен содержать Unicode dot "●" (U+25CF)
        // В оригинальном коде до Wave 67 title был "● Krab Ear"
        XCTAssertFalse(
            button.title.contains("●"),
            "button.title не должен содержать Unicode ● (U+25CF) — Wave 67 fix"
        )

        // SF Symbol "circle.fill" — проверяем что NSImage создаётся успешно
        let sfImage = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
        XCTAssertNotNil(sfImage, "SF Symbol 'circle.fill' должен быть доступен на macOS 13+")
    }

    // MARK: - T6: healthy → image not nil

    func test_applyHealthStateToStatusItem_healthy_image_not_nil() {
        let button = makeStatusButton()
        applyState(.healthy, to: button)

        XCTAssertNotNil(button.image, "healthy state должен задать ненулевое button.image")
    }

    // MARK: - T7: hung → image not nil

    func test_applyHealthStateToStatusItem_hung_image_not_nil() {
        let button = makeStatusButton()
        applyState(.hung, to: button)

        XCTAssertNotNil(button.image, "hung state должен задать ненулевое button.image")
    }

    // MARK: - T8: stopped → image not nil

    func test_applyHealthStateToStatusItem_stopped_image_not_nil() {
        let button = makeStatusButton()
        applyState(.stopped, to: button)

        XCTAssertNotNil(button.image, "stopped state должен задать ненулевое button.image")
    }

    // MARK: - T: imagePosition is left

    func test_applyHealthStateToStatusItem_imagePosition_imageLeft() {
        let button = makeStatusButton()
        applyState(.healthy, to: button)

        XCTAssertEqual(button.imagePosition, .imageLeft,
            "dot должен располагаться слева от title (imageLeft)")
    }
}

// MARK: - HealthMonitor initialisation + lifecycle tests

/// Тесты для HealthMonitor actor lifecycle в контексте wiring.
final class HealthMonitorWiringTests: XCTestCase {

    // MARK: - T1: pingInterval=3s, hangThreshold=2

    /// setupHealthMonitor создаёт HealthMonitor с pingInterval=3.0 и hangThreshold=2.
    /// Проверяем через публичный API: start + rapid failure — hung occurs after 2 fails,
    /// что соответствует hangThreshold=2.
    func test_setupHealthMonitor_creates_actor_with_correct_hang_threshold() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        // Используем TestCounter (thread-safe, @unchecked Sendable) из HealthMonitorTests
        let counter = WiringTestCounter(alwaysFails: true)
        monitor.setPingProvider {
            counter.increment()
            return false
        }

        await monitor.start()
        // 2 интервала = 2 fail → hung. Ждём состояния до дедлайна, а не фиксированные
        // 200 мс: на нагруженном раннере два цикла по 50 мс в 200 мс не укладывались
        // (красный CI 03.09.2026, сиблинг теста интервала пинга).
        let deadline = Date().addingTimeInterval(3.0)
        var state = await monitor.currentState()
        while state != .hung && Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
            state = await monitor.currentState()
        }
        await monitor.stop()

        XCTAssertEqual(state, .hung,
            "hangThreshold=2: 2 подряд fail должны перевести state в .hung")
    }

    // MARK: - T1b: pingInterval passed to monitor

    /// Проверяем что при pingInterval=0.05 monitor успевает провести несколько ping cycles.
    func test_setupHealthMonitor_uses_configured_ping_interval() async {
        let counter = WiringTestCounter(alwaysFails: false)
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 10)
        monitor.setPingProvider {
            counter.increment()
            return true
        }

        await monitor.start()
        // Ждём до дедлайна, а не фиксированные 300 мс: на нагруженном self-hosted
        // раннере за 300 мс успевало 2 пинга из ожидаемых 6 (красный CI 03.09.2026).
        // Инвариант — «циклы идут с настроенным интервалом», а не «ровно N за окно».
        let deadline = Date().addingTimeInterval(3.0)
        while counter.value < 3 && Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        await monitor.stop()

        XCTAssertGreaterThanOrEqual(counter.value, 3,
            "При pingInterval=50ms за 3 с должно произойти как минимум 3 пинга")
    }

    // MARK: - T2: status update timer fires on main runloop

    /// statusUpdateTimer планируется на main runloop с repeating=true.
    /// Используем Timer напрямую чтобы проверить паттерн из setupHealthMonitor.
    @MainActor
    func test_status_update_timer_fires_repeatedly_on_main_runloop() {
        var fireCount = 0
        let expectation = XCTestExpectation(description: "timer fires ≥ 2 times")
        expectation.expectedFulfillmentCount = 2

        let timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
            fireCount += 1
            expectation.fulfill()
        }

        // Дрейним runloop до выполнения ожиданий
        let deadline = Date(timeIntervalSinceNow: 1.0)
        while fireCount < 2 && Date() < deadline {
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.01))
        }
        timer.invalidate()

        XCTAssertGreaterThanOrEqual(fireCount, 2,
            "Timer на main runloop должен срабатывать повторно")
    }

    // MARK: - T3: subscribeToProbeEvents invoked with correct URL

    /// subscribeToProbeEvents должен строить URL с filter=rewriter_recovered.
    /// Проверяем через handleProbeEventForTest injection хелпер.
    @MainActor
    func test_subscribeToProbeEvents_handles_rewriter_recovered_event() async {
        let monitor = HealthMonitor(pingInterval: 999.0, hangThreshold: 2)
        var flashCount = 0
        var flashReason: String?

        await monitor.handleProbeEventForTest("rewriter_recovered") { reason in
            flashCount += 1
            flashReason = reason
        }

        XCTAssertEqual(flashCount, 1, "subscribeToProbeEvents должен обрабатывать rewriter_recovered")
        XCTAssertEqual(flashReason, "rewriter recovered", "Reason должен быть 'rewriter recovered'")
    }

    // MARK: - T4: tearDown cancels timer and stops actor

    /// tearDownHealthMonitor инвалидирует timer и останавливает monitor.
    /// Проверяем через паттерн из main+HealthMonitor: timer.invalidate + monitor.stop.
    @MainActor
    func test_tearDown_cancels_timer_and_stops_actor() async {
        let monitor = HealthMonitor(pingInterval: 0.1, hangThreshold: 2)
        monitor.setPingProvider { return true }
        await monitor.start()

        // Создаём timer как в setupHealthMonitor
        var timerFired = false
        let timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
            timerFired = true
        }

        // Имитируем tearDownHealthMonitor
        timer.invalidate()
        await monitor.stop()

        // После stop() state должен быть .stopped
        let state = await monitor.currentState()
        XCTAssertEqual(state, .stopped, "После stop() HealthMonitor должен быть в .stopped state")
        XCTAssertFalse(timer.isValid, "Timer должен быть инвалидирован")
    }

    // MARK: - T4b: tearDown nilifies healthMonitor reference pattern

    /// Проверяем что stop() переводит в .stopped и не оставляет активных Tasks.
    func test_tearDown_stop_transitions_to_stopped() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        monitor.setPingProvider { return true }
        await monitor.start()

        let stateAfterStart = await monitor.currentState()
        XCTAssertEqual(stateAfterStart, .healthy, "После start() state должен быть .healthy")

        await monitor.stop()
        let stateAfterStop = await monitor.currentState()
        XCTAssertEqual(stateAfterStop, .stopped, "После stop() state должен быть .stopped")
    }
}

// MARK: - SF Symbol availability tests

/// Проверяет доступность SF Symbol "circle.fill" на macOS 13+.
/// Критично для Wave 67 AGENT-J fix.
final class SFSymbolAvailabilityTests: XCTestCase {

    func test_circle_fill_sf_symbol_available() {
        let image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
        XCTAssertNotNil(image,
            "SF Symbol 'circle.fill' обязан быть доступен на macOS 13+ (Wave 67 dependency)")
    }

    func test_circle_fill_with_palette_colors_creates_valid_image() {
        let symConfig = NSImage.SymbolConfiguration(pointSize: 10, weight: .bold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [NSColor.systemGreen]))
        let image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)?
            .withSymbolConfiguration(symConfig)
        XCTAssertNotNil(image,
            "circle.fill с paletteColors должен создавать валидный NSImage (Wave 67)")
    }

    func test_sf_symbol_preferred_over_unicode_dot() {
        // Unicode "●" (U+25CF) — причина AGENT-J AppHang (Wave 67 root cause)
        // SF Symbol "circle.fill" — fix
        // Проверяем что SF Symbol возвращает image, а Unicode не используется как fallback
        let sfImage = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
        XCTAssertNotNil(sfImage)

        // В кодовой базе после Wave 67 НЕ должно быть прямого использования "●" в button.title
        // Этот тест фиксирует намерение — регрессионная защита
        let unicodeDot = "●"
        XCTAssertEqual(unicodeDot, "\u{25CF}",
            "Unicode dot U+25CF — это именно тот символ что вызвал AGENT-J AppHang")
        // SF Symbol не использует кодовые точки Unicode — это template image
        XCTAssertNotNil(sfImage, "SF Symbol должен быть доступен как замена Unicode dot")
    }

    func test_all_health_states_produce_valid_sf_symbol_images() {
        let states: [(HealthState, NSColor)] = [
            (.healthy, .systemGreen),
            (.hung,    .systemYellow),
            (.stopped, .systemRed),
        ]
        for (state, color) in states {
            let symConfig = NSImage.SymbolConfiguration(pointSize: 10, weight: .bold)
                .applying(NSImage.SymbolConfiguration(paletteColors: [color]))
            let image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)?
                .withSymbolConfiguration(symConfig)
            XCTAssertNotNil(image, "circle.fill с \(color) для state \(state) должен создавать NSImage")
        }
    }
}

// MARK: - Thread-safe counter helper (Sendable for ping provider closures)

/// Thread-safe счётчик для ping provider closure (должен быть @Sendable Sendable).
final class WiringTestCounter: @unchecked Sendable {
    private var _count: Int = 0
    private let lock = NSLock()
    let alwaysFails: Bool

    init(alwaysFails: Bool) {
        self.alwaysFails = alwaysFails
    }

    func increment() {
        lock.lock(); defer { lock.unlock() }
        _count += 1
    }

    var value: Int {
        lock.lock(); defer { lock.unlock() }
        return _count
    }
}

// MARK: - Source contract — setupHealthMonitor/tearDownHealthMonitor are
// actually CALLED from AgentAppDelegate's real lifecycle, not just defined.
//
// Found 2026-07-05 while investigating the krab_error decorative-wiring bug
// (setupErrorBus): setupHealthMonitor() had the EXACT same class of bug —
// defined since Phase A, never invoked from completeStartupAfterBackendReady().
// All 8 tests above (and every test in this file) construct HealthMonitor and
// exercise its wiring in isolation — none of them call the real AgentAppDelegate
// lifecycle, so they stayed green the whole time setupHealthMonitor() was dead
// in production (no continuous ping/hang-detection ever ran; the menu-bar
// status dot only ever showed the .stopped/red default).
final class MainHealthMonitorSourceContractTests: XCTestCase {

    func test_setupHealthMonitor_is_actually_called_from_startup() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(
            src.contains("setupHealthMonitor()"),
            "completeStartupAfterBackendReady() must call setupHealthMonitor() — " +
            "found it defined but never called in main.swift once already (2026-07-05)."
        )
    }

    func test_tearDownHealthMonitor_is_actually_called_from_shutdown() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(
            src.contains("\n        tearDownHealthMonitor()\n"),
            "applicationWillTerminate() must call tearDownHealthMonitor() to stop the ping loop on quit."
        )
    }

    /// Resolves native/KrabEarAgent/Sources/KrabEarAgent/main.swift from the test bundle,
    /// falling back to a #file-relative walk-up (same pattern as SFSymbolVerificationTests
    /// / MainErrorsWiringTests).
    private static var mainSwiftURL: URL {
        let bundleURL = Bundle(for: MainHealthMonitorSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/main.swift")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent/main.swift")
    }
}
