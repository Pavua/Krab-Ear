import AppKit
import Foundation

/// Снимок адресата: ожидание не выбирает другое приложение вместо исходного.
@MainActor
struct PasteActivationTarget {
    let pid: pid_t
    let isAlive: () -> Bool
    let requestActivation: () -> Bool

    init(pid: pid_t, isAlive: @escaping () -> Bool, requestActivation: @escaping () -> Bool) {
        self.pid = pid
        self.isAlive = isAlive
        self.requestActivation = requestActivation
    }

    init(application: NSRunningApplication) {
        pid = application.processIdentifier
        isAlive = { !application.isTerminated }
        requestActivation = { application.activate(options: [.activateIgnoringOtherApps]) }
    }
}

/// Общая точка активации для диктовки/QuickEdit и явной вставки из истории.
/// OS leaves подменяются тестом без активации окон или отправки клавиш.
@MainActor
final class PasteTargetActivator {
    private let frontmostPID: () -> pid_t?
    private let budget: Duration
    private var inFlight = false

    init(budget: Duration = .milliseconds(400), frontmostPID: @escaping () -> pid_t? = {
        NSWorkspace.shared.frontmostApplication?.processIdentifier
    }) {
        self.budget = budget
        self.frontmostPID = frontmostPID
    }

    /// Владение сохраняется и на время paste: другая собственная активация не
    /// вклинивается между подтверждением foreground и async вызовом сервиса.
    func perform(target: PasteActivationTarget, deadline suppliedDeadline: ContinuousClock.Instant? = nil, paste: (pid_t) async -> PasteAttemptResult) async -> PasteAttemptResult {
        guard !inFlight else { return PasteAttemptResult(ok: false, reason: "target_activation_busy") }
        inFlight = true
        defer { inFlight = false }
        let deadline = min(ContinuousClock.now + budget, suppliedDeadline ?? .now + budget)
        func refusal() -> PasteAttemptResult? {
            if Task.isCancelled { return PasteAttemptResult(ok: false, reason: "target_activation_cancelled") }
            if !target.isAlive() { return PasteAttemptResult(ok: false, reason: "target_activation_terminated") }
            if ContinuousClock.now >= deadline { return PasteAttemptResult(ok: false, reason: "target_activation_timeout") }
            return nil
        }
        if let failure = refusal() { return failure }
        if frontmostPID() != target.pid {
            guard target.requestActivation() else {
                return PasteAttemptResult(ok: false, reason: "target_activation_failed")
            }
            while frontmostPID() != target.pid {
                if let failure = refusal() { return failure }
                do {
                    try await Task.sleep(until: min(deadline, .now + .milliseconds(20)), clock: .continuous)
                } catch {
                    return PasteAttemptResult(ok: false, reason: "target_activation_cancelled")
                }
            }
        }
        if let failure = refusal() { return failure }
        return await paste(target.pid)
    }
}
