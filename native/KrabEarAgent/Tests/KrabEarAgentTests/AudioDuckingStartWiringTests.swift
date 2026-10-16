import AppKit
import XCTest
@testable import KrabEarAgent

private struct SilentDuckingNotifications: NotificationProcessRunning {
    func run(executableURL: URL, arguments: [String]) throws {}
}

private final class DuckingIPC: IPCSocketProviding, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [String] = []
    var onStart: (() -> Void)?
    var response: (([String: Any]) async throws -> [String: Any])?
    var methods: [String] { lock.withLock { calls } }
    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        let request = try XCTUnwrap(JSONSerialization.jsonObject(with: payload) as? [String: Any])
        let method = try XCTUnwrap(request["method"] as? String)
        lock.withLock { calls.append(method) }
        if method == "start_recording" { onStart?() }
        if let response { return try JSONSerialization.data(withJSONObject: ["ok": true, "result": try await response(request)]) }
        // Никаких живых сокетов; уже занятый recorder завершает start без overlay/paste.
        return try JSONSerialization.data(withJSONObject: ["ok": true, "result": ["status": "already_recording"]])
    }
}

private actor DuckingStateReplyGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var opened = false
    func wait() async {
        if opened { return }
        await withCheckedContinuation { continuation = $0 }
    }
    func release() { opened = true; continuation?.resume(); continuation = nil }
}

@MainActor
final class AudioDuckingStartWiringTests: XCTestCase {
    private var defaults: IsolatedUserDefaultsDomain!
    override func setUp() async throws {
        _ = NSApplication.shared
        defaults = IsolatedUserDefaultsDomain(scope: "AudioDuckingStartWiring")
    }
    override func tearDown() async throws { defaults.removePersistentDomain(); defaults = nil }

