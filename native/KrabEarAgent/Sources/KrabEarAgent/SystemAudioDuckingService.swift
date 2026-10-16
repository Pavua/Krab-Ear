import Foundation

struct AudioDuckingLease: Hashable, Sendable {
    let id: UUID
    init(id: UUID = UUID()) { self.id = id }
}

enum DuckingPreparationResult: Equatable, Sendable {
    case ready, bypassed, failed(DuckingFailure)
}
enum DuckingRestoreResult: Equatable, Sendable {
    case restored, absentOrStale, retained(DuckingFailure)
}

private final class DuckingCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func cancel() { lock.withLock { value = true } }
    var isCancelled: Bool { lock.withLock { value } }
}

/// Очередь владеет операцией целиком: snapshot → mute → restore.
/// MainActor только ожидает continuation и никогда не ждёт subprocess синхронно.
final class SystemAudioDuckingService: @unchecked Sendable {
    private struct Snapshot {
        let lease: AudioDuckingLease
        let muted: Bool
        let volume: Int
        var mutationMayHaveOccurred: Bool
        var restorePending: Bool
    }
    private let queue = DispatchQueue(label: "krabear.audio-ducking", qos: .userInitiated)
    private let runner: any AudioDuckingCommandRunning
    private let operationBudget: Duration
    private let logger = AgentLogger.shared
    // Только queue; неизвестное завершение child запрещает все будущие команды.
    private var snapshot: Snapshot?
    private var quarantined = false

    init(runner: any AudioDuckingCommandRunning = SystemAudioDuckingCommandRunner(), operationBudget: Duration = .seconds(2)) {
        self.runner = runner
        self.operationBudget = operationBudget
    }

    func prepare(lease: AudioDuckingLease, enabled: Bool, duckPercent: Int) async -> DuckingPreparationResult {
        let deadline = ContinuousClock.now + operationBudget
        let cancellation = DuckingCancellation()
        return await withTaskCancellationHandler {
            if Task.isCancelled { cancellation.cancel() }
            return await withCheckedContinuation { continuation in
                queue.async {
                    continuation.resume(returning: self.prepareOnQueue(
                        lease: lease, enabled: enabled, duckPercent: duckPercent,
                        deadline: deadline, cancellation: cancellation
                    ))
                }
            }
        } onCancel: { cancellation.cancel() }
    }

    /// Постановка в очередь происходит до возврата: следующий prepare не обгонит restore.
    func requestRestore(
        lease: AudioDuckingLease,
        completion: (@MainActor @Sendable (DuckingRestoreResult) -> Void)? = nil
    ) {
        let deadline = ContinuousClock.now + operationBudget
        queue.async {
            let result = self.restoreOnQueue(lease: lease, deadline: deadline)
            if let completion { Task { @MainActor in completion(result) } }
        }
    }

    private func prepareOnQueue(
        lease: AudioDuckingLease, enabled: Bool, duckPercent: Int,
        deadline: ContinuousClock.Instant, cancellation: DuckingCancellation
    ) -> DuckingPreparationResult {
        guard !quarantined else { return .failed(.cleanupUnconfirmed) }
        guard ContinuousClock.now < deadline else { return .failed(.deadlineExceeded) }
        guard !cancellation.isCancelled else { return .failed(.cancelled) }
        if let current = snapshot {
            guard current.restorePending else { return .failed(.busy) }
            guard restoreOnQueue(lease: current.lease, deadline: deadline) == .restored else {
                return .failed(quarantined ? .cleanupUnconfirmed : .busy)
            }
        }
        guard enabled else { return .bypassed }
        let mutedResult = command("output muted of (get volume settings)", deadline: deadline)
        guard case let .success(mutedRaw) = mutedResult else { return .failed(reason(mutedResult)) }
        let mutedText = mutedRaw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard mutedText == "true" || mutedText == "false" else { return .failed(.invalidSnapshot) }
        if mutedText == "true" {
            snapshot = Snapshot(lease: lease, muted: true, volume: 0, mutationMayHaveOccurred: false, restorePending: false)
            return cancellation.isCancelled ? .failed(.cancelled) : .bypassed
        }
        guard !cancellation.isCancelled else { return .failed(.cancelled) }
        let volumeResult = command("output volume of (get volume settings)", deadline: deadline)
        guard case let .success(volumeRaw) = volumeResult else { return .failed(reason(volumeResult)) }
        guard let volume = Int(volumeRaw.trimmingCharacters(in: .whitespacesAndNewlines)), (0...100).contains(volume) else {
            return .failed(.invalidSnapshot)
        }
        guard !cancellation.isCancelled else { return .failed(.cancelled) }
        guard ContinuousClock.now < deadline else { return .failed(.deadlineExceeded) }
        // Публикуем original snapshot ДО команды: false exit не исключает partial write.
        snapshot = Snapshot(lease: lease, muted: false, volume: volume, mutationMayHaveOccurred: true, restorePending: false)
        let percent = max(0, min(duckPercent, 100))
        let script = percent >= 100 ? "set volume output muted true"
            : "set volume output volume \(volume * percent / 100)\nset volume output muted false"
        let result = command(script, deadline: deadline)
        guard case .success = result else {
            snapshot?.restorePending = true
            return .failed(reason(result))
        }
        if cancellation.isCancelled {
            snapshot?.restorePending = true
            return .failed(.cancelled)
        }
        return .ready
    }

    private func restoreOnQueue(lease: AudioDuckingLease, deadline: ContinuousClock.Instant) -> DuckingRestoreResult {
        guard let current = snapshot, current.lease == lease else { return .absentOrStale }
        guard !quarantined else { return .retained(.cleanupUnconfirmed) }
        if !current.mutationMayHaveOccurred { snapshot = nil; return .restored }
        snapshot?.restorePending = true
        let script = current.muted ? "set volume output muted true"
            : "set volume output volume \(current.volume)\nset volume output muted false"
        let result = command(script, deadline: deadline)
        guard case .success = result else {
            let failure = reason(result)
            logger.warn("Восстановление системного звука сохранено для повтора: \(failure.rawValue)")
            return .retained(failure)
        }
        snapshot = nil
        return .restored
    }

    private func command(_ script: String, deadline: ContinuousClock.Instant) -> AudioDuckingCommandResult {
        guard !quarantined else { return .failure(.cleanupUnconfirmed, cleanupConfirmed: false) }
        guard ContinuousClock.now < deadline else { return .failure(.deadlineExceeded, cleanupConfirmed: true) }
        let result = runner.run(script: script, deadline: deadline)
        if case let .failure(_, cleanupConfirmed) = result, !cleanupConfirmed { quarantined = true }
        return result
    }
    private func reason(_ result: AudioDuckingCommandResult) -> DuckingFailure {
        if case let .failure(failure, _) = result { return failure }
        return .commandFailed
    }
}
