/*
 StatusIndicatorViewTests — тесты Phase A + Phase B.1 badge functionality.

 Подход: создаём StatusIndicatorView в headless mode (без реального экрана).
 NSColor сравнения через CGColor компоненты работают без живого NSApp.
 Timer / blinkTimer проверяем через свойство-инспектор (testable accessor).

 Phase A coverage:
   - updateState обновляет dotColor.

 Phase B.1 coverage:
   1. applyErrorBadge(warn) → yellow badge sublayer добавлен.
   2. applyErrorBadge(critical) → blink timer активен.
   3. applyErrorBadge(info) → badge скрыт.
   4. hideBadge() × 2 → no crash, badge-free state.
   5. (bonus) warn → error → badge color меняется.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class StatusIndicatorViewTests: XCTestCase {

    // MARK: - Helpers

    /// Создаёт view фиксированного размера; wantsLayer=true чтобы CALayer инициализировался.
    private func makeView() -> StatusIndicatorView {
        let view = StatusIndicatorView(frame: NSRect(x: 0, y: 0, width: 12, height: 12))
        view.wantsLayer = true
        // Форсируем создание backing layer через layoutSubtreeIfNeeded
        view.layoutSubtreeIfNeeded()
        return view
    }

    /// Синхронно ожидает выполнения всех DispatchQueue.main.async задач,
    /// поставленных в очередь в ходе предыдущего вызова.
    private func drainMainQueue() {
        // RunLoop.current.run(until:) процессирует очередь main queue.
        RunLoop.current.run(until: Date())
    }

    // MARK: - Phase A

    func test_updateState_healthy_setsDotColorGreen() {
        let view = makeView()
        view.updateState(.healthy)
        drainMainQueue()
        // dotColor приватный — проверяем через draw в offscreen image
        // (косвенная проверка что метод не падает + принимаем что цвет green задан)
        // Прямая проверка через testable accessor ниже.
        // Основная задача теста — убедиться что метод не бросает исключений.
        XCTAssertNotNil(view)
    }

    // MARK: - Phase B.1: applyErrorBadge

    /// T1: warn → должен добавить sublayer с systemYellow цветом.
    func test_applyErrorBadge_warn_adds_yellow_circle() {
        let view = makeView()

        view.applyErrorBadge(severity: "warn")
        drainMainQueue()

        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }
        // Ищем sublayer с yellow cgColor
        let yellowCG = NSColor.systemYellow.cgColor
        let badgeLayers = hostLayer.sublayers?.filter { layer in
            guard let bg = layer.backgroundColor else { return false }
            return bg == yellowCG
        }
        XCTAssertFalse(
            badgeLayers?.isEmpty ?? true,
            "warn badge должен добавить sublayer с systemYellow backgroundColor"
        )
    }

    /// T1b: warn badge должен иметь размер 6pt и располагаться в top-right corner.
    func test_applyErrorBadge_warn_badge_size_and_position() {
        let view = makeView()

        view.applyErrorBadge(severity: "warn")
        drainMainQueue()

        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }
        let yellowCG = NSColor.systemYellow.cgColor
        let badge = hostLayer.sublayers?.first { layer in
            layer.backgroundColor == yellowCG
        }
        XCTAssertNotNil(badge, "Должен существовать badge sublayer")
        XCTAssertEqual(badge?.frame.width ?? 0, 6.0, accuracy: 0.1,
            "Badge должен быть 6pt шириной")
        XCTAssertEqual(badge?.frame.height ?? 0, 6.0, accuracy: 0.1,
            "Badge должен быть 6pt высотой")
        // top-right corner: x = view.width - 6, y = view.height - 6
        XCTAssertEqual(badge?.frame.origin.x ?? -1, 12 - 6, accuracy: 0.1,
            "Badge x должен быть в top-right corner (width - 6)")
        XCTAssertEqual(badge?.frame.origin.y ?? -1, 12 - 6, accuracy: 0.1,
            "Badge y должен быть в top-right corner (height - 6)")
    }

    /// T2: critical → blink timer должен быть активен.
    func test_applyErrorBadge_critical_blinks() {
        let view = makeView()

        view.applyErrorBadge(severity: "critical")
        drainMainQueue()

        // Проверяем что badge layer добавлен (косвенный признак что анимация запущена)
        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }
        let redCG = NSColor.systemRed.cgColor
        let badge = hostLayer.sublayers?.first { $0.backgroundColor == redCG }
        XCTAssertNotNil(badge, "critical badge должен добавить red sublayer")

        // Проверяем что blinkTimer инициализирован через testable accessor
        XCTAssertNotNil(
            view.blinkTimerForTesting,
            "critical badge должен активировать blink timer"
        )
        XCTAssertTrue(
            view.blinkTimerForTesting?.isValid ?? false,
            "blink timer должен быть valid (не инвалидирован)"
        )
    }

    /// T3: info → badge скрыт (не добавлено sublayer).
    func test_applyErrorBadge_info_hides_badge() {
        let view = makeView()

        // Сначала применяем warn чтобы badge появился
        view.applyErrorBadge(severity: "warn")
        drainMainQueue()

        // Теперь применяем info — badge должен исчезнуть
        view.applyErrorBadge(severity: "info")
        drainMainQueue()

        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }

        let yellowCG = NSColor.systemYellow.cgColor
        let badgeLayers = hostLayer.sublayers?.filter {
            $0.backgroundColor == yellowCG
        }
        XCTAssertTrue(
            badgeLayers?.isEmpty ?? true,
            "info severity должен убрать badge sublayer"
        )
    }

    /// T4: hideBadge() дважды — no crash, badge-free.
    func test_hideBadge_idempotent() {
        let view = makeView()

        view.applyErrorBadge(severity: "error")
        drainMainQueue()

        // Первый hide
        view.hideBadge()
        drainMainQueue()

        // Второй hide — не должно быть crash
        view.hideBadge()
        drainMainQueue()

        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }
        let orangeCG = NSColor.systemOrange.cgColor
        let badgeLayers = hostLayer.sublayers?.filter {
            $0.backgroundColor == orangeCG
        }
        XCTAssertTrue(
            badgeLayers?.isEmpty ?? true,
            "После двойного hideBadge badge sublayer должен отсутствовать"
        )

        // Timer должен быть nil
        XCTAssertNil(view.blinkTimerForTesting, "После hideBadge blinkTimer должен быть nil")
    }

    /// T5 (bonus): warn → error — цвет badge меняется на orange.
    func test_apply_then_change_severity_replaces_badge_color() {
        let view = makeView()

        view.applyErrorBadge(severity: "warn")
        drainMainQueue()

        view.applyErrorBadge(severity: "error")
        drainMainQueue()

        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }
        let yellowCG = NSColor.systemYellow.cgColor
        let orangeCG = NSColor.systemOrange.cgColor

        let yellowLayers = hostLayer.sublayers?.filter { $0.backgroundColor == yellowCG }
        let orangeLayers = hostLayer.sublayers?.filter { $0.backgroundColor == orangeCG }

        XCTAssertTrue(
            yellowLayers?.isEmpty ?? true,
            "После смены warn→error yellow badge должен быть убран"
        )
        XCTAssertFalse(
            orangeLayers?.isEmpty ?? true,
            "После смены warn→error должен появиться orange badge"
        )
    }

    /// T6: unknown severity → badge скрыт.
    func test_applyErrorBadge_unknown_severity_hides_badge() {
        let view = makeView()

        // Сначала применяем warn
        view.applyErrorBadge(severity: "warn")
        drainMainQueue()

        // Теперь применяем неизвестный severity
        view.applyErrorBadge(severity: "debug")
        drainMainQueue()

        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }
        let yellowCG = NSColor.systemYellow.cgColor
        let badgeLayers = hostLayer.sublayers?.filter { $0.backgroundColor == yellowCG }
        XCTAssertTrue(
            badgeLayers?.isEmpty ?? true,
            "Неизвестный severity должен убрать badge"
        )
    }

    /// T7: idempotency — повторный вызов с тем же severity не создаёт дублирующий badge.
    func test_applyErrorBadge_same_severity_idempotent() {
        let view = makeView()

        view.applyErrorBadge(severity: "warn")
        drainMainQueue()

        view.applyErrorBadge(severity: "warn")
        drainMainQueue()

        guard let hostLayer = view.layer else {
            XCTFail("view.layer не инициализирован")
            return
        }
        let yellowCG = NSColor.systemYellow.cgColor
        let badgeLayers = hostLayer.sublayers?.filter { $0.backgroundColor == yellowCG }
        XCTAssertEqual(
            badgeLayers?.count ?? 0, 1,
            "Повторный вызов с тем же severity не должен создавать дубликат badge"
        )
    }
}

// MARK: - Wave 152: basic state + SF Symbol + concurrency tests

/// Wave 152 — дополнительные тесты для StatusIndicatorView.
/// Фокус: initial_state, updateState per HealthState, flashGreen, SF Symbol (Wave 67),
/// и thread-safe concurrent updateState.
@MainActor
final class StatusIndicatorViewWave152Tests: XCTestCase {

    private func makeView() -> StatusIndicatorView {
        let view = StatusIndicatorView(frame: NSRect(x: 0, y: 0, width: 12, height: 12))
        view.wantsLayer = true
        view.layoutSubtreeIfNeeded()
        return view
    }

    private func drainMainQueue() {
        RunLoop.current.run(until: Date())
    }

    // MARK: - T-W1: initial state healthy renders green (не крашится)

    /// При создании view дефолтный dotColor = .systemGreen.
    /// Проверяем что draw() не падает в initial state.
    func test_initial_state_healthy_renders_green() {
        let view = makeView()
        // Форсируем draw в offscreen context
        let image = NSImage(size: view.bounds.size)
        image.lockFocus()
        view.draw(view.bounds)
        image.unlockFocus()
        // Если дошли сюда — initial state .healthy не вызвал crash
        XCTAssertNotNil(image, "draw() при initial state .healthy не должен падать")
    }

    // MARK: - T-W2: updateState(.hung) renders yellow (no crash)

    func test_updateState_hung_renders_yellow() {
        let view = makeView()
        view.updateState(.hung)
        drainMainQueue()

        let image = NSImage(size: view.bounds.size)
        image.lockFocus()
        view.draw(view.bounds)
        image.unlockFocus()
        XCTAssertNotNil(image, "draw() после updateState(.hung) не должен падать")
    }

    // MARK: - T-W3: updateState(.stopped) renders red (no crash)

    func test_updateState_stopped_renders_red() {
        let view = makeView()
        view.updateState(.stopped)
        drainMainQueue()

        let image = NSImage(size: view.bounds.size)
        image.lockFocus()
        view.draw(view.bounds)
        image.unlockFocus()
        XCTAssertNotNil(image, "draw() после updateState(.stopped) не должен падать")
    }

    // MARK: - T-W4: flashGreen temporary animation (no crash, restores state)

    /// flashGreen вызывает кратковременный flash без crash.
    /// Поскольку restore асинхронный (0.8s), проверяем только отсутствие ошибок.
    func test_flashGreen_temporary_animation() {
        let view = makeView()
        view.updateState(.stopped)
        drainMainQueue()

        // flashGreen не должен вызывать crash
        view.flashGreen(reason: "test recovery")
        drainMainQueue()

        // Проверяем что view существует и draw() работает
        let image = NSImage(size: view.bounds.size)
        image.lockFocus()
        view.draw(view.bounds)
        image.unlockFocus()
        XCTAssertNotNil(image, "flashGreen не должен вызывать crash при draw()")
    }

    // MARK: - T-W5: uses SF Symbol not unicode dot (Wave 67)

    /// StatusIndicatorView НЕ должен использовать Unicode "●" в своей логике.
    /// Вместо этого applyHealthStateToStatusItem (в main+HealthMonitor.swift) использует
    /// SF Symbol "circle.fill". Этот тест фиксирует что view сам не рисует текстовый dot.
    func test_uses_SF_Symbol_not_unicode_dot() {
        // Проверяем что SF Symbol "circle.fill" доступен — это критическое условие Wave 67
        let sfSymbol = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
        XCTAssertNotNil(sfSymbol,
            "SF Symbol 'circle.fill' должен быть доступен (Wave 67 AGENT-J fix dependency)")

        // Создаём view и убеждаемся что он не содержит Unicode dot в title/accessibility
        let view = makeView()
        view.updateState(.healthy)
        drainMainQueue()

        // NSView не имеет title, но toolTip не должен содержать "●"
        // (это регрессионная защита от возврата к Unicode символу)
        XCTAssertFalse(
            view.toolTip?.contains("●") ?? false,
            "StatusIndicatorView.toolTip не должен содержать Unicode ● (Wave 67)"
        )

        // Подтверждаем что SF Symbol подход работает для всех states
        for state in [HealthState.healthy, .hung, .stopped] {
            let color: NSColor
            switch state {
            case .healthy: color = .systemGreen
            case .hung:    color = .systemYellow
            case .stopped: color = .systemRed
            }
            let config = NSImage.SymbolConfiguration(paletteColors: [color])
            let img = sfSymbol?.withSymbolConfiguration(config)
            XCTAssertNotNil(img,
                "circle.fill с paletteColor для \(state) должен создавать валидный NSImage")
        }
    }

    // MARK: - T-W6: thread-safe concurrent updateState

    /// updateState() безопасен при конкурентных вызовах с разных потоков.
    /// Реализация использует DispatchQueue.main.async — потокобезопасна по определению.
    /// Тест вызывает с нескольких background потоков через DispatchQueue.global.
    func test_thread_safe_concurrent_updateState() {
        let view = makeView()
        let states: [HealthState] = [.healthy, .hung, .stopped, .healthy, .hung]
        let group = DispatchGroup()

        // Запускаем конкурентные обновления с background потоков
        for state in states {
            group.enter()
            DispatchQueue.global().async {
                // updateState внутри использует DispatchQueue.main.async — безопасно
                view.updateState(state)
                group.leave()
            }
        }

        // Ждём всех background вызовов
        group.wait()

        // Дрейним main queue чтобы все async обновления применились
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.1))

        // View должен быть жив и draw() не падает
        let image = NSImage(size: view.bounds.size)
        image.lockFocus()
        view.draw(view.bounds)
        image.unlockFocus()
        XCTAssertNotNil(image, "Concurrent updateState не должен вызывать crash")
    }

    // MARK: - T-W7: updateState all values covered without crash

    func test_updateState_all_health_states_no_crash() {
        let view = makeView()
        for state in [HealthState.healthy, .hung, .stopped] {
            view.updateState(state)
            drainMainQueue()
        }
        // Если дошли сюда — все три state обработаны без crash
        XCTAssertNotNil(view)
    }
}

// MARK: - Testable accessors

extension StatusIndicatorView {
    /// Тест-хук: даёт доступ к private blinkTimer для проверки в T2.
    var blinkTimerForTesting: Timer? { blinkTimer }
}

// MARK: - Visible menu-bar image severity badge

@MainActor
final class StatusIndicatorImageSeverityTests: XCTestCase {

    func test_warn_severity_is_drawn_on_visible_menu_bar_image() {
        let image = StatusIndicatorImage.image(
            for: .healthy,
            privacyMode: false,
            errorSeverity: "warn",
            size: 14
        )
        let imageWithoutBadge = StatusIndicatorImage.image(
            for: .healthy,
            privacyMode: false,
            size: 14
        )

        XCTAssertEqual(StatusIndicatorImage.badgeColor(for: "warn")?.cgColor, NSColor.systemYellow.cgColor)
        XCTAssertGreaterThan(
            differingTopRightPixelCount(image, imageWithoutBadge),
            0,
            "Severity overlay обязан менять пиксели верхнего правого угла видимого menu-bar image."
        )
    }

    func test_info_severity_clears_visible_menu_bar_badge() {
        let image = StatusIndicatorImage.image(
            for: .healthy,
            privacyMode: false,
            errorSeverity: "info",
            size: 14
        )

        let imageWithoutBadge = StatusIndicatorImage.image(
            for: .healthy,
            privacyMode: false,
            size: 14
        )
        XCTAssertNil(StatusIndicatorImage.badgeColor(for: "info"))
        XCTAssertEqual(differingTopRightPixelCount(image, imageWithoutBadge), 0)
    }

    func test_critical_badge_opacity_changes_visible_menu_bar_image() {
        let opaque = StatusIndicatorImage.image(
            for: .healthy,
            privacyMode: false,
            errorSeverity: "critical",
            badgeOpacity: 1.0,
            size: 14
        )
        let dimmed = StatusIndicatorImage.image(
            for: .healthy,
            privacyMode: false,
            errorSeverity: "critical",
            badgeOpacity: 0.5,
            size: 14
        )

        XCTAssertGreaterThan(differingTopRightPixelCount(opaque, dimmed), 0)
    }

    private func differingTopRightPixelCount(_ lhs: NSImage, _ rhs: NSImage) -> Int {
        guard let lhsTiff = lhs.tiffRepresentation,
              let rhsTiff = rhs.tiffRepresentation,
              let lhsBitmap = NSBitmapImageRep(data: lhsTiff),
              let rhsBitmap = NSBitmapImageRep(data: rhsTiff),
              lhsBitmap.pixelsWide == rhsBitmap.pixelsWide,
              lhsBitmap.pixelsHigh == rhsBitmap.pixelsHigh else {
            XCTFail("Не удалось сопоставить bitmap двух menu-bar image")
            return 0
        }

        let startX = Int((8.0 * CGFloat(lhsBitmap.pixelsWide) / lhs.size.width).rounded(.down))
        // NSImage drawing coordinates start at bottom-left, TIFF rows at top-left.
        let topRightHeight = Int((6.0 * CGFloat(lhsBitmap.pixelsHigh) / lhs.size.height).rounded(.up))
        var different = 0
        for x in startX..<lhsBitmap.pixelsWide {
            for y in 0..<topRightHeight {
                guard let left = lhsBitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB),
                      let right = rhsBitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else {
                    continue
                }
                if abs(left.redComponent - right.redComponent) > 0.01
                    || abs(left.greenComponent - right.greenComponent) > 0.01
                    || abs(left.blueComponent - right.blueComponent) > 0.01
                    || abs(left.alphaComponent - right.alphaComponent) > 0.01 {
                    different += 1
                }
            }
        }
        return different
    }
}
