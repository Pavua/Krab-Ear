/*
 Расширение HistoryPanelController — методы импорта аудио.

 Содержит:
 - onImportAudio / onCancelImport / onToggleImportPause / onOpenImportReport
 - enqueueImport / processNextImportIfNeeded / finishImportQueueIfNeeded
 - sendImportNotification / updateImportStatusLabel
 - startImportElapsedTimer / stopImportElapsedTimer
 - normalizedImportSignature / previewImport / writeImportQueueReport
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

        guard panel.runModal() == .OK else { return }
        let paths = panel.urls.map(\.path)
        enqueueImport(paths: paths, sourceTag: "open_panel")
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

        let preview = previewImport(paths: clean)
        if preview.audioCount == 0 {
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
                paths: clean,
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
            ? "\(folderLine)Подготовлено файлов: \(preview.audioCount)\nОбъём: \(formatBytes(preview.totalBytes))\nФорматы: \(extSummary)"
            : "\(folderLine)Подготовлено файлов: \(preview.audioCount)\nОбъём: \(formatBytes(preview.totalBytes))\nФорматы: \(extSummary)\nПримеры:\n\(preview.sample.joined(separator: "\n"))"
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

        DispatchQueue.global(qos: .userInitiated).async { [weak self, endpoint, jobPaths, qualityProfile, cleanupProfile, translationMode, translationStyle, translateAndPaste] in
            let backgroundClient = IPCClient(socketPath: endpoint)
            let response = try? backgroundClient.call(
                method: "transcribe_paths",
                params: [
                    "paths": jobPaths,
                    "quality_profile": qualityProfile,
                    "cleanup_profile": cleanupProfile,
                    "translation_mode": translationMode,
                    "translation_style": translationStyle,
                    "translate_and_paste": translateAndPaste,
                ]
            )
            let result = response?["result"] as? [String: Any]
            let processed = (result?["processed"] as? Int) ?? 0
            let errorStrings = (result?["errors"] as? [String]) ?? []
            let errors = errorStrings.count
            let failed = (result == nil)
            let durationSec = Date().timeIntervalSince(startedAt)

            DispatchQueue.main.async {
                guard let self else { return }
                self.isImportRunning = false
                self.stopImportElapsedTimer()
                self.currentImportJobStartedAt = nil
                self.importJobsCompleted += 1
                self.importProcessedTotal += processed
                self.importErrorsTotal += failed ? 1 : errors
                if failed {
                    self.importErrorMessages.append("IPC error: backend вернул пустой ответ")
                } else {
                    self.importErrorMessages.append(contentsOf: errorStrings)
                }
                self.importDurationTotalSec += durationSec
                self.importJobSignatures.remove(signature)
                self.currentImportJob = nil
                self.loadInitial()

                if self.importCancellationRequested {
                    self.importCancellationRequested = false
                    self.isImportPaused = false
                    self.importQueue.removeAll()
                }
                self.updateImportStatusLabel()
                self.processNextImportIfNeeded()
            }
        }
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
        let errorsPreview: String
        if importErrorMessages.isEmpty {
            errorsPreview = ""
        } else {
            let shown = importErrorMessages.prefix(3).map { "• \($0)" }.joined(separator: "\n")
            let more = importErrorMessages.count > 3 ? "\n… +ещё \(importErrorMessages.count - 3) (см. отчёт)" : ""
            errorsPreview = "\n\nОшибки:\n\(shown)\(more)"
        }
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
        // Звук завершения импорта.
        NSSound(named: "Purr")?.play()

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
    }

    func sendImportNotification(filesProcessed: Int, errors: Int, duration: Int) {
        notificationService.notify(
            title: "Krab Ear — Импорт завершён",
            body: "Файлов: \(filesProcessed), ошибок: \(errors), время: \(duration)с"
        )
    }

    func updateImportStatusLabel() {
        if isImportRunning {
            let current = min(importJobsPlanned, importJobsCompleted + 1)
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
            importStatusLabel.stringValue = "Импорт: в очереди \(importQueue.count), файлов \(importFilesPlanned)\(folderSuffix), объём \(formatBytes(importBytesPlanned))"
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
        let normalized = paths
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .sorted()
        return normalized.joined(separator: "|")
    }

    func previewImport(paths: [String]) -> ImportPreview {
        guard
            let response = try? ipcClient.call(
                method: "preview_transcribe_paths",
                params: ["paths": paths, "sample_limit": 3]
            ),
            let result = response["result"] as? [String: Any]
        else {
            return ImportPreview(audioCount: 0, folderCount: 0, sample: [], byExtension: [:], totalBytes: 0)
        }
        let audioCount = (result["audio_count"] as? Int) ?? 0
        let folderCount = (result["folder_count"] as? Int) ?? 0
        let sample = (result["sample"] as? [String]) ?? []
        let totalBytes = (result["total_bytes"] as? Int) ?? 0
        let byExtension = (result["by_ext"] as? [String: Int]) ?? [:]
        return ImportPreview(
            audioCount: audioCount,
            folderCount: folderCount,
            sample: sample,
            byExtension: byExtension,
            totalBytes: totalBytes
        )
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
