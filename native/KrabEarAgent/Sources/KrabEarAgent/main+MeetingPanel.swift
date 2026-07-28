/*
 main+MeetingPanel — C2c Task 3: владение MeetingLivePanelController + точки входа
 (спека §2.7/§2.7a п.3).

 Единственный владелец панели — AgentAppDelegate (свойство meetingPanelController
 в main.swift, НЕ associated object — тот паттерн зарезервирован для Phase 2B
 live-subs state, здесь класс определён в основном файле). Вызывающие точки:
 меню-бар «Встреча» (main+StatusMenu.swift) и кнопка «Встреча» в topActionsRow
 истории (HistoryPanelController.swift) — обе роутят сюда, в onMeetingPanelToggle().
*/

import AppKit
import Foundation

enum MeetingPromotionRoutingDecision: Equatable {
    case deferUntilStart(token: String)
    case acceptPendingMeeting(token: String)
    case handoffNow
    case rejectForeignAmbiguous
    case ignore
}

/// Чистая политика порядка асинхронного старта диктовки и повышения до встречи.
enum MeetingPromotionRoutingPolicy {
    static func decide(
        promoted: Bool,
        meetingToken: String?,
        recordingStartInFlight: Bool,
        recordingStartAmbiguous: Bool = false,
        ambiguousStartToken: String? = nil,
        activeOwner: String?,
        activeToken: String?,
        activeOwnerRevision: Int? = nil,
        pendingPromotionToken: String? = nil,
        isRecording: Bool = false,
        meetingOwnerRevision: Int? = nil,
        meetingOwnerRevisionIsValid: Bool = true
    ) -> MeetingPromotionRoutingDecision {
        guard promoted else { return .ignore }
        guard
            meetingOwnerRevisionIsValid,
            let token = normalizedToken(meetingToken)
        else {
            return .ignore
        }

        if recordingStartInFlight && activeOwner == nil {
            return .deferUntilStart(token: token)
        }

        if recordingStartAmbiguous {
            guard let ambiguousToken = normalizedToken(ambiguousStartToken) else {
                return .deferUntilStart(token: token)
            }
            guard ambiguousToken == token else {
                return .rejectForeignAmbiguous
            }
            if let meetingOwnerRevision {
                guard
                    let activeOwnerRevision,
                    meetingOwnerRevision > activeOwnerRevision
                else {
                    return .rejectForeignAmbiguous
                }
            }
            return .handoffNow
        }

        // Сверка уже доказала, что G1 повышена до встречи, но панель получила
        // ответ после снятия локального маршрута диктовки. Такой путь допустим
        // только по сохранённому точному токену и только при отсутствии G2.
        if
            !recordingStartInFlight,
            activeOwner == nil,
            activeToken == nil,
            activeOwnerRevision == nil,
            !isRecording,
            let pendingPromotionToken = normalizedToken(pendingPromotionToken),
            pendingPromotionToken == token {
            return .acceptPendingMeeting(token: token)
        }

        guard
            activeOwner == "dictation",
            let activeToken = normalizedToken(activeToken),
            activeToken == token
        else {
            return .ignore
        }

        // Когда обе ревизии доступны, старое повышение не вправе перехватить
        // уже откатанное или заменённое поколение диктовки.
        if let meetingOwnerRevision {
            guard
                let activeOwnerRevision,
                meetingOwnerRevision > activeOwnerRevision
            else {
                return .ignore
            }
        }
        return .handoffNow
    }

    private static func normalizedToken(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }
}

/// Чистое решение для позднего ответа старта при уже принятом повышении встречи.
/// Несовпавший ответ поглощается, но ожидающее ограждение остаётся до безопасного пути.
enum PendingMeetingPromotionConsumptionPolicy {
    enum Decision: Equatable {
        case noPendingPromotion
        case preservePendingAndRejectResponse
        case completeHandoff(token: String)
    }

    static func decide(
        pendingToken: String?,
        startToken: String?
    ) -> Decision {
        guard let pendingToken = normalizedToken(pendingToken) else {
            return pendingToken == nil
                ? .noPendingPromotion
                : .preservePendingAndRejectResponse
        }
        guard let startToken = normalizedToken(startToken) else {
            return .preservePendingAndRejectResponse
        }
        return startToken == pendingToken
            ? .completeHandoff(token: pendingToken)
            : .preservePendingAndRejectResponse
    }

