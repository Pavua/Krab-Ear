/*
 Расширение HistoryPanelController — методы импорта аудио.

 Содержит:
 - onImportAudio / onCancelImport / onToggleImportPause / onOpenImportReport
 - enqueueImport / processNextImportIfNeeded / finishImportQueueIfNeeded
 - sendImportNotification / updateImportStatusLabel
 - startImportElapsedTimer / stopImportElapsedTimer
 - normalizedImportSignature / writeImportQueueReport
 - ImportDropZoneView (drag-and-drop зона)
*/

import AppKit
import Foundation
import UniformTypeIdentifiers

// MARK: - Import methods

extension HistoryPanelController {

    @objc func onImportAudio() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.title = "Выберите аудиофайлы или папки"
        panel.message = "Выберите аудиофайлы или папки с записями звонков для транскрибации"
        panel.prompt = "Транскрибировать"

        presentPanelSheet(panel, for: self.window) { [weak self] resp in
            guard let self, resp == .OK else { return }
            let paths = panel.urls.map(\.path)
            self.enqueueImport(paths: paths, sourceTag: "open_panel")
        }
    }

    @objc func onCancelImport() {
        guard isImportRunning || !importQueue.isEmpty else { return }
        importCancellationRequested = true
        isImportPaused = false
        importQueue.removeAll()
        if let currentImportJob {
            importJobSignatures = [normalizedImportSignature(currentImportJob.paths)]
        } else {
            importJobSignatures.removeAll()
        }
        cancelImportButton.isEnabled = false
        pauseImportButton.isEnabled = false
        pauseImportButton.title = "Пауза импорта"
        importStatusLabel.stringValue = "Импорт: остановка после текущей задачи..."

        // Async PR #14: отменяем активную backend-задачу, чтобы STT-пайплайн остановился
        // после текущего файла (без прерывания частичной транскрибации).
        if let jobID = currentTranscribeJobID {
            let endpoint = ipcClient.endpoint
            DispatchQueue.global(qos: .userInitiated).async {
                let client = IPCClient(socketPath: endpoint)
                _ = try? client.call(method: "cancel_transcribe_job", params: ["job_id": jobID])
            }
        }
    }

    @objc func onToggleImportPause() {
        guard isImportRunning || !importQueue.isEmpty else { return }
        isImportPaused.toggle()
        pauseImportButton.title = isImportPaused ? "Продолжить импорт" : "Пауза импорта"
        if isImportPaused {
            importStatusLabel.stringValue = "Импорт: пауза (текущая задача завершится, новые не стартуют)"
            return
        }
        updateImportStatusLabel()
        processNextImportIfNeeded()
    }

    @objc func onOpenImportReport() {
        guard let lastImportReportPath else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: lastImportReportPath))
    }

    func enqueueImport(paths: [String], sourceTag: String) {
        let clean = paths
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !clean.isEmpty else { return }
        let signature = normalizedImportSignature(clean)
        if importJobSignatures.contains(signature) {
            importStatusLabel.stringValue = "Импорт: дубликат задачи пропущен"
            return
        }
        if importQueue.count >= 30 {
            showInfoAlert(title: "Импорт аудио", body: "Очередь переполнена (макс. 30 задач). Дождитесь завершения текущих задач.")
            return
        }

        // Wave 59: preview IPC moved off-main to avoid AppHang (5 s timeout).
        // Show interim status immediately; enqueue job after background fetch completes.
        importStatusLabel.stringValue = "Импорт: проверяем файлы..."
        let endpoint = ipcClient.endpoint
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let client = IPCClient(socketPath: endpoint)
            let preview: ImportPreview
            if
                let response = try? client.call(
                    method: "preview_transcribe_paths",
                    params: ["paths": clean, "sample_limit": 3]),
                let result = response["result"] as? [String: Any]
            {
                let audioCount  = (result["audio_count"]  as? Int)          ?? 0
                let folderCount = (result["folder_count"] as? Int)          ?? 0
                let sample      = (result["sample"]       as? [String])     ?? []
                let totalBytes  = (result["total_bytes"]  as? Int)          ?? 0
                let byExtension = (result["by_ext"]       as? [String: Int]) ?? [:]
                preview = ImportPreview(
                    audioCount: audioCount, folderCount: folderCount,
                    sample: sample, byExtension: byExtension, totalBytes: totalBytes)
            } else {
                preview = ImportPreview(
                    audioCount: 0, folderCount: 0, sample: [],
                    byExtension: [:], totalBytes: 0)
            }
            DispatchQueue.main.async {
                self?._enqueueImportWithPreview(
                    paths: clean, sourceTag: sourceTag,
                    signature: signature, preview: preview)
            }
        }
    }

    /// Завершает добавление задачи в очередь — вызывается на main thread после
    /// background fetch preview. Разделение необходимо чтобы избежать захвата
    /// [String: Any] (non-Sendable) через границу concurrency.
    private func _enqueueImportWithPreview(
        paths: [String],
        sourceTag: String,
        signature: String,
        preview: ImportPreview
    ) {
        if preview.audioCount == 0 {
            importStatusLabel.stringValue = "Импорт: готов"
            showInfoAlert(title: "Импорт аудио", body: "Не найдено поддерживаемых аудиофайлов.")
            return
        }

        if importSessionStartedAt == nil {
            importSessionStartedAt = Date()
            lastImportReportPath = nil
            openImportReportButton.isEnabled = false
        }
        importQueue.append(
            ImportJob(
                paths: paths,
                sourceTag: sourceTag,
                audioCount: preview.audioCount,
                folderCount: preview.folderCount,
                totalBytes: preview.totalBytes,
                byExtension: preview.byExtension
            )
        )
        importJobSignatures.insert(signature)
        importJobsPlanned += 1
        importFilesPlanned += preview.audioCount
        importBytesPlanned += preview.totalBytes
        importSourceStats[sourceTag, default: 0] += 1
        for (ext, count) in preview.byExtension {
            importFormatStats[ext, default: 0] += count
        }
        cancelImportButton.isEnabled = true
        pauseImportButton.isEnabled = true
        pauseImportButton.title = isImportPaused ? "Продолжить импорт" : "Пауза импорта"
        let extSummary = preview.byExtension
            .sorted { $0.key < $1.key }
            .map { "\($0.key)=\($0.value)" }
            .joined(separator: ", ")
        let folderLine = preview.folderCount > 1
            ? "Папок: \(preview.folderCount)\n"
            : ""
        importStatusLabel.toolTip = preview.sample.isEmpty
            ? "\(folderLine)Подготовлено файлов: \(preview.audioCount)\nОбъём: \(HistoryPanelController.formatBytes(preview.totalBytes))\nФорматы: \(extSummary)"
            : "\(folderLine)Подготовлено файлов: \(preview.audioCount)\nОбъём: \(HistoryPanelController.formatBytes(preview.totalBytes))\nФорматы: \(extSummary)\nПримеры:\n\(preview.sample.joined(separator: "\n"))"
        updateImportStatusLabel()
        processNextImportIfNeeded()
    }

    func processNextImportIfNeeded() {
        guard !isImportRunning else { return }
        guard !isImportPaused else {
            updateImportStatusLabel()
            return
        }
        guard !importQueue.isEmpty else {
            finishImportQueueIfNeeded()
            return
        }

        isImportRunning = true
        currentImportJob = importQueue.removeFirst()
        currentImportJobStartedAt = Date()
        importProgressBar.isHidden = false
        importProgressBar.doubleValue = importJobsPlanned > 0
            ? (Double(importJobsCompleted) / Double(importJobsPlanned)) * 100.0
            : 0.0
        startImportElapsedTimer()
        updateImportStatusLabel()

        guard let job = currentImportJob else {
            isImportRunning = false
            stopImportElapsedTimer()
            return
        }
        let endpoint = ipcClient.endpoint
        let settings = settingsProvider()
        let jobPaths = job.paths
        let qualityProfile = settings.qualityProfile
        let cleanupProfile = settings.cleanupProfile
        let translationMode = settings.translationMode
        let translationStyle = settings.translationStyle
        let translateAndPaste = settings.translateAndPaste
        let startedAt = Date()
        let signature = normalizedImportSignature(jobPaths)

        // Async PR #14: отправляем transcribe_paths_async → получаем job_id → опрашиваем
        // get_transcribe_progress каждую секунду, показывая этап, file_index/total_files,
        // elapsed + ETA. Снимает блокировку IPC сокета на весь STT.
        transcribeProgressFailCount = 0
        let params: [String: Any] = [
            "paths": jobPaths,
            "quality_profile": qualityProfile,
            "cleanup_profile": cleanupProfile,
            "translation_mode": translationMode,
            "translation_style": translationStyle,
            "translate_and_paste": translateAndPaste,
        ]
        dispatchAsyncTranscribeStart(
            endpoint: endpoint,
            params: params,
            signature: signature,
            startedAt: startedAt
        )
    }

    /// Разбивка на 2 функции + explicit types уменьшает работу type-checker — Swift 6
    /// крашился SIGSEGV при объединённом closure из-за optional chaining + nested async.
    ///
    /// Swift 6 Sendable: `[String: Any]` не Sendable, поэтому сериализуем params в JSON Data
    /// (Sendable) до отправки в background thread, а в background десериализуем обратно.
    private func dispatchAsyncTranscribeStart(
        endpoint: String,
        params: [String: Any],
        signature: String,
        startedAt: Date
    ) {
        // Сериализуем params в Data на main thread — Data является Sendable, и это
        // снимает варнинг capture of non-Sendable type в @Sendable closure.
        let paramsData: Data
        do {
            paramsData = try JSONSerialization.data(withJSONObject: params)
        } catch {
            handleAsyncTranscribeStarted(
                jobID: nil,
                endpoint: endpoint,
                signature: signature,
                startedAt: startedAt
            )
            return
        }

        let queue = DispatchQueue.global(qos: .userInitiated)
        queue.async { [weak self] in
            let jobID: String? = Self.requestAsyncJobID(endpoint: endpoint, paramsData: paramsData)
            DispatchQueue.main.async {
                self?.handleAsyncTranscribeStarted(
                    jobID: jobID,
                    endpoint: endpoint,
                    signature: signature,
                    startedAt: startedAt
                )
            }
        }
    }

    /// Синхронный (в рамках background thread) IPC-вызов transcribe_paths_async.
    ///
    /// `nonisolated` — функция вызывается из background thread, без main-actor context.
    /// Принимает JSON-сериализованные params (Sendable), десериализует локально.
    nonisolated private static func requestAsyncJobID(endpoint: String, paramsData: Data) -> String? {
        guard let params = (try? JSONSerialization.jsonObject(with: paramsData)) as? [String: Any] else {
            return nil
        }
        let client = IPCClient(socketPath: endpoint)
        guard let response = try? client.call(method: "transcribe_paths_async", params: params) else {
            return nil
        }
        let result = response["result"] as? [String: Any]
        return result?["job_id"] as? String
    }

    /// Обработка ответа transcribe_paths_async на main thread.
    private func handleAsyncTranscribeStarted(
        jobID: String?,
        endpoint: String,
        signature: String,
        startedAt: Date
    ) {
        guard let jobID, !jobID.isEmpty else {
            importErrorMessages.append("Не удалось запустить асинхронную транскрибацию")
            completeImportJob(
                signature: signature,
                processed: 0,
                errors: 1,
                failed: true,
                startedAt: startedAt
            )
            return
        }
        currentTranscribeJobID = jobID
        startTranscribeProgressTimer(
            jobID: jobID,
            endpoint: endpoint,
            signature: signature,
            startedAt: startedAt
        )
    }

    // MARK: - Async transcribe polling (PR #14)

    /// Запускает таймер опроса get_transcribe_progress с шагом 1 секунда.
    /// При status == done|failed|cancelled останавливает таймер и вызывает completeImportJob.
    func startTranscribeProgressTimer(jobID: String, endpoint: String, signature: String, startedAt: Date) {
        stopTranscribeProgressTimer()
        transcribeProgressFailCount = 0
        transcribeProgressTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            DispatchQueue.global(qos: .userInitiated).async {
                let client = IPCClient(socketPath: endpoint)
                let response = try? client.call(
                    method: "get_transcribe_progress",
                    params: ["job_id": jobID]
                )
                DispatchQueue.main.async {
                    self.handleTranscribeProgress(
                        response: response,
                        jobID: jobID,
                        signature: signature,
                        startedAt: startedAt
                    )
                }
            }
        }
    }

    func stopTranscribeProgressTimer() {
        transcribeProgressTimer?.invalidate()
        transcribeProgressTimer = nil
    }

    /// Обрабатывает один tick ответа от get_transcribe_progress.
    private func handleTranscribeProgress(
        response: [String: Any]?,
        jobID: String,
        signature: String,
        startedAt: Date
    ) {
        // Таймер мог быть сброшен параллельно (cancel, timeout).
        guard currentTranscribeJobID == jobID else { return }

        guard let result = response?["result"] as? [String: Any] else {
            transcribeProgressFailCount += 1
            if transcribeProgressFailCount >= 3 {
                importErrorMessages.append("Потеряна связь с backend")
                completeImportJob(
                    signature: signature,
                    processed: 0,
                    errors: 1,
                    failed: true,
                    startedAt: startedAt
                )
            }
            return
        }
        // Сбрасываем счётчик после любого успешного ответа.
        transcribeProgressFailCount = 0

        let status = (result["status"] as? String) ?? "running"
        let currentStage = (result["current_stage"] as? String) ?? "idle"
        let fileIndex = (result["file_index"] as? Int) ?? 0
        let totalFiles = (result["total_files"] as? Int) ?? max(currentImportJob?.audioCount ?? 0, 1)
        let elapsedSec = (result["elapsed_sec"] as? Double) ?? Date().timeIntervalSince(startedAt)
        let etaSec = (result["eta_sec"] as? Double) ?? 0

        // Обновляем статус-строку + progress bar (file_index/total_files * 100).
        let stageText = stageRu(currentStage)
        let elapsedText = mmss(elapsedSec)
        let etaText = etaSec > 0 ? mmss(etaSec) : "--:--"
        importStatusLabel.stringValue =
            "Импорт: файл \(fileIndex)/\(totalFiles) — \(stageText), прошло \(elapsedText), ETA \(etaText)"
        if totalFiles > 0 {
            importProgressBar.isHidden = false
            importProgressBar.doubleValue = Double(fileIndex) / Double(totalFiles) * 100.0
        }

        // Терминальные состояния: извлекаем items/errors, завершаем job.
        if status == "done" || status == "failed" || status == "cancelled" {
            let items = (result["items"] as? [[String: Any]]) ?? []
            let errorsList = (result["errors"] as? [String]) ?? []
            let processedFromServer = (result["processed"] as? Int) ?? items.count
            let processed = max(processedFromServer, items.count)
            let failed = (status == "failed")

            // Накапливаем тексты ошибок от backend для итогового отчёта.
            for errText in errorsList where !errText.isEmpty {
                importErrorMessages.append(errText)
            }
            if status == "cancelled" {
                importErrorMessages.append("Задача отменена пользователем")
            }

            completeImportJob(
                signature: signature,
                processed: processed,
                errors: errorsList.count + (failed ? 1 : 0),
                failed: failed,
                startedAt: startedAt
            )
        }
    }

    /// Завершение текущего ImportJob: сброс счётчиков, остановка таймеров, следующий шаг очереди.
    /// Реплицирует блок, который раньше был внутри DispatchQueue.main.async завершения sync transcribe_paths.
    private func completeImportJob(
        signature: String,
        processed: Int,
        errors: Int,
        failed: Bool,
        startedAt: Date
    ) {
        stopTranscribeProgressTimer()
        currentTranscribeJobID = nil
        transcribeProgressFailCount = 0

        let durationSec = Date().timeIntervalSince(startedAt)

        isImportRunning = false
        stopImportElapsedTimer()
        currentImportJobStartedAt = nil
        importJobsCompleted += 1
        importProcessedTotal += processed
        importErrorsTotal += failed ? max(errors, 1) : errors
        importDurationTotalSec += durationSec
        importJobSignatures.remove(signature)
        currentImportJob = nil
        loadInitial()

        if importCancellationRequested {
            importCancellationRequested = false
            isImportPaused = false
            importQueue.removeAll()
        }
        updateImportStatusLabel()
        processNextImportIfNeeded()
    }

    /// Русские подписи стадий пайплайна транскрибации для статус-строки.
    private func stageRu(_ stage: String) -> String {
        HistoryPanelController.stageRuStatic(stage)
    }

    /// Форматирует секунды в строку MM:SS (напр. 754.2 → "12:34").
    private func mmss(_ sec: Double) -> String {
        HistoryPanelController.mmssStatic(sec)
    }

    func finishImportQueueIfNeeded() {
        stopImportElapsedTimer()
        guard importJobsPlanned > 0 else {
            importStatusLabel.stringValue = "Импорт: idle"
            cancelImportButton.isEnabled = false
            pauseImportButton.isEnabled = false
            pauseImportButton.title = "Пауза импорта"
            return
        }
        if importJobsCompleted < importJobsPlanned {
            return
        }

        cancelImportButton.isEnabled = false
        pauseImportButton.isEnabled = false
        pauseImportButton.title = "Пауза импорта"
        isImportPaused = false
        importProgressBar.doubleValue = 100.0
        importProgressBar.isHidden = true
        let totalSec = max(0, Int(importDurationTotalSec.rounded()))
        let summary = "Импорт завершён: файлов \(importProcessedTotal)/\(importFilesPlanned), ошибок \(importErrorsTotal), задач \(importJobsCompleted), время \(totalSec)с."
        importStatusLabel.stringValue = summary
        let reportPath = writeImportQueueReport(summary: summary)
        lastImportReportPath = reportPath
        openImportReportButton.isEnabled = (reportPath != nil)
        let errorsPreview = HistoryPanelController.errorsPreviewText(errorMessages: importErrorMessages)
        if let reportPath {
            showInfoAlert(title: "Импорт аудио", body: "\(summary)\(errorsPreview)\nОтчёт: \(reportPath)")
        } else {
            showInfoAlert(title: "Импорт аудио", body: "\(summary)\(errorsPreview)")
        }

        // macOS-уведомление для случая, когда пользователь переключился в другое приложение.
        sendImportNotification(
            filesProcessed: importProcessedTotal,
            errors: importErrorsTotal,
            duration: totalSec
        )
        // Звук завершения импорта (off main thread: AudioQueueXPC.Start синхронный).
        DispatchQueue.global(qos: .userInitiated).async {
            NSSound(named: "Purr")?.play()
        }

        // Сбрасываем агрегаторы для следующей очереди.
        importJobsPlanned = 0
        importJobsCompleted = 0
        importProcessedTotal = 0
        importErrorsTotal = 0
        importErrorMessages.removeAll()
        importDurationTotalSec = 0
        importSessionStartedAt = nil
        importJobSignatures.removeAll()
        importSourceStats.removeAll()
        importFormatStats.removeAll()
        importFilesPlanned = 0
        importBytesPlanned = 0
        importErrorMessages.removeAll()
    }

    func sendImportNotification(filesProcessed: Int, errors: Int, duration: Int) {
        notificationService.notify(
            title: "Krab Ear — Импорт завершён",
            body: "Файлов: \(filesProcessed), ошибок: \(errors), время: \(duration)с"
        )
    }

    func updateImportStatusLabel() {
        if isImportRunning {
            let _ = min(importJobsPlanned, importJobsCompleted + 1)
            let avgSec = importJobsCompleted > 0 ? (importDurationTotalSec / Double(importJobsCompleted)) : 0
            let remainingJobs = max(0, importJobsPlanned - importJobsCompleted)
            let eta = Int((Double(remainingJobs) * avgSec).rounded())
            let currentFiles = currentImportJob?.audioCount ?? 0
            let currentFolders = currentImportJob?.folderCount ?? 0
            let elapsed = currentImportJobStartedAt.map { Int(Date().timeIntervalSince($0).rounded()) } ?? 0
            let folderSuffix = currentFolders > 1 ? " (\(currentFolders) папок)" : ""
            importStatusLabel.stringValue = "Импорт: \(importJobsCompleted + 1)/\(importJobsPlanned) задач" +
                (importFilesPlanned > 0 ? ", файлов \(importProcessedTotal + 1)/\(importFilesPlanned)" : ", файлов \(currentFiles)\(folderSuffix)") +
                ", \(elapsed)с" + (eta > 0 ? ", ETA ~\(eta)с" : "")
            // Update progress bar: progress by jobs completed (file-level is approximated per-job)
            let progress = importJobsPlanned > 0
                ? (Double(importJobsCompleted) / Double(importJobsPlanned)) * 100.0
                : 0.0
            importProgressBar.isHidden = false
            importProgressBar.doubleValue = progress
            return
        }
        importProgressBar.isHidden = true
        if isImportPaused {
            importStatusLabel.stringValue = "Импорт: пауза, в очереди \(importQueue.count), обработано \(importProcessedTotal)/\(importFilesPlanned)"
            return
        }
        if !importQueue.isEmpty {
            let totalFolders = importQueue.reduce(0) { $0 + $1.folderCount }
            let folderSuffix = totalFolders > 1 ? " в \(totalFolders) папках" : ""
            importStatusLabel.stringValue = "Импорт: в очереди \(importQueue.count), файлов \(importFilesPlanned)\(folderSuffix), объём \(HistoryPanelController.formatBytes(importBytesPlanned))"
            return
        }
        if importJobsPlanned > 0 && importJobsCompleted >= importJobsPlanned {
            importStatusLabel.stringValue = "Импорт: завершён"
            return
        }
        importStatusLabel.stringValue = "Импорт: idle"
    }

    func startImportElapsedTimer() {
        stopImportElapsedTimer()
        importElapsedTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.isImportRunning else { return }
                self.updateImportStatusLabel()
            }
        }
    }

    func stopImportElapsedTimer() {
        importElapsedTimer?.invalidate()
        importElapsedTimer = nil
    }

    func normalizedImportSignature(_ paths: [String]) -> String {
        HistoryPanelController.normalizedImportSignatureStatic(paths)
    }

    func writeImportQueueReport(summary: String) -> String? {
        let reportsDir = (NSString(string: "~/Library/Application Support/KrabEar/reports").expandingTildeInPath)
        do {
            try FileManager.default.createDirectory(
                atPath: reportsDir,
                withIntermediateDirectories: true,
                attributes: nil
            )
        } catch {
            return nil
        }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let timestamp = formatter.string(from: Date())
        let reportPath = (reportsDir as NSString).appendingPathComponent("import_queue_\(timestamp).md")
        let startedText = importSessionStartedAt.map { ISO8601DateFormatter().string(from: $0) } ?? "-"
        let finishedText = ISO8601DateFormatter().string(from: Date())

        let errorsSection: String
        if importErrorMessages.isEmpty {
            errorsSection = ""
        } else {
            let bullets = importErrorMessages.map { "- \($0)" }.joined(separator: "\n")
            errorsSection = "\n\n## Errors\n\(bullets)\n"
        }

        let body = """
        # Import Queue Report

        - started_at: \(startedText)
        - finished_at: \(finishedText)
        - planned_jobs: \(importJobsPlanned)
        - completed_jobs: \(importJobsCompleted)
        - planned_files: \(importFilesPlanned)
        - processed_files: \(importProcessedTotal)
        - planned_bytes: \(importBytesPlanned)
        - errors: \(importErrorsTotal)
        - duration_sec: \(Int(importDurationTotalSec.rounded()))
        - sources: \(importSourceStats.map { "\($0.key)=\($0.value)" }.sorted().joined(separator: ", "))
        - formats: \(importFormatStats.map { "\($0.key)=\($0.value)" }.sorted().joined(separator: ", "))

        ## Summary
        \(summary)\(errorsSection)
        """

        do {
            try body.write(toFile: reportPath, atomically: true, encoding: .utf8)
            return reportPath
        } catch {
            return nil
        }
    }
}

