import XCTest

/// Source-контракт: setupCallObserver реально вызывается из старта агента
/// (класс бага setupErrorBus/setupHealthMonitor — определено, но не вызвано).
final class MainCallObserverWiringTests: XCTestCase {
    private func sourceText(_ file: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        return try String(contentsOf: root.appendingPathComponent(file), encoding: .utf8)
    }

    func test_setupCallObserver_is_actually_called_from_startup() throws {
        let main = try sourceText("main.swift")
        XCTAssertTrue(main.contains("setupCallObserver()"),
                      "setupCallObserver определён, но не вызван из main.swift — декоративная проводка")
    }

    func test_tearDownCallObserver_is_actually_called_from_shutdown() throws {
        let main = try sourceText("main.swift")
        XCTAssertTrue(main.contains("tearDownCallObserver()"))
    }

    /// T9 (доп.скоуп 2а): fail-until-swapped гард — до замены T8-заглушек
    /// (CallObserverHUDStub/CallObserverPanelStub) на реальные CallObserverHUD/
    /// CallObserverPanelController этот тест обязан быть КРАСНЫМ. Держит
    /// проводку честной: main+CallObserver.swift не смеет тихо откатиться на
    /// заглушки в будущей правке.
    func test_main_call_observer_wiring_uses_no_stubs() throws {
        let source = try sourceText("main+CallObserver.swift")
        XCTAssertFalse(source.contains("Stub()"),
                       "main+CallObserver.swift обязан использовать реальные " +
                       "CallObserverHUD()/CallObserverPanelController(), не заглушки T8")
    }

    /// MED-2 (w1 final): пункт «Звонок агента…» обязан гейтиться через
    /// NSMenuItemValidation — с autoenablesItems=true (default) ручной
    /// isEnabled без этого conformance декоративен, AppKit перезаписывает его
    /// при каждом открытии меню (см. refreshCallObserverMenuItem doc-comment).
    func test_validateMenuItem_gates_call_observer_menu_item() throws {
        let source = try sourceText("main+CallObserver.swift")
        XCTAssertTrue(source.contains("AgentAppDelegate: NSMenuItemValidation"),
                      "AgentAppDelegate обязан conform-иться к NSMenuItemValidation")
        XCTAssertTrue(source.contains("func validateMenuItem"),
                      "validateMenuItem обязан быть реализован — иначе гейт декоративен")
        XCTAssertTrue(source.contains("#selector(onOpenCallObserverPanel)") &&
                      source.contains("hasLiveCall"),
                      "validateMenuItem обязан гейтить именно onOpenCallObserverPanel по hasLiveCall")
    }
}
