/*
 main+QuickCapture.swift
 C3a (спека 2026-07-16-c3-quick-capture-design.md §2-§3): быстрая голосовая
 заметка — запись БЕЗ вставки в активное окно; результат уходит в history +
 коллекцию «Быстрые заметки» (лениво создаётся при первом использовании),
 opt-in дублирование в Notes/Obsidian наполняется в Task 3.

 Переиспользует штатный record-флоу (start_recording/stop_recording +
 start/stopRealtimeOverlayPolling — там живут wake-word пауза и оверлей).
 Финальный paste НЕ вызывается — заметка никогда не входит в обычный
 paste-пайплайн диктовки (см. main+PasteHandling.swift), streaming-paste
 подавлен отдельным гардом в main+RealtimeOverlay.swift.

 C3b Task 2 (спека 2026-07-16-c3b-scratchpad-panel.md): точки входа панели-
 скретчпада (QuickCapturePanelController) живут ЗДЕСЬ, не в отдельном
 main+QuickCapturePanel.swift (план предполагал такой файл — на практике
 проще встроить в уже существующий main+QuickCapture.swift, см. отчёт волны).
 Единственный владелец панели — quickCapturePanelController (main.swift),
 лениво создаётся ensureQuickCapturePanelController().
*/

import AppKit

/// Чистая политика публикации recovery-route осиротевшего quick start.
///
/// `activeToken == nil` сам по себе не означает пустой маршрут: новый Q2
/// может уже иметь UI-epoch, но ещё ждать backend-token.
enum QuickCaptureOrphanRecoveryPolicy {
    static func canAdopt(
        activeToken: String?,
        startRequestPending: Bool,
        quickCaptureActive: Bool
    ) -> Bool {
        activeToken == nil
            && !startRequestPending
            && !quickCaptureActive
    }
}

extension AgentAppDelegate {
    static let quickCaptureCollectionName = "Быстрые заметки"

    @objc func onQuickCaptureToggle() {
        // C3a ревью F2a: debounce переиспользует ТО ЖЕ поле, что диктовка
        // (main+HotkeyRecording.swift:handleRecordToggleRequest) — оба хоткея
        // переключают состояние ОДНОГО общего recorder'а (см. F1 NOTES), так что
        // общий таймер защищает от гонки старт/стоп независимо от того, какой
        // именно "переключатель записи" сработал повторно (не только авто-repeat
        // ОДНОГО и того же хоткея — двойной тап Cmd+Shift+N сразу после Right
        // Option тоже гасится, что логично при общем recorder'е).
        let now = Date().timeIntervalSince1970
        if now - lastToggleRequestAt < toggleDebounceSec {
            logger.warn("Игнорирую повторный quick capture toggle (debounce)")
            return
        }
        lastToggleRequestAt = now

        // Pending/ambiguous Q1 остаётся самостоятельным маршрутом даже когда
        // пользователь уже отменил его визуально. Новый Q2 до reconciliation
        // мог бы перезаписать единственную ссылку на lease старого старта.
        if quickCaptureActive
            || quickCaptureStartRequestID != nil
            || quickCaptureStartAmbiguousRequestID != nil {
            stopQuickCapture()
            return
        }
        // Взаимное исключение: диктовка/обработка. Встреча отсекается своим
        // собственным гардом (main+MeetingPanel.swift); чужая активная запись
        // backend'а (если она всё же есть) отразится в status != "recording"
        // на старте ниже.
        if isRecording
            || isProcessing
            || recordingStartInFlight
            || recordingStartAmbiguous {
            BackendToast.shared.show("Уже идёт запись — заметка недоступна")
            return
        }
        guard recordingStartGate.tryAcquire() else {
            BackendToast.shared.show("Предыдущий старт ещё подтверждается")
            return
        }
        quickCaptureStartGateHeld = true
        quickCaptureActive = true
        let startRequestID = UUID()
        quickCaptureStartRequestID = startRequestID
        quickCaptureStartAmbiguousRequestID = nil
        quickCaptureStopRequestedStartID = nil
        quickCaptureStopRequestedDuringAmbiguousStart = false
        rebuildStatusMenu()
        Task { @MainActor [weak self] in
            await self?.startQuickCapture(requestID: startRequestID)
        }
    }

    /// Выполнить единственный side-effect start Quick Capture. Ошибку канала
    /// не повторяем: только read-only reconciliation вправе следовать за ней.
    private func startQuickCapture(requestID: UUID) async {
        defer { releaseQuickCaptureStartGateIfHeld() }
        do {
            let response = try await ipcClient.callAsync(
                method: "start_recording",
                params: [
                    "source": "quick_capture",
                    "start_request_id": requestID.uuidString,
                ],
                timeoutSec: 10
            )
            let result = response["result"] as? [String: Any] ?? [:]
            let returnedStartRequestID = result["start_request_id"] as? String
            if let returnedStartRequestID,
               !returnedStartRequestID.isEmpty,
               returnedStartRequestID != requestID.uuidString {
                throw IPCError.invalidResponse
            }
            await handleQuickCaptureStartResponse(
                result,
                requestID: requestID
            )
        } catch {
            guard
                quickCaptureStartRequestID == requestID
                    || quickCaptureStartAmbiguousRequestID == requestID
            else {
                return
            }
            if isAmbiguousStartError(error) {
                quickCaptureStartAmbiguousRequestID = requestID
                quickCaptureStopRequestedDuringAmbiguousStart = (
                    quickCaptureStopRequestedDuringAmbiguousStart
                    || quickCaptureStopRequestedStartID == requestID
                )
                await reconcileAmbiguousQuickCaptureStart(requestID: requestID)
                return
            }
            clearQuickCaptureStartAttempt(requestID: requestID)
            BackendToast.shared.show("Не удалось начать заметку")
        }
    }