// MARK: - Testable static helpers (pure functions)

extension HistoryPanelController {
    /// Статическая версия stageRu — доступна без инстанцирования для юнит-тестов.
    static func stageRuStatic(_ stage: String) -> String {
        switch stage {
        case "audio_load": return "загрузка"
        case "normalize": return "нормализация"
        case "stt": return "распознавание"
        case "cleanup": return "обработка текста"
        case "diarize": return "разделение говорящих"
        case "translate": return "перевод"
        case "llm_rewrite": return "LLM-правка"
        case "idle": return "ожидание"
        default: return stage
        }
    }

    /// Статическая версия mmss — доступна без инстанцирования для юнит-тестов.
    static func mmssStatic(_ sec: Double) -> String {
        let total = max(0, Int(sec.rounded()))
        let minutes = total / 60
        let seconds = total % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }

    /// Статическая версия normalizedImportSignature — для юнит-тестов.
    static func normalizedImportSignatureStatic(_ paths: [String]) -> String {
        let normalized = paths
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .sorted()
        return normalized.joined(separator: "|")
    }

    /// Статическая версия errorsPreview-блока — для юнит-тестов.
    static func errorsPreviewText(errorMessages: [String]) -> String {
        guard !errorMessages.isEmpty else { return "" }
        let shown = errorMessages.prefix(3).map { "• \($0)" }.joined(separator: "\n")
        let more = errorMessages.count > 3 ? "\n… +ещё \(errorMessages.count - 3) (см. отчёт)" : ""
        return "\n\nОшибки:\n\(shown)\(more)"
    }
}

