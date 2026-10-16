import Foundation
import XCTest
@testable import KrabEarAgent

final class ControlledDuckingRunner: AudioDuckingCommandRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var commands: [String] = []
    var operation: (String) -> AudioDuckingCommandResult

    init(operation: @escaping (String) -> AudioDuckingCommandResult = { script in
        .success(script.contains("get volume settings") ? (script.hasPrefix("output muted") ? "false" : "70") : "")
    }) { self.operation = operation }

    func run(script: String, deadline: ContinuousClock.Instant) -> AudioDuckingCommandResult {
        lock.withLock { commands.append(script) }
        return operation(script)
    }
    var scripts: [String] { lock.withLock { commands } }
}

func restoreDucking(_ service: SystemAudioDuckingService, lease: AudioDuckingLease) async -> DuckingRestoreResult {
    await withCheckedContinuation { continuation in
        service.requestRestore(lease: lease) { continuation.resume(returning: $0) }
    }
}

/// Настоящий сервис; команды не обращаются к системному звуку.
final class AudioDuckingLifecycleWiringTests: XCTestCase {
    func testFailedRestoreCanRetryOriginalSnapshot() async {
        var writes = 0
        let runner = ControlledDuckingRunner { script in
            if script.contains("get volume settings") {
                return .success(script.hasPrefix("output muted") ? "false" : "70")
            }
            writes += 1
            return writes == 2 ? .failure(.commandFailed, cleanupConfirmed: true) : .success("")
        }
        let service = SystemAudioDuckingService(runner: runner)
        let lease = AudioDuckingLease()
        let prepared = await service.prepare(lease: lease, enabled: true, duckPercent: 100)
        XCTAssertEqual(prepared, .ready)
        let first = await restoreDucking(service, lease: lease)
        let second = await restoreDucking(service, lease: lease)
        XCTAssertEqual(first, .retained(.commandFailed))
        XCTAssertEqual(second, .restored)
        XCTAssertEqual(runner.scripts.filter { $0.hasPrefix("set") }.count, 3)
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
    }

    func testPossiblyPartialMutationStillRestoresOriginalSnapshot() async {
        var writes = 0
        let runner = ControlledDuckingRunner { script in
            if script.contains("get volume settings") {
                return .success(script.hasPrefix("output muted") ? "false" : "70")
            }
            writes += 1
            return writes == 1 ? .failure(.commandFailed, cleanupConfirmed: true) : .success("")
        }
        let service = SystemAudioDuckingService(runner: runner)
        let lease = AudioDuckingLease()
        let result = await service.prepare(lease: lease, enabled: true, duckPercent: 15)
        XCTAssertEqual(result, .failed(.commandFailed))
        let restored = await restoreDucking(service, lease: lease)
        XCTAssertEqual(restored, .restored)
        XCTAssertEqual(runner.scripts.filter { $0.hasPrefix("set") }.count, 2)
        XCTAssertEqual(runner.scripts.last, "set volume output volume 70\nset volume output muted false")
    }

    @MainActor
    func testSlowCommandDoesNotBlockMainActorHeartbeat() async {
        let runner = ControlledDuckingRunner { script in
            if script.contains("get volume settings") {
                return .success(script.hasPrefix("output muted") ? "false" : "70")
            }
            Thread.sleep(forTimeInterval: 0.2)
            return .success("")
        }
        let service = SystemAudioDuckingService(runner: runner)
        let lease = AudioDuckingLease()
        let started = ContinuousClock.now
        let heartbeat = Task { @MainActor in ContinuousClock.now - started }
        let prepared = await service.prepare(lease: lease, enabled: true, duckPercent: 100)
        let latency = await heartbeat.value
        XCTAssertEqual(prepared, .ready)
        XCTAssertLessThan(latency, .milliseconds(100))
        _ = await restoreDucking(service, lease: lease)
    }