    /// Разобрать единственный полученный ответ start и применить его только к
    /// текущему Q1. Отменённый Q1 компенсируется исключительно полным lease.
    private func handleQuickCaptureStartResponse(
        _ result: [String: Any],
        requestID: UUID
    ) async {
        let status = result["status"] as? String ?? ""
        let rawGenerationToken = result["generation_token"] as? String
        let generationToken = rawGenerationToken?.isEmpty == false
            ? rawGenerationToken
            : nil
        let ownerRevision = result["owner_revision"] as? Int
        let ownsStartRequest = quickCaptureStartRequestID == requestID

        guard status == "recording" else {
            guard ownsStartRequest else { return }
            clearQuickCaptureStartAttempt(requestID: requestID)
            let message = status == "already_recording"
                ? "Микрофон занят другой записью"
                : "Не удалось начать заметку"
            BackendToast.shared.show(message)
            return
        }

        let stopRequested = (
            quickCaptureStopRequestedStartID == requestID
            || quickCaptureStopRequestedDuringAmbiguousStart
        )
        guard ownsStartRequest && quickCaptureActive && !stopRequested else {
            if ownsStartRequest {
                quickCaptureStartRequestID = nil
            }
            await stopOrphanQuickCapture(
                generationToken: generationToken,
                ownerRevision: ownerRevision,
                originRequestID: requestID
            )
            return
        }

        quickCaptureStartRequestID = nil
        quickCaptureStartAmbiguousRequestID = nil
        quickCaptureStopRequestedStartID = nil
        quickCaptureStopRequestedDuringAmbiguousStart = false
        activeGenerationToken = generationToken
        activeGenerationOwner = "quick_capture"
        activeGenerationOwnerRevision = ownerRevision
        activeGenerationStartRequestID = result["start_request_id"] as? String
        recordingStopRecoveryPending = false
        if generationToken == nil {
            // Прямой legacy-ответ допускается только для обычной записи. В
            // cancel/ambiguity tokenless компенсация ниже жёстко запрещена.
            logger.warn(
                "quick_capture start: backend не вернул generation_token; " +
                "обычная остановка останется legacy-совместимой"
            )
        }
        startRealtimeOverlayPolling()
        BackendToast.shared.show("Быстрая заметка: запись…")
        await showQuickCapturePanelIfEnabled()
    }

    /// Отпустить общий start-gate ровно один раз после ответа либо snapshot.
    private func releaseQuickCaptureStartGateIfHeld() {
        guard quickCaptureStartGateHeld else { return }
        quickCaptureStartGateHeld = false
        recordingStartGate.release()
    }

    /// Снять только UI-ссылки конкретного Q1. Глобальный lease другого режима
    /// намеренно не меняется: он может быть опубликован поздним meeting-start.
    private func clearQuickCaptureStartAttempt(requestID: UUID) {
        if quickCaptureStartRequestID == requestID {
            quickCaptureStartRequestID = nil
        }
        if quickCaptureStartAmbiguousRequestID == requestID {
            quickCaptureStartAmbiguousRequestID = nil
        }
        if quickCaptureStopRequestedStartID == requestID {
            quickCaptureStopRequestedStartID = nil
        }
        quickCaptureStopRequestedDuringAmbiguousStart = false
        quickCaptureActive = false
        isProcessing = false
        refreshStatusItemTitle()
        rebuildStatusMenu()
    }

    func stopQuickCapture() {
        stopRealtimeOverlayPolling()
        // Pending start не имеет доказанного lease. Фиксируем отмену Q1, но
        // никогда не отправляем source-only stop: он мог бы остановить meeting
        // или dictation, успевшие занять общий recorder.
        if let requestID = quickCaptureStartRequestID
            ?? quickCaptureStartAmbiguousRequestID {
            quickCaptureStopRequestedStartID = requestID
            quickCaptureStopRequestedDuringAmbiguousStart = (
                quickCaptureStartAmbiguousRequestID == requestID
            )
            quickCaptureActive = false
            isProcessing = true
            refreshStatusItemTitle()
            rebuildStatusMenu()
            if quickCaptureStartAmbiguousRequestID == requestID,
               !quickCaptureStartGateHeld {
                Task { @MainActor [weak self] in
                    await self?.reconcileAmbiguousQuickCaptureStart(
                        requestID: requestID
                    )
                }
            }
            return
        }

        guard activeGenerationOwner == "quick_capture" else {
            isProcessing = false
            refreshStatusItemTitle()
            rebuildStatusMenu()
            return
        }
        isProcessing = true
        refreshStatusItemTitle()
        rebuildStatusMenu()
        let stopToken = activeGenerationToken
        let stopOwnerRevision = activeGenerationOwnerRevision
        Task { @MainActor [weak self] in
            await self?.performQuickCaptureStop(
                stopToken: stopToken,
                stopOwnerRevision: stopOwnerRevision
            )
        }
    }

