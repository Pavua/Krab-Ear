/*
 StatusBarTooltipTests.swift
 Tests for menu bar status icon dynamic tooltip.

 Strategy:
   T1-T9  — pure helper `humanReadableSTTEngineShared` (no IPC, no AppKit)
   T10-T14 — `buildStatusBarTooltip` using IPCSocketProviding mock injected into
              AgentAppDelegate.ipcClient (same pattern as IPCClientReconnectE2ETests)

 The mock provider returns pre-baked JSON bytes for each IPC method.
 AgentAppDelegate.ipcClient is a `var`, so we replace it with an IPCClient(socketProvider:)
 test initialiser to avoid real socket I/O.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

// MARK: - T1-T9: Pure helper — humanReadableSTTEngineShared

final class HumanReadableSTTEngineTests: XCTestCase {

    func test_gigaam_rnnt() {
        XCTAssertEqual(humanReadableSTTEngineShared("gigaam-rnnt"), "GigaAM (RNNT)")
    }

    func test_gigaam_ctc() {
        XCTAssertEqual(humanReadableSTTEngineShared("gigaam-ctc"), "GigaAM (CTC)")
    }

    func test_whisper_large_v3_mlx() {
        XCTAssertEqual(humanReadableSTTEngineShared("whisper-large-v3-mlx"), "Whisper Large v3 (MLX)")
    }

    func test_whisper_turbo() {
        // contains("turbo") match
        XCTAssertEqual(humanReadableSTTEngineShared("openai-whisper-turbo"), "Whisper Turbo (MLX)")
    }

    func test_whisper_plain() {
        XCTAssertEqual(humanReadableSTTEngineShared("openai-whisper-base"), "Whisper (MLX)")
    }

    func test_remote() {
        XCTAssertEqual(humanReadableSTTEngineShared("remote"), "Whisper Remote")
    }

    func test_vad_skip() {
        XCTAssertEqual(humanReadableSTTEngineShared("vad_skip"), "VAD пропуск (тишина)")
    }

    func test_nil_returns_dash() {
        XCTAssertEqual(humanReadableSTTEngineShared(nil), "—")
    }

    func test_empty_returns_dash() {
        XCTAssertEqual(humanReadableSTTEngineShared(""), "—")
    }

    func test_unknown_raw_returned_as_is() {
        XCTAssertEqual(humanReadableSTTEngineShared("my-custom-engine-v2"), "my-custom-engine-v2")
    }
}

// MARK: - Mock socket provider

/// Returns pre-baked JSON responses keyed by IPC method name.
/// Implements IPCSocketProviding.send — parses the method from the JSON payload
/// and returns the matching response bytes.
final class TooltipMockSocketProvider: IPCSocketProviding, @unchecked Sendable {

    /// Map from IPC method name → full JSON response string (without trailing newline).
    var responses: [String: String]

    /// If true, throw connectionFailed for every call.
    var alwaysFail: Bool

    init(responses: [String: String] = [:], alwaysFail: Bool = false) {
        self.responses = responses
        self.alwaysFail = alwaysFail
    }

    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        if alwaysFail {
            throw IPCError.socketConnectFailed("mock: backend offline")
        }
        // Parse the method from the JSON payload.
        let method = Self.extractMethod(from: payload)
        let responseJSON: String
        if let method, let r = responses[method] {
            responseJSON = r
        } else {
            responseJSON = #"{"id":"x","ok":false,"error":"method_not_found"}"#
        }
        return Data((responseJSON + "\n").utf8)
    }

    private static func extractMethod(from payload: Data) -> String? {
        guard
            let dict = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
            let method = dict["method"] as? String
        else { return nil }
        return method
    }
}

// MARK: - T10-T14: buildStatusBarTooltip integration tests

@MainActor
final class StatusBarTooltipTests: XCTestCase {

    /// Create a minimal AgentAppDelegate with a mock IPC provider injected.
    private func makeDelegate(
        diagResponse: String? = nil,
        errorsResponse: String? = nil,
        alwaysFail: Bool = false
    ) -> AgentAppDelegate {
        var responses: [String: String] = [:]
        if let d = diagResponse { responses["get_diagnostics"] = d }
        if let e = errorsResponse { responses["list_recent_errors"] = e }

        let provider = TooltipMockSocketProvider(responses: responses, alwaysFail: alwaysFail)
        let delegate = AgentAppDelegate(options: LaunchOptions(arguments: [CommandLine.arguments[0]]))
        delegate.ipcClient = IPCClient(socketProvider: provider)
        return delegate
    }

    // MARK: - T10: IPC fails → single-line "Krab Ear" (graceful degradation)

    func test_buildTooltip_offlineFallback_singleLine() async {
        let delegate = makeDelegate(alwaysFail: true)
        let tip = await delegate.buildStatusBarTooltip()
        // On IPC failure, both callAsync calls throw and are silently swallowed.
        // Result: only the header line.
        XCTAssertEqual(tip, "Krab Ear",
            "При ошибке IPC tooltip должен содержать только заголовок 'Krab Ear'")
    }

    // MARK: - T11: STT engine present → "STT: GigaAM (RNNT)"

    func test_buildTooltip_includesSTTEngine() async {
        let diagJSON = #"{"id":"1","ok":true,"result":{"stt":{"last_engine":"gigaam-rnnt"}}}"#
        let delegate = makeDelegate(diagResponse: diagJSON)
        let tip = await delegate.buildStatusBarTooltip()

        XCTAssertTrue(tip.contains("STT:"), "Tooltip должен содержать строку STT:")
        XCTAssertTrue(tip.contains("GigaAM"), "Tooltip должен содержать GigaAM")
        XCTAssertTrue(tip.contains("RNNT"), "Tooltip должен содержать RNNT")
    }

    // MARK: - T12: Recent error present → "Last error: rewriter.timeout"

    func test_buildTooltip_includesLastError() async {
        let diagJSON = #"{"id":"1","ok":true,"result":{"stt":{"last_engine":"gigaam-rnnt"}}}"#
        let errJSON = #"{"id":"2","ok":true,"result":{"errors":[{"code":"rewriter.timeout"}]}}"#
        let delegate = makeDelegate(diagResponse: diagJSON, errorsResponse: errJSON)
        let tip = await delegate.buildStatusBarTooltip()

        XCTAssertTrue(tip.contains("Last error:"), "Tooltip должен содержать 'Last error:'")
        XCTAssertTrue(tip.contains("rewriter.timeout"), "Tooltip должен содержать код ошибки")
    }

    // MARK: - T13: No recent errors → no "Last error" line

    func test_buildTooltip_noErrorLine_whenErrorsEmpty() async {
        let diagJSON = #"{"id":"1","ok":true,"result":{"stt":{"last_engine":"gigaam-ctc"}}}"#
        let errJSON = #"{"id":"2","ok":true,"result":{"errors":[]}}"#
        let delegate = makeDelegate(diagResponse: diagJSON, errorsResponse: errJSON)
        let tip = await delegate.buildStatusBarTooltip()

        XCTAssertFalse(tip.contains("Last error"),
            "При пустом списке ошибок строки 'Last error' быть не должно")
    }

    // MARK: - T14: Full 3-line format

    func test_buildTooltip_fullThreeLineFormat() async {
        let diagJSON = #"{"id":"1","ok":true,"result":{"stt":{"last_engine":"gigaam-rnnt"}}}"#
        let errJSON = #"{"id":"2","ok":true,"result":{"errors":[{"code":"backend.crash"}]}}"#
        let delegate = makeDelegate(diagResponse: diagJSON, errorsResponse: errJSON)
        let tip = await delegate.buildStatusBarTooltip()

        let lines = tip.components(separatedBy: "\n")
        XCTAssertEqual(lines.count, 3,
            "Full tooltip должен состоять из 3 строк: заголовок + STT + ошибка")
        XCTAssertEqual(lines[0], "Krab Ear", "Первая строка — 'Krab Ear'")
        XCTAssertTrue(lines[1].hasPrefix("STT:"), "Вторая строка начинается с 'STT:'")
        XCTAssertTrue(lines[2].hasPrefix("Last error:"), "Третья строка начинается с 'Last error:'")
    }

    // MARK: - T15: No stt key in diagnostics → no STT line

    func test_buildTooltip_noSTTKey_noSTTLine() async {
        // Diagnostics exists but has no stt section
        let diagJSON = #"{"id":"1","ok":true,"result":{"system":{"cpu":10}}}"#
        let delegate = makeDelegate(diagResponse: diagJSON)
        let tip = await delegate.buildStatusBarTooltip()

        XCTAssertFalse(tip.contains("STT:"),
            "При отсутствии stt в диагностике строки 'STT:' быть не должно")
        XCTAssertEqual(tip, "Krab Ear",
            "Без STT и без ошибок tooltip — только заголовок")
    }
}

// MARK: - Last-error menu line + no pointer-tracking toolTip
//
// Bug (2026-11-02): NSStatusBarButton.toolTip is reassigned every 10s from
// list_recent_errors. On macOS 26 / Tahoe the native help window follows the
// pointer, and StatusBarDropView can swallow mouseExited, so
// "Last error: system.unclean_restart" sticks to the caret after a real
// unclean restart. The error itself is honest (ErrorBus ring); the surface
// must not be button.toolTip.

final class LastErrorMenuTitleTests: XCTestCase {

    func test_title_nil_when_code_missing() {
        XCTAssertNil(lastErrorMenuTitle(code: nil))
        XCTAssertNil(lastErrorMenuTitle(code: ""))
    }

    func test_title_includes_code() {
        XCTAssertEqual(
            lastErrorMenuTitle(code: "system.unclean_restart"),
            "Last error: system.unclean_restart"
        )
    }

    func test_code_from_empty_list_is_nil() {
        XCTAssertNil(lastErrorCode(from: nil))
        XCTAssertNil(lastErrorCode(from: []))
        XCTAssertNil(lastErrorCode(from: [["severity": "error"]]))
        XCTAssertNil(lastErrorCode(from: [["code": "   "]]))
    }

    func test_code_takes_newest_when_oldest_first() {
        let errors: [[String: Any]] = [
            ["code": "older.timeout"],
            ["code": "system.unclean_restart"],
        ]
        XCTAssertEqual(lastErrorCode(from: errors), "system.unclean_restart")
    }
}

final class StatusBarTooltipWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_startTooltipRefresh_does_not_loop_assign_button_toolTip() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("func startTooltipRefresh()"),
                      "startup hook обязан существовать")
        XCTAssertFalse(src.contains("10_000_000_000"),
                       "10s sleep больше не должен переназначать native toolTip")
        XCTAssertFalse(src.contains("button?.toolTip = tip"),
                       "startTooltipRefresh не должен писать dynamic tip на кнопку")
        XCTAssertTrue(src.contains("statusItem?.button?.toolTip = nil"),
                      "startup hook обязан сбрасывать native toolTip")
    }

    func test_ensureStatusItem_does_not_install_krab_ear_toolTip() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertFalse(src.contains("item.button?.toolTip = \"Krab Ear\""),
                       "статический toolTip на NSStatusBarButton тоже следует за курсором")
        XCTAssertTrue(src.contains("item.button?.toolTip = nil"),
                      "ensureStatusItem обязан явно обнулять toolTip")
    }

    func test_lastError_refresher_exists_and_calls_ipc() throws {
        let src = try source("main+LastErrorLine.swift")
        XCTAssertTrue(src.contains("func refreshLastErrorMenuItem"),
                      "refresher обязан существовать")
        XCTAssertTrue(src.contains("method: \"list_recent_errors\""),
                      "IPC-ключ обязан совпадать с backend dispatch буква-в-букву")
        XCTAssertTrue(src.contains("DispatchQueue.global"),
                      "IPC обязан уходить off-main (AGENT-3)")
    }

    func test_menuWillOpen_actually_calls_lastError_refresher() throws {
        let src = try source("main+MenuBarRecap.swift")
        XCTAssertTrue(src.contains("refreshLastErrorMenuItem()"),
                      "menuWillOpen обязан обновлять строку Last error")
    }

    func test_menu_item_created_in_rebuildStatusMenu() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("lastErrorMenuItem"),
                      "пункт обязан создаваться при построении status-меню")
        XCTAssertTrue(src.contains("refreshLastErrorMenuItem()"),
                      "rebuildStatusMenu обязан сделать первичный fetch Last error")
    }

    func test_dropView_clears_inherited_toolTip() throws {
        let src = try source("main+StatusDragDrop.swift")
        XCTAssertTrue(src.contains("toolTip = nil"),
                      "StatusBarDropView не должен наследовать pointer-tracking toolTip")
    }
}
