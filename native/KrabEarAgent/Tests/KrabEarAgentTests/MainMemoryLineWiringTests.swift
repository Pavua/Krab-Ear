/*
 MainMemoryLineWiringTests — T8 (волна Memory Conductor), строка «Память: …»
 в status-меню (main+MemoryLine.swift).

 Source-contract тесты (anti test-validates-the-hole, паттерн
 BrainLeaseMenuTests / QuickCaptureWiringTests): пинят реальную проводку
 refreshMemoryLineMenuItem в menuWillOpen, IPC-ключ get_memory_ledger
 буква-в-букву и создание пункта меню. Плюс юниты чистого форматтера
 memoryLineMenuTitle(from:enabled:now:) на все состояния.
*/

import XCTest
@testable import KrabEarAgent

final class MainMemoryLineWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    // MARK: - Source-contract: проводка

    func test_memoryLine_refresher_exists_and_calls_ipc() throws {
        let src = try source("main+MemoryLine.swift")
        XCTAssertTrue(src.contains("func refreshMemoryLineMenuItem"),
                      "refresher обязан существовать")
        // IPC-ключ, сверенный с backend dispatch table буква-в-букву (урок
        // «гейт agy IPC-ключей»).
        XCTAssertTrue(src.contains("method: \"get_memory_ledger\""),
                      "IPC-ключ обязан совпадать с backend dispatch буква-в-букву")
        XCTAssertTrue(src.contains("method: \"get_settings\""),
                      "видимость строки обязана сверяться с get_settings")
        // AGENT-3: IPC строго off-main.
        XCTAssertTrue(src.contains("DispatchQueue.global"),
                      "IPC обязан уходить off-main (AGENT-3)")
    }

    func test_menuWillOpen_actually_calls_memoryLine_refresher() throws {
        // Класс «setupErrorBus определён, но не вызван»: пиним реальный вызов.
        let src = try source("main+MenuBarRecap.swift")
        XCTAssertTrue(src.contains("refreshMemoryLineMenuItem()"),
                      "menuWillOpen обязан обновлять строку «Память»")
    }

    func test_menu_item_created_in_rebuildStatusMenu() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("memoryLineMenuItem"),
                      "пункт обязан создаваться при построении status-меню")
    }

    func test_memoryLine_item_placed_after_brainLease_item() throws {
        // ТЗ: «Твоя строка ставится СРАЗУ ПОСЛЕ brain-lease строки».
        let src = try source("main+StatusMenu.swift")
        guard let brainRange = src.range(of: "self.brainLeaseMenuItem = brainItem"),
              let memoryRange = src.range(of: "self.memoryLineMenuItem = memoryItem")
        else {
            XCTFail("оба пункта меню обязаны создаваться в rebuildStatusMenu")
            return
        }
        XCTAssertTrue(brainRange.upperBound < memoryRange.lowerBound,
                      "строка «Память» обязана идти ПОСЛЕ brain-lease строки")
    }

    // MARK: - Юниты форматтера

    private let refTs: Double = 1_800_000_000   // фиксированная точка отсчёта

    private var refDate: Date { Date(timeIntervalSince1970: refTs) }

    func test_title_disabled_returns_nil_hides_item() {
        // memory_conductor_enabled=false → nil → пункт скрывается, независимо
        // от содержимого ledger.
        XCTAssertNil(memoryLineMenuTitle(
            from: ["ledger": ["v": 1, "entries": ["krab_ear/brain": ["state": "active", "size_mb": 19456.0]]]],
            enabled: false, now: refDate))
    }

    func test_title_ipc_failure_placeholder() {
        // enabled=true, но result nil (провал get_memory_ledger) → плейсхолдер,
        // НЕ скрытие (тот же класс, что brainLeaseMenuTitle).
        XCTAssertEqual(memoryLineMenuTitle(from: nil, enabled: true, now: refDate), "Память: —")
    }

    func test_title_empty_entries_placeholder() {
        XCTAssertEqual(
            memoryLineMenuTitle(
                from: ["ledger": ["v": 1, "entries": [String: Any]()]],
                enabled: true, now: refDate),
            "Память: —")
    }

    func test_title_basic_render_active_and_idle() {
        // Каноничный пример из ТЗ: «Память: brain 19Г · whisper idle 4м».
        let idleSince = refTs - 4 * 60   // 4 минуты назад
        let result: [String: Any] = [
            "ledger": [
                "v": 1,
                "entries": [
                    "krab_ear/brain": ["owner": "krab_ear", "resident": "brain",
                                        "state": "active", "size_mb": 19456.0],
                    "krab_ear/whisper": ["owner": "krab_ear", "resident": "whisper",
                                          "state": "idle", "size_mb": 3072.0,
                                          "idle_since_ts": idleSince],
                ],
            ],
            "conductor": ["thread_alive": true],
        ]
        XCTAssertEqual(
            memoryLineMenuTitle(from: result, enabled: true, now: refDate),
            "Память: brain 19Г · whisper idle 4м")
    }

    func test_title_warm_state_shows_size_no_suffix() {
        let result: [String: Any] = [
            "ledger": ["v": 1, "entries": ["krab/rewriter": ["state": "warm", "size_mb": 8192.0]]],
        ]
        XCTAssertEqual(
            memoryLineMenuTitle(from: result, enabled: true, now: refDate),
            "Память: rewriter 8Г")
    }

    func test_title_idle_without_idle_since_ts_omits_minutes() {
        // Малформный ответ (idle без idle_since_ts) — без хвоста «idle Nм»,
        // просто «idle» без цифры (fail-safe: не выдумывать число).
        let result: [String: Any] = [
            "ledger": ["v": 1, "entries": ["krab_ear/whisper": ["state": "idle", "size_mb": 3072.0]]],
        ]
        XCTAssertEqual(
            memoryLineMenuTitle(from: result, enabled: true, now: refDate),
            "Память: whisper idle")
    }

    func test_title_shadow_suffix_after_7_days() {
        let shadowSince = refTs - 8 * 86400   // 8 дней назад
        let result: [String: Any] = [
            "ledger": ["v": 1, "entries": ["krab_ear/brain": ["state": "active", "size_mb": 19456.0]]],
            "conductor": ["shadow_since": shadowSince],
        ]
        XCTAssertEqual(
            memoryLineMenuTitle(from: result, enabled: true, now: refDate),
            "Память: brain 19Г · shadow 8 дн")
    }

    func test_title_shadow_suffix_absent_before_7_days() {
        let shadowSince = refTs - 2 * 86400   // 2 дня назад — суффикса быть не должно
        let result: [String: Any] = [
            "ledger": ["v": 1, "entries": ["krab_ear/brain": ["state": "active", "size_mb": 19456.0]]],
            "conductor": ["shadow_since": shadowSince],
        ]
        XCTAssertEqual(
            memoryLineMenuTitle(from: result, enabled: true, now: refDate),
            "Память: brain 19Г")
    }

    func test_title_names_key_after_slash() {
        // Ключ entries — "<owner>/<resident>"; имя пункта = часть после «/».
        let result: [String: Any] = [
            "ledger": ["v": 1, "entries": ["voice_gateway/tts_kokoro": ["state": "active", "size_mb": 1024.0]]],
        ]
        XCTAssertEqual(
            memoryLineMenuTitle(from: result, enabled: true, now: refDate),
            "Память: tts_kokoro 1Г")
    }

    // MARK: три состояния brain (условие enforce-волны)

    func test_unloaded_state_says_so_instead_of_zero_gigabytes() throws {
        let result: [String: Any] = [
            "ledger": ["entries": ["krab_ear/brain": [
                "state": "unloaded", "size_mb": 0, "updated_ts": 1.0,
            ]]],
            "conductor": [:],
        ]
        let title = memoryLineMenuTitle(from: result, enabled: true, now: refDate)
        XCTAssertNotNil(title)
        XCTAssertTrue(title!.contains("brain выгружен"), "получено: \(title!)")
        XCTAssertFalse(title!.contains("0Г"), "выгруженная модель не должна выглядеть как «0Г»")
    }

    func test_unknown_size_renders_dash_not_zero() throws {
        let result: [String: Any] = [
            "ledger": ["entries": ["krab_ear/brain": [
                "state": "unknown", "updated_ts": 1.0,
            ]]],
            "conductor": [:],
        ]
        let title = memoryLineMenuTitle(from: result, enabled: true, now: refDate)
        XCTAssertNotNil(title)
        XCTAssertTrue(title!.contains("brain —"), "получено: \(title!)")
        XCTAssertFalse(title!.contains("0Г"))
    }
}