    func testActiveLeaseCannotBeReplacedAndStaleRestoreCannotTouchNextLease() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let first = AudioDuckingLease(), second = AudioDuckingLease()
        let initial = await service.prepare(lease: first, enabled: true, duckPercent: 100)
        XCTAssertEqual(initial, .ready)
        let replacement = await service.prepare(lease: second, enabled: true, duckPercent: 100)
        XCTAssertEqual(replacement, .failed(.busy))
        let count = runner.scripts.count
        let stale = await restoreDucking(service, lease: second)
        XCTAssertEqual(stale, .absentOrStale)
        XCTAssertEqual(runner.scripts.count, count)
        // Постановка restore синхронна: следующий prepare не вправе его обогнать.
        service.requestRestore(lease: first)
        let next = await service.prepare(lease: second, enabled: true, duckPercent: 100)
        XCTAssertEqual(next, .ready)
        let nextCount = runner.scripts.count
        let old = await restoreDucking(service, lease: first)
        XCTAssertEqual(old, .absentOrStale)
        XCTAssertEqual(runner.scripts.count, nextCount)
        _ = await restoreDucking(service, lease: second)
    }

    func testDisabledAndAlreadyMutedNeverWrite() async {
        let runner = ControlledDuckingRunner { _ in .success("true") }
        let service = SystemAudioDuckingService(runner: runner)
        let disabled = await service.prepare(lease: AudioDuckingLease(), enabled: false, duckPercent: 100)
        XCTAssertEqual(disabled, .bypassed)
        XCTAssertTrue(runner.scripts.isEmpty)
        let lease = AudioDuckingLease()
        let muted = await service.prepare(lease: lease, enabled: true, duckPercent: 100)
        XCTAssertEqual(muted, .bypassed)
        _ = await restoreDucking(service, lease: lease)
        XCTAssertEqual(runner.scripts, ["output muted of (get volume settings)"])
    }

    func testUnconfirmedCleanupQuarantinesEveryFollowingCommand() async {
        let runner = ControlledDuckingRunner { _ in .failure(.cleanupUnconfirmed, cleanupConfirmed: false) }
        let service = SystemAudioDuckingService(runner: runner)
        let first = AudioDuckingLease()
        let result = await service.prepare(lease: first, enabled: true, duckPercent: 100)
        XCTAssertEqual(result, .failed(.cleanupUnconfirmed))
        let second = await service.prepare(lease: AudioDuckingLease(), enabled: true, duckPercent: 100)
        XCTAssertEqual(second, .failed(.cleanupUnconfirmed))
        _ = await restoreDucking(service, lease: first)
        XCTAssertEqual(runner.scripts.count, 1)
    }

    @MainActor
    func testCancellationBeforePreparationRunsNoCommand() async {
        let runner = ControlledDuckingRunner()
        let service = SystemAudioDuckingService(runner: runner)
        let task = Task { await service.prepare(lease: AudioDuckingLease(), enabled: true, duckPercent: 100) }
        task.cancel()
        let result = await task.value
        XCTAssertEqual(result, .failed(.cancelled))
        XCTAssertTrue(runner.scripts.isEmpty)
    }

    func testNewPrepareRetriesPendingRestoreBeforeTakingNewSnapshot() async {
        var writes = 0
        let runner = ControlledDuckingRunner { script in
            if script.contains("get volume settings") {
                return .success(script.hasPrefix("output muted") ? "false" : "70")
            }
            writes += 1
            return writes == 2 ? .failure(.commandFailed, cleanupConfirmed: true) : .success("")
        }
        let service = SystemAudioDuckingService(runner: runner)
        let first = AudioDuckingLease(), second = AudioDuckingLease()
        _ = await service.prepare(lease: first, enabled: true, duckPercent: 100)
        _ = await restoreDucking(service, lease: first)
        let result = await service.prepare(lease: second, enabled: true, duckPercent: 100)
        XCTAssertEqual(result, .ready)
        XCTAssertEqual(Array(runner.scripts[3...]), [
            "set volume output volume 70\nset volume output muted false",
            "set volume output volume 70\nset volume output muted false",
            "output muted of (get volume settings)", "output volume of (get volume settings)",
            "set volume output muted true"
        ])
        _ = await restoreDucking(service, lease: second)
    }

    func testReadFailureDoesNotMutate() async {
        let runner = ControlledDuckingRunner { _ in .success("invalid") }
        let service = SystemAudioDuckingService(runner: runner)
        let result = await service.prepare(lease: AudioDuckingLease(), enabled: true, duckPercent: 100)
        XCTAssertEqual(result, .failed(.invalidSnapshot))
        XCTAssertEqual(runner.scripts.count, 1)
    }

    func testExpiredQueuedPreparationNeverLaunchesCommands() async {
        let entered = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        let runner = ControlledDuckingRunner { script in
            if script.hasPrefix("output muted") { entered.signal(); _ = release.wait(timeout: .now() + 2) }
            return .success(script.hasPrefix("output muted") ? "false" : "70")
        }
        let service = SystemAudioDuckingService(runner: runner, operationBudget: .milliseconds(80))
        let first = Task { await service.prepare(lease: AudioDuckingLease(), enabled: true, duckPercent: 100) }
        XCTAssertEqual(entered.wait(timeout: .now() + 1), .success)
        let second = Task { await service.prepare(lease: AudioDuckingLease(), enabled: true, duckPercent: 100) }
        try? await Task.sleep(for: .milliseconds(130))
        release.signal()
        _ = await first.value
        let queued = await second.value
        XCTAssertEqual(queued, .failed(.deadlineExceeded))
        XCTAssertEqual(runner.scripts.count, 1)
    }
}

final class AudioDuckingChildWiringTests: XCTestCase {
    func testOwnedChildDeadlineReapsAndDoesNotKillNextChild() {
        let runner = SystemAudioDuckingCommandRunner(executable: "/bin/sleep", arguments: { _ in ["2"] })
        let start = ContinuousClock.now
        let result = runner.run(script: "", deadline: .now + .milliseconds(60))
        XCTAssertEqual(result, .failure(.deadlineExceeded, cleanupConfirmed: true))
        XCTAssertLessThan(ContinuousClock.now - start, .seconds(1))
        let next = SystemAudioDuckingCommandRunner(executable: "/usr/bin/printf", arguments: { _ in ["harmless"] })
        XCTAssertEqual(next.run(script: "", deadline: .now + .seconds(1)), .success("harmless"))
        Thread.sleep(forTimeInterval: 0.1)
        XCTAssertEqual(next.run(script: "", deadline: .now + .seconds(1)), .success("harmless"))
    }

    func testOutputCapIsBoundedAndChildReaped() {
        let runner = SystemAudioDuckingCommandRunner(executable: "/usr/bin/yes", arguments: { _ in ["synthetic"] }, outputLimit: 128)
        XCTAssertEqual(runner.run(script: "", deadline: .now + .seconds(1)), .failure(.outputLimit, cleanupConfirmed: true))
    }

    func testExpiredDeadlineDoesNotSpawn() {
        let runner = SystemAudioDuckingCommandRunner(executable: "/not/a/real/executable", arguments: { _ in [] })
        XCTAssertEqual(runner.run(script: "", deadline: .now - .milliseconds(1)), .failure(.deadlineExceeded, cleanupConfirmed: true))
    }
}