    /// Остановить живую заметку через общий bounded coordinator.
    private func performQuickCaptureStop(
        stopToken: String?,
        stopOwnerRevision: Int?
    ) async {
        let stopOwner = "quick_capture"
        let request = quickCaptureStopRequest(
            generationToken: stopToken,
            ownerRevision: stopOwnerRevision
        )
        let client = ipcClient
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

        isProcessing = false
        let routeStillMatches = (
            activeGenerationOwner == stopOwner
            && activeGenerationToken == stopToken
            && activeGenerationOwnerRevision == stopOwnerRevision
        )
        guard routeStillMatches else {
            logger.warn(
                "Поздний quick_capture stop-ответ не меняет новое поколение"
            )
            refreshStatusItemTitle()
            rebuildStatusMenu()
            return
        }

        switch outcome.decision {
        case .surfaceAsIs where outcome.hasTerminalResponse:
            // Пока Notes/Obsidian/collection post-processing делает await,
            // новый toggle не должен открыть reentrant stop/start.
            isProcessing = true
            quickCaptureActive = false
            await handleQuickCaptureResult(outcome.result ?? [:])
            clearQuickCaptureStopRoute()
        case .foreignOwner:
            clearQuickCaptureStopRoute()
            BackendToast.shared.show(
                "Идёт другая запись — быстрая заметка её не остановила"
            )
        case .recoveryPending:
            retainQuickCaptureStopRecovery(
                result: outcome.result,
                message: "Аудио ещё восстанавливается — нажмите остановку ещё раз"
            )
        case .finalizationSlow:
            retainQuickCaptureStopRecovery(
                result: outcome.result,
                message: "Финализация затянулась — результат появится в истории"
            )
        case .giveUpRescuePending:
            // Симметрично диктовке (main+HotkeyRecording): unknown_generation —
            // авторитетное «поколения нет», а не транспортная неопределённость.
            // Под ним quickCaptureActive=true залипал бы навсегда, блокируя и
            // заметку, и диктовку, и встречу. Rescue отработает на старте backend.
            if (outcome.result?["status"] as? String) == "unknown_generation" {
                clearQuickCaptureStopRoute()
                BackendToast.shared.show(
                    "Поколение заметки уже закрыто backend — повторять остановку не нужно"
                )
                return
            }
            retainQuickCaptureStopRecovery(
                result: outcome.result,
                message: "Остановка не подтверждена; запись восстановится при следующем запуске"
            )
        case .surfaceAsIs:
            retainQuickCaptureStopRecovery(
                result: outcome.result,
                message: "Ответ остановки не получен — нажмите остановку ещё раз"
            )
        case .retry, .retryRecorderStop, .pollAgain:
            logger.error(
                "RecordingStopCoordinator вернул промежуточное quick_capture решение"
            )
            retainQuickCaptureStopRecovery(
                result: outcome.result,
                message: "Остановка не подтверждена — повторите действие"
            )
        }
    }

