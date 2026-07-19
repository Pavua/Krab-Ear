/*
 BrainLeaseMenuTests — B3 brain-lease видимость (спека
 2026-07-19-b3-brain-lease-visibility-design.md).

 Source-contract тесты (anti test-validates-the-hole, паттерн
 QuickCaptureWiringTests): пинят реальную проводку refreshBrainLeaseMenuItem
 в menuWillOpen и вызов get_brain_lease_status. Плюс юниты чистого
 форматтера brainLeaseMenuTitle(from:) на все состояния.
*/

import XCTest
@testable import KrabEarAgent

final class BrainLeaseMenuTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    // MARK: - Source-contract: проводка

    func test_brainLease_refresher_exists_and_calls_ipc() throws {
        let src = try source("main+BrainLease.swift")
        XCTAssertTrue(src.contains("func refreshBrainLeaseMenuItem"),
                      "refresher обязан существовать")
        // Греп по вызову метода (класс AST-подхода: сигнатура вызова, не подстрока
        // в комментарии) — реальный IPC-ключ, сверенный с dispatch table backend.
        XCTAssertTrue(src.contains("method: \"get_brain_lease_status\""),
                      "IPC-ключ обязан совпадать с backend dispatch буква-в-букву")
        // AGENT-3: IPC строго off-main.
        XCTAssertTrue(src.contains("DispatchQueue.global"),
                      "IPC обязан уходить off-main (AGENT-3)")
    }

    func test_menuWillOpen_actually_calls_brainLease_refresher() throws {
        // Класс «setupErrorBus определён, но не вызван»: пиним реальный вызов.
        let src = try source("main+MenuBarRecap.swift")
        XCTAssertTrue(src.contains("refreshBrainLeaseMenuItem()"),
                      "menuWillOpen обязан обновлять строку brain-lease")
    }

    func test_menu_item_created_in_rebuildStatusMenu() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("brainLeaseMenuItem"),
                      "пункт обязан создаваться при построении status-меню")
    }

    // MARK: - Юниты форматтера (чистая функция, 4 состояния + IPC-fail)

    func test_title_free() {
        let title = brainLeaseMenuTitle(from: [
            "ok": true, "enabled": true, "held": false,
        ])
        XCTAssertEqual(title, "LM Studio: свободен")
    }

    func test_title_held_by_ear() {
        let title = brainLeaseMenuTitle(from: [
            "ok": true, "enabled": true, "held": true,
            "owner": "krab_ear", "seconds_left": 21.4,
        ])
        XCTAssertEqual(title, "LM Studio: Krab Ear · ещё 21с")
    }

    func test_title_held_by_krab() {
        let title = brainLeaseMenuTitle(from: [
            "ok": true, "enabled": true, "held": true,
            "owner": "krab", "seconds_left": 5.9,
        ])
        XCTAssertEqual(title, "LM Studio: Краб · ещё 5с")
    }

    func test_title_disabled_returns_nil_hides_item() {
        // enabled=false → nil → пункт скрывается (меню не засоряем).
        XCTAssertNil(brainLeaseMenuTitle(from: [
            "ok": true, "enabled": false, "held": false,
        ]))
    }

    func test_title_ipc_failure_placeholder() {
        // nil-ответ IPC → плейсхолдер, НЕ скрытие (скрытие маскировало бы
        // умерший backend — рядом уже есть status-dot для этого).
        XCTAssertEqual(brainLeaseMenuTitle(from: nil), "LM Studio: —")
    }

    func test_title_unknown_owner_passthrough() {
        // forward-compat: новый владелец отображается как есть.
        let title = brainLeaseMenuTitle(from: [
            "ok": true, "enabled": true, "held": true,
            "owner": "voice_gateway", "seconds_left": 10.0,
        ])
        XCTAssertEqual(title, "LM Studio: voice_gateway · ещё 10с")
    }

    func test_title_held_without_seconds_left() {
        // Малформный ответ (seconds_left null при held=true) — без хвоста «ещё Nс».
        let title = brainLeaseMenuTitle(from: [
            "ok": true, "enabled": true, "held": true, "owner": "krab",
        ])
        XCTAssertEqual(title, "LM Studio: Краб")
    }
}
