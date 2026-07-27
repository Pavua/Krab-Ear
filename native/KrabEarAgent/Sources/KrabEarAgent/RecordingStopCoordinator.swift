/*
 RecordingStopCoordinator.swift

 Единый R2-координатор безопасной остановки записи. Он повторяет один
 неизменяемый IPC-запрос с opaque generation_token по ограниченным бюджетам
 транспортной неоднозначности, recorder_timeout и stop_in_progress.
 UI-состояние остаётся у AgentAppDelegate/MeetingLivePanelController, а этот
 модуль возвращает только решение и последний ответ, чтобы все три потребителя
 применяли одинаковую retry-семантику без блокировки главного потока.
*/

import Foundation

/// Решение о следующем шаге или финальном исходе одного stop-цикла R2.
enum StopDecision: Equatable, Sendable {
    /// Транспорт неоднозначен — повторить тот же запрос.
    case retry
    /// Physical worker не отдал аудио — повторить stop той же G1.
    case retryRecorderStop(delaySec: TimeInterval)
    /// Бюджет recorder_timeout исчерпан; токен остаётся recovery-handle.
    case recoveryPending
    /// Финализация ещё идёт — подождать и переспросить.
    case pollAgain
    /// Пятиминутный бюджет исчерпан; результат может появиться в истории позже.
    case finalizationSlow
    /// Поколение неизвестно или транспортный бюджет исчерпан; нужен rescue.
    case giveUpRescuePending
    /// Активная запись принадлежит другому потребителю.
    case foreignOwner
    /// Ответ/ошибка не допускает автоматического повтора.
    case surfaceAsIs
}

/// Неизменяемый stop-запрос; один экземпляр переиспользуется во всех попытках.
struct RecordingStopRequest: @unchecked Sendable {
    let method: String
    let params: [String: Any]
    let timeoutSec: Int
}

/// Итог bounded stop-цикла вместе с последним IPC-конвертом или ошибкой.
struct RecordingStopOutcome: @unchecked Sendable {
    let decision: StopDecision
    let response: [String: Any]?
    let result: [String: Any]?
    let error: Error?

    /// Терминальным можно считать только реально полученный обычный ответ.
    /// `.surfaceAsIs` без response означает нетипизированную неопределённость:
    /// вызывающий код обязан сохранить token/recovery-state.
    var hasTerminalResponse: Bool {
        decision == .surfaceAsIs && response != nil && result != nil
    }
}

enum RecordingStopCoordinator {
    /// Максимум дополнительных попыток после transient transport error.
    static let maxTransportRetries = 2
    /// Максимум дополнительных physical-stop попыток после recorder_timeout.
    static let maxRecorderTimeoutRetries = 2
    /// Опрос stop_in_progress: раз в 2 секунды.
    static let pollIntervalSec: TimeInterval = 2
    /// Абсолютный монотонный бюджет polling-фазы, включая время IPC-вызовов.
    static let finalizationBudgetSec: TimeInterval = 5 * 60
    /// Защитный потолок числа ответов; не заменяет wall-clock deadline.
    static let maxPolls = 150

    /// Короткий backoff транспортных повторов без скрытого шестикратного retry.
    private static let transportRetryDelays: [TimeInterval] = [0.25, 0.5]

    static func decide(
        afterTransportError isTransport: Bool,
        attempt: Int
    ) -> StopDecision {
        guard isTransport else { return .surfaceAsIs }
        return attempt <= maxTransportRetries
            ? .retry
            : .giveUpRescuePending
    }

    static func decide(
        afterStatus status: String,
        attempt: Int
    ) -> StopDecision {
        switch status {
        case "recorder_timeout":
            guard attempt <= maxRecorderTimeoutRetries else {
                return .recoveryPending
            }
            return .retryRecorderStop(
                delaySec: TimeInterval(1 << attempt)
            )
        case "stop_in_progress":
            return attempt <= maxPolls
                ? .pollAgain
                : .finalizationSlow
        case "unknown_generation":
            return .giveUpRescuePending
        case "owner_mismatch":
            return .foreignOwner
        default:
            // Все прочие типизированные ответы обрабатывает вызывающий путь:
            // coordinator не имеет права самовольно повторять backend-ошибку.
            return .surfaceAsIs
        }
    }