    /// Сверить потерянный Quick start с backend. Snapshot без точного request
    /// ID не даёт права ни принять, ни скомпенсировать запись.
    private func reconcileAmbiguousQuickCaptureStart(requestID: UUID) async {
        guard
            quickCaptureStartRequestID == requestID
                || quickCaptureStartAmbiguousRequestID == requestID
        else {
            return
        }

        quickCaptureStartAmbiguousRequestID = requestID
        let snapshot = await recordingStateSnapshot()
        let decision = RecordingStartAmbiguityPolicy.decide(
            snapshot: snapshot,
            expectedOwner: "quick_capture",
            expectedStartRequestID: requestID.uuidString,
            allowsMeetingPromotion: false
        )
        let stopRequested = (
            quickCaptureStopRequestedStartID == requestID
            || quickCaptureStopRequestedDuringAmbiguousStart
        )

        switch decision {
        case .retryReconciliation:
            quickCaptureActive = !stopRequested
            isProcessing = stopRequested
            BackendToast.shared.show(
                "Старт заметки не подтверждён; новый старт заблокирован до проверки backend"
            )
            refreshStatusItemTitle()
            rebuildStatusMenu()
        case let .adoptExpectedOwner(generationToken):
            guard
                let generationToken,
                !generationToken.isEmpty,
                let ownerRevision = snapshot.ownerRevision
            else {
                // У нового backend этот guard недостижим; при несовместимом
                // снимке сохраняем Q1 неизвестным, а не отправляем legacy stop.
                quickCaptureStartAmbiguousRequestID = requestID
                return
            }
            quickCaptureStartRequestID = nil
            quickCaptureStartAmbiguousRequestID = nil
            quickCaptureStopRequestedStartID = nil
            quickCaptureStopRequestedDuringAmbiguousStart = false
            activeGenerationToken = generationToken
            activeGenerationOwner = "quick_capture"
            activeGenerationOwnerRevision = ownerRevision
            activeGenerationStartRequestID = requestID.uuidString
            recordingStopRecoveryPending = false
            if stopRequested {
                quickCaptureActive = true
                isProcessing = true
                await performQuickCaptureStop(
                    stopToken: generationToken,
                    stopOwnerRevision: ownerRevision
                )
                return
            }
            quickCaptureActive = true
            isProcessing = false
            startRealtimeOverlayPolling()
            BackendToast.shared.show("Быстрая заметка: запись…")
            refreshStatusItemTitle()
            rebuildStatusMenu()
            await showQuickCapturePanelIfEnabled()
        case .awaitPromotedMeeting:
            // Для Quick Capture такой promote не является разрешённым
            // переходом: текущий Q1 остаётся неизвестным до следующего read.
            quickCaptureStartAmbiguousRequestID = requestID
            quickCaptureActive = !stopRequested
            isProcessing = stopRequested
        case .rejectAsIdleOrForeign:
            if snapshot.isRecording && snapshot.owner == "quick_capture" {
                // Same-source, но чужой/legacy request ID: нельзя отдать его
                // следующему Q2 и нельзя остановить без доказанного lease.
                quickCaptureStartRequestID = requestID
                quickCaptureStartAmbiguousRequestID = requestID
                quickCaptureActive = !stopRequested
                isProcessing = stopRequested
                BackendToast.shared.show(
                    "Заметка не подтверждена; ожидаю безопасную сверку backend"
                )
                refreshStatusItemTitle()
                rebuildStatusMenu()
                return
            }
            clearQuickCaptureStartAttempt(requestID: requestID)
            BackendToast.shared.show("Старт заметки не подтвердился; запись не тронута")
        }
    }

    /// Компенсировать отменённый/устаревший Q1. Непустые token и revision
    /// обязательны: source-only компенсация здесь запрещена без исключений.
    private func stopOrphanQuickCapture(
        generationToken: String?,
        ownerRevision: Int?,
        originRequestID: UUID
    ) async {
        guard
            let generationToken,
            !generationToken.isEmpty,
            let ownerRevision,
            ownerRevision > 0
        else {
            // Оставляем Q1 в fail-safe ambiguity. Старый backend может не
            // поддерживать lease, но компенсировать его tokenless опаснее.
            if activeGenerationToken == nil,
               activeGenerationOwner == nil {
                quickCaptureStartRequestID = originRequestID
                quickCaptureStartAmbiguousRequestID = originRequestID
                quickCaptureStopRequestedStartID = originRequestID
                quickCaptureStopRequestedDuringAmbiguousStart = true
                quickCaptureActive = false
                isProcessing = false
                BackendToast.shared.show(
                    "Отмена заметки ожидает подтверждения lease backend"
                )
                refreshStatusItemTitle()
                rebuildStatusMenu()
            }
            return
        }

        let request = quickCaptureStopRequest(
            generationToken: generationToken,
            ownerRevision: ownerRevision
        )
        let client = ipcClient
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

        let mayMutateUI = QuickCaptureOrphanRecoveryPolicy.canAdopt(
            activeToken: activeGenerationToken,
            startRequestPending: quickCaptureStartRequestID != nil,
            quickCaptureActive: quickCaptureActive
        ) && quickCaptureStartAmbiguousRequestID == nil
        guard mayMutateUI else {
            logger.warn(
                "Orphan quick_capture stop не меняет более новый UI-route"
            )
            return
        }

        quickCaptureStopRequestedStartID = nil
        quickCaptureStopRequestedDuringAmbiguousStart = false
        if outcome.decision == .foreignOwner {
            // Строгий CAS доказал, что G1 уже повышен/передан другому owner.
            // Ни recovery-route, ни повторный stop Quick Capture здесь нельзя
            // публиковать: это был бы UI-способ присвоить чужую запись.
            isProcessing = false
            BackendToast.shared.show(
                "Другая запись уже владеет заметкой; остановка не выполнена"
            )
            refreshStatusItemTitle()
            rebuildStatusMenu()
            return
        }
        if outcome.hasTerminalResponse {
            isProcessing = true
            await handleQuickCaptureResult(outcome.result ?? [:])
            isProcessing = false
            refreshStatusItemTitle()
            rebuildStatusMenu()
            return
        }

        activeGenerationToken = generationToken
        activeGenerationOwner = "quick_capture"
        activeGenerationOwnerRevision = ownerRevision
        activeGenerationStartRequestID = originRequestID.uuidString
        recordingStopRecoveryPending = true
        quickCaptureActive = true
        quickCapturePanelController?.setRecording(true)
        BackendToast.shared.show(
            "Заметка ещё восстанавливается — нажмите остановку ещё раз"
        )
        rebuildStatusMenu()
    }

