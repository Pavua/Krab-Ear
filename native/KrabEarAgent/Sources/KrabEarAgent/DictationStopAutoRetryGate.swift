/*
 DictationStopAutoRetryGate — решение «можно ли авто-дострелить отложенный
 stop_recording» (спека 2026-08-11-auto-refire-pending-stop-design.md §2.3).

 Чистая struct без побочных эффектов: AgentAppDelegate передаёт снимок
 своего состояния и получает решение. Выделена из делегата ровно затем,
 чтобы гарды тестировались юнитами без конструирования AgentAppDelegate
 (демон-объекты, NSApp — нетестируемо в чистом XCTest).
*/

struct DictationStopAutoRetrySnapshot {
    var recoveryPending: Bool
    var generationOwner: String?
    var isProcessing: Bool
    var quickCaptureActive: Bool
    var remainingBudget: Int
}

enum DictationStopAutoRetryGate {
    /// Полный бюджет авто-попыток на один «эпизод потери» (непрерывный
    /// период recoveryPending). Кап против карусели «healthy ping каждые
    /// 3с → полный coordinator-цикл» на терминально-невнятном backend'е —
    /// тот же паттерн, что give-up cap WedgedEscalationTracker.
    static let fullBudget = 2

    static func shouldAttempt(_ s: DictationStopAutoRetrySnapshot) -> Bool {
        return s.recoveryPending
            && s.generationOwner == "dictation"
            && !s.isProcessing
            && !s.quickCaptureActive
            && s.remainingBudget > 0
    }
}