    private static func normalizedToken(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }
}

/// Чистый CAS-фильтр очистки локального маршрута диктовки после повышения.
/// Он запрещает G1 очистить уже опубликованную G2 даже при позднем обратном вызове.
enum MeetingPromotionHandoffCleanupPolicy {
    enum Decision: Equatable {
        case clearCurrentGeneration
        case preserveNoLocalGeneration
        case reject
    }

    static func decide(
        expectedToken: String?,
        activeOwner: String?,
        activeToken: String?,
        activeOwnerRevision: Int?,
        expectedOwnerRevision: Int?,
        isRecording: Bool
    ) -> Decision {
        guard let expectedToken = normalizedToken(expectedToken) else {
            return .reject
        }

        guard let activeToken = normalizedToken(activeToken) else {
            return activeOwner == nil && activeOwnerRevision == nil && !isRecording
                ? .preserveNoLocalGeneration
                : .reject
        }

        guard
            activeOwner == "dictation",
            activeToken == expectedToken,
            activeOwnerRevision == expectedOwnerRevision
        else {
            return .reject
        }
        return .clearCurrentGeneration
    }

    private static func normalizedToken(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }
}

extension AgentAppDelegate {

    /// Единый вход: сессии нет → meeting_start (backend идемпотентен:
    /// already_active/promoted) → показать панель; сессия есть → просто показать.
    @objc func onMeetingPanelToggle() {
        // C3a: во время быстрой заметки встреча отвергается (взаимное
        // исключение — спека 2026-07-16-c3-quick-capture-design.md §2a).
        if quickCaptureActive
            || quickCaptureStartRequestID != nil
            || quickCaptureStartAmbiguousRequestID != nil {
            BackendToast.shared.show(
                "Идёт или подтверждается быстрая заметка — сначала завершите её"
            )
            return
        }
        let controller = ensureMeetingPanelController()
        controller.show()
        // Пока G1 финализируется или удерживается для явного retry, новый
        // meeting_start способен открыть G2 после уже состоявшегося terminal
        // stop с потерянным ответом. Сначала разрешаем G1 poll/SSE-путём.
        guard !controller.hasUnresolvedMeetingStop else {
            // Обновления обязаны идти именно здесь: show() их не поднимает, а
            // закрытие панели крестиком их погасило. Без этого единственный
            // выход из sticky-finalizing (poll со снимком inactive) мёртв, и
            // переоткрытая панель навсегда показывает «Финализирую…».
            controller.startUpdates()
            return
        }
        let client = ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "meeting_start", params: [:])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async { [weak self] in
                    // Панель получает токен только после проверки маршрута повышения.
                    // Иначе поздний ответ G1 способен подменить уже живую G2.
                    let mayAcceptGeneration = self?.adoptPromotedMeetingGeneration(
                        result
                    ) ?? false
                    if mayAcceptGeneration {
                        controller.acceptGenerationToken(
                            result["generation_token"] as? String
                        )
                        // Первый опрос и SSE стартуют только после проверки токена:
                        // ранний опрос иначе мог бы обойти проверку повышения.
                        controller.startUpdates()
                        controller.pollNow()
                    }
                    if (result["skipped"] as? String) == "privacy_mode" {
                        controller.render(state: ["ok": true, "active": false,
                                                  "privacy_mode_active": true])
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    controller.showTransientError("Не удалось начать встречу: \(error.localizedDescription)")
                }
            }
        }
    }

    /// Единственный инстанс панели: создаётся лениво, инжектится ipcClient
    /// и onFinished-колбэк (финализация → отчёт).
    func ensureMeetingPanelController() -> MeetingLivePanelController {
        if let existing = meetingPanelController { return existing }
        let c = MeetingLivePanelController()
        c.ipcClient = ipcClient
        c.onFinished = { [weak self, weak c] itemID in
            let finishedGenerationToken = c?.lastDeliveredGenerationToken
            self?.releasePromotedMeetingDucking()
            self?.openMeetingReportAfterFinish(
                itemID: itemID,
                expectedGenerationToken: finishedGenerationToken
            )
        }
        meetingPanelController = c
        return c
    }

    /// finished → get_meeting_report → standalone-окно; без item_id — панель в idle.
    func openMeetingReportAfterFinish(
        itemID: String?,
        expectedGenerationToken: String?
    ) {
        guard let itemID, !itemID.isEmpty else {
            meetingPanelController?.resetToIdleAfterFinished(
                expectedGenerationToken: expectedGenerationToken
            )
            return
        }
        let client = ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "get_meeting_report", params: ["id": itemID])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async { [weak self] in
                    self?.meetingPanelController?.resetToIdleAfterFinished(
                        expectedGenerationToken: expectedGenerationToken
                    )
                    HistoryPanelController.presentMeetingReportStandalone(result: result)
                }
            } catch {
                DispatchQueue.main.async { [weak self] in
                    guard
                        let controller = self?.meetingPanelController,
                        controller.isFinishedCompletionCurrent(
                            expectedGenerationToken: expectedGenerationToken
                        )
                    else {
                        return
                    }
                    controller.showTransientError(
                        "Отчёт не построился: \(error.localizedDescription)")
                    controller.resetToIdle()
                }
            }
        }
    }

    /// Передать панели ту же G1 при повышении диктовки до встречи.
    /// Проверить повышение до передачи токена панели. Возвращает `true` только
    /// когда ответ принадлежит текущей локальной ветке или новой встрече.
    @discardableResult
    private func adoptPromotedMeetingGeneration(_ result: [String: Any]) -> Bool {
        let meetingToken = result["generation_token"] as? String
        let isPromoted = (result["promoted"] as? Bool) == true
        guard isPromoted else {
            // У новой встречи нет маршрута диктовки, который можно перехватить.
            return true
        }

        let rawMeetingOwnerRevision = result["owner_revision"]
        let meetingOwnerRevision = rawMeetingOwnerRevision as? Int
        let meetingOwnerRevisionIsValid: Bool
        if rawMeetingOwnerRevision == nil {
            meetingOwnerRevisionIsValid = true
        } else if let meetingOwnerRevision, meetingOwnerRevision >= 0 {
            meetingOwnerRevisionIsValid = true
        } else {
            meetingOwnerRevisionIsValid = false
        }
        let decision = MeetingPromotionRoutingPolicy.decide(
            promoted: isPromoted,
            meetingToken: meetingToken,
            recordingStartInFlight: recordingStartInFlight,
            recordingStartAmbiguous: recordingStartAmbiguous,
            ambiguousStartToken: activeGenerationToken,
            activeOwner: activeGenerationOwner,
            activeToken: activeGenerationToken,
            activeOwnerRevision: activeGenerationOwnerRevision,
            pendingPromotionToken: pendingMeetingPromotionToken,
            isRecording: isRecording,
            meetingOwnerRevision: meetingOwnerRevision,
            meetingOwnerRevisionIsValid: meetingOwnerRevisionIsValid
        )
        switch decision {
        case let .deferUntilStart(token):
            if let pendingToken = pendingMeetingPromotionToken,
               pendingToken != token {
                logger.warn(
                    "Повышение встречи не вправе заменить ожидающий токен другого поколения"
                )
                return false
            }
            pendingMeetingPromotionToken = token
            meetingInheritedDictationDucking = true
            logger.info(
                "Повышение встречи G1 опередило ответ старта диктовки; " +
                "передача состояния отложена до того же токена backend"
            )
            return true
        case let .acceptPendingMeeting(token):
            // Этот путь разрешён только для G1, уже доказанной сверкой состояния.
            // Повторная проверка не даёт очистить или принять чужое поколение.
            guard
                pendingMeetingPromotionToken == token,
                activeGenerationOwner == nil,
                activeGenerationToken == nil,
                activeGenerationOwnerRevision == nil,
                !isRecording,
                !recordingStartInFlight
            else {
                logger.warn(
                    "Подтверждённая встреча изменилась до принятия панели; " +
                    "токен отклонён"
                )
                return false
            }
            pendingMeetingPromotionToken = nil
            meetingInheritedDictationDucking = true
            return true
        case .handoffNow:
            guard let expectedToken = nonEmptyToken(meetingToken) else {
                logger.warn("Повышение встречи без токена отклонено до передачи состояния")
                return false
            }
            if let pendingToken = pendingMeetingPromotionToken,
               pendingToken != expectedToken {
                logger.warn(
                    "Повышение встречи не совпало с ожидающим токеном; передача отклонена"
                )
                return false
            }
            let expectedOwnerRevision = activeGenerationOwnerRevision
            guard completePromotedMeetingHandoff(
                expectedToken: expectedToken,
                expectedOwnerRevision: expectedOwnerRevision
            ) else {
                logger.warn(
                    "Повышение встречи не прошло локальную CAS-проверку передачи"
                )
                return false
            }
            pendingMeetingPromotionToken = nil
            meetingInheritedDictationDucking = true
            return true
        case .rejectForeignAmbiguous:
            logger.warn(
                "Повышение встречи не совпало с наблюдённым токеном неоднозначного старта"
            )
            return false
        case .ignore:
            logger.warn(
                "Повышение встречи не совпало с локальным маршрутом диктовки; " +
                "передача состояния отклонена"
            )
            return false
        }
    }

    /// Поглотить поздний ответ старта диктовки после раннего повышения встречи.
    /// Возвращает true, если ответ уже принадлежит передаче встречи и не должен
    /// публиковать локальный оверлей или владельца.
    func consumePendingMeetingPromotion(startToken: String?) -> Bool {
        let decision = PendingMeetingPromotionConsumptionPolicy.decide(
            pendingToken: pendingMeetingPromotionToken,
            startToken: startToken
        )
        switch decision {
        case .noPendingPromotion:
            return false
        case .preservePendingAndRejectResponse:
            logger.warn(
                "Поздний ответ старта диктовки без точного токена отклонён; " +
                "ожидающее ограждение встречи сохранено"
            )
            // Не позволяем чужому или неполученному ответу стать локальной
            // диктовкой поверх уже принятого повышения встречи.
            return true
        case let .completeHandoff(token):
            guard completePromotedMeetingHandoff(
                expectedToken: token,
                expectedOwnerRevision: activeGenerationOwnerRevision
            ) else {
                logger.warn(
                    "Поздний ответ старта диктовки не прошёл CAS-проверку; " +
                    "ожидающее ограждение встречи сохранено"
                )
                return true
            }
            pendingMeetingPromotionToken = nil
            return true
        }
    }

    /// Очистить только тот маршрут диктовки, который был проверен при повышении.
    /// Возвращает `false`, если обратный вызов относится к уже заменённому поколению.
    @discardableResult
    private func completePromotedMeetingHandoff(
        expectedToken: String,
        expectedOwnerRevision: Int?
    ) -> Bool {
        let decision = MeetingPromotionHandoffCleanupPolicy.decide(
            expectedToken: expectedToken,
            activeOwner: activeGenerationOwner,
            activeToken: activeGenerationToken,
            activeOwnerRevision: activeGenerationOwnerRevision,
            expectedOwnerRevision: expectedOwnerRevision,
            isRecording: isRecording
        )
        switch decision {
        case .preserveNoLocalGeneration:
            // Раннее повышение встречи опередило публикацию диктовки G1.
            // Локального маршрута нет, значит очищать глобальное состояние нельзя.
            return true
        case .reject:
            return false
        case .clearCurrentGeneration:
            activeGenerationToken = nil
            activeGenerationOwner = nil
            activeGenerationOwnerRevision = nil
            activeGenerationStartRequestID = nil
            recordingStopRecoveryPending = false
            isRecording = false
            recordingTargetApp = nil
            stopRealtimeOverlayPolling()
            refreshStatusItemTitle()
            rebuildStatusMenu()
            return true
        }
    }

    private func nonEmptyToken(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }

    /// Восстановить audio snapshot только после terminal meeting.finished.
    private func releasePromotedMeetingDucking() {
        guard meetingInheritedDictationDucking else { return }
        meetingInheritedDictationDucking = false
        audioDuckingService.restoreAfterRecording()
    }
}
