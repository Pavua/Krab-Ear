/*
 Точка входа нативного агента Krab Ear для macOS.

 Связи модуля:
 1) BackendSupervisor + IPCClient: управление Python backend и командами записи/истории.
 2) HotkeyManager: глобальный hotkey Right Option для toggle записи.
 3) HistoryPanelController: панель безлимитной истории с пагинацией и поиском.
 4) PasteService: вставка текста в активное поле с fallback в буфер обмена.
*/

import AppKit
import Foundation
import AVFoundation

struct LaunchOptions {
    let projectRoot: String
    let showHistoryOnLaunch: Bool
    let launchedByLaunchd: Bool

    init(arguments: [String]) {
        var explicitProjectRoot: String?
        var showHistory = false
        var launchedByLaunchd = false

        var idx = 1
        while idx < arguments.count {
            let arg = arguments[idx]
            switch arg {
            case "--project-root":
                if idx + 1 < arguments.count {
                    explicitProjectRoot = arguments[idx + 1]
                    idx += 1
                }
            case "--show-history":
                showHistory = true
            case "--launched-by-launchd":
                launchedByLaunchd = true
            default:
                break
            }
            idx += 1
        }

        self.projectRoot = Self.resolveProjectRoot(explicitPath: explicitProjectRoot, executablePath: arguments.first)
        self.showHistoryOnLaunch = showHistory
        self.launchedByLaunchd = launchedByLaunchd
    }

    private static func resolveProjectRoot(explicitPath: String?, executablePath: String?) -> String {
        let fileManager = FileManager.default

        func isProjectRoot(_ path: String) -> Bool {
            let backend = (path as NSString).appendingPathComponent("KrabEar/backend/service.py")
            return fileManager.fileExists(atPath: backend)
        }

        if let explicitPath {
            let expanded = NSString(string: explicitPath).expandingTildeInPath
            if isProjectRoot(expanded) {
                return expanded
            }
        }

        if let envRoot = ProcessInfo.processInfo.environment["KRAB_EAR_PROJECT_ROOT"] {
            let expanded = NSString(string: envRoot).expandingTildeInPath
            if isProjectRoot(expanded) {
                return expanded
            }
        }

        let cwd = fileManager.currentDirectoryPath
        if isProjectRoot(cwd) {
            return cwd
        }

        if let executablePath {
            let execURL = URL(fileURLWithPath: executablePath).standardizedFileURL
            var probe = execURL.deletingLastPathComponent()
            for _ in 0..<8 {
                let candidate = probe.path
                if isProjectRoot(candidate) {
                    return candidate
                }
                probe.deleteLastPathComponent()
            }
        }

        return cwd
    }
}

struct LastTranscriptionSnapshot {
    let finalText: String
    let originalText: String
    let translatedText: String
    let historyId: String?
    let translationMode: String
    let translationStatus: String
}

@MainActor
final class AgentAppDelegate: NSObject, NSApplicationDelegate {
    private let controlNotificationName = Notification.Name("com.krabear.agent.control")
    private let options: LaunchOptions
    let backendSupervisor: BackendSupervisor
    private let launchAgentManager: LaunchAgentManager
    var ipcClient: IPCClient

    private let pasteService = PasteService()
    let audioDuckingService = SystemAudioDuckingService()
    private let notificationService = NotificationService()
    private let realtimeOverlay = RealtimeOverlayController()
    let logger = AgentLogger.shared

    private var historyPanel: HistoryPanelController?
    private var hotkeyManager: HotkeyManager?
    private var statusItem: NSStatusItem?
    // PR 1.5: Wake word listener (Porcupine)
    private var wakeWordListener: WakeWordListener?
    private var quickStartController: QuickStartWindowController?
    private var realtimeOverlayTimer: Timer?

    var settings: AgentSettings = .default
    var isRecording = false
    var isProcessing = false
    var lastToggleRequestAt: TimeInterval = 0
    let toggleDebounceSec: TimeInterval = 0.35
    var recordingTargetApp: NSRunningApplication?
    private var lastExternalApp: NSRunningApplication?
    private var hasShownAccessibilityHint = false
    var lastResult: LastTranscriptionSnapshot?
    var lastPreviewTranslationSource = ""
    var lastPreviewTranslationText = ""
    var lastPreviewTranslationAt: TimeInterval = 0
    var lastPreviewTranslationMode = ""
    var lastPreviewTranslationFailureAt: TimeInterval = 0
    var lastPreviewTranslationFailures = 0
    var lastPreviewTranslationSuccessAt: TimeInterval = 0
    private var recentAutoPasteFingerprints: [String: TimeInterval] = [:]

    init(options: LaunchOptions) {
        self.options = options
        self.backendSupervisor = BackendSupervisor(projectRoot: options.projectRoot)
        self.launchAgentManager = LaunchAgentManager(projectRoot: options.projectRoot)
        self.ipcClient = IPCClient(socketPath: (NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath as NSString).appendingPathComponent("krabear.sock"))
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        logger.info("Старт агента. projectRoot=\(options.projectRoot), launchedByLaunchd=\(options.launchedByLaunchd)")
        logger.info("BackendSupervisor режим: \(backendSupervisor.supervisionMode == .passive ? "passive (launchd Variant B)" : "active (standalone)")")
        notificationService.requestAuthorizationIfNeeded()
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(handleControlNotification(_:)),
            name: controlNotificationName,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(handleWorkspaceActivatedApp(_:)),
            name: NSWorkspace.didActivateApplicationNotification,
            object: nil
        )

        do {
            try backendSupervisor.ensureBackendRunning()
            logger.info("Backend доступен и готов к IPC")
        } catch {
            logger.error("Backend недоступен: \(error.localizedDescription)")
            showFatalAndTerminate(
                title: "Krab Ear: backend недоступен",
                body: error.localizedDescription
            )
            return
        }

        ipcClient = IPCClient(socketPath: backendSupervisor.socketPath)
        settings = loadSettings()
        realtimeOverlay.setOpacityPercent(settings.overlayOpacityPercent)
        logger.info(
            "Настройки загружены: mode=\(settings.mode), autoPaste=\(settings.autoPaste), quality=\(settings.qualityProfile), translation=\(settings.translationMode)"
        )

    // PermissionWizard удален, используем QuickStartWindowController
        historyPanel = HistoryPanelController(
            ipcClient: ipcClient,
            settingsProvider: { [weak self] in
                self?.settings ?? .default
            },
            settingsUpdater: { [weak self] payload in
                self?.updateSettingsFromPanel(payload) ?? .default
            },
            onToggleRecording: { [weak self] in
                self?.handleRecordToggleRequest()
            },
            onRestartAgent: { [weak self] in
                self?.restartAgent()
            },
            onStopAgent: { [weak self] in
                self?.stopAgent()
            },
            onPasteHistoryItem: { [weak self] item in
                self?.pasteSnapshotText(
                    text: item.text,
                    historyId: item.id,
                    sourceTag: "history_panel"
                )
            },
            onSwapRuEsDirection: { [weak self] in
                self?.swapRuEsDirection()
            }
        )

        hotkeyManager = HotkeyManager(variant: settings.hotkey, onToggle: { [weak self] in
            DispatchQueue.main.async {
                self?.handleRecordToggleRequest()
            }
        })

        // PR 1.5: Wire Right Option double-tap → Разговор с AI trigger
        hotkeyManager?.onConversationDoubleTap = { [weak self] in
            DispatchQueue.main.async {
                self?.historyPanel?.triggerConversationStart()
            }
        }

        hotkeyManager?.start()
        logger.info("Глобальный hotkey активирован")

        // PR 1.5: Wake word listener (default OFF — toggle в Settings)
        setupWakeWordListenerIfEnabled()

        applyMode(settings.mode, persist: false)

