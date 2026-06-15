/*
 main+StatusDragDrop.swift
 AgentAppDelegate extension: поддержка drag-and-drop аудиофайлов на иконку menu bar.

 Цель: пользователь перетаскивает аудиофайл(ы) на иконку KE в menu bar →
 файлы немедленно ставятся в очередь на транскрибацию (без открытия панели).
 Toast показывает «N файлов в очереди», затем отдельный toast по завершению.

 Архитектура:
 - StatusBarDropView: невидимый NSView поверх statusItem.button,
   реализует NSDraggingDestination. Подсвечивает иконку при наведении.
 - AgentAppDelegate.setupStatusItemDragDrop(): создаёт и монтирует дроп-вью.
 - Обработка дропа: фильтрация по audio extension → IPC transcribe_paths_async
   (off-main согласно AGENT-3) → BackendToast.shared.show() на main thread.

 IPC: используется уже существующий метод transcribe_paths_async
 (RecordingCoreService) — тот же, что и HistoryPanelController+Import.swift.

 Связи модуля:
 1) main+StatusMenu.swift: вызывает setupStatusItemDragDrop() в ensureStatusItem()
    после создания statusItem.
 2) IPCClient: transribe_paths_async off-main (AGENT-3 compliant).
 3) BackendToast: show() — prewarm в main.swift, вызов на main thread.
*/

import AppKit
import Foundation
import UniformTypeIdentifiers

// MARK: - Список поддерживаемых аудио-расширений

/// Допустимые расширения аудиофайлов — синхронизированы с
/// RecordingCoreService.audio_ext в KrabEar/backend/recording_core_service.py.
/// Используем Set для O(1) lookup при проверке каждого файла.
private let kSupportedAudioExtensions: Set<String> = [
    "wav", "mp3", "m4a", "aac", "flac",
    "ogg", "opus", "mp4", "m4b", "aif", "aiff",
]

// MARK: - StatusBarDropView

/// Прозрачный NSView, монтируемый поверх кнопки statusItem.
/// Реализует NSDraggingDestination: принимает .fileURL драги,
/// фильтрует по audio extension, передаёт пути в onAudioPathsDropped.
///
/// Glyph-guard: не рендерит текст напрямую — иконка кнопки управляется
/// refreshStatusItemTitle() из main+StatusMenu.swift.
final class StatusBarDropView: NSView {

    /// Колбэк с отфильтрованными audio-путями (main thread).
    var onAudioPathsDropped: (([String]) -> Void)?

    /// Сохраняем оригинальный цвет button layer для восстановления после drag.
    private var isDragActive = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        setupDragRegistration()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setupDragRegistration()
    }

    private func setupDragRegistration() {
        // Регистрируем только fileURL — игнорируем текст, изображения и прочее.
        registerForDraggedTypes([.fileURL])
    }

    // MARK: - NSDraggingDestination

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard containsAudioURLs(sender) else { return [] }
        setDragHighlight(true)
        return .copy
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard containsAudioURLs(sender) else {
            setDragHighlight(false)
            return []
        }
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        setDragHighlight(false)
        super.draggingExited(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        setDragHighlight(false)
        let urls = extractFileURLs(from: sender)
        let audioPaths = urls
            .filter { isAudioURL($0) }
            .map(\.path)
        guard !audioPaths.isEmpty else { return false }
        // Передаём на main thread — drag callbacks уже на main, но явно
        // используем async чтобы performDragOperation вернулся быстро.
        DispatchQueue.main.async { [weak self] in
            self?.onAudioPathsDropped?(audioPaths)
        }
        return true
    }

    override func concludeDragOperation(_ sender: NSDraggingInfo?) {
        setDragHighlight(false)
        super.concludeDragOperation(sender)
    }

    // MARK: - Helpers

    /// Проверяет наличие хотя бы одного audio-URL в pasteboard (для draggingEntered/Updated).
    private func containsAudioURLs(_ sender: NSDraggingInfo) -> Bool {
        return !extractFileURLs(from: sender).filter { isAudioURL($0) }.isEmpty
    }

    private func extractFileURLs(from sender: NSDraggingInfo) -> [URL] {
        let pasteboard = sender.draggingPasteboard
        return (pasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL]) ?? []
    }

    /// Проверяет, является ли URL аудиофайлом по расширению.
    private func isAudioURL(_ url: URL) -> Bool {
        let ext = url.pathExtension.lowercased()
        return kSupportedAudioExtensions.contains(ext)
    }

    // MARK: - Visual feedback

    /// Подсвечиваем кнопку menu bar изменением alpha overlay во время drag.
    /// Не используем glyph-символы в NSTextField — только CGColor слой.
    private func setDragHighlight(_ active: Bool) {
        guard isDragActive != active else { return }
        isDragActive = active
        wantsLayer = true
        // Тонкий полупрозрачный highlight-overlay поверх иконки.
        // Не меняем title/image statusItem.button — это зона main+StatusMenu.swift.
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.15
            self.animator().layer?.backgroundColor = active
                ? NSColor.controlAccentColor.withAlphaComponent(0.30).cgColor
                : NSColor.clear.cgColor
        }
    }
}