    private func delegate(service: SystemAudioDuckingService, provider: DuckingIPC = DuckingIPC()) -> AgentAppDelegate {
        let app = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]), userDefaults: defaults.defaults,
            audioDuckingService: service, notificationService: NotificationService(processRunner: SilentDuckingNotifications())
        )
        app.ipcClient = IPCClient(socketProvider: provider)
        app.settings.playStartSound = false
        app.settings.autoPaste = false
        return app
    }

    func testToggleDuringPreparationSendsNoStartIPCAndRestores() async {
        let entered = expectation(description: "mutating command entered")
        let release = DispatchSemaphore(value: 0)
        let runner = ControlledDuckingRunner { script in
            if script == "set volume output muted true" {
                entered.fulfill(); _ = release.wait(timeout: .now() + 2)
            }
            return .success(script.contains("get volume settings") ? (script.hasPrefix("output muted") ? "false" : "70") : "")
        }
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        let start = Task { await app.startRecording() }
        await fulfillment(of: [entered], timeout: 1)
        XCTAssertTrue(app.recordingStartInFlight)
        XCTAssertFalse(app.recordingStartRequestMayBeActive)
        await app.performRecordToggle(wasRecordingLocally: false)
        app.onMeetingPanelToggle()
        XCTAssertNil(app.meetingPanelController, "Подготовка аудио не является pending backend start")
        release.signal()
        await start.value
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertTrue(provider.methods.isEmpty)
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
        XCTAssertNil(app.dictationDuckingLease)
        XCTAssertFalse(app.recordingStartInFlight)
    }

    func testTaskCancellationBeforeIPCStillRestoresPossibleMutation() async {
        let entered = expectation(description: "mutating command entered")
        let release = DispatchSemaphore(value: 0)
        let runner = ControlledDuckingRunner { script in
            if script == "set volume output muted true" { entered.fulfill(); _ = release.wait(timeout: .now() + 2) }
            return .success(script.contains("get volume settings") ? (script.hasPrefix("output muted") ? "false" : "70") : "")
        }
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        let start = Task { await app.startRecording() }
        await fulfillment(of: [entered], timeout: 1)
        start.cancel(); release.signal(); await start.value
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertTrue(provider.methods.isEmpty)
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
    }

    func testMuteCompletesBeforeStartIPCAndDeclineRestores() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC()
        provider.onStart = { XCTAssertEqual(runner.scripts.last, "set volume output muted true") }
        let app = delegate(service: service, provider: provider)
        await app.startRecording()
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(provider.methods, ["start_recording"])
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
    }

    func testLateStartAttemptCannotReplaceActiveDictationAudioLease() async {
        let runner = ControlledDuckingRunner(), original = AudioDuckingLease()
        let service = SystemAudioDuckingService(runner: runner)
        _ = await service.prepare(lease: original, enabled: true, duckPercent: 100)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        app.dictationDuckingLease = original
        app.activeGenerationOwner = "dictation"
        app.activeGenerationToken = "G1"
        app.activeGenerationOwnerRevision = 1
        app.isRecording = true
        // Поздний idle state второго toggle может дойти до настоящего start уже после G1.
        await app.startRecording()
        XCTAssertEqual(app.dictationDuckingLease, original)
        XCTAssertTrue(provider.methods.isEmpty)
        app.releaseDictationDucking(expectedLease: original)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
    }

    func testVerifiedIdleReleasesOldLeaseAndNextToggleCanStart() async {
        let runner = ControlledDuckingRunner(), original = AudioDuckingLease()
        let service = SystemAudioDuckingService(runner: runner)
        _ = await service.prepare(lease: original, enabled: true, duckPercent: 100)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        provider.response = { request in
            request["method"] as? String == "get_recording_state"
                ? ["is_recording": false, "owner": NSNull()] : ["status": "already_recording"]
        }
        app.dictationDuckingLease = original
        app.activeGenerationOwner = "dictation"
        app.activeGenerationToken = "G1"
        app.activeGenerationOwnerRevision = 1
        app.isRecording = true
        await app.performRecordToggle(wasRecordingLocally: true)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertNil(app.dictationDuckingLease)
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
        await app.performRecordToggle(wasRecordingLocally: false)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(provider.methods, ["get_recording_state", "get_recording_state", "start_recording"])
    }

    func testDelayedIdleFromBeforeNewLeaseCannotReleaseThatLease() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        let entered = expectation(description: "state read started before G1")
        let gate = DuckingStateReplyGate()
        provider.response = { _ in
            entered.fulfill()
            await gate.wait()
            return ["is_recording": false, "owner": NSNull()]
        }
        let toggle = Task { await app.performRecordToggle(wasRecordingLocally: false) }
        await fulfillment(of: [entered], timeout: 1)
        let newLease = AudioDuckingLease()
        _ = await service.prepare(lease: newLease, enabled: true, duckPercent: 100)
        app.dictationDuckingLease = newLease
        app.activeGenerationOwner = "dictation"
        app.activeGenerationToken = "G1"
        app.activeGenerationOwnerRevision = 1
        app.isRecording = true
        await gate.release()
        await toggle.value
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(provider.methods, ["get_recording_state"])
        XCTAssertEqual(app.dictationDuckingLease, newLease)
        XCTAssertEqual(runner.scripts.count, 3)
        app.releaseDictationDucking(expectedLease: newLease)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
    }

    func testDelayedIdleCannotClearNonterminalStopRecovery() async {
        await checkDelayedIdleDuringStop(keepStopInFlight: false)
    }

    func testDelayedIdleCannotClearInFlightFinalization() async {
        await checkDelayedIdleDuringStop(keepStopInFlight: true)
    }

    private func checkDelayedIdleDuringStop(keepStopInFlight: Bool) async {
        let runner = ControlledDuckingRunner(), original = AudioDuckingLease()
        let service = SystemAudioDuckingService(runner: runner)
        _ = await service.prepare(lease: original, enabled: true, duckPercent: 100)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        app.dictationDuckingLease = original
        app.activeGenerationOwner = "dictation"
        app.activeGenerationToken = "G1"
        app.activeGenerationOwnerRevision = 1
        app.isRecording = true
        let stateEntered = expectation(description: "delayed state requested")
        let stopEntered = expectation(description: "stop requested")
        let stateGate = DuckingStateReplyGate(), stopGate = DuckingStateReplyGate()
        provider.response = { request in
            if request["method"] as? String == "stop_recording" {
                stopEntered.fulfill()
                if keepStopInFlight { await stopGate.wait() }
                throw IPCError.backendError("synthetic nonterminal refusal")
            }
            stateEntered.fulfill()
            await stateGate.wait()
            return ["is_recording": false, "owner": NSNull()]
        }
        let toggle = Task { await app.performRecordToggle(wasRecordingLocally: true) }
        await fulfillment(of: [stateEntered], timeout: 1)
        let stop = Task { await app.stopRecording() }
        await fulfillment(of: [stopEntered], timeout: 1)
        if !keepStopInFlight { await stop.value }
        await stateGate.release()
        await toggle.value
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(app.dictationDuckingLease, original)
        XCTAssertEqual(app.activeGenerationToken, "G1")
        XCTAssertEqual(app.activeGenerationOwner, "dictation")
        XCTAssertEqual(runner.scripts.count, 3)
        if keepStopInFlight { XCTAssertTrue(app.isProcessing) }
        else { XCTAssertTrue(app.recordingStopRecoveryPending) }
        await stopGate.release()
        await stop.value
        app.releaseDictationDucking(expectedLease: original)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
    }

    func testPhysicalIdleWithActiveGenerationIsNotTerminalIdle() async {
        let runner = ControlledDuckingRunner(), original = AudioDuckingLease()
        let service = SystemAudioDuckingService(runner: runner)
        _ = await service.prepare(lease: original, enabled: true, duckPercent: 100)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        provider.response = { _ in ["is_recording": false, "owner": "dictation", "generation_token": "G1"] }
        app.dictationDuckingLease = original
        app.activeGenerationOwner = "dictation"
        app.activeGenerationToken = "G1"
        app.activeGenerationOwnerRevision = 1
        app.isRecording = true
        await app.performRecordToggle(wasRecordingLocally: true)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(app.dictationDuckingLease, original)
        XCTAssertEqual(app.activeGenerationToken, "G1")
        XCTAssertEqual(runner.scripts.count, 3)
        app.releaseDictationDucking(expectedLease: original)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
    }

    func testFailedPreparationSendsNoStartIPC() async {
        let runner = ControlledDuckingRunner { _ in .failure(.commandFailed, cleanupConfirmed: true) }
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC(), app = delegate(service: service)
        app.ipcClient = IPCClient(socketProvider: provider)
        await app.startRecording()
        XCTAssertTrue(provider.methods.isEmpty)
        XCTAssertNil(app.recordingStartRequestID)
    }

    func testAmbiguousStartReconcilesSameRequestAsMeetingAndTransfersLease() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC()
        var requestID: String?
        provider.response = { request in
            if request["method"] as? String == "start_recording" {
                requestID = (request["params"] as? [String: Any])?["start_request_id"] as? String
                throw IPCError.timeout
            }
            return ["is_recording": true, "owner": "meeting", "generation_token": "G1",
                    "start_request_id": requestID ?? "missing", "owner_revision": 2]
        }
        let app = delegate(service: service, provider: provider)
        await app.startRecording()
        XCTAssertEqual(provider.methods, ["start_recording", "get_recording_state"])
        XCTAssertFalse(app.recordingStartAmbiguous)
        XCTAssertNil(app.dictationDuckingLease)
        XCTAssertTrue(app.promotedMeetingDucking?.confirmed == true)
        XCTAssertEqual(runner.scripts.count, 3)
        XCTAssertTrue(app.adoptPromotedMeetingGeneration(["promoted": true, "generation_token": "G1", "owner_revision": 2]))
        finish(app, token: "G1")
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(runner.scripts.count, 4)
        XCTAssertNil(app.promotedMeetingDucking)
    }

    func testEarlyMeetingFinishThenLostStartAndVerifiedIdleRestores() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC()
        let app = delegate(service: service, provider: provider)
        provider.response = { request in
            if request["method"] as? String == "start_recording" {
                await MainActor.run {
                    XCTAssertTrue(app.adoptPromotedMeetingGeneration([
                        "promoted": true, "generation_token": "G1", "owner_revision": 2
                    ]))
                    self.finish(app, token: "G1")
                    XCTAssertTrue(app.promotedMeetingDucking?.finished == true)
                }
                throw IPCError.timeout
            }
            return ["is_recording": false, "owner": NSNull()]
        }
        await app.startRecording()
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertNil(app.promotedMeetingDucking, "Подтверждённый idle разрешает provisional handoff этой попытки")
        XCTAssertNil(app.dictationDuckingLease)
        XCTAssertNil(app.pendingMeetingPromotionToken)
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
        let nextLease = AudioDuckingLease()
        let next = await service.prepare(lease: nextLease, enabled: true, duckPercent: 100)
        XCTAssertEqual(next, .ready, "Потерянный start не должен навсегда удерживать active snapshot")
        _ = await restoreDucking(service, lease: nextLease)
    }

    func testEarlyMeetingFinishPhysicalIdleStillRetainsLogicalGeneration() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC()
        let app = delegate(service: service, provider: provider)
        provider.response = { request in
            if request["method"] as? String == "start_recording" {
                await MainActor.run {
                    XCTAssertTrue(app.adoptPromotedMeetingGeneration([
                        "promoted": true, "generation_token": "G1", "owner_revision": 2
                    ]))
                    self.finish(app, token: "G1")
                }
                throw IPCError.timeout
            }
            return ["is_recording": false, "owner": "meeting", "generation_token": "G1"]
        }
        await app.startRecording()
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(app.promotedMeetingDucking?.generationToken, "G1")
        XCTAssertTrue(app.promotedMeetingDucking?.finished == true)
        XCTAssertEqual(runner.scripts.count, 3, "Capture idle не разрешает logical G1")
        app.inheritDictationDuckingForMeeting(token: "G1", confirmed: true)
        _ = await restoreDucking(service, lease: AudioDuckingLease())
    }

    func testAmbiguousStartVerifiedIdleRestoresWithoutSecondStart() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let provider = DuckingIPC()
        provider.response = { request in
            if request["method"] as? String == "start_recording" { throw IPCError.timeout }
            return ["is_recording": false, "owner": NSNull()]
        }
        let app = delegate(service: service, provider: provider)
        await app.startRecording()
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(provider.methods, ["start_recording", "get_recording_state"])
        XCTAssertFalse(app.recordingStartAmbiguous)
        XCTAssertNil(app.dictationDuckingLease)
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
    }

    private func finish(_ app: AgentAppDelegate, token: String) {
        let panel = app.ensureMeetingPanelController()
        panel.acceptGenerationToken(token)
        panel._testHandleSSELine("event: meeting.finished")
        panel._testHandleSSELine("data: {\"generation_token\":\"\(token)\"}")
    }

    func testEarlyMeetingFinishWaitsForMatchingStartConfirmation() async {
        let runner = ControlledDuckingRunner(), lease = AudioDuckingLease()
        let service = SystemAudioDuckingService(runner: runner)
        _ = await service.prepare(lease: lease, enabled: true, duckPercent: 100)
        let app = delegate(service: service)
        app.dictationDuckingLease = lease
        app.recordingStartInFlight = true
        app.recordingStartRequestMayBeActive = true
        XCTAssertTrue(app.adoptPromotedMeetingGeneration(["promoted": true, "generation_token": "G1", "owner_revision": 2]))
        finish(app, token: "G1")
        XCTAssertTrue(app.promotedMeetingDucking?.finished == true)
        XCTAssertFalse(app.promotedMeetingDucking?.confirmed == true)
        XCTAssertEqual(runner.scripts.count, 3)
        XCTAssertTrue(app.consumePendingMeetingPromotion(startToken: "G1"))
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertNil(app.promotedMeetingDucking)
        XCTAssertNil(app.dictationDuckingLease)
        XCTAssertEqual(runner.scripts.count, 4)
    }

    func testHandoffTransfersLeaseAndStaleFinishCannotRestoreNewMeeting() async {
        let runner = ControlledDuckingRunner(), first = AudioDuckingLease()
        let service = SystemAudioDuckingService(runner: runner)
        _ = await service.prepare(lease: first, enabled: true, duckPercent: 100)
        let app = delegate(service: service)
        app.dictationDuckingLease = first
        app.activeGenerationOwner = "dictation"
        app.activeGenerationToken = "G1"
        app.activeGenerationOwnerRevision = 1
        app.isRecording = true
        XCTAssertTrue(app.adoptPromotedMeetingGeneration(["promoted": true, "generation_token": "G1", "owner_revision": 2]))
        app.releaseDictationDucking(expectedLease: first)
        XCTAssertNil(app.dictationDuckingLease)
        XCTAssertEqual(runner.scripts.count, 3)
        finish(app, token: "G1")
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        let second = AudioDuckingLease()
        _ = await service.prepare(lease: second, enabled: true, duckPercent: 100)
        app.dictationDuckingLease = second
        app.inheritDictationDuckingForMeeting(token: "G2", confirmed: true)
        let count = runner.scripts.count
        app.releasePromotedMeetingDucking(expectedGenerationToken: "G1")
        _ = await restoreDucking(service, lease: AudioDuckingLease())
        XCTAssertEqual(runner.scripts.count, count)
        XCTAssertEqual(app.promotedMeetingDucking?.lease, second)
        app.releasePromotedMeetingDucking(expectedGenerationToken: "G2")
        _ = await restoreDucking(service, lease: AudioDuckingLease())
    }
}