    /// Собрать immutable stop-запрос. Новый lease получает строгий CAS, тогда
    /// как обычный подтверждённый legacy start сохраняет совместимый fallback.
    private func quickCaptureStopRequest(
        generationToken: String?,
        ownerRevision: Int?
    ) -> RecordingStopRequest {
        var params: [String: Any] = ["source": "quick_capture"]
        if let generationToken, !generationToken.isEmpty {
            params["generation_token"] = generationToken
        }
        if let generationToken, !generationToken.isEmpty,
           let ownerRevision {
            params["expected_owner_revision"] = ownerRevision
        }
        return RecordingStopRequest(
            method: "stop_recording",
            params: params,
            timeoutSec: 120
        )
    }

    private func retainQuickCaptureStopRecovery(
        result: [String: Any]?,
        message: String
    ) {
        activeGenerationOwner = "quick_capture"
        recordingStopRecoveryPending = true
        quickCaptureActive = true
        quickCapturePanelController?.setRecording(true)
        let rawPreview = (result?["preview_text"] as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let preview = rawPreview.isEmpty
            ? ""
            : ": \(String(rawPreview.prefix(120)))"
        BackendToast.shared.show(message + preview)
        refreshStatusItemTitle()
        rebuildStatusMenu()
    }

    private func clearQuickCaptureStopRoute() {
        activeGenerationToken = nil
        activeGenerationOwner = nil
        activeGenerationOwnerRevision = nil
        activeGenerationStartRequestID = nil
        recordingStopRecoveryPending = false
        isProcessing = false
        quickCaptureActive = false
        quickCapturePanelController?.setRecording(false)
        refreshStatusItemTitle()
        rebuildStatusMenu()
    }

    private func handleQuickCaptureResult(_ result: [String: Any]) async {
        // C3b ревью F1 (sibling-gate asymmetry): запись физически остановлена
        // независимо от исхода сохранения (дубликат/ошибка/успех) И независимо
        // от того, видима ли панель СЕЙЧАС — isRecording обязан быть источником
        // правды всегда, иначе после закрытия панели мид-записи он застревает
        // true навсегда (setRecording(true) на следующем старте молча не
        // применяется — guard в setRecording трактует состояние как уже
        // синхронизированное). Раньше здесь был `isVisible`-гейт — сам этот
        // гейт и был багом (панель, ЗАКРЫТАЯ во время записи, никогда не
        // получала свой setRecording(false)).
        quickCapturePanelController?.setRecording(false)
        let status = result["status"] as? String ?? ""
        if result["skipped"] as? String == "duplicate" {
            await MainActor.run { BackendToast.shared.show("Заметка совпала с недавней записью — пропущена") }
            return
        }
        guard status == "ok", let historyId = result["history_id"] as? String, !historyId.isEmpty else {
            await MainActor.run { BackendToast.shared.show("Заметка не сохранилась") }
            return
        }
        // 1) коллекция (лениво создать), 2) нейтральный paste_status, 3) отправки.
        // create_collection на уже существующей коллекции кидает ValueError
        // ("Коллекция '...' уже существует", collection_manager.py:189) —
        // та же exception-ветка (invalid_request), что и «нет имени», но проверка
        // выполняется ДО записи на диск, поэтому try?-проглатывание дёшево и
        // корректно идемпотентно для фиксированного имени коллекции.
        _ = try? await ipcClient.callAsync(
            method: "create_collection",
            params: ["name": Self.quickCaptureCollectionName,
                     "description": "Быстрые голосовые заметки"], timeoutSec: 10)
        _ = try? await ipcClient.callAsync(
            method: "add_to_collection",
            params: ["collection_name": Self.quickCaptureCollectionName, "item_id": historyId],
            timeoutSec: 10)
        // Параметры сверены с _handle_set_paste_status (service.py:2459) — ключи
        // "id" и "paste_status", НЕ "item_id"/"status".
        _ = try? await ipcClient.callAsync(
            method: "set_paste_status",
            params: ["id": historyId, "paste_status": "skipped"], timeoutSec: 10)
        await sendQuickCaptureCopies(text: result["text"] as? String ?? "", historyId: historyId)
        // Свежая заметка должна появиться в списке панели, если она открыта.
        if quickCapturePanelController?.window?.isVisible == true {
            refreshQuickCapturePanelNotes()
        }
        await MainActor.run { BackendToast.shared.show("Заметка сохранена") }
    }

    /// C3a Task 3 (спека §3.3): opt-in дублирование сохранённой заметки в Apple
    /// Notes / Obsidian. Настройки читаются ЖИВЬЁМ через get_settings (НЕ кэш
    /// агента `settings`) — чекбокс в Settings должен действовать сразу после
    /// переключения, без ожидания следующего цикла обновления кэша.
    func sendQuickCaptureCopies(text: String, historyId: String) async {
        var liveSettings: [String: Any] = [:]
        if let resp = try? await ipcClient.callAsync(method: "get_settings", params: [:], timeoutSec: 10),
           let result = resp["result"] as? [String: Any] {
            liveSettings = result
        }

        if (liveSettings["quick_capture_send_to_notes"] as? Bool) == true {
            await sendQuickCaptureNoteToAppleNotes(text: text)
        }
        if (liveSettings["quick_capture_obsidian_sync"] as? Bool) == true {
            await syncQuickCaptureNoteToObsidian(text: text, historyId: historyId)
        }
    }

    /// title — первые ~60 символов ПЕРВОЙ строки текста (не всего текста целиком —
    /// длинная заметка без переводов строк не должна порождать гигантский заголовок).
    /// Приватный режим и сам осечка osascript уже гейтятся внутри
    /// handle_create_apple_note (apple_integration_service.py) — здесь только
    /// разворачиваем ok:false в toast с текстом ответа backend'а.
    private func sendQuickCaptureNoteToAppleNotes(text: String) async {
        let firstLine = text.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
            .first.map(String.init) ?? text
        let trimmedFirstLine = firstLine.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = trimmedFirstLine.count > 60
            ? String(trimmedFirstLine.prefix(60)) + "…"
            : trimmedFirstLine
        guard let resp = try? await ipcClient.callAsync(
            method: "create_apple_note",
            params: ["title": title.isEmpty ? "Быстрая заметка" : title, "body": text, "folder": "Krab Ear"],
            timeoutSec: 15
        ), let result = resp["result"] as? [String: Any] else {
            await MainActor.run { BackendToast.shared.show("Не удалось сохранить заметку в Notes") }
            return
        }
        if (result["ok"] as? Bool) != true {
            // Ответ backend'а несёт user_msg (человекочитаемо, напр. privacy-гейт)
            // либо error (техническое сообщение osascript) — сверено с
            // apple_integration_service.py::handle_create_apple_note.
            let message = (result["user_msg"] as? String)
                ?? (result["error"] as? String)
                ?? "Не удалось сохранить заметку в Notes"
            await MainActor.run { BackendToast.shared.show(message) }
        }
    }

    /// Obsidian: per-item IPC-метода НЕТ — ObsidianSyncManager.sync(items, force)
    /// (backend/obsidian_sync.py) принимает СПИСОК items, поэтому форс-синк
    /// заметки собирает минимальный item-словарь {id, ts, text} из данных, уже
    /// имеющихся в этом флоу (см. NOTES отчёта Task 3 — точное имя IPC-метода:
    /// run_obsidian_sync → ObsidianSyncManager.handle_sync). Нет настроенного
    /// vault → sync() кидает RuntimeError → IPC-диспетчер отдаёт тихий
    /// invalid_request (ok:false) — здесь молча игнорируем (спека §3.3: "иначе
    /// чекбокс disabled с подсказкой"; v1 упрощение — тихий no-op, без тоста).
    private func syncQuickCaptureNoteToObsidian(text: String, historyId: String) async {
        let ts = ISO8601DateFormatter().string(from: Date())
        _ = try? await ipcClient.callAsync(
            method: "run_obsidian_sync",
            params: ["items": [["id": historyId, "ts": ts, "text": text]], "force": true],
            timeoutSec: 15
        )
    }

    // MARK: - C3b Task 2: панель-скретчпад (QuickCapturePanelController)

    /// Единственный инстанс панели: создаётся лениво, инжектится ipcClient и
    /// onToggleRecording-колбэк (тот же паттерн, что ensureMeetingPanelController
    /// в main+MeetingPanel.swift).
    func ensureQuickCapturePanelController() -> QuickCapturePanelController {
        if let existing = quickCapturePanelController { return existing }
        let c = QuickCapturePanelController()
        c.ipcClient = ipcClient
        c.onToggleRecording = { [weak self] in self?.onQuickCaptureToggle() }
        quickCapturePanelController = c
        return c
    }

    /// Вызывается ТОЛЬКО после настоящего успешного старта записи (см.
    /// onQuickCaptureToggle). Настройка читается ЖИВЬЁМ через get_settings —
    /// тот же приём, что sendQuickCaptureCopies — чекбокс «Показывать скретчпад
    /// при записи» должен действовать сразу после переключения, без ожидания
    /// следующего цикла обновления кэша.
    func showQuickCapturePanelIfEnabled() async {
        // C3b ревью F1 (sibling-gate asymmetry): уже существующая панель
        // (например открытая вручную из меню, см. onOpenQuickCapturePanel)
        // обязана узнать о реальном старте записи БЕЗУСЛОВНО — настройка
        // quick_capture_show_panel решает только "показывать ли панель
        // САМОСТОЯТЕЛЬНО", не "врать ли уже открытой панели о состоянии
        // записи". Раньше setRecording(true) был доступен ТОЛЬКО за этим
        // гейтом — при дефолтной (выключенной) настройке вручную открытая
        // панель никогда не отражала собственный успешный старт записи.
        // Nil-safe: если панель ещё не создана, optional chaining — no-op.
        quickCapturePanelController?.setRecording(true)

        guard let resp = try? await ipcClient.callAsync(method: "get_settings", params: [:], timeoutSec: 10),
              let result = resp["result"] as? [String: Any],
              (result["quick_capture_show_panel"] as? Bool) == true else { return }
        let controller = ensureQuickCapturePanelController()
        controller.setRecording(true)
        controller.show()
        refreshQuickCapturePanelNotes()
    }

    /// Обновляет список заметок панели тем же IPC-контрактом, что и подменю
    /// «Быстрые заметки» (refreshQuickNotesSubmenu, get_collection_items +
    /// suffix(7).reversed()) — продублировано минимально, сигнатура
    /// refreshQuickNotesSubmenu не меняется.
    func refreshQuickCapturePanelNotes() {
        guard let controller = quickCapturePanelController else { return }
        let ipc = self.ipcClient
        let collectionName = Self.quickCaptureCollectionName
        DispatchQueue.global(qos: .userInitiated).async {
            var items: [[String: Any]] = []
            if let resp = try? ipc.call(
                method: "get_collection_items",
                params: ["collection_name": collectionName]),
               let result = resp["result"] as? [String: Any],
               let fetched = result["items"] as? [[String: Any]] {
                items = fetched
            }
            let latest = Array(items.suffix(7).reversed())
            DispatchQueue.main.async { controller.renderNotes(latest) }
        }
    }

    /// Пункт меню-бара «Открыть скретчпад» (main+StatusMenu.swift) — показывает
    /// панель независимо от состояния записи (ручной вызов, план Task 2).
    /// Простой вариант «всегда показать» (симметрично onOpenHistory), а не
    /// toggle — план допускал оба варианта на усмотрение исполнителя.
    @objc func onOpenQuickCapturePanel() {
        let controller = ensureQuickCapturePanelController()
        controller.setRecording(quickCaptureActive)
        controller.show()
        refreshQuickCapturePanelNotes()
    }

    // MARK: - Task 2/3: глобальный хоткей (настраиваемая комбинация)

    /// Три фиксированные комбинации (спека §3.2, план Task 3) — allowlist сверен
    /// с settings_validator.py _ENUM_FIELDS["quick_capture_hotkey"]. Все три
    /// используют N = keyCode 45 (kVK_ANSI_N). cmd_shift_n — дефолт/фоллбэк для
    /// неизвестного/пустого значения.
    static func quickCaptureHotkeyCombo(for hotkeyId: String) -> (modifiers: NSEvent.ModifierFlags, keyCode: UInt16) {
        switch hotkeyId {
        case "cmd_opt_n": return ([.command, .option], 45)
        case "ctrl_shift_n": return ([.control, .shift], 45)
        default: return ([.command, .shift], 45)
        }
    }

    /// Образец — SelectionTranslator.swift:126, но с СОХРАНЕНИЕМ монитора
    /// (урок main+QuickPresets.swift: несохранённый монитор не снять в teardown).
    /// Комбинация читается ЖИВЬЁМ (get_settings, НЕ кэш `settings`) ДО регистрации
    /// монитора — чтобы пере-арм после смены в дропдауне (Task 3, см.
    /// HistoryPanelController+Settings.swift::onQuickCaptureHotkeyChanged) сразу
    /// подхватывал новое значение, а не устаревший кэш.
    func startQuickCaptureHotkeyMonitor() {
        guard quickCaptureHotkeyMonitor == nil else { return }
        let ipc = self.ipcClient
        Task.detached { [weak self] in
            var hotkeyId = "cmd_shift_n"
            if let resp = try? await ipc.callAsync(method: "get_settings", params: [:], timeoutSec: 10),
               let result = resp["result"] as? [String: Any],
               let value = result["quick_capture_hotkey"] as? String, !value.isEmpty {
                hotkeyId = value
            }
            // Rebind to a fresh `let` right before crossing the actor boundary —
            // a captured `var` (even unmutated at this point) trips the Swift 6
            // Sendable-closure-capture checker.
            let resolvedHotkeyId = hotkeyId
            await MainActor.run { [weak self] in
                self?.installQuickCaptureHotkeyMonitor(hotkeyId: resolvedHotkeyId)
            }
        }
    }

    /// Регистрирует NSEvent-монитор на main-потоке. Повторный guard-чек — защита
    /// от гонки: startQuickCaptureHotkeyMonitor() мог быть вызван дважды подряд,
    /// пока первый async-запрос настроек ещё не вернулся.
    private func installQuickCaptureHotkeyMonitor(hotkeyId: String) {
        guard quickCaptureHotkeyMonitor == nil else { return }
        let (modifiers, keyCode) = AgentAppDelegate.quickCaptureHotkeyCombo(for: hotkeyId)
        quickCaptureHotkeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            // C3a ревью F2a: OS auto-repeat (зажатая клавиша) шлёт keyDown-события
            // без реального повторного нажатия пользователем — проверяем ПЕРВОЙ,
            // до модификаторов/keyCode, чтобы не тратить на неё дальнейшую логику.
            guard !event.isARepeat else { return }
            guard let self,
                  event.modifierFlags.intersection(.deviceIndependentFlagsMask) == modifiers,
                  event.keyCode == keyCode else { return }
            DispatchQueue.main.async { self.onQuickCaptureToggle() }
        }
    }

