/*
 main+HotkeyRecording.swift
 Расширение AgentAppDelegate: горячая клавиша, запуск/остановка записи
 и безопасная синхронизация владельца общего backend-рекордера.
*/

import AppKit
import Foundation

/// Чистая политика сопоставления общего backend-рекордера с hotkey-диктовкой.
/// Отсутствующий owner сохраняет legacy, а явный owner:null означает unmanaged.
enum HotkeyRecordingOwnershipPolicy {
    static func isForeignRecording(
        isRecording: Bool,
        owner: String?,
        ownerFieldPresent: Bool
    ) -> Bool {
        guard isRecording else { return false }
        if let owner {
            return owner != "dictation"
        }
        return ownerFieldPresent
    }

    static func representsLocalDictation(
        isRecording: Bool,
        owner: String?,
        ownerFieldPresent: Bool
    ) -> Bool {
        guard isRecording else { return false }
        if let owner {
            return owner == "dictation"
        }
        return !ownerFieldPresent
    }
}

/// Неизменяемый снимок lease общего рекордера. Он отделён от UI-состояния,
/// чтобы transport-ошибка start сначала подтверждалась backend, а не
/// угадывалась по локальному флагу `isRecording`.
struct RecordingStateSnapshot: Equatable {
    let isRecording: Bool
    let owner: String?
    let ownerFieldPresent: Bool
    let generationToken: String?
    let ownerRevision: Int?
    let startRequestID: String?
    let stateVerified: Bool

    init(
        isRecording: Bool,
        owner: String?,
        ownerFieldPresent: Bool,
        generationToken: String?,
        ownerRevision: Int? = nil,
        startRequestID: String? = nil,
        stateVerified: Bool
    ) {
        self.isRecording = isRecording
        self.owner = owner
        self.ownerFieldPresent = ownerFieldPresent
        self.generationToken = generationToken
        self.ownerRevision = ownerRevision
        self.startRequestID = startRequestID
        self.stateVerified = stateVerified
    }
}

/// Решение reconciliation после неоднозначного start. Не допускает stop или
/// присвоение записи, пока backend не доказал совпадение request ID и G1.
enum RecordingStartAmbiguityDecision: Equatable {
    case retryReconciliation
    case adoptExpectedOwner(generationToken: String?)
    case awaitPromotedMeeting(generationToken: String)
    case rejectAsIdleOrForeign
}

/// Чистая политика сопоставления потерянного start-ответа с backend lease.
/// Новый клиент всегда передаёт `expectedStartRequestID`; optional оставлен
/// лишь для unit-тестов и изолированной совместимости старого снимка.
enum RecordingStartAmbiguityPolicy {
    static func decide(
        snapshot: RecordingStateSnapshot,
        expectedOwner: String,
        expectedStartRequestID: String? = nil,
        allowsMeetingPromotion: Bool
    ) -> RecordingStartAmbiguityDecision {
        guard snapshot.stateVerified else {
            return .retryReconciliation
        }
        guard
            snapshot.isRecording,
            let generationToken = snapshot.generationToken,
            !generationToken.isEmpty
        else {
            return .rejectAsIdleOrForeign
        }
        if let expectedStartRequestID {
            guard
                snapshot.startRequestID == expectedStartRequestID,
                snapshot.ownerRevision != nil
            else {
                return .rejectAsIdleOrForeign
            }
        }
        if snapshot.owner == expectedOwner {
            return .adoptExpectedOwner(generationToken: generationToken)
        }
        if allowsMeetingPromotion,
           expectedOwner == "dictation",
           snapshot.owner == "meeting" {
            return .awaitPromotedMeeting(generationToken: generationToken)
        }
        return .rejectAsIdleOrForeign
    }
}

/// Сериализует физический start, включая IPC и владение audio-ducking snapshot.
/// NSLock нужен потому, что toggle стартует из detached task, а hold — с main.
final class RecordingStartGate: @unchecked Sendable {
    private let lock = NSLock()
    private var inFlight = false

    func tryAcquire() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !inFlight else { return false }
        inFlight = true
        return true
    }

    func release() {
        lock.lock()
        inFlight = false
        lock.unlock()
    }
}

extension AgentAppDelegate {

    // MARK: - Hotkey recording

