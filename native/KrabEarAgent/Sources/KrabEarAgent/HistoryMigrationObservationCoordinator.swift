import Foundation

/// Оркестрация UI-наблюдения; транспорт, SSE и AppKit остаются в адаптере панели.
@MainActor
final class HistoryMigrationObservationCoordinator {
    typealias Completion = @MainActor ([String: Any]?) -> Void
    typealias Request = @MainActor (String, @escaping Completion) -> Void
    typealias ScheduledAction = @MainActor () -> Void
    typealias Schedule = @MainActor (TimeInterval, @escaping ScheduledAction) -> Void
    typealias Confirmation = @MainActor (Bool) -> Void
    typealias PresentConfirmation = @MainActor (Int, @escaping Confirmation) -> Void

    enum RenderEvent: Equatable {
        case visibility(enabled: Bool, plaintext: Int)
        case starting
        case polling
        case progress(pct: Int, encrypted: Int, total: Int, showCount: Bool)
        case finishing
        case unavailable
        case outcome(HistoryMigrationOutcome)
    }

    private let request: Request
    private let schedule: Schedule
    private let startWatcher: ScheduledAction
    private let stopWatcher: ScheduledAction
    private let hasWatcher: @MainActor () -> Bool
    private let terminalStatus: @MainActor () -> String?
    private let render: @MainActor (RenderEvent) -> Void
    private let presentConfirmation: PresentConfirmation
    private var active = false
    private var startPending = false
    // polling уже false, но terminal fallback ещё владеет наблюдением.
    private var finishing = false
    private var epoch: UInt64 = 0
    // Refresh, отправленный до нового наблюдения, не должен перерисовать его итог.
    private var observationRevision: UInt64 = 0
    private(set) var polling = false

    init(request: @escaping Request, schedule: @escaping Schedule,
         startWatcher: @escaping ScheduledAction, stopWatcher: @escaping ScheduledAction,
         hasWatcher: @escaping @MainActor () -> Bool,
         terminalStatus: @escaping @MainActor () -> String?,
         render: @escaping @MainActor (RenderEvent) -> Void,
         presentConfirmation: @escaping PresentConfirmation) {
        self.request = request
        self.schedule = schedule
        self.startWatcher = startWatcher
        self.stopWatcher = stopWatcher
        self.hasWatcher = hasWatcher
        self.terminalStatus = terminalStatus
        self.render = render
        self.presentConfirmation = presentConfirmation
    }

    func activate() {
        guard !active else { return }
        epoch &+= 1
        active = true
    }

    private func isCurrent(_ expectedEpoch: UInt64) -> Bool {
        active && epoch == expectedEpoch
    }

    func close() {
        guard active else { return }
        // Сначала инвалидируем callbacks; stopWatcher не может оживить старое наблюдение.
        epoch &+= 1
        active = false
        startPending = false
        finishing = false
        polling = false
        stopWatcher()
    }

    func refresh() {
        guard active, !polling, !startPending, !finishing else { return }
        let expectedEpoch = epoch
        let expectedRevision = observationRevision
        request("get_history_encryption_status") { [weak self] response in
            guard let self, self.isCurrent(expectedEpoch),
                  self.observationRevision == expectedRevision, !self.polling, !self.finishing,
                  let result = response?["result"] as? [String: Any] else { return }
            if result["migrating"] as? Bool ?? false {
                self.beginPolling()
            } else {
                self.render(.visibility(enabled: result["enabled"] as? Bool ?? false,
                                        plaintext: result["plaintext"] as? Int ?? 0))
            }
        }
    }

    func confirmAndStartMigration() {
        guard active else { return }
        let expectedEpoch = epoch
        request("get_history_encryption_status") { [weak self] response in
            guard let self, self.isCurrent(expectedEpoch) else { return }
            let result = response?["result"] as? [String: Any]
            let plaintext = result?["plaintext"] as? Int ?? 0
            guard plaintext > 0 else { self.refresh(); return }
            self.presentConfirmation(plaintext) { [weak self] confirmed in
                guard let self, self.isCurrent(expectedEpoch), confirmed else { return }
                self.startMigration()
            }
        }
    }

    func startMigration() {
        guard active, !startPending, !finishing else { return }
        let expectedEpoch = epoch
        startPending = true
        observationRevision &+= 1
        startWatcher()
        render(.starting)
        request("migrate_history_encryption") { [weak self] response in
            guard let self, self.isCurrent(expectedEpoch) else { return }
            self.startPending = false
            guard let result = response?["result"] as? [String: Any],
                  result["ok"] as? Bool == true,
                  result["status"] as? String != "encryption_unavailable" else {
                self.polling = false
                self.stopWatcher()
                self.render(.unavailable)
                return
            }
            self.beginPolling()
        }
    }

    private func beginPolling() {
        guard active, !polling, !finishing else { return }
        observationRevision &+= 1
        polling = true
        if !hasWatcher() { startWatcher() }
        render(.polling)
        pollOnce()
    }

    private func pollOnce() {
        guard active, polling else { return }
        let expectedEpoch = epoch
        request("get_history_encryption_status") { [weak self] response in
            guard let self, self.isCurrent(expectedEpoch) else { return }
            guard let result = response?["result"] as? [String: Any] else {
                self.retryPoll()
                return
            }
            let total = result["total"] as? Int ?? 0
            let encrypted = result["encrypted"] as? Int ?? 0
            let plaintext = result["plaintext"] as? Int ?? 0
            let migrating = result["migrating"] as? Bool ?? false
            self.render(.progress(pct: result["pct"] as? Int ?? 0, encrypted: encrypted,
                                  total: total, showCount: migrating || plaintext > 0))
            if migrating { self.retryPoll(); return }
            self.polling = false
            let ipcTerminal = result["last_migration_status"] as? String
            if let ipcTerminal, !ipcTerminal.isEmpty {
                self.finish(plaintext: plaintext, ipcTerminal: ipcTerminal, epoch: expectedEpoch)
            } else if plaintext == 0, self.hasWatcher(), self.terminalStatus() == nil {
                self.finishing = true
                self.render(.finishing)
                self.schedule(1.5) { [weak self] in
                    guard let self, self.isCurrent(expectedEpoch) else { return }
                    self.finish(plaintext: plaintext, ipcTerminal: nil, epoch: expectedEpoch)
                }
            } else {
                self.finish(plaintext: plaintext, ipcTerminal: nil, epoch: expectedEpoch)
            }
        }
    }

    private func retryPoll() {
        let expectedEpoch = epoch
        schedule(0.5) { [weak self] in
            guard let self, self.isCurrent(expectedEpoch) else { return }
            self.pollOnce()
        }
    }

    private func finish(plaintext: Int, ipcTerminal: String?, epoch expectedEpoch: UInt64) {
        guard isCurrent(expectedEpoch) else { return }
        // Отсекаем refresh, отправленный до получения свежего terminal outcome.
        observationRevision &+= 1
        finishing = false
        let terminal = HistoryMigrationOutcome.effectiveTerminalStatus(ipc: ipcTerminal, sse: terminalStatus())
        let outcome = HistoryMigrationOutcome.resolve(plaintext: plaintext, terminalStatus: terminal)
        stopWatcher()
        render(.outcome(outcome))
    }
}