        if !settings.onboardingCompleted && !options.launchedByLaunchd {
            openQuickStart()
        } else if options.showHistoryOnLaunch {
            openHistoryPanel(forceMenubar: true)
        }
    }

    // MARK: - Backend recovery (see main+IPCRecovery.swift)

    func applicationWillTerminate(_ notification: Notification) {
        DistributedNotificationCenter.default().removeObserver(self)
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        hotkeyManager?.stop()
        wakeWordListener?.stop()
        stopRealtimeOverlayPolling()
        backendSupervisor.stopBackend()
    }

    // MARK: - PR 1.5: Wake Word Setup

    /// Инициализирует и запускает WakeWordListener если включён в настройках.
    /// Дефолт: выключен (приватность). Включается в Settings → Аудио-пайплайн.
    func setupWakeWordListenerIfEnabled() {
        let enabled = UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled")
        guard enabled else {
            logger.info("Wake word listener: выключен (UserDefaults KrabEar_WakeWordEnabled=false)")
            return
        }

        let listener = WakeWordListener { [weak self] in
            DispatchQueue.main.async {
                self?.historyPanel?.triggerConversationFromWakeWord()
            }
        }

        let started = listener.start()
        if started {
            wakeWordListener = listener
            logger.info("Wake word listener «Краб» запущен.")
        } else {
            logger.warn("Wake word listener не удалось запустить. Проверьте AccessKey и .ppn файл.")
        }
    }

    /// Перезапустить WakeWordListener с новым значением enabled.
    /// Вызывается из HistoryPanelController+Settings при изменении тогглера.
    func applyWakeWordEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: "KrabEar_WakeWordEnabled")
        wakeWordListener?.stop()
        wakeWordListener = nil

        if enabled {
            setupWakeWordListenerIfEnabled()
        }
    }

    /// Включить/выключить Right Option double-tap hotkey для Разговора с AI.
    func applyConversationHotkeyEnabled(_ enabled: Bool) {
        if enabled {
            hotkeyManager?.onConversationDoubleTap = { [weak self] in
                DispatchQueue.main.async {
                    self?.historyPanel?.triggerConversationStart()
                }
            }
        } else {
            hotkeyManager?.onConversationDoubleTap = nil
        }
        logger.info("Conversation hotkey double-tap: \(enabled ? "включён" : "выключен")")
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            openHistoryPanel(forceMenubar: false)
        }
        return true
    }

    @objc private func onRecordToggle() {
        handleRecordToggleRequest()
    }

    @objc private func onOpenHistory() {
        openHistoryPanel(forceMenubar: false)
    }

    @objc private func onOpenTranscriptsInFinder() {
        let dataDir = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath
        let url = URL(fileURLWithPath: dataDir, isDirectory: true)
        NSWorkspace.shared.open(url)
    }

    @objc private func onModeToggle() {
        let nextMode = settings.mode == "menubar" ? "headless" : "menubar"
        applyMode(nextMode, persist: true)
    }

    @objc private func onQualityBalanced() {
        settings.qualityProfile = "balanced"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onQualityMax() {
        settings.qualityProfile = "max"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onAutoPasteToggle() {
        settings.autoPaste.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onStartSoundToggle() {
        settings.playStartSound.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onAudioDuckingToggle() {
        settings.audioDuckingEnabled.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onAudioDuckingPercent25() {
        settings.audioDuckingPercent = 25
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onAudioDuckingPercent50() {
        settings.audioDuckingPercent = 50
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onAudioDuckingPercent75() {
        settings.audioDuckingPercent = 75
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onAudioDuckingPercent100() {
        settings.audioDuckingPercent = 100
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onOverlayOpacity25() {
        settings.overlayOpacityPercent = 25
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onOverlayOpacity45() {
        settings.overlayOpacityPercent = 45
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onOverlayOpacity65() {
        settings.overlayOpacityPercent = 65
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onAutostartToggle() {
        settings.autoStartEnabled.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onDockIconToggle() {
        settings.showDockIcon.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onCompactHistory() {
        _ = try? ipcClient.call(method: "compact_history", params: [:])
        historyPanel?.showPanel()
    }

    @objc private func onNetworkOfflineDefault() {
        settings.networkMode = "offline_default"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onNetworkOfflineStrict() {
        settings.networkMode = "offline_strict"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onNetworkOnlineOptIn() {
        settings.networkMode = "online_opt_in"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslateAndPasteToggle() {
        settings.translateAndPaste.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationModeOff() {
        settings.translationMode = "off"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationModeRuToEs() {
        settings.translationMode = "ru_to_es"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationModeEsToRu() {
        settings.translationMode = "es_to_ru"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationModeEnToRu() {
        settings.translationMode = "en_to_ru"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationModeAuto() {
        settings.translationMode = "auto"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationModeBilingualRuEs() {
        settings.translationMode = "bilingual_ru_es"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onApplyLiveTranslationPreset() {
        settings.translationMode = "auto"
        settings.translationStyle = "chat"
        settings.translateAndPaste = false
        settings.realtimePreviewEnabled = true
        settings.networkMode = "offline_default"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        notify(
            title: "Krab Ear",
            body: "Live Translation preset включен: auto + chat + realtime."
        )
    }

    @objc private func onSwapRuEsDirection() {
        swapRuEsDirection()
    }

    private func swapRuEsDirection() {
        let nextMode: String
        switch settings.translationMode {
        case "ru_to_es":
            nextMode = "es_to_ru"
        case "es_to_ru":
            nextMode = "ru_to_es"
        case "bilingual_ru_es":
            nextMode = "ru_to_es"
        default:
            nextMode = "ru_to_es"
        }
        settings.translationMode = nextMode
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        notify(
            title: "Krab Ear",
            body: "Направление перевода переключено: \(nextMode == "ru_to_es" ? "RU -> ES" : "ES -> RU")."
        )
    }

    @objc private func onTranslationStyleNeutral() {
        settings.translationStyle = "neutral"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationStyleChat() {
        settings.translationStyle = "chat"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onTranslationStyleFormal() {
        settings.translationStyle = "formal"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onClipboardModeAlways() {
        settings.clipboardMode = "always_copy"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onClipboardModeOnFail() {
        settings.clipboardMode = "copy_on_fail"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onClipboardModeNever() {
        settings.clipboardMode = "never_copy"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc private func onHotkeyProfileDefault() {
        applyHotkeyProfile("default")
    }

    @objc private func onHotkeyProfileMeeting() {
        applyHotkeyProfile("meeting")
    }

    @objc private func onHotkeyProfileTranslation() {
        applyHotkeyProfile("translation")
    }

    @objc private func onUpdateChannelStable() {
        settings.updateChannel = "stable"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        notify(title: "Krab Ear", body: "Канал обновлений: stable")
    }

    @objc private func onUpdateChannelBeta() {
        settings.updateChannel = "beta"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        notify(title: "Krab Ear", body: "Канал обновлений: beta")
    }

    private func applyHotkeyProfile(_ profile: String) {
        switch profile {
        case "meeting":
            settings.hotkeyProfile = "meeting"
            settings.translationMode = "auto_to_ru"
            settings.translationStyle = "chat"
            settings.translateAndPaste = false
            settings.autoPaste = false
            settings.realtimePreviewEnabled = true
            settings.clipboardMode = "copy_on_fail"
        case "translation":
            settings.hotkeyProfile = "translation"
            settings.translationMode = "auto"
            settings.translationStyle = "chat"
            settings.translateAndPaste = true
            settings.autoPaste = false
            settings.realtimePreviewEnabled = true
            settings.clipboardMode = "always_copy"
        default:
            settings.hotkeyProfile = "default"
            settings.translationMode = "off"
            settings.translationStyle = "neutral"
            settings.translateAndPaste = false
            settings.autoPaste = true
            settings.realtimePreviewEnabled = true
            settings.clipboardMode = "always_copy"
        }
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        let title: String
        switch settings.hotkeyProfile {
        case "meeting":
            title = "Meeting"
        case "translation":
            title = "Translation"
        default:
            title = "Default"
        }
        notify(title: "Krab Ear", body: "Профиль hotkey переключен: \(title)")
    }

    @objc private func onCopyLastResult() {
        guard let lastResult else {
            notify(title: "Krab Ear", body: "Пока нет последнего результата для копирования")
            return
        }
        pasteService.putToClipboard(lastResult.finalText)
        notify(title: "Krab Ear", body: "Последний результат скопирован в буфер")
    }

    @objc private func onPasteLastResult() {
        guard let lastResult else {
            notify(title: "Krab Ear", body: "Пока нет последнего результата для вставки")
            return
        }
        pasteSnapshotText(
            text: lastResult.finalText,
            historyId: lastResult.historyId,
            sourceTag: "last_final"
        )
    }

    @objc private func onPasteLastOriginal() {
        guard let lastResult else {
            notify(title: "Krab Ear", body: "Пока нет исходного текста для вставки")
            return
        }
        pasteSnapshotText(
            text: lastResult.originalText,
            historyId: lastResult.historyId,
            sourceTag: "last_original"
        )
    }

    @objc private func onPasteLastTranslation() {
        guard let lastResult else {
            notify(title: "Krab Ear", body: "Пока нет перевода для вставки")
            return
        }
        let translated = lastResult.translatedText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !translated.isEmpty else {
            notify(title: "Krab Ear", body: "Последний результат не содержит перевода")
            return
        }
        pasteSnapshotText(
            text: translated,
            historyId: lastResult.historyId,
            sourceTag: "last_translation"
        )
    }

    @objc private func onPasteLastPlainText() {
        guard let lastResult else {
            notify(title: "Krab Ear", body: "Пока нет последнего результата для plain-вставки")
            return
        }
        let plain = normalizePlainText(lastResult.finalText)
        guard !plain.isEmpty else {
            notify(title: "Krab Ear", body: "Последний результат пуст после нормализации")
            return
        }
        pasteSnapshotText(
            text: plain,
            historyId: lastResult.historyId,
            sourceTag: "last_plain"
        )
    }

    @objc private func onApplyTemplateRu() {
        applyTemplateAndPaste(templateKey: "follow_up_ru")
    }

    @objc private func onApplyTemplateEs() {
        applyTemplateAndPaste(templateKey: "follow_up_es")
    }

    private func applyTemplateAndPaste(templateKey: String) {
        guard let lastResult else {
            notify(title: "Krab Ear", body: "Нет последнего результата для шаблона")
            return
        }
        let template = settings.textTemplates[templateKey]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !template.isEmpty else {
            notify(title: "Krab Ear", body: "Шаблон `\(templateKey)` не задан")
            return
        }
        let source = lastResult.originalText.trimmingCharacters(in: .whitespacesAndNewlines)
        let fallbackSource = lastResult.finalText.trimmingCharacters(in: .whitespacesAndNewlines)
        let baseText = source.isEmpty ? fallbackSource : source
        let rendered = template
            .replacingOccurrences(of: "{text}", with: baseText)
            .replacingOccurrences(of: "{next_step}", with: "уточнить детали")
            .replacingOccurrences(of: "{lang}", with: settings.translationMode)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !rendered.isEmpty else {
            notify(title: "Krab Ear", body: "Шаблон после подстановки пуст")
            return
        }
        pasteSnapshotText(
            text: rendered,
            historyId: lastResult.historyId,
            sourceTag: "template_\(templateKey)"
        )
    }

    @objc private func onQuit() {
        NSApp.terminate(nil)
    }

    @objc private func onRestartAgent() {
        restartAgent()
    }

    @objc private func onStopAgent() {
        stopAgent()
    }

    @objc private func handleControlNotification(_ notification: Notification) {
        guard
            let userInfo = notification.userInfo,
            let action = userInfo["action"] as? String
        else {
            return
        }

        switch action {
        case "show_history":
            openHistoryPanel(forceMenubar: true)
        case "toggle_recording":
            handleRecordToggleRequest()
        case "quit":
            NSApp.terminate(nil)
        default:
            break
        }
    }

    @objc private func handleWorkspaceActivatedApp(_ notification: Notification) {
        guard
            let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
        else {
            return
        }
        guard app.processIdentifier != ProcessInfo.processInfo.processIdentifier else { return }
        guard app.activationPolicy == .regular else { return }
        let bundle = app.bundleIdentifier ?? ""
        if bundle == "com.apple.finder" || bundle == "com.apple.systemuiserver" || bundle == "com.apple.dock" {
            return
        }
        lastExternalApp = app
    }

    // MARK: - Hotkey recording (see main+HotkeyRecording.swift)

    func handleTranscriptionResult(text: String, historyId: String?) {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            logger.warn("handleTranscriptionResult получил пустой текст")
            notify(title: "Krab Ear", body: "Пустой текст после транскрибации")
            return
        }
        let effectiveHistoryId = ensureHistoryItem(text: cleanText, existingId: historyId)
        if lastResult == nil || lastResult?.finalText != cleanText {
            lastResult = LastTranscriptionSnapshot(
                finalText: cleanText,
                originalText: cleanText,
                translatedText: "",
                historyId: effectiveHistoryId,
                translationMode: "off",
                translationStatus: "not_requested"
            )
        }
        logger.info("Транскрибация готова: len=\(cleanText.count), history_id=\(effectiveHistoryId ?? "nil")")

        // Защита от случайной повторной автовставки одного и того же результата.
        if isDuplicateAutopasteCandidate(historyId: effectiveHistoryId, text: cleanText) {
            logger.warn("Пропущена дублирующая автовставка: history_id=\(effectiveHistoryId ?? "nil")")
            return
        }

        if settings.clipboardMode == "always_copy" {
            pasteService.putToClipboard(cleanText)
        }

        guard settings.autoPaste else {
            markPasteStatus(historyId: effectiveHistoryId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.info("Автовставка выключена, текст сохранён в истории")
            if settings.clipboardMode == "always_copy" {
                notify(title: "Krab Ear", body: "Текст скопирован в буфер обмена")
            } else {
                notify(title: "Krab Ear", body: "Текст сохранён в истории")
            }
            return
        }

        guard let targetApp = resolvePreferredPasteTargetApp() else {
            markPasteStatus(historyId: effectiveHistoryId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.warn("Не найден target app для вставки")
            handlePasteFailure(reason: "no_external_target")
            return
        }
        let targetPID = activateTargetForPaste(targetApp)
        let pasteResult = pasteService.pasteToFrontmostApp(cleanText, targetPID: targetPID)
        logger.info(
            "Попытка вставки: bundle=\(targetApp.bundleIdentifier ?? "unknown"), pid=\(targetPID), ok=\(pasteResult.ok), reason=\(pasteResult.reason)"
        )
        markPasteStatus(historyId: effectiveHistoryId, status: pasteResult.ok ? "ok" : "failed")
        historyPanel?.onHistoryDidUpdate()

        if !pasteResult.ok {
            handlePasteFailure(reason: pasteResult.reason, text: cleanText)
        }

        // Звук завершения транскрибации.
        NSSound(named: "Purr")?.play()
    }

    private func isDuplicateAutopasteCandidate(historyId: String?, text: String) -> Bool {
        let now = Date().timeIntervalSince1970
        let normalizedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = historyId?.isEmpty == false ? "id:\(historyId!)" : "text:\(normalizedText)"
        if let previous = recentAutoPasteFingerprints[key], (now - previous) < 4.0 {
            return true
        }
        recentAutoPasteFingerprints[key] = now

        // Периодически чистим старые отпечатки, чтобы структура не росла бесконечно.
        if recentAutoPasteFingerprints.count > 120 {
            let cutoff = now - 120.0
            recentAutoPasteFingerprints = recentAutoPasteFingerprints.filter { $0.value >= cutoff }
        }
        return false
    }

    func pasteSnapshotText(text: String, historyId: String?, sourceTag: String) {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            notify(title: "Krab Ear", body: "Нечего вставлять: текст пустой")
            return
        }

        pasteService.putToClipboard(cleanText)
        guard let targetApp = resolvePreferredPasteTargetApp() else {
            markPasteStatus(historyId: historyId, status: "failed")
            historyPanel?.onHistoryDidUpdate()
            logger.warn("Быстрая вставка \(sourceTag): target app не найден")
            handlePasteFailure(reason: "no_external_target")
            return
        }

        let targetPID = activateTargetForPaste(targetApp)
        let pasteResult = pasteService.pasteToFrontmostApp(cleanText, targetPID: targetPID)
        logger.info(
            "Быстрая вставка \(sourceTag): bundle=\(targetApp.bundleIdentifier ?? "unknown"), pid=\(targetPID), ok=\(pasteResult.ok), reason=\(pasteResult.reason)"
        )
        markPasteStatus(historyId: historyId, status: pasteResult.ok ? "ok" : "failed")
        historyPanel?.onHistoryDidUpdate()
        if !pasteResult.ok {
            handlePasteFailure(reason: pasteResult.reason)
        }
    }

    private func handlePasteFailure(reason: String, text: String? = nil) {
        let details: String
        switch reason {
        case "accessibility_not_granted":
            details = "Не выдан доступ Accessibility. Откройте: Системные настройки -> Конфиденциальность и безопасность -> Accessibility."
            if hasShownAccessibilityHint {
                logger.warn("Повторная ошибка accessibility_not_granted подавлена")
                return
            }
            hasShownAccessibilityHint = true
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
                NSWorkspace.shared.open(url)
            }
        case "no_editable_focus":
            details = "Активное окно найдено, но текстовое поле не в фокусе."
        case "no_external_target", "no_external_target_app":
            details = "Не найдено внешнее активное приложение для вставки."
        case "modifiers_stuck":
            details = "Клавиши-модификаторы не были отпущены вовремя."
        default:
            details = "Причина: \(reason)."
        }

        if let text, settings.clipboardMode != "never_copy" {
            pasteService.putToClipboard(text)
        }

        // Не выдёргиваем панель поверх всех окон при чисто permission-проблеме.
        if reason != "accessibility_not_granted" {
            if settings.mode != "menubar" {
                applyMode("menubar", persist: true)
            }
            openHistoryPanel(forceMenubar: false)
        }
        logger.warn("Вставка не удалась: \(details)")
        notify(
            title: "Krab Ear",
            body: "Вставка не удалась. \(details) Текст сохранён в истории и буфере обмена."
        )
    }

    private func markPasteStatus(historyId: String?, status: String) {
        guard let historyId else { return }
        logger.info("Обновление paste_status: history_id=\(historyId), status=\(status)")
        _ = try? ipcClient.call(
            method: "set_paste_status",
            params: [
                "id": historyId,
                "paste_status": status,
            ]
        )
    }

    private func openHistoryPanel(forceMenubar: Bool) {
        if forceMenubar && settings.mode != "menubar" {
            applyMode("menubar", persist: true)
        }
        historyPanel?.showPanel()
    }

    private func loadSettings() -> AgentSettings {
        do {
            let response = try callWithRecovery(method: "get_settings", params: [:])
            guard let result = response["result"] as? [String: Any] else {
                return .default
            }
            return AgentSettings(from: result)
        } catch {
            return .default
        }
    }

    private func persistSettingsPayload(_ payload: [String: Any]) {
        let previous = settings
        do {
            let response = try callWithRecovery(method: "set_settings", params: payload)
            guard let result = response["result"] as? [String: Any] else {
                return
            }
            settings = AgentSettings(from: result)
            applySettingsSideEffects(previous: previous, current: settings)
            applyMode(settings.mode, persist: false)
        } catch {
            notify(title: "Krab Ear", body: "Не удалось сохранить настройки")
        }
    }

    private func updateSettingsFromPanel(_ payload: [String: Any]) -> AgentSettings {
        persistSettingsPayload(payload)
        rebuildStatusMenu()
        return settings
    }

    private func applyMode(_ mode: String, persist: Bool) {
        let normalized = mode == "menubar" ? "menubar" : "headless"
        settings.mode = normalized
        applyActivationPolicy()

        if normalized == "menubar" {
            ensureStatusItem()
        } else {
            removeStatusItem()
        }

        if persist {
            persistSettingsPayload(settings.toPayload())
        }

        refreshStatusItemTitle()
        rebuildStatusMenu()
    }

    private func ensureStatusItem() {
        if statusItem == nil {
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            item.button?.title = "KE"
            item.button?.toolTip = "Krab Ear"
            statusItem = item
        }
    }

    private func applyActivationPolicy() {
        let policy: NSApplication.ActivationPolicy = settings.showDockIcon ? .regular : .accessory
        _ = NSApp.setActivationPolicy(policy)
    }

    private func applySettingsSideEffects(previous: AgentSettings, current: AgentSettings) {
        if previous.autoStartEnabled != current.autoStartEnabled {
            launchAgentManager.setAutostart(enabled: current.autoStartEnabled)
        }
        if previous.realtimePreviewEnabled != current.realtimePreviewEnabled {
            if current.realtimePreviewEnabled && isRecording {
                startRealtimeOverlayPolling()
            } else {
                stopRealtimeOverlayPolling()
            }
        }
        if previous.overlayOpacityPercent != current.overlayOpacityPercent {
            realtimeOverlay.setOpacityPercent(current.overlayOpacityPercent)
        }
        if previous.hotkey != current.hotkey {
            logger.info("Перезапуск hotkey manager с вариантом: \(current.hotkey)")
            hotkeyManager?.stop()
            hotkeyManager = HotkeyManager(variant: current.hotkey, onToggle: { [weak self] in
                DispatchQueue.main.async {
                    self?.handleRecordToggleRequest()
                }
            })
            hotkeyManager?.start()
        }
    }

    func captureRecordingTargetApp() {
        if let frontmost = NSWorkspace.shared.frontmostApplication,
           frontmost.processIdentifier != ProcessInfo.processInfo.processIdentifier,
           frontmost.activationPolicy == .regular {
            recordingTargetApp = frontmost
            lastExternalApp = frontmost
            logger.info("Запомнен target app на старте записи: \(frontmost.bundleIdentifier ?? "unknown")")
            return
        }
        recordingTargetApp = lastExternalApp
        if let lastExternalApp {
            logger.info("Использован fallback target app: \(lastExternalApp.bundleIdentifier ?? "unknown")")
        } else {
            logger.warn("Не удалось определить target app на старте записи")
        }
    }

    private func resolvePreferredPasteTargetApp() -> NSRunningApplication? {
        let selfPID = ProcessInfo.processInfo.processIdentifier
        if let current = NSWorkspace.shared.frontmostApplication,
           current.processIdentifier != selfPID,
           current.activationPolicy == .regular {
            return current
        }
        if let recordingTargetApp, !recordingTargetApp.isTerminated {
            return recordingTargetApp
        }
        if let lastExternalApp, !lastExternalApp.isTerminated {
            return lastExternalApp
        }
        return nil
    }

    private func activateTargetForPaste(_ target: NSRunningApplication) -> pid_t {
        let currentPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        if currentPID != target.processIdentifier {
            logger.info("Активация target app перед вставкой: \(target.bundleIdentifier ?? "unknown")")
            _ = target.activate(options: [.activateIgnoringOtherApps])

            var attempts = 0
            while attempts < 10 {
                let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier
                if pid == target.processIdentifier {
                    break
                }
                usleep(40_000)
                attempts += 1
            }
        }
        return target.processIdentifier
    }

    private func ensureHistoryItem(text: String, existingId: String?) -> String? {
        if let existingId, !existingId.isEmpty {
            return existingId
        }
        guard
            let response = try? ipcClient.call(
                method: "add_history_item",
                params: [
                    "text": text,
                    "paste_status": "failed",
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            logger.warn("Не удалось создать fallback запись в истории")
            return nil
        }
        logger.info("Создана fallback запись истории: id=\(result["id"] as? String ?? "nil")")
        return result["id"] as? String
    }

    private func removeStatusItem() {
        guard let statusItem else {
            return
        }
        NSStatusBar.system.removeStatusItem(statusItem)
        self.statusItem = nil
    }

    func refreshStatusItemTitle() {
        guard let button = statusItem?.button else {
            return
        }
        if isRecording {
            button.title = "KE ●"
        } else if isProcessing {
            button.title = "KE …"
        } else {
            button.title = "KE"
        }
    }

    func rebuildStatusMenu() {
        guard let statusItem else {
            return
        }

        let menu = NSMenu()

        let recordItem = NSMenuItem(
            title: isRecording ? "Остановить запись" : "Начать запись",
            action: #selector(onRecordToggle),
            keyEquivalent: ""
        )
        recordItem.target = self
        recordItem.isEnabled = !isProcessing
        menu.addItem(recordItem)

        let historyItem = NSMenuItem(
            title: "Открыть историю",
            action: #selector(onOpenHistory),
            keyEquivalent: "h"
        )
        historyItem.target = self
        historyItem.keyEquivalentModifierMask = [.command, .option]
        menu.addItem(historyItem)

        let showPanelItem = NSMenuItem(
            title: "Показать панель",
            action: #selector(onOpenHistory),
            keyEquivalent: "k"
        )
        showPanelItem.target = self
        showPanelItem.keyEquivalentModifierMask = [.command, .shift]
        menu.addItem(showPanelItem)

        let openTranscriptsItem = NSMenuItem(
            title: "Открыть транскрипты",
            action: #selector(onOpenTranscriptsInFinder),
            keyEquivalent: ""
        )
        openTranscriptsItem.target = self
        menu.addItem(openTranscriptsItem)

        menu.addItem(.separator())

        let modeItem = NSMenuItem(
            title: settings.mode == "menubar" ? "Переключить в headless" : "Переключить в menu bar",
            action: #selector(onModeToggle),
            keyEquivalent: ""
        )
        modeItem.target = self
        menu.addItem(modeItem)

        let qualityMenuItem = NSMenuItem(title: "Качество транскрибации", action: nil, keyEquivalent: "")
        menu.addItem(qualityMenuItem)

        let qualitySubmenu = NSMenu()
        let balancedItem = NSMenuItem(
            title: "Balanced (turbo)",
            action: #selector(onQualityBalanced),
            keyEquivalent: ""
        )
        balancedItem.target = self
        balancedItem.state = settings.qualityProfile == "balanced" ? .on : .off
        qualitySubmenu.addItem(balancedItem)

        let maxItem = NSMenuItem(
            title: "Max (настраиваемый)",
            action: #selector(onQualityMax),
            keyEquivalent: ""
        )
        maxItem.target = self
        maxItem.state = settings.qualityProfile == "max" ? .on : .off
        qualitySubmenu.addItem(maxItem)

        menu.setSubmenu(qualitySubmenu, for: qualityMenuItem)

        let autoPasteItem = NSMenuItem(
            title: settings.autoPaste ? "Автовставка: вкл" : "Автовставка: выкл",
            action: #selector(onAutoPasteToggle),
            keyEquivalent: ""
        )
        autoPasteItem.target = self
        menu.addItem(autoPasteItem)

        let translationLabel: String
        switch settings.translationMode {
        case "ru_to_es":
            translationLabel = "RU -> ES"
        case "es_to_ru":
            translationLabel = "ES -> RU"
        case "en_to_ru":
            translationLabel = "EN -> RU"
        case "auto":
            translationLabel = "Auto"
        case "auto_to_ru":
            translationLabel = "Auto -> RU"
        case "bilingual_ru_es":
            translationLabel = "Bilingual RU<->ES"
        default:
            translationLabel = "Off"
        }
        let translationItem = NSMenuItem(title: "Перевод", action: nil, keyEquivalent: "")
        menu.addItem(translationItem)

        let translationSubmenu = NSMenu()
        let translationInfoItem = NSMenuItem(
            title: settings.translateAndPaste
                ? "Текущий: \(translationLabel) / вставка перевода"
                : "Текущий: \(translationLabel) / вставка оригинала",
            action: nil,
            keyEquivalent: ""
        )
        translationInfoItem.isEnabled = false
        translationSubmenu.addItem(translationInfoItem)
        translationSubmenu.addItem(.separator())

        let modeOffItem = NSMenuItem(title: "Off", action: #selector(onTranslationModeOff), keyEquivalent: "")
        modeOffItem.target = self
        modeOffItem.state = settings.translationMode == "off" ? .on : .off
        translationSubmenu.addItem(modeOffItem)

        let modeRuToEsItem = NSMenuItem(title: "RU -> ES", action: #selector(onTranslationModeRuToEs), keyEquivalent: "")
        modeRuToEsItem.target = self
        modeRuToEsItem.state = settings.translationMode == "ru_to_es" ? .on : .off
        translationSubmenu.addItem(modeRuToEsItem)

        let modeEsToRuItem = NSMenuItem(title: "ES -> RU", action: #selector(onTranslationModeEsToRu), keyEquivalent: "")
        modeEsToRuItem.target = self
        modeEsToRuItem.state = settings.translationMode == "es_to_ru" ? .on : .off
        translationSubmenu.addItem(modeEsToRuItem)

        let modeEnToRuItem = NSMenuItem(title: "EN -> RU", action: #selector(onTranslationModeEnToRu), keyEquivalent: "")
        modeEnToRuItem.target = self
        modeEnToRuItem.state = settings.translationMode == "en_to_ru" ? .on : .off
        translationSubmenu.addItem(modeEnToRuItem)

        let modeAutoItem = NSMenuItem(title: "Auto", action: #selector(onTranslationModeAuto), keyEquivalent: "")
        modeAutoItem.target = self
        modeAutoItem.state = settings.translationMode == "auto" ? .on : .off
        translationSubmenu.addItem(modeAutoItem)

        let modeBilingualItem = NSMenuItem(
            title: "Bilingual RU<->ES",
            action: #selector(onTranslationModeBilingualRuEs),
            keyEquivalent: ""
        )
        modeBilingualItem.target = self
        modeBilingualItem.state = settings.translationMode == "bilingual_ru_es" ? .on : .off
        translationSubmenu.addItem(modeBilingualItem)

        translationSubmenu.addItem(.separator())
        let livePresetItem = NSMenuItem(
            title: "Live Translation preset",
            action: #selector(onApplyLiveTranslationPreset),
            keyEquivalent: ""
        )
        livePresetItem.target = self
        translationSubmenu.addItem(livePresetItem)

        let swapRuEsItem = NSMenuItem(
            title: "Swap RU <-> ES",
            action: #selector(onSwapRuEsDirection),
            keyEquivalent: ""
        )
        swapRuEsItem.target = self
        swapRuEsItem.isEnabled = settings.translationMode == "ru_to_es"
            || settings.translationMode == "es_to_ru"
            || settings.translationMode == "auto"
            || settings.translationMode == "bilingual_ru_es"
        translationSubmenu.addItem(swapRuEsItem)

        translationSubmenu.addItem(.separator())
        let translateAndPasteItem = NSMenuItem(
            title: settings.translateAndPaste ? "Перевод + вставка: вкл" : "Перевод + вставка: выкл",
            action: #selector(onTranslateAndPasteToggle),
            keyEquivalent: ""
        )
        translateAndPasteItem.target = self
        translationSubmenu.addItem(translateAndPasteItem)

        translationSubmenu.addItem(.separator())
        let styleMenuItem = NSMenuItem(title: "Стиль перевода", action: nil, keyEquivalent: "")
        translationSubmenu.addItem(styleMenuItem)
        let styleSubmenu = NSMenu()
        let styleNeutral = NSMenuItem(title: "Neutral", action: #selector(onTranslationStyleNeutral), keyEquivalent: "")
        styleNeutral.target = self
        styleNeutral.state = settings.translationStyle == "neutral" ? .on : .off
        styleSubmenu.addItem(styleNeutral)
        let styleChat = NSMenuItem(title: "Chat", action: #selector(onTranslationStyleChat), keyEquivalent: "")
        styleChat.target = self
        styleChat.state = settings.translationStyle == "chat" ? .on : .off
        styleSubmenu.addItem(styleChat)
        let styleFormal = NSMenuItem(title: "Formal", action: #selector(onTranslationStyleFormal), keyEquivalent: "")
        styleFormal.target = self
        styleFormal.state = settings.translationStyle == "formal" ? .on : .off
        styleSubmenu.addItem(styleFormal)
        translationSubmenu.setSubmenu(styleSubmenu, for: styleMenuItem)

        menu.setSubmenu(translationSubmenu, for: translationItem)

        let clipboardMenuItem = NSMenuItem(title: "Буфер обмена", action: nil, keyEquivalent: "")
        menu.addItem(clipboardMenuItem)
        let clipboardSubmenu = NSMenu()
        let clipAlways = NSMenuItem(title: "Always copy", action: #selector(onClipboardModeAlways), keyEquivalent: "")
        clipAlways.target = self
        clipAlways.state = settings.clipboardMode == "always_copy" ? .on : .off
        clipboardSubmenu.addItem(clipAlways)
        let clipOnFail = NSMenuItem(title: "Copy on fail", action: #selector(onClipboardModeOnFail), keyEquivalent: "")
        clipOnFail.target = self
        clipOnFail.state = settings.clipboardMode == "copy_on_fail" ? .on : .off
        clipboardSubmenu.addItem(clipOnFail)
        let clipNever = NSMenuItem(title: "Never copy", action: #selector(onClipboardModeNever), keyEquivalent: "")
        clipNever.target = self
        clipNever.state = settings.clipboardMode == "never_copy" ? .on : .off
        clipboardSubmenu.addItem(clipNever)
        menu.setSubmenu(clipboardSubmenu, for: clipboardMenuItem)

        let networkMenuItem = NSMenuItem(title: "Сетевой режим", action: nil, keyEquivalent: "")
        menu.addItem(networkMenuItem)

        let networkSubmenu = NSMenu()
        let offlineDefaultItem = NSMenuItem(
            title: "Offline default",
            action: #selector(onNetworkOfflineDefault),
            keyEquivalent: ""
        )
        offlineDefaultItem.target = self
        offlineDefaultItem.state = settings.networkMode == "offline_default" ? .on : .off
        networkSubmenu.addItem(offlineDefaultItem)

        let offlineStrictItem = NSMenuItem(
            title: "Offline strict",
            action: #selector(onNetworkOfflineStrict),
            keyEquivalent: ""
        )
        offlineStrictItem.target = self
        offlineStrictItem.state = settings.networkMode == "offline_strict" ? .on : .off
        networkSubmenu.addItem(offlineStrictItem)

        let onlineOptInItem = NSMenuItem(
            title: "Online opt-in",
            action: #selector(onNetworkOnlineOptIn),
            keyEquivalent: ""
        )
        onlineOptInItem.target = self
        onlineOptInItem.state = settings.networkMode == "online_opt_in" ? .on : .off
        networkSubmenu.addItem(onlineOptInItem)

        menu.setSubmenu(networkSubmenu, for: networkMenuItem)

        let hotkeyProfileItem = NSMenuItem(title: "Hotkey Profile", action: nil, keyEquivalent: "")
        menu.addItem(hotkeyProfileItem)
        let hotkeyProfileSubmenu = NSMenu()
        let profileDefaultItem = NSMenuItem(title: "Default", action: #selector(onHotkeyProfileDefault), keyEquivalent: "")
        profileDefaultItem.target = self
        profileDefaultItem.state = settings.hotkeyProfile == "default" ? .on : .off
        hotkeyProfileSubmenu.addItem(profileDefaultItem)
        let profileMeetingItem = NSMenuItem(title: "Meeting", action: #selector(onHotkeyProfileMeeting), keyEquivalent: "")
        profileMeetingItem.target = self
        profileMeetingItem.state = settings.hotkeyProfile == "meeting" ? .on : .off
        hotkeyProfileSubmenu.addItem(profileMeetingItem)
        let profileTranslationItem = NSMenuItem(title: "Translation", action: #selector(onHotkeyProfileTranslation), keyEquivalent: "")
        profileTranslationItem.target = self
        profileTranslationItem.state = settings.hotkeyProfile == "translation" ? .on : .off
        hotkeyProfileSubmenu.addItem(profileTranslationItem)
        menu.setSubmenu(hotkeyProfileSubmenu, for: hotkeyProfileItem)

        let updateChannelItem = NSMenuItem(title: "Update Channel", action: nil, keyEquivalent: "")
        menu.addItem(updateChannelItem)
        let updateChannelSubmenu = NSMenu()
        let stableChannelItem = NSMenuItem(title: "Stable", action: #selector(onUpdateChannelStable), keyEquivalent: "")
        stableChannelItem.target = self
        stableChannelItem.state = settings.updateChannel == "stable" ? .on : .off
        updateChannelSubmenu.addItem(stableChannelItem)
        let betaChannelItem = NSMenuItem(title: "Beta", action: #selector(onUpdateChannelBeta), keyEquivalent: "")
        betaChannelItem.target = self
        betaChannelItem.state = settings.updateChannel == "beta" ? .on : .off
        updateChannelSubmenu.addItem(betaChannelItem)
        menu.setSubmenu(updateChannelSubmenu, for: updateChannelItem)

        let quickActionsItem = NSMenuItem(title: "Быстрые действия", action: nil, keyEquivalent: "")
        menu.addItem(quickActionsItem)

        let quickActionsSubmenu = NSMenu()
        let copyLastItem = NSMenuItem(
            title: "Копировать последний",
            action: #selector(onCopyLastResult),
            keyEquivalent: "c"
        )
        copyLastItem.target = self
        copyLastItem.keyEquivalentModifierMask = [.command, .option]
        copyLastItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(copyLastItem)

        let pasteLastItem = NSMenuItem(
            title: "Вставить последний",
            action: #selector(onPasteLastResult),
            keyEquivalent: "v"
        )
        pasteLastItem.target = self
        pasteLastItem.keyEquivalentModifierMask = [.command, .option]
        pasteLastItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(pasteLastItem)

        let pasteOriginalItem = NSMenuItem(
            title: "Вставить оригинал",
            action: #selector(onPasteLastOriginal),
            keyEquivalent: ""
        )
        pasteOriginalItem.target = self
        pasteOriginalItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(pasteOriginalItem)

        let pasteTranslationItem = NSMenuItem(
            title: "Вставить перевод",
            action: #selector(onPasteLastTranslation),
            keyEquivalent: ""
        )
        pasteTranslationItem.target = self
        pasteTranslationItem.isEnabled = (lastResult?.translatedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
        quickActionsSubmenu.addItem(pasteTranslationItem)

        let pastePlainItem = NSMenuItem(
            title: "Вставить plain (1 строка)",
            action: #selector(onPasteLastPlainText),
            keyEquivalent: ""
        )
        pastePlainItem.target = self
        pastePlainItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(pastePlainItem)

        quickActionsSubmenu.addItem(.separator())
        let templateRuItem = NSMenuItem(
            title: "Шаблон RU follow-up",
            action: #selector(onApplyTemplateRu),
            keyEquivalent: ""
        )
        templateRuItem.target = self
        templateRuItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(templateRuItem)

        let templateEsItem = NSMenuItem(
            title: "Шаблон ES follow-up",
            action: #selector(onApplyTemplateEs),
            keyEquivalent: ""
        )
        templateEsItem.target = self
        templateEsItem.isEnabled = lastResult != nil
        quickActionsSubmenu.addItem(templateEsItem)

        menu.setSubmenu(quickActionsSubmenu, for: quickActionsItem)

        let startSoundItem = NSMenuItem(
            title: settings.playStartSound ? "Звук старта: вкл" : "Звук старта: выкл",
            action: #selector(onStartSoundToggle),
            keyEquivalent: ""
        )
        startSoundItem.target = self
        menu.addItem(startSoundItem)

        let duckingMenuItem = NSMenuItem(title: "Приглушение звука", action: nil, keyEquivalent: "")
        menu.addItem(duckingMenuItem)
        let duckingSubmenu = NSMenu()
        let duckEnabledItem = NSMenuItem(
            title: settings.audioDuckingEnabled ? "При записи: вкл" : "При записи: выкл",
            action: #selector(onAudioDuckingToggle),
            keyEquivalent: ""
        )
        duckEnabledItem.target = self
        duckingSubmenu.addItem(duckEnabledItem)
        duckingSubmenu.addItem(.separator())
        let duck25 = NSMenuItem(title: "25%", action: #selector(onAudioDuckingPercent25), keyEquivalent: "")
        duck25.target = self
        duck25.state = settings.audioDuckingPercent == 25 ? .on : .off
        duckingSubmenu.addItem(duck25)
        let duck50 = NSMenuItem(title: "50%", action: #selector(onAudioDuckingPercent50), keyEquivalent: "")
        duck50.target = self
        duck50.state = settings.audioDuckingPercent == 50 ? .on : .off
        duckingSubmenu.addItem(duck50)
        let duck75 = NSMenuItem(title: "75%", action: #selector(onAudioDuckingPercent75), keyEquivalent: "")
        duck75.target = self
        duck75.state = settings.audioDuckingPercent == 75 ? .on : .off
        duckingSubmenu.addItem(duck75)
        let duck100 = NSMenuItem(title: "100% (mute)", action: #selector(onAudioDuckingPercent100), keyEquivalent: "")
        duck100.target = self
        duck100.state = settings.audioDuckingPercent == 100 ? .on : .off
        duckingSubmenu.addItem(duck100)
        menu.setSubmenu(duckingSubmenu, for: duckingMenuItem)

        let overlayMenuItem = NSMenuItem(title: "Прозрачность realtime", action: nil, keyEquivalent: "")
        menu.addItem(overlayMenuItem)
        let overlaySubmenu = NSMenu()
        let overlay25 = NSMenuItem(title: "25%", action: #selector(onOverlayOpacity25), keyEquivalent: "")
        overlay25.target = self
        overlay25.state = settings.overlayOpacityPercent == 25 ? .on : .off
        overlaySubmenu.addItem(overlay25)
        let overlay45 = NSMenuItem(title: "45%", action: #selector(onOverlayOpacity45), keyEquivalent: "")
        overlay45.target = self
        overlay45.state = settings.overlayOpacityPercent == 45 ? .on : .off
        overlaySubmenu.addItem(overlay45)
        let overlay65 = NSMenuItem(title: "65%", action: #selector(onOverlayOpacity65), keyEquivalent: "")
        overlay65.target = self
        overlay65.state = settings.overlayOpacityPercent == 65 ? .on : .off
        overlaySubmenu.addItem(overlay65)
        menu.setSubmenu(overlaySubmenu, for: overlayMenuItem)

        let autostartItem = NSMenuItem(
            title: settings.autoStartEnabled ? "Автозапуск: вкл" : "Автозапуск: выкл",
            action: #selector(onAutostartToggle),
            keyEquivalent: ""
        )
        autostartItem.target = self
        menu.addItem(autostartItem)

        let dockItem = NSMenuItem(
            title: settings.showDockIcon ? "Иконка в Dock: вкл" : "Иконка в Dock: выкл",
            action: #selector(onDockIconToggle),
            keyEquivalent: ""
        )
        dockItem.target = self
        menu.addItem(dockItem)

        let compactItem = NSMenuItem(
            title: "Оптимизировать историю",
            action: #selector(onCompactHistory),
            keyEquivalent: ""
        )
        compactItem.target = self
        menu.addItem(compactItem)

        menu.addItem(.separator())

        let restartItem = NSMenuItem(
            title: "Перезапустить агент",
            action: #selector(onRestartAgent),
            keyEquivalent: ""
        )
        restartItem.target = self
        menu.addItem(restartItem)

        let stopItem = NSMenuItem(
            title: "Остановить агент",
            action: #selector(onStopAgent),
            keyEquivalent: ""
        )
        stopItem.target = self
        menu.addItem(stopItem)

        let quitItem = NSMenuItem(title: "Выход", action: #selector(onQuit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }

    func playStartSoundIfEnabled() {
        guard settings.playStartSound else {
            return
        }
        // Уведомляющий звук о старте записи.
        if let sound = NSSound(named: "Glass") {
            sound.play()
        } else {
            NSSound.beep()
        }
    }

    func notify(title: String, body: String) {
        notificationService.notify(title: title, body: body)
    }

    func recoverFromPreviewFallback(reason: String) -> Bool {
        logger.warn("Запуск fallback из realtime preview: \(reason)")
        guard
            let stateResponse = try? ipcClient.call(method: "get_recording_state", params: [:]),
            let state = stateResponse["result"] as? [String: Any]
        else {
            return false
        }

        let rawPreviewText = ((state["preview_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let previewText = sanitizePreviewFallbackText(rawPreviewText)
        guard previewText.count >= 8 else {
            logger.warn("Fallback отменён: previewText слишком короткий")
            return false
        }

        var historyId: String?
        if let addResponse = try? ipcClient.call(
            method: "add_history_item",
            params: [
                "text": previewText,
                "paste_status": "failed",
            ]
        ), let result = addResponse["result"] as? [String: Any] {
            historyId = result["id"] as? String
        }

        handleTranscriptionResult(text: previewText, historyId: historyId)
        notify(
            title: "Krab Ear",
            body: "Использован fallback realtime-текста: \(reason)"
        )
        return true
    }

    private func sanitizePreviewFallbackText(_ text: String) -> String {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return "" }

        let lowered = clean.lowercased()
        if lowered.contains("<begin_of_box>") || lowered.contains("<end_of_box>") || lowered.contains("\"action\":") {
            return ""
        }

        let normalized = normalizeForHeuristic(clean)
        let blockedFragments = [
            "продолжение следует",
            "to be continued",
            "сохраняй смысл ставь корректную пунктуац",
            "сохраняй смысл ставь корректную пункту",
            "ставь корректную пунктуац",
            "ставь корректную пункту",
        ]
        if blockedFragments.contains(where: { normalized.contains($0) }) {
            return ""
        }

        let tokens = normalized.split(separator: " ").map(String.init)
        let hasSaveMeaningEcho = tokens.contains("сохраняй")
            && tokens.contains("смысл")
            && tokens.contains(where: { $0.hasPrefix("корр") })
            && tokens.contains(where: { $0.hasPrefix("пункт") })
        if hasSaveMeaningEcho {
            return ""
        }
        if looksLikeLoopingFallback(tokens: tokens) {
            return ""
        }
        return clean
    }

    private func normalizeForHeuristic(_ text: String) -> String {
        let lowered = text.lowercased()
        let allowed = lowered.map { char -> Character in
            if char.isLetter || char.isNumber || char == " " || char == "-" {
                return char
            }
            return " "
        }
        let compact = String(allowed).replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
        return compact.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func looksLikeLoopingFallback(tokens: [String]) -> Bool {
        guard tokens.count >= 6 else { return false }

        var frequency: [String: Int] = [:]
        for token in tokens {
            frequency[token, default: 0] += 1
        }

        let maxFreq = frequency.values.max() ?? 0
        let uniqueRatio = Double(frequency.count) / Double(max(tokens.count, 1))
        if uniqueRatio <= 0.42 && maxFreq >= max(3, Int(Double(tokens.count) * 0.34)) {
            return true
        }

        if frequency.count <= 2 && tokens.count >= 5 && maxFreq >= 4 {
            return true
        }

        var bigrams: [String: Int] = [:]
        if tokens.count >= 2 {
            for idx in 0..<(tokens.count - 1) {
                let key = "\(tokens[idx]) \(tokens[idx + 1])"
                bigrams[key, default: 0] += 1
            }
        }
        let topBigram = bigrams.values.max() ?? 0
        if topBigram >= max(3, tokens.count / 5) {
            return true
        }

        return containsRepeatedChunk(tokens: tokens, minRepeats: 3)
    }

    private func containsRepeatedChunk(tokens: [String], minRepeats: Int) -> Bool {
        let total = tokens.count
        guard total >= 6 else { return false }

        let maxChunk = min(7, total / max(minRepeats, 1))
        guard maxChunk >= 2 else { return false }

        for chunkSize in 2...maxChunk {
            var start = 0
            while start + (chunkSize * minRepeats) <= total {
                let chunk = Array(tokens[start..<(start + chunkSize)])
                var repeats = 1
                while start + (chunkSize * (repeats + 1)) <= total {
                    let nextChunk = Array(tokens[(start + chunkSize * repeats)..<(start + chunkSize * (repeats + 1))])
                    if nextChunk != chunk {
                        break
                    }
                    repeats += 1
                }
                if repeats >= minRepeats {
                    return true
                }
                start += 1
            }
        }
        return false
    }

    func isBackendNotRecordingError(_ error: Error) -> Bool {
        let lowered = error.localizedDescription.lowercased()
        return lowered.contains("запись не была запущена")
            || lowered.contains("recording was not started")
            || lowered.contains("not recording")
    }

    private func stopAgent() {
        NSApp.terminate(nil)
    }

    private func restartAgent() {
        let scriptPath = (options.projectRoot as NSString).appendingPathComponent("scripts/start_agent.command")
        let escaped = scriptPath.replacingOccurrences(of: "\"", with: "\\\"")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lc", "\"\(escaped)\" --show-history >/dev/null 2>&1 &"]
        try? process.run()
        NSApp.terminate(nil)
    }

    func startRealtimeOverlayPolling() {
        stopRealtimeOverlayPolling()
        guard settings.realtimePreviewEnabled else { return }

        realtimeOverlay.show()
        realtimeOverlayTimer = Timer.scheduledTimer(withTimeInterval: 0.85, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.refreshRealtimeOverlay()
            }
        }
        if let realtimeOverlayTimer {
            RunLoop.main.add(realtimeOverlayTimer, forMode: .common)
        }
        refreshRealtimeOverlay()
    }

    func stopRealtimeOverlayPolling() {
        realtimeOverlayTimer?.invalidate()
        realtimeOverlayTimer = nil
        lastPreviewTranslationSource = ""
        lastPreviewTranslationText = ""
        lastPreviewTranslationAt = 0
        lastPreviewTranslationMode = ""
        lastPreviewTranslationFailureAt = 0
        lastPreviewTranslationFailures = 0
        lastPreviewTranslationSuccessAt = 0
        realtimeOverlay.hide()
    }

    private func refreshRealtimeOverlay() {
        guard isRecording, settings.realtimePreviewEnabled else {
            return
        }
        guard
            let response = try? ipcClient.call(method: "get_recording_state", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            return
        }

        let previewText = (result["preview_text"] as? String) ?? ""
        let durationSec = (result["duration_sec"] as? Double) ?? 0.0
        let durationText = formatDuration(durationSec)
        let translatedPreview = translatePreviewTextIfNeeded(previewText)
        let modeHint = previewTranslationModeHint()
        realtimeOverlay.update(
            previewText: previewText,
            translatedText: translatedPreview,
            durationText: durationText,
            modeHint: modeHint
        )
    }

    private func previewTranslationModeHint() -> String {
        switch settings.translationMode {
        case "ru_to_es":
            return "RU -> ES"
        case "es_to_ru":
            return "ES -> RU"
        case "en_to_ru":
            return "EN -> RU"
        case "auto":
            return "AUTO"
        case "auto_to_ru":
            return "AUTO -> RU"
        case "bilingual_ru_es":
            return "RU<->ES"
        default:
            return "OFF"
        }
    }

    private func translatePreviewTextIfNeeded(_ previewText: String) -> String? {
        guard settings.translationMode != "off" else {
            return nil
        }
        let cleanPreview = previewText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard cleanPreview.count >= 8 else {
            return nil
        }

        let now = Date().timeIntervalSince1970
        let minInterval: TimeInterval
        if cleanPreview.count >= 240 {
            minInterval = 1.1
        } else if cleanPreview.count >= 120 {
            minInterval = 1.35
        } else {
            minInterval = 1.8
        }
        let hasSentenceBoundary = cleanPreview.hasSuffix(".") || cleanPreview.hasSuffix("!") || cleanPreview.hasSuffix("?")
        let deltaLength = abs(cleanPreview.count - lastPreviewTranslationSource.count)
        let minimumDelta = cleanPreview.count >= 120 ? 8 : 4
        let modeChanged = settings.translationMode != lastPreviewTranslationMode
        let enoughProgress = hasSentenceBoundary || deltaLength >= minimumDelta

        let failureBackoff = min(6.0, 1.2 + Double(lastPreviewTranslationFailures) * 0.9)
        if lastPreviewTranslationFailureAt > 0, (now - lastPreviewTranslationFailureAt) < failureBackoff {
            return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
        }

        let needsRefresh = (
            modeChanged ||
            (cleanPreview != lastPreviewTranslationSource &&
                enoughProgress &&
                (now - lastPreviewTranslationAt >= minInterval || lastPreviewTranslationText.isEmpty))
        )

        guard needsRefresh else {
            return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
        }

        guard
            let response = try? ipcClient.call(
                method: "translate_text",
                params: [
                    "text": cleanPreview,
                    "translation_mode": settings.translationMode,
                    "translation_style": settings.translationStyle,
                    "network_mode": settings.networkMode,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
        }

        let status = (result["status"] as? String) ?? "unknown"
        let translated = ((result["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        lastPreviewTranslationSource = cleanPreview
        lastPreviewTranslationAt = now
        lastPreviewTranslationMode = settings.translationMode
        if status == "ok", !translated.isEmpty {
            lastPreviewTranslationText = translated
            lastPreviewTranslationFailures = 0
            lastPreviewTranslationFailureAt = 0
            lastPreviewTranslationSuccessAt = now
        } else {
            lastPreviewTranslationFailures += 1
            lastPreviewTranslationFailureAt = now
            // Если перевод временно недоступен, удерживаем последний валидный перевод короткое время.
            if now - lastPreviewTranslationSuccessAt > 8.0 {
                lastPreviewTranslationText = ""
            }
        }
        return lastPreviewTranslationText.isEmpty ? nil : lastPreviewTranslationText
    }

    private func formatDuration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let minutes = total / 60
        let secs = total % 60
        return String(format: "%02d:%02d", minutes, secs)
    }

    private func normalizePlainText(_ text: String) -> String {
        // Plain режим: убираем табы/лишние переносы и схлопываем повторяющиеся пробелы.
        let replaced = text
            .replacingOccurrences(of: "\t", with: " ")
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        let lines = replaced
            .split(separator: "\n")
            .map { raw in
                raw
                    .split(whereSeparator: { $0 == " " || $0 == "\t" })
                    .joined(separator: " ")
            }
            .filter { !$0.isEmpty }
        return lines.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func showFatalAndTerminate(title: String, body: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.addButton(withTitle: "Закрыть")
        alert.runModal()
        NSApp.terminate(nil)
    }

    private func openQuickStart() {
        if quickStartController == nil {
            quickStartController = QuickStartWindowController(
                onComplete: { [weak self] in
                    guard let self = self else { return }
                    self.settings.onboardingCompleted = true
                    self.persistSettingsPayload(self.settings.toPayload())
                    self.quickStartController?.close()
                    self.quickStartController = nil
                    self.notify(title: "Krab Ear", body: "Настройка завершена! Нажмите Right Option для записи.")
                },
                onOpenPanel: { [weak self] in
                    self?.openHistoryPanel(forceMenubar: false)
                },
                launchAgentManager: launchAgentManager
            )
        }
        quickStartController?.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

final class QuickStartWindowController: NSWindowController, NSWindowDelegate {
    private let onComplete: () -> Void
    private let onOpenPanel: () -> Void
    private let launchAgentManager: LaunchAgentManager
    private let autostartCheckbox = NSButton(checkboxWithTitle: "Запускать Krab Ear при входе в систему", target: nil, action: nil)
    private let microphoneStatusLabel = NSTextField(labelWithString: "Микрофон: ...")
    private let accessibilityStatusLabel = NSTextField(labelWithString: "Accessibility: ...")

    init(onComplete: @escaping () -> Void, onOpenPanel: @escaping () -> Void, launchAgentManager: LaunchAgentManager) {
        self.onComplete = onComplete
        self.onOpenPanel = onOpenPanel
        self.launchAgentManager = launchAgentManager
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 400),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Добро пожаловать в Krab Ear 🦀"
        window.center()
        super.init(window: window)
        window.delegate = self
        setupUI(window: window)
        checkPermissions()
    }
    
    required init?(coder: NSCoder) { fatalError() }

    private func setupUI(window: NSWindow) {
        let contentView = NSView(frame: window.contentView!.bounds)
        window.contentView = contentView
        
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = KrabEarTheme.Metrics.spacious
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        stack.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(stack)
        
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            stack.topAnchor.constraint(equalTo: contentView.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: contentView.bottomAnchor)
        ])
        
        let titleLabel = NSTextField(labelWithString: "Быстрая настройка")
        titleLabel.font = .systemFont(ofSize: 20, weight: .bold)
        stack.addArrangedSubview(titleLabel)
        
        let infoLabel = NSTextField(wrappingLabelWithString: "Для корректной работы Krab Ear необходимы разрешения на доступ к микрофону и специальным возможностям (Accessibility) для глобальных клавиш.")
        stack.addArrangedSubview(infoLabel)
        
        let separator1 = NSBox()
        separator1.boxType = .separator
        stack.addArrangedSubview(separator1)
        separator1.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        
        // Microphone Check
        let micRow = NSStackView()
        micRow.orientation = .horizontal
        let checkMicBtn = ThemeSecondaryButton(title: "Проверить микрофон", target: self, action: #selector(onCheckMic))
        checkMicBtn.applyThemeSecondary()
        micRow.addArrangedSubview(checkMicBtn)
        micRow.addArrangedSubview(microphoneStatusLabel)
        stack.addArrangedSubview(micRow)
        
        // Accessibility Check
        let axRow = NSStackView()
        axRow.orientation = .horizontal
        let checkAxBtn = ThemeSecondaryButton(title: "Проверить Accessibility", target: self, action: #selector(onCheckAx))
        checkAxBtn.applyThemeSecondary()
        axRow.addArrangedSubview(checkAxBtn)
        axRow.addArrangedSubview(accessibilityStatusLabel)
        stack.addArrangedSubview(axRow)
        
        let separator2 = NSBox()
        separator2.boxType = .separator
        stack.addArrangedSubview(separator2)
        separator2.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        
        let helpText = NSTextField(wrappingLabelWithString: "Если Accessibility не удается включить автоматически, откройте Системные настройки -> Конфиденциальность и безопасность -> Универсальный доступ.")
        helpText.font = .systemFont(ofSize: 11)
        helpText.textColor = .secondaryLabelColor
        helpText.font = .systemFont(ofSize: 11)
        helpText.textColor = .secondaryLabelColor
        stack.addArrangedSubview(helpText)
        
        // Autostart Checkbox
        let separator3 = NSBox()
        separator3.boxType = .separator
        stack.addArrangedSubview(separator3)
        separator3.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        
        autostartCheckbox.applyThemeCheckbox()
        stack.addArrangedSubview(autostartCheckbox)
        // Set initial state
        autostartCheckbox.state = launchAgentManager.isAutostartEnabled() ? .on : .off
        
        stack.addArrangedSubview(NSView()) // spacer
        
        let buttonsRow = NSStackView()
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.comfortable
        stack.addArrangedSubview(buttonsRow)
        
        let openPanelBtn = ThemeSecondaryButton(title: "Открыть историю", target: self, action: #selector(onOpenHistory))
        openPanelBtn.applyThemeSecondary()
        buttonsRow.addArrangedSubview(openPanelBtn)
        
        buttonsRow.addArrangedSubview(NSView()) // spacer
        
        let completeBtn = ThemePrimaryButton(title: "Готово", target: self, action: #selector(onFinish))
        completeBtn.applyThemePrimary()
        completeBtn.keyEquivalent = "\r"
        buttonsRow.addArrangedSubview(completeBtn)
    }

    private func checkPermissions() {
        // Микрофон
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            microphoneStatusLabel.stringValue = "✅ Разрешено"
            microphoneStatusLabel.textColor = .systemGreen
        case .denied, .restricted:
            microphoneStatusLabel.stringValue = "❌ Запрещено"
            microphoneStatusLabel.textColor = .systemRed
        case .notDetermined:
            microphoneStatusLabel.stringValue = "❓ Требуется запрос"
            microphoneStatusLabel.textColor = .systemOrange
        @unknown default:
            break
        }
        
        // Accessibility
        let axTrusted = AXIsProcessTrusted()
        if axTrusted {
            accessibilityStatusLabel.stringValue = "✅ Включено"
            accessibilityStatusLabel.textColor = .systemGreen
        } else {
            accessibilityStatusLabel.stringValue = "❌ Выключено"
            accessibilityStatusLabel.textColor = .systemRed
        }
    }
    
    @objc private func onCheckMic() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            DispatchQueue.main.async {
                self?.checkPermissions()
            }
        }
    }
    
    @objc private func onCheckAx() {
        // Используем строковый ключ напрямую, чтобы избежать warning/error
        // о небезопасном доступе к глобальной C-константе в strict concurrency.
        let options = ["AXTrustedCheckOptionPrompt": true]
        _ = AXIsProcessTrustedWithOptions(options as CFDictionary)
        // Обновляем статус через секунду, т.к. пользователь может нажать в диалоге
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.checkPermissions()
        }
    }
    
    @objc private func onOpenHistory() {
        onOpenPanel()
    }
    
    @objc private func onFinish() {
        let shouldAutostart = (autostartCheckbox.state == .on)
        launchAgentManager.setAutostart(enabled: shouldAutostart)
        onComplete()
    }
}

let options = LaunchOptions(arguments: CommandLine.arguments)
let app = NSApplication.shared
let delegate = AgentAppDelegate(options: options)
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
