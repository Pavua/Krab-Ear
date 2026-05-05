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