// MARK: - ImportDropZoneView

final class ImportDropZoneView: NSView {
    var onPathsDropped: (([String]) -> Void)?
    private let hintLabel = NSTextField(wrappingLabelWithString: "Перетащите сюда аудиофайлы или папки для пакетной транскрибации")
    private var isHighlighted = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setup()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }

    private func setup() {
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.cgColor
        layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.withAlphaComponent(0.25).cgColor

        registerForDraggedTypes([.fileURL])

        hintLabel.translatesAutoresizingMaskIntoConstraints = false
        hintLabel.alignment = .center
        hintLabel.font = KrabEarTheme.Typography.body
        hintLabel.textColor = .secondaryLabelColor
        hintLabel.maximumNumberOfLines = 2
        hintLabel.lineBreakMode = .byWordWrapping
        addSubview(hintLabel)

        NSLayoutConstraint.activate([
            hintLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            hintLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            hintLabel.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        setHighlighted(true)
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        setHighlighted(false)
        super.draggingExited(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        setHighlighted(false)
        let pasteboard = sender.draggingPasteboard
        guard
            let urls = pasteboard.readObjects(
                forClasses: [NSURL.self],
                options: [.urlReadingFileURLsOnly: true]
            ) as? [URL]
        else {
            return false
        }

        let paths = urls.map(\.path).filter { !$0.isEmpty }
        guard !paths.isEmpty else { return false }
        onPathsDropped?(paths)
        return true
    }

    override func concludeDragOperation(_ sender: NSDraggingInfo?) {
        setHighlighted(false)
        super.concludeDragOperation(sender)
    }

    private func setHighlighted(_ value: Bool) {
        guard isHighlighted != value else { return }
        isHighlighted = value
        layer?.borderColor = value
            ? KrabEarTheme.Colors.accent.cgColor
            : NSColor.separatorColor.cgColor
        layer?.backgroundColor = value
            ? KrabEarTheme.Colors.accent.withAlphaComponent(0.15).cgColor
            : KrabEarTheme.Colors.cardBackground.withAlphaComponent(0.25).cgColor
        hintLabel.textColor = value ? KrabEarTheme.Colors.accent : NSColor.secondaryLabelColor
    }
}