    /// Собрать тот же запрос с timeout, не выходящим за polling-deadline.
    /// `method` и `params` намеренно не меняются: в params лежит opaque token G1.
    private static func boundedRequest(
        _ original: RecordingStopRequest,
        boundedBy deadline: TimeInterval?,
        monotonicNow: () -> TimeInterval
    ) -> RecordingStopRequest? {
        guard let deadline else { return original }
        let remaining = deadline - monotonicNow()
        let wholeRemainingSeconds = Int(remaining.rounded(.down))
        guard wholeRemainingSeconds > 0 else { return nil }

        let timeoutSec = min(original.timeoutSec, wholeRemainingSeconds)
        guard timeoutSec > 0 else { return nil }
        return RecordingStopRequest(
            method: original.method,
            params: original.params,
            timeoutSec: timeoutSec
        )
    }

    /// Подождать не дольше остатка deadline; `false` значит, что он уже истёк.
    private static func sleepWithinDeadline(
        _ requestedDelay: TimeInterval,
        boundedBy deadline: TimeInterval?,
        monotonicNow: () -> TimeInterval,
        sleep: (TimeInterval) async throws -> Void
    ) async throws -> Bool {
        guard let deadline else {
            try await sleep(requestedDelay)
            return true
        }

        let remaining = deadline - monotonicNow()
        guard remaining > 0 else { return false }
        try await sleep(min(requestedDelay, remaining))
        return monotonicNow() < deadline
    }

    private static func finalizationSlowOutcome(
        response: [String: Any]?,
        result: [String: Any]?
    ) -> RecordingStopOutcome {
        RecordingStopOutcome(
            decision: .finalizationSlow,
            response: response,
            result: result,
            error: nil
        )
    }