    func stopQuickCaptureHotkeyMonitor() {
        if let monitor = quickCaptureHotkeyMonitor {
            NSEvent.removeMonitor(monitor)
            quickCaptureHotkeyMonitor = nil
        }
    }

    // MARK: - Task 2: подменю «Быстрые заметки»

    /// Вызывается из menuWillOpen (main+MenuBarRecap.swift) при каждом открытии
    /// status-меню. IPC строго off-main (AGENT-3); UI-обновление подменю — на main.
    func refreshQuickNotesSubmenu() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            // get_collection_items кидает RuntimeError, если коллекция ещё не
            // создана (ни одной заметки не сохранено) — try? сворачивает эту
            // ветку в пустой список, что рендерится как «Заметок пока нет»
            // тем же путём, что и privacy-режим (backend возвращает items: []).
            var items: [[String: Any]] = []
            if let resp = try? self.ipcClient.call(
                method: "get_collection_items",
                params: ["collection_name": Self.quickCaptureCollectionName]),
               // call() возвращает ПОЛНЫЙ конверт {ok, result, id} — реальные
               // поля лежат в result (тот же контракт, что callAsync, сверено
               // с IPCClient.swift:400-480).
               let result = resp["result"] as? [String: Any],
               let fetched = result["items"] as? [[String: Any]] {
                items = fetched
            }
            let latest = Array(items.suffix(7).reversed())
            DispatchQueue.main.async { self.rebuildQuickNotesSubmenu(latest) }
        }
    }

    /// Перестраивает содержимое подменю «Быстрые заметки» на main-потоке.
    /// items — уже усечённый и развёрнутый (последние первыми) список из
    /// refreshQuickNotesSubmenu; поля сверены с HistoryItem.to_dict()
    /// (models.py) — "text" (str) и "ts" (ISO-8601 UTC, timespec=seconds).
    func rebuildQuickNotesSubmenu(_ items: [[String: Any]]) {
        guard let submenu = quickNotesSubmenu else { return }
        submenu.removeAllItems()

        if items.isEmpty {
            let emptyItem = NSMenuItem(title: "Заметок пока нет", action: nil, keyEquivalent: "")
            emptyItem.isEnabled = false
            submenu.addItem(emptyItem)
        } else {
            for item in items {
                let text = (item["text"] as? String) ?? ""
                let ts = (item["ts"] as? String) ?? ""
                let noteItem = NSMenuItem(
                    title: Self.quickNoteMenuTitle(text: text, ts: ts),
                    action: #selector(onQuickNoteItemClicked(_:)),
                    keyEquivalent: ""
                )
                noteItem.target = self
                noteItem.representedObject = text
                submenu.addItem(noteItem)
            }
        }

        submenu.addItem(.separator())
        let showAllItem = NSMenuItem(
            title: "Показать все…",
            action: #selector(onOpenHistory),
            keyEquivalent: ""
        )
        showAllItem.target = self
        submenu.addItem(showAllItem)
    }

    /// Клик по пункту заметки — копирует полный текст в буфер обмена (тот же
    /// helper, что «Копировать последний»; заметка никогда не входит в
    /// paste-пайплайн активного окна).
    @objc func onQuickNoteItemClicked(_ sender: NSMenuItem) {
        guard let text = sender.representedObject as? String, !text.isEmpty else { return }
        // S34 / Fable-ревью F3: explicit user-initiated copy — не через concealed-guard.
        pasteService.putToClipboardUserInitiated(text)
        BackendToast.shared.show("Скопировано")
    }

    /// «первые ~40 символов… · HH:mm» — разделитель U+00B7 MIDDLE DOT (глиф-гейт
    /// AGENT-J: уже используется в main+MenuBarRecap.swift).
    private static func quickNoteMenuTitle(text: String, ts: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let snippetLimit = 40
        let snippet: String
        if trimmed.isEmpty {
            snippet = "(без текста)"
        } else if trimmed.count > snippetLimit {
            snippet = String(trimmed.prefix(snippetLimit)) + "…"
        } else {
            snippet = trimmed
        }
        return "\(snippet) · \(quickNoteTimeLabel(from: ts))"
    }

    /// ts приходит как ISO-8601 UTC (HistoryItem.ts = isoformat(timespec="seconds"));
    /// парсим двумя формататерами (с/без дробных секунд — образец
    /// HistoryPanelController+RecordingChain.swift:144-147) и рендерим в local HH:mm.
    private static func quickNoteTimeLabel(from ts: String) -> String {
        let isoFrac = ISO8601DateFormatter()
        isoFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let isoNoFrac = ISO8601DateFormatter()
        isoNoFrac.formatOptions = [.withInternetDateTime]
        guard let date = isoFrac.date(from: ts) ?? isoNoFrac.date(from: ts) else {
            return "--:--"
        }
        let hhmm = DateFormatter()
        hhmm.dateFormat = "HH:mm"
        return hhmm.string(from: date)
    }
}