    func handleRecordToggleRequest() {
        // C3a: во время быстрой заметки диктовка отвергается (взаимное
        // исключение — спека 2026-07-16-c3-quick-capture-design.md §2a).
        if quickCaptureActive {
            BackendToast.shared.show("Идёт быстрая заметка — сначала завершите её")
            return
        }
        if isProcessing {
            return
        }

        let now = Date().timeIntervalSince1970
        if now - lastToggleRequestAt < toggleDebounceSec {
            logger.warn("Игнорирую повторный toggle (debounce)")
            return
        }
        lastToggleRequestAt = now

        // State machine остаётся на MainActor, но конечный socket I/O выполняет
        // callAsync и suspend'ит actor. Синхронный callWithRecovery раньше
        // блокировал runloop более чем на 2 секунды (Sentry KRAB-EAR-AGENT-3).
        let wasRecordingLocally = isRecording
        Task { @MainActor [weak self] in
            await self?.performRecordToggle(wasRecordingLocally: wasRecordingLocally)
        }
    }

    /// Выполнить actor-isolated state machine; каждый IPC-вызов внутри —
    /// асинхронный и не блокирует главный run loop.
    func performRecordToggle(wasRecordingLocally: Bool) async {
        // Второй осмысленный toggle во время async start — это stop-intent.
        // Нельзя просто отменить client Task: backend мог уже открыть G1 и
        // только ещё не успеть вернуть opaque-token.
        if recordingStartInFlight {
            recordingStopRequestedDuringStart = true
            logger.info(
                "Toggle получен во время start_recording; " +
                "остановлю generation сразу после ответа"
            )
            return
        }

        // После транспортной ошибки нельзя начинать G2: предыдущий G1 мог
        // успешно открыться, но его ответ потерялся. Повторный toggle означает
        // только намерение остановить доказанный собственный lease.
        if recordingStartAmbiguous,
           let requestID = recordingStartAmbiguousRequestID {
            guard recordingStartGate.tryAcquire() else {
                recordingStopRequestedDuringAmbiguousStart = true
                return
            }
            recordingStartInFlight = true
            recordingStopRequestedDuringAmbiguousStart = true
            defer {
                recordingStartInFlight = false
                recordingStopRequestedDuringStart = false
                recordingStartGate.release()
            }
            await reconcileAmbiguousDictationStart(requestID: requestID)
            return
        }

        // После non-terminal stop публичный recorder-флаг может уже стать false,
        // но token G1 остаётся единственным recovery-handle. Не сверяем его с
        // общим state и не открываем G2 — повторяем stop с новым локальным budget.
        if recordingStopRecoveryPending
            && activeGenerationOwner == "dictation" {
            await stopRecording()
            return
        }

        let (backendRecording, backendOwner, ownerFieldPresent, stateVerified) =
            await syncRecordingStateWithBackend()
        // Ошибку IPC нельзя выдавать за старый backend без owner: в таком
        // «legacy»-виде promoted meeting снова можно было бы остановить тапом.
        // Пока снимок не подтверждён, fail-safe запрещает и start, и stop.
        if !stateVerified {
            logger.warn("Не удалось подтвердить владельца записи — toggle отклонён")
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Не удалось проверить режим записи — запись не тронута."
                )
            }
            return
        }
        if backendRecording != wasRecordingLocally {
            logger.warn(
                "Десинхрон состояния записи: local=\(wasRecordingLocally), " +
                "backend=\(backendRecording), owner=\(backendOwner ?? "nil"), " +
                "ownerFieldPresent=\(ownerFieldPresent)"
            )
        }

        // Owner-гейт обязан стоять ДО любой ветки stop: это закрывает не только
        // потерянный локальный флаг, но и dictation→meeting promote, при котором
        // снимок wasRecordingLocally законно остаётся true.
        if HotkeyRecordingOwnershipPolicy.isForeignRecording(
            isRecording: backendRecording,
            owner: backendOwner,
            ownerFieldPresent: ownerFieldPresent
        ) {
            let human: String
            switch backendOwner {
            case "meeting":
                human = "встреча"
            case "quick_capture":
                human = "быстрая заметка"
            default:
                human = "запись другого режима"
            }
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Идёт \(human) — запись не тронута."
                )
            }
            return
        }

        // Если локально считалось, что пишем, но backend уже idle — не стартуем новую
        // запись этим же нажатием. Сначала фиксируем состояние, следующий toggle начнёт запись явно.
        if wasRecordingLocally && !backendRecording {
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Запись уже остановлена в backend. Состояние синхронизировано."
                )
            }
            return
        }

        // После owner-гейта выше здесь остаётся только своя/legacy-диктовка.
        if !wasRecordingLocally && backendRecording {
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Найден рассинхрон записи. Сначала завершаю зависшую сессию."
                )
            }
            await stopRecording()
            return
        }

        if wasRecordingLocally {
            await stopRecording()
        } else {
            await startRecording()
        }
    }

    /// Прочитать backend-state без предположений о его связи с текущим UI.
    /// Этот read-only IPC можно безопасно повторять после потерянного ответа
    /// side-effect start_recording; сам start повторять нельзя.
    func recordingStateSnapshot() async -> RecordingStateSnapshot {
        guard
            let stateResponse = try? await callAsyncWithRecovery(
                method: "get_recording_state",
                params: [:],
                timeoutSec: IPCClient.quickTimeoutSec
            ),
            let state = stateResponse["result"] as? [String: Any]
        else {
            return RecordingStateSnapshot(
                isRecording: isRecording,
                owner: nil,
                ownerFieldPresent: false,
                generationToken: nil,
                stateVerified: false
            )
        }

        let backendRecording = (state["is_recording"] as? Bool) ?? false
        let backendOwner = state["owner"] as? String
        let ownerFieldPresent = state.keys.contains("owner")
        let rawGenerationToken = state["generation_token"] as? String
        let generationToken = rawGenerationToken?.isEmpty == false
            ? rawGenerationToken
            : nil
        let rawStartRequestID = state["start_request_id"] as? String
        let startRequestID = rawStartRequestID?.isEmpty == false
            ? rawStartRequestID
            : nil
        let ownerRevision = state["owner_revision"] as? Int
        return RecordingStateSnapshot(
            isRecording: backendRecording,
            owner: backendOwner,
            ownerFieldPresent: ownerFieldPresent,
            generationToken: generationToken,
            ownerRevision: ownerRevision,
            startRequestID: startRequestID,
            stateVerified: true
        )
    }

    /// Возвращает флаг записи, владельца и наличие owner в IPC-контракте.
    /// Старый backend не отдаёт ключ, новый всегда отдаёт строку либо null.
    func syncRecordingStateWithBackend() async -> (
        recording: Bool,
        owner: String?,
        ownerFieldPresent: Bool,
        stateVerified: Bool
    ) {
        let snapshot = await recordingStateSnapshot()
        guard snapshot.stateVerified else {
            return (isRecording, nil, false, false)
        }

        let backendRecording = snapshot.isRecording
        let backendOwner = snapshot.owner
        let ownerFieldPresent = snapshot.ownerFieldPresent
        // Общий backend-флаг нельзя слепо зеркалить в hotkey-состояние:
        // при явном meeting/quick_capture второй тап иначе остановит чужую запись.
        // Только ОТСУТСТВУЮЩИЙ ключ сохраняет auto-heal старого backend;
        // owner:null нового backend — достижимая unmanaged/pending запись.
        let backendRepresentsLocalDictation =
            HotkeyRecordingOwnershipPolicy.representsLocalDictation(
                isRecording: backendRecording,
                owner: backendOwner,
                ownerFieldPresent: ownerFieldPresent
            )
        if backendRepresentsLocalDictation != isRecording {
            isRecording = backendRepresentsLocalDictation
            refreshStatusItemTitle()
            rebuildStatusMenu()
        }
        return (backendRecording, backendOwner, ownerFieldPresent, true)
    }

    func startRecording() async {
        guard recordingStartGate.tryAcquire() else {
            recordingStopRequestedDuringStart = true
            logger.warn(
                "start_recording уже выполняется — повтор трактую как stop-intent"
            )
            return
        }
        let startRequestID = UUID().uuidString
        recordingStartInFlight = true
        recordingStopRequestedDuringStart = false
        recordingStartRequestID = startRequestID
        recordingStartAmbiguous = false
        recordingStartAmbiguousRequestID = nil
        recordingStopRequestedDuringAmbiguousStart = false
        defer {
            recordingStartInFlight = false
            recordingStopRequestedDuringStart = false
            recordingStartGate.release()
        }

        captureRecordingTargetApp()
        let targetBundle = recordingTargetApp?.bundleIdentifier ?? "nil"
        logger.info("Старт записи. targetApp=\(targetBundle)")
        do {
            // Сначала приглушаем системный звук, чтобы в запись не попадали внешние звуки.
            // В режиме mic принудительно используем mute (100), иначе даже 25%/50%
            // может физически пробиваться в микрофон и давать ложную транскрипцию.
            let effectiveDuckingPercent: Int
            if settings.captureSourceMode == "mic" {
                effectiveDuckingPercent = 100
            } else {
                effectiveDuckingPercent = settings.audioDuckingPercent
            }
            audioDuckingService.duckForRecording(
                enabled: settings.audioDuckingEnabled,
                duckPercent: effectiveDuckingPercent
            )
            // start_recording имеет side-effect. В отличие от read-only query
            // его нельзя прогонять через generic recovery: повтор получит
            // новый IPC-id и может скрыть уже открытый G1.
            let response = try await ipcClient.callAsync(
                method: "start_recording",
                params: [
                    "source": "dictation",
                    "start_request_id": startRequestID,
                ],
                timeoutSec: 10
            )
            let result = response["result"] as? [String: Any]
            let status = (result?["status"] as? String) ?? "recording"
            let rawGenerationToken = result?["generation_token"] as? String
            let generationToken = (
                rawGenerationToken?.isEmpty == false
                    ? rawGenerationToken
                    : nil
            )
            let returnedStartRequestID = result?["start_request_id"] as? String
            let ownerRevision = result?["owner_revision"] as? Int
            if let returnedStartRequestID,
               !returnedStartRequestID.isEmpty,
               returnedStartRequestID != startRequestID {
                // Валидный, но чужой request ID не доказывает владение. Через
                // reconciliation проверяем именно исходный ID, ничего не стопая.
                throw IPCError.invalidResponse
            }
            if consumePendingMeetingPromotion(startToken: generationToken) {
                recordingStartRequestID = nil
                logger.info(
                    "Поздний dictation start-response передан живой встрече"
                )
                return
            }
            if status == "already_recording" {
                // already_recording — не наш успешный старт: принятие его за успех
                // позволяло следующему тапу остановить чужую встречу или заметку.
                logger.warn("start_recording: запись уже идёт — не перехватываем")
                recordingStartRequestID = nil
                audioDuckingService.restoreAfterRecording()
                notify(
                    title: "Krab Ear",
                    body: "Запись уже идёт — новая не начата."
                )
                return
            }
            if status != "recording" {
                logger.warn("start_recording вернул неожиданный статус: \(status)")
                recordingStartRequestID = nil
                audioDuckingService.restoreAfterRecording()
                notify(
                    title: "Krab Ear",
                    body: "Не удалось начать запись: неожиданный статус backend."
                )
                return
            }
            activeGenerationToken = generationToken
            activeGenerationOwner = "dictation"
            activeGenerationOwnerRevision = ownerRevision
            activeGenerationStartRequestID = returnedStartRequestID
            recordingStopRecoveryPending = false
            recordingStartRequestID = nil
            if activeGenerationToken == nil {
                // Совместимость со старым backend: stop останется tokenless,
                // но живой R2 backend всегда обязан вернуть opaque-token.
                logger.warn(
                    "start_recording: backend не вернул generation_token; " +
                    "использую legacy stop-контракт"
                )
            }
            isRecording = true
            lastPreviewTranslationSource = ""
            lastPreviewTranslationText = ""
            lastPreviewTranslationAt = 0
            if recordingStopRequestedDuringStart {
                guard
                    let generationToken,
                    !generationToken.isEmpty,
                    ownerRevision != nil
                else {
                    // Это именно отмена pending start, а не обычный legacy
                    // stop. Без полного lease source-only компенсация способна
                    // остановить уже повышенную или чужую запись.
                    recordingStartAmbiguous = true
                    recordingStartAmbiguousRequestID = startRequestID
                    recordingStartRequestID = startRequestID
                    recordingStopRequestedDuringAmbiguousStart = true
                    notify(
                        title: "Krab Ear",
                        body: "Отмена записи ожидает подтверждения lease backend."
                    )
                    return
                }
                logger.info(
                    "start_recording завершён после release/toggle; " +
                    "немедленно останавливаю выданный generation"
                )
                await stopRecording()
                return
            }
            startRealtimeOverlayPolling()
            playStartSoundIfEnabled()
            refreshStatusItemTitle()
            rebuildStatusMenu()
        } catch {
            logger.error("Ошибка start_recording: \(error.localizedDescription)")
            if isAmbiguousStartError(error) {
                recordingStartAmbiguous = true
                recordingStartAmbiguousRequestID = startRequestID
                recordingStopRequestedDuringAmbiguousStart = (
                    recordingStopRequestedDuringAmbiguousStart
                    || recordingStopRequestedDuringStart
                )
                await reconcileAmbiguousDictationStart(requestID: startRequestID)
                return
            }
            // Ducking включается до IPC; любой отказ старта обязан восстановить
            // системный звук, а не только отдельный already_recording.
            recordingStartRequestID = nil
            audioDuckingService.restoreAfterRecording()
            notify(
                title: "Krab Ear",
                body: "Не удалось начать запись: \(error.localizedDescription)"
            )
        }
    }

    /// Только transport/декодирование оставляют неопределённость о side-effect
    /// start. Явный backend-отказ уже доказывает, что lease не был выдан.
    /// Общая классификация transport-неоднозначности для диктовки и Quick
    /// Capture. Явный backend-отказ не запускает reconciliation.
    func isAmbiguousStartError(_ error: Error) -> Bool {
        guard let ipcError = error as? IPCError else {
            return false
        }
        if case .backendError = ipcError {
            return false
        }
        return true
    }

    /// Безопасно сопоставить потерянный dictation start с backend lease.
    /// Функция не отправляет stop без точного request ID, token и revision.
    private func reconcileAmbiguousDictationStart(requestID: String) async {
        guard recordingStartAmbiguousRequestID == requestID else {
            return
        }
        recordingStopRequestedDuringAmbiguousStart = (
            recordingStopRequestedDuringAmbiguousStart
            || recordingStopRequestedDuringStart
        )

        let snapshot = await recordingStateSnapshot()
        let decision = RecordingStartAmbiguityPolicy.decide(
            snapshot: snapshot,
            expectedOwner: "dictation",
            expectedStartRequestID: requestID,
            allowsMeetingPromotion: true
        )
        switch decision {
        case .retryReconciliation:
            // Сохраняем блокировку нового start и audio ducking: G1 всё ещё
            // может существовать, а restore позволил бы ему записывать звук.
            recordingStartAmbiguous = true
            recordingStartRequestID = requestID
            logger.warn(
                "Не удалось подтвердить результат start_recording; " +
                "повторный toggle выполнит только reconciliation"
            )
            notify(
                title: "Krab Ear",
                body: "Старт записи не подтверждён. Повторите переключение после восстановления backend."
            )
        case let .adoptExpectedOwner(generationToken):
            guard
                let generationToken,
                !generationToken.isEmpty,
                let ownerRevision = snapshot.ownerRevision
            else {
                // Политика уже проверяет это для нового контракта; guard
                // остаётся барьером против будущего ослабления policy.
                recordingStartAmbiguous = true
                return
            }
            activeGenerationToken = generationToken
            activeGenerationOwner = "dictation"
            activeGenerationOwnerRevision = ownerRevision
            activeGenerationStartRequestID = requestID
            recordingStopRecoveryPending = false
            recordingStartAmbiguous = false
            recordingStartAmbiguousRequestID = nil
            recordingStartRequestID = nil
            isRecording = true
            lastPreviewTranslationSource = ""
            lastPreviewTranslationText = ""
            lastPreviewTranslationAt = 0

            let stopRequested = (
                recordingStopRequestedDuringAmbiguousStart
                || recordingStopRequestedDuringStart
            )
            recordingStopRequestedDuringAmbiguousStart = false
            if stopRequested {
                logger.info(
                    "Потерянный start подтверждён своим G1; " +
                    "исполняю token/revision-bound stop-intent"
                )
                await stopRecording()
                return
            }
            startRealtimeOverlayPolling()
            refreshStatusItemTitle()
            rebuildStatusMenu()
        case let .awaitPromotedMeeting(generationToken):
            // Backend уже подтвердил owner=meeting для того же request ID.
            // Сохраняем audio ducking за встречей и фехтуем handoff G1-token.
            // Не вызываем consume здесь: meeting_start callback мог ещё быть
            // в пути, и pending-token служит его единственным доказательством.
            pendingMeetingPromotionToken = generationToken
            meetingInheritedDictationDucking = true
            recordingStartAmbiguous = false
            recordingStartAmbiguousRequestID = nil
            recordingStartRequestID = nil
            recordingStopRequestedDuringAmbiguousStart = false
            if meetingPanelController?.hasAcceptedGenerationToken(
                generationToken
            ) == true {
                // Callback meeting_start уже принял именно G1 до нашего
                // read-only reconcile. Панель — точное подтверждение, поэтому
                // pending больше не должен блокировать будущие переходы.
                pendingMeetingPromotionToken = nil
            }
        case .rejectAsIdleOrForeign:
            if snapshot.isRecording && snapshot.owner == "dictation" {
                // Same-source snapshot с чужим/legacy request ID не является
                // доказательством нашего G1. Оставляем fail-safe блокировку,
                // иначе следующий toggle мог бы открыть G2 поверх него.
                recordingStartAmbiguous = true
                recordingStartAmbiguousRequestID = requestID
                recordingStartRequestID = requestID
                logger.warn(
                    "Найден неподтверждённый dictation lease; " +
                    "новый start запрещён до безопасной сверки"
                )
                notify(
                    title: "Krab Ear",
                    body: "Запись диктовки не подтверждена. Повторите переключение после проверки backend."
                )
                return
            }
            // Подтверждённый idle/foreign snapshot означает, что G1 этого
            // request ID не существует. Ничего физически не останавливаем.
            recordingStartAmbiguous = false
            recordingStartAmbiguousRequestID = nil
            recordingStartRequestID = nil
            recordingStopRequestedDuringAmbiguousStart = false
            audioDuckingService.restoreAfterRecording()
            notify(
                title: "Krab Ear",
                body: "Старт записи не подтвердился; текущая запись не тронута."
            )
            refreshStatusItemTitle()
            rebuildStatusMenu()
        }
    }

    func stopRecording() async {
        logger.info("Остановка записи запрошена")
        stopRealtimeOverlayPolling()
        isProcessing = true
        refreshStatusItemTitle()
        rebuildStatusMenu()

        let stopToken = activeGenerationToken
        let stopOwner = "dictation"
        let stopOwnerRevision = activeGenerationOwnerRevision
        var params: [String: Any] = [
            "source": stopOwner,
            "quality_profile": settings.qualityProfile,
            "cleanup_profile": settings.cleanupProfile,
            "translation_mode": settings.translationMode,
            "translation_style": settings.translationStyle,
            "translate_and_paste": settings.translateAndPaste,
        ]
        if let stopToken, !stopToken.isEmpty {
            params["generation_token"] = stopToken
        }
        // Новый backend делает этот stop строгим CAS только при полном lease.
        // Старый backend без revision сохраняет legacy-путь совместимости.
        if let stopToken, !stopToken.isEmpty,
           let stopOwnerRevision {
            params["expected_owner_revision"] = stopOwnerRevision
        }

        let request = RecordingStopRequest(
            method: "stop_recording",
            params: params,
            timeoutSec: 120
        )
        let client = ipcClient
        // 2026-08-03: на время финализации приостанавливаем детектор зависания.
        // Backend занят STT и не отвечает на ping; порог сторожа — 6 с
        // (3 × 2), а финализация длинной диктовки занимает десятки секунд
        // (живой замер: 32-секундная запись → STT 46.17 с). Без паузы сторож
        // убивал backend ПОСРЕДИ транскрибации, и запись не доезжала ни до
        // вставки, ни до истории — короткие диктовки проходили, длинные не
        // проходили никогда. defer гарантирует снятие на ВСЕХ путях выхода,
        // включая бросок и ранний return: залипшая пауза оставила бы реальное
        // зависание backend'а без сторожа.
        let monitor = healthMonitor
        await monitor?.suspend(.finalizingRecording)
        // Тот же щит для wake-word эскалации: пауза `.recording` снимается уже
        // в момент запроса остановки, и в окно финализации поллер успевал
        // увидеть wedged и попросить принудительный рестарт backend'а.
        wakeWordPoller?.pause(.finalizing)
        defer {
            Task { await monitor?.resume(.finalizingRecording) }
            Task { @MainActor in self.wakeWordPoller?.resume(.finalizing) }
        }
        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { repeatedRequest in
                try await client.callAsync(
                    method: repeatedRequest.method,
                    params: repeatedRequest.params,
                    timeoutSec: repeatedRequest.timeoutSec
                )
            }
        )

        // isProcessing описывает только этот bounded IPC-цикл. При recovery
        // сама запись остаётся локально «активной», чтобы следующий тап снова
        // вызвал stop с тем же token.
        isProcessing = false

        let routeStillMatches = (
            activeGenerationOwner == stopOwner
            && activeGenerationToken == stopToken
            && activeGenerationOwnerRevision == stopOwnerRevision
        )
        guard routeStillMatches else {
            logger.warn(
                "Поздний stop-ответ проигнорирован: локальный generation уже сменился"
            )
            refreshStatusItemTitle()
            rebuildStatusMenu()
            return
        }

        switch outcome.decision {
        case .recoveryPending:
            retainDictationStopRecovery(
                result: outcome.result,
                message: "Аудио ещё восстанавливается. Нажмите остановку ещё раз; при необходимости перезапустите backend."
            )
            return
        case .giveUpRescuePending:
            // `unknown_generation` — АВТОРИТЕТНЫЙ ответ backend «такого
            // поколения не существует», а не транспортная неопределённость.
            // Удерживать под него recovery-route нечем: короткое замыкание в
            // handleRecordToggleRequest повторяло бы stop с тем же мёртвым
            // токеном на каждый тап хоткея, а сверка с backend стоит ниже и
            // недостижима — диктовка залипала бы до рестарта агента с
            // неснятым ducking. Rescue отработает на следующем старте backend.
            if (outcome.result?["status"] as? String) == "unknown_generation" {
                clearDictationStopRecovery(
                    result: outcome.result,
                    message: "Поколение записи уже закрыто backend. Запись восстановится при следующем запуске — повторять остановку не нужно."
                )
                return
            }
            retainDictationStopRecovery(
                result: outcome.result,
                message: "Ответ остановки не подтверждён. Запись восстановится при следующем запуске; можно повторить остановку сейчас."
            )
            return
        case .finalizationSlow:
            retainDictationStopRecovery(
                result: outcome.result,
                message: "Финализация затянулась — результат появится в истории. Повторная остановка безопасна."
            )
            return
        case .foreignOwner:
            logger.warn("stop_recording: generation принадлежит другому режиму")
            activeGenerationToken = nil
            activeGenerationOwner = nil
            activeGenerationOwnerRevision = nil
            activeGenerationStartRequestID = nil
            recordingStopRecoveryPending = false
            isRecording = false
            recordingTargetApp = nil
            notify(
                title: "Krab Ear",
                body: "Идёт другая запись — диктовка её не остановила."
            )
            refreshStatusItemTitle()
            rebuildStatusMenu()
            return
        case .retry, .retryRecorderStop, .pollAgain:
            // Эти решения полностью потребляются coordinator и наружу не выходят.
            logger.error("RecordingStopCoordinator вернул промежуточное решение")
            retainDictationStopRecovery(
                result: outcome.result,
                message: "Остановка не подтверждена; повторите действие."
            )
            return
        case .surfaceAsIs:
            guard outcome.hasTerminalResponse, let result = outcome.result else {
                let errorText = outcome.error?.localizedDescription
                    ?? "backend не вернул терминальный ответ"
                logger.error("stop_recording не подтверждён: \(errorText)")
                retainDictationStopRecovery(
                    result: outcome.result,
                    message: "Ответ остановки не получен. Токен сохранён — нажмите остановку ещё раз."
                )
                return
            }

            let status = (result["status"] as? String) ?? "unknown"
            let historyId = result["history_id"] as? String
            let text = (result["text"] as? String) ?? ""
            let originalText = (result["original_text"] as? String) ?? text
            let translatedText = (result["translated_text"] as? String) ?? ""
            let translationMode = (result["translation_mode"] as? String) ?? "off"
            let translationStatus = (result["translation_status"] as? String) ?? "not_requested"
            let translateAndPaste = (result["translate_and_paste"] as? Bool) ?? false
            logger.info("Ответ stop_recording: status=\(status), text_len=\(text.count), history_id=\(historyId ?? "nil")")

            switch status {
            case "ok":
                if translationMode != "off" && translationStatus != "ok" && translateAndPaste {
                    notify(
                        title: "Krab Ear",
                        body: "Перевод сейчас недоступен (\(translationStatus)). Вставлен оригинальный текст."
                    )
                }
                lastResult = LastTranscriptionSnapshot(
                    finalText: text,
                    originalText: originalText,
                    translatedText: translatedText,
                    historyId: historyId,
                    translationMode: translationMode,
                    translationStatus: translationStatus
                )
                handleTranscriptionResult(text: text, historyId: historyId)
                // Обновляем индикатор STT движка после успешной транскрибации.
                historyPanel?.fetchAndUpdateSTTEngineLabel()
            case "already_stopped":
                // Идемпотентный stop: backend уже в idle, лишние уведомления пользователю не нужны.
                logger.info("stop_recording: backend уже idle (already_stopped)")
            case "empty_audio":
                logger.warn("stop_recording вернул empty_audio")
                notify(title: "Krab Ear", body: "Аудио пустое, попробуйте ещё раз")
            case "empty_text":
                logger.warn("stop_recording вернул empty_text")
                recoverFromPreviewFallback(reason: "Финальная транскрибация пустая") { recovered in
                    if !recovered {
                        self.notify(title: "Krab Ear", body: "Речь не распознана")
                    }
                }
            default:
                logger.warn("stop_recording вернул неожиданный статус: \(status)")
                recoverFromPreviewFallback(reason: "Неожиданный статус stop: \(status)") { recovered in
                    if !recovered {
                        self.notify(title: "Krab Ear", body: "Неожиданный статус: \(status)")
                    }
                }
            }

            // Terminal cleanup выполняется ПОСЛЕ обработки текста: target app
            // нужен paste-пайплайну. Fence выше гарантирует, что поздняя G1 не
            // очистит состояние более нового поколения.
            activeGenerationToken = nil
            activeGenerationOwner = nil
            activeGenerationOwnerRevision = nil
            activeGenerationStartRequestID = nil
            recordingStopRecoveryPending = false
            isRecording = false
            recordingTargetApp = nil
            audioDuckingService.restoreAfterRecording()
            refreshStatusItemTitle()
            rebuildStatusMenu()
        }
    }

    /// Отпустить G1, когда backend авторитетно сообщил, что поколения нет.
    ///
    /// Симметрична `retainDictationStopRecovery`, но для случая, когда
    /// удерживать recovery-route бессмысленно: токен мёртв, повторный stop
    /// вернёт ровно тот же `unknown_generation`. Чистим состояние по образцу
    /// `.rejectAsIdleOrForeign` — включая ducking, иначе системный звук
    /// остался бы приглушённым до перезапуска агента.
    private func clearDictationStopRecovery(
        result: [String: Any]?,
        message: String
    ) {
        activeGenerationToken = nil
        activeGenerationOwner = nil
        activeGenerationOwnerRevision = nil
        recordingStopRecoveryPending = false
        isRecording = false
        isProcessing = false
        audioDuckingService.restoreAfterRecording()
        let rawPreview = (result?["preview_text"] as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let preview = rawPreview.isEmpty
            ? ""
            : "\nПревью: \(String(rawPreview.prefix(140)))"
        notify(title: "Krab Ear", body: message + preview)
        refreshStatusItemTitle()
        rebuildStatusMenu()
    }

    /// Сохранить G1 как явный recovery-route и показать безопасное превью.
    private func retainDictationStopRecovery(
        result: [String: Any]?,
        message: String
    ) {
        activeGenerationOwner = "dictation"
        recordingStopRecoveryPending = true
        isRecording = true
        let rawPreview = (result?["preview_text"] as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let preview = rawPreview.isEmpty
            ? ""
            : "\nПревью: \(String(rawPreview.prefix(140)))"
        notify(title: "Krab Ear", body: message + preview)
        refreshStatusItemTitle()
        rebuildStatusMenu()
    }
}