    /// Выполнить stop через внедрённую асинхронную IPC-операцию.
    ///
    /// Счётчики независимы: transport error не расходует recorder_timeout
    /// budget, а polling долгой финализации не открывает новые retry-бюджеты.
    static func execute(
        request: RecordingStopRequest,
        operation: (RecordingStopRequest) async throws -> [String: Any],
        sleep: (TimeInterval) async throws -> Void,
        monotonicNow: () -> TimeInterval = {
            ProcessInfo.processInfo.systemUptime
        },
        finalizationBudgetSec: TimeInterval = RecordingStopCoordinator.finalizationBudgetSec
    ) async -> RecordingStopOutcome {
        var transportFailures = 0
        var recorderTimeoutResponses = 0
        var stopInProgressResponses = 0
        // Deadline создаётся только после первого stop_in_progress: до этого
        // backend ещё не подтвердил, что physical stop уже перешёл в финализацию.
        // Его точка отсчёта — начало IPC, вернувшего этот статус, чтобы даже
        // первый долгий request вошёл в абсолютный wall-clock budget.
        var finalizationDeadline: TimeInterval?
        var lastStopInProgressResponse: [String: Any]?
        var lastStopInProgressResult: [String: Any]?

        while true {
            guard let repeatedRequest = boundedRequest(
                request,
                boundedBy: finalizationDeadline,
                monotonicNow: monotonicNow
            ) else {
                return finalizationSlowOutcome(
                    response: lastStopInProgressResponse,
                    result: lastStopInProgressResult
                )
            }

            do {
                let requestStartedAt = monotonicNow()
                let response = try await operation(repeatedRequest)
                guard let result = response["result"] as? [String: Any] else {
                    return RecordingStopOutcome(
                        decision: .surfaceAsIs,
                        response: response,
                        result: nil,
                        error: IPCError.invalidResponse
                    )
                }
                let status = result["status"] as? String ?? ""

                let decision: StopDecision
                switch status {
                case "recorder_timeout":
                    // Этот исход имеет собственный recovery-budget. Если backend
                    // вернулся из poll-фазы в timeout, не смешиваем два бюджета.
                    finalizationDeadline = nil
                    lastStopInProgressResponse = nil
                    lastStopInProgressResult = nil
                    stopInProgressResponses = 0
                    recorderTimeoutResponses += 1
                    decision = decide(
                        afterStatus: status,
                        attempt: recorderTimeoutResponses
                    )
                case "stop_in_progress":
                    stopInProgressResponses += 1
                    if finalizationDeadline == nil {
                        finalizationDeadline = requestStartedAt + max(
                            0,
                            finalizationBudgetSec
                        )
                    }
                    lastStopInProgressResponse = response
                    lastStopInProgressResult = result
                    if let deadline = finalizationDeadline,
                       monotonicNow() >= deadline {
                        return finalizationSlowOutcome(
                            response: response,
                            result: result
                        )
                    }
                    decision = decide(
                        afterStatus: status,
                        attempt: stopInProgressResponses
                    )
                default:
                    decision = decide(afterStatus: status, attempt: 1)
                }

                switch decision {
                case .retryRecorderStop(let delaySec):
                    do {
                        let sleptBeforeDeadline = try await sleepWithinDeadline(
                            delaySec,
                            boundedBy: finalizationDeadline,
                            monotonicNow: monotonicNow,
                            sleep: sleep
                        )
                        guard sleptBeforeDeadline else {
                            return finalizationSlowOutcome(
                                response: lastStopInProgressResponse,
                                result: lastStopInProgressResult
                            )
                        }
                    } catch {
                        return RecordingStopOutcome(
                            decision: .surfaceAsIs,
                            response: nil,
                            result: nil,
                            error: error
                        )
                    }
                case .pollAgain:
                    do {
                        let sleptBeforeDeadline = try await sleepWithinDeadline(
                            pollIntervalSec,
                            boundedBy: finalizationDeadline,
                            monotonicNow: monotonicNow,
                            sleep: sleep
                        )
                        guard sleptBeforeDeadline else {
                            return finalizationSlowOutcome(
                                response: lastStopInProgressResponse,
                                result: lastStopInProgressResult
                            )
                        }
                    } catch {
                        return RecordingStopOutcome(
                            decision: .surfaceAsIs,
                            response: nil,
                            result: nil,
                            error: error
                        )
                    }
                case .retry:
                    // Ответ backend не возвращает это решение: retry относится
                    // только к catch-ветке транспортной ошибки ниже.
                    continue
                case .recoveryPending, .finalizationSlow,
                     .giveUpRescuePending, .foreignOwner, .surfaceAsIs:
                    return RecordingStopOutcome(
                        decision: decision,
                        response: response,
                        result: result,
                        error: nil
                    )
                }
            } catch {
                transportFailures += 1
                let decision = decide(
                    afterTransportError: isTransientTransportError(error),
                    attempt: transportFailures
                )
                guard decision == .retry else {
                    return RecordingStopOutcome(
                        decision: decision,
                        response: nil,
                        result: nil,
                        error: error
                    )
                }

                if let deadline = finalizationDeadline,
                   monotonicNow() >= deadline {
                    return finalizationSlowOutcome(
                        response: lastStopInProgressResponse,
                        result: lastStopInProgressResult
                    )
                }

                let delayIndex = transportFailures - 1
                let delay = transportRetryDelays[delayIndex]
                do {
                    let sleptBeforeDeadline = try await sleepWithinDeadline(
                        delay,
                        boundedBy: finalizationDeadline,
                        monotonicNow: monotonicNow,
                        sleep: sleep
                    )
                    guard sleptBeforeDeadline else {
                        return finalizationSlowOutcome(
                            response: lastStopInProgressResponse,
                            result: lastStopInProgressResult
                        )
                    }
                } catch {
                    return RecordingStopOutcome(
                        decision: .surfaceAsIs,
                        response: nil,
                        result: nil,
                        error: error
                    )
                }
            }
        }
    }

    /// Production-вариант ожидания через Swift Concurrency, без Thread.sleep.
    static func execute(
        request: RecordingStopRequest,
        operation: (RecordingStopRequest) async throws -> [String: Any]
    ) async -> RecordingStopOutcome {
        await execute(
            request: request,
            operation: operation,
            sleep: { delaySec in
                try await Task.sleep(
                    nanoseconds: UInt64(delaySec * 1_000_000_000)
                )
            },
            monotonicNow: {
                ProcessInfo.processInfo.systemUptime
            }
        )
    }

    /// Совпадает с текущим явным transient-контрактом IPCClient.
    private static func isTransientTransportError(_ error: Error) -> Bool {
        guard let ipcError = error as? IPCError else { return false }
        return ipcError.isTransient
    }
}