// MARK: - AgentAppDelegate + Drag-Drop Setup

extension AgentAppDelegate {

    // MARK: - Публичный entry-point

    /// Монтирует StatusBarDropView поверх кнопки statusItem.
    /// Вызывать ПОСЛЕ создания statusItem в ensureStatusItem().
    ///
    /// Идемпотентен: повторный вызов безвреден (guard по existing subview).
    func setupStatusItemDragDrop() {
        guard let button = statusItem?.button else { return }

        // Идемпотентность: не добавляем второй дроп-вью при повторном вызове.
        if button.subviews.first(where: { $0 is StatusBarDropView }) != nil { return }

        let dropView = StatusBarDropView(frame: button.bounds)
        dropView.autoresizingMask = [.width, .height]

        // Слабая ссылка на self — дроп-вью живёт пока живёт button.
        dropView.onAudioPathsDropped = { [weak self] paths in
            self?.handleStatusBarAudioDrop(paths: paths)
        }

        button.addSubview(dropView)
    }

    // MARK: - Обработка дропнутых путей (main thread)

    /// Принимает отфильтрованные audio-пути с main thread,
    /// показывает немедленный toast, затем запускает IPC off-main (AGENT-3).
    private func handleStatusBarAudioDrop(paths: [String]) {
        // Drag-validation на main — OK (AGENT-3).
        guard !paths.isEmpty else { return }

        let count = paths.count
        // Немедленный feedback до IPC — toast виден сразу при дропе.
        let queueMsg = count == 1
            ? "1 файл в очереди транскрибации"
            : "\(count) файлов в очереди транскрибации"
        BackendToast.shared.show(queueMsg, duration: 3.0)

        // Захватываем endpoint и paths как Sendable-значения до ухода в background.
        let endpoint = ipcClient.endpoint
        let pathsCopy = paths

        // AGENT-3: IPC только off-main.
        DispatchQueue.global(qos: .userInitiated).async {
            Self.dispatchStatusBarTranscribe(endpoint: endpoint, paths: pathsCopy)
        }
    }

    // MARK: - Off-main IPC вызов (nonisolated static)

    /// Синхронный IPC-вызов transcribe_paths_async в рамках background thread.
    /// `nonisolated static` — не захватывает self, Data-Sendable параметры.
    ///
    /// Использует существующий метод transcribe_paths_async
    /// (RecordingCoreService / HistoryPanelController+Import.swift).
    /// Не возвращает job_id — drag-drop не показывает детальный прогресс
    /// (это задача HistoryPanel), просто ставит в очередь backend.
    private nonisolated static func dispatchStatusBarTranscribe(
        endpoint: String,
        paths: [String]
    ) {
        let client = IPCClient(socketPath: endpoint)

        // Минимальные params: paths + дефолтные профили.
        // Backend подставит default quality/cleanup/translation из settings.
        let params: [String: Any] = [
            "paths": paths,
            // Явно не передаём quality_profile / translation_mode —
            // backend использует текущие настройки пользователя.
        ]

        let response = try? client.call(
            method: "transcribe_paths_async",
            params: params,
            timeoutSec: IPCClient.defaultTimeoutSec
        )

        // Возвращаемся на main только для toast финала.
        DispatchQueue.main.async {
            if let result = response?["result"] as? [String: Any],
               let jobID = result["job_id"] as? String, !jobID.isEmpty {
                // Backend принял задачу — покажем тихое подтверждение.
                BackendToast.shared.show(
                    "Транскрибация запущена (job: \(jobID.prefix(8))...)",
                    duration: 2.5
                )
            } else {
                // Backend не ответил или вернул ошибку — toast об ошибке.
                BackendToast.shared.show(
                    "Не удалось запустить транскрибацию",
                    duration: 4.0
                )
            }
        }
    }
}
