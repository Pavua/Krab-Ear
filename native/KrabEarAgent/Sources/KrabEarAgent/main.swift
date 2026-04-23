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
    let launchAgentManager: LaunchAgentManager
    var ipcClient: IPCClient

    let pasteService = PasteService()
    let audioDuckingService = SystemAudioDuckingService()
    private let notificationService = NotificationService()
    let realtimeOverlay = RealtimeOverlayController()
    let logger = AgentLogger.shared

    /// Phase 2A: Selection translator — Cmd+Shift+T auto-translate selection.
    var selectionTranslator: SelectionTranslator?

    /// Phase 2B: Live субтитры для видео — захват системного аудио через ScreenCaptureKit.
    /// НЕ запускается автоматически — только по Cmd+Shift+L или Settings toggle.
    @available(macOS 12.3, *)
    private(set) lazy var systemAudioCapture: SystemAudioCapture = SystemAudioCapture()

    /// Глобальный monitor для Cmd+Shift+L (Live сабы toggle).
    var liveSubsHotkeyMonitor: Any?

    var historyPanel: HistoryPanelController?
    var hotkeyManager: HotkeyManager?
    var statusItem: NSStatusItem?
    // PR 1.5: Wake word listener (Porcupine)
    private var wakeWordListener: WakeWordListener?
    private var quickStartController: QuickStartWindowController?
    var realtimeOverlayTimer: Timer?

    var settings: AgentSettings = .default
    var isRecording = false
    var isProcessing = false
    var lastToggleRequestAt: TimeInterval = 0
    let toggleDebounceSec: TimeInterval = 0.35
    var recordingTargetApp: NSRunningApplication?
    var lastExternalApp: NSRunningApplication?
    var hasShownAccessibilityHint = false
    var lastResult: LastTranscriptionSnapshot?
    var lastPreviewTranslationSource = ""
    var lastPreviewTranslationText = ""
    var lastPreviewTranslationAt: TimeInterval = 0
    var lastPreviewTranslationMode = ""
    var lastPreviewTranslationFailureAt: TimeInterval = 0
    var lastPreviewTranslationFailures = 0
    var lastPreviewTranslationSuccessAt: TimeInterval = 0
    var recentAutoPasteFingerprints: [String: TimeInterval] = [:]

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

        // Phase 2A: Selection translator — Cmd+Shift+T
        selectionTranslator = SelectionTranslator(
            ipcClient: ipcClient,
            notificationService: notificationService
        )
        selectionTranslator?.start()
        logger.info("SelectionTranslator запущен (Cmd+Shift+T)")

        // Phase 2B: Live субтитры — Cmd+Shift+L toggle (только macOS 12.3+)
        setupLiveSubsHotkey()

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
        selectionTranslator?.stop()
        teardownLiveSubsHotkey()
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

    @objc func onRecordToggle() {
        handleRecordToggleRequest()
    }

    @objc func onOpenHistory() {
        openHistoryPanel(forceMenubar: false)
    }

    @objc func onOpenTranscriptsInFinder() {
        let dataDir = NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath
        let url = URL(fileURLWithPath: dataDir, isDirectory: true)
        NSWorkspace.shared.open(url)
    }

    @objc func onModeToggle() {
        let nextMode = settings.mode == "menubar" ? "headless" : "menubar"
        applyMode(nextMode, persist: true)
    }

    @objc func onQualityBalanced() {
        settings.qualityProfile = "balanced"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onQualityMax() {
        settings.qualityProfile = "max"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onAutoPasteToggle() {
        settings.autoPaste.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onStartSoundToggle() {
        settings.playStartSound.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingToggle() {
        settings.audioDuckingEnabled.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent25() {
        settings.audioDuckingPercent = 25
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent50() {
        settings.audioDuckingPercent = 50
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent75() {
        settings.audioDuckingPercent = 75
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent100() {
        settings.audioDuckingPercent = 100
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onOverlayOpacity25() {
        settings.overlayOpacityPercent = 25
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onOverlayOpacity45() {
        settings.overlayOpacityPercent = 45
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onOverlayOpacity65() {
        settings.overlayOpacityPercent = 65
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onAutostartToggle() {
        settings.autoStartEnabled.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onDockIconToggle() {
        settings.showDockIcon.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onCompactHistory() {
        _ = try? ipcClient.call(method: "compact_history", params: [:])
        historyPanel?.showPanel()
    }

    @objc func onNetworkOfflineDefault() {
        settings.networkMode = "offline_default"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onNetworkOfflineStrict() {
        settings.networkMode = "offline_strict"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onNetworkOnlineOptIn() {
        settings.networkMode = "online_opt_in"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslateAndPasteToggle() {
        settings.translateAndPaste.toggle()
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationModeOff() {
        settings.translationMode = "off"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationModeRuToEs() {
        settings.translationMode = "ru_to_es"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationModeEsToRu() {
        settings.translationMode = "es_to_ru"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationModeEnToRu() {
        settings.translationMode = "en_to_ru"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationModeAuto() {
        settings.translationMode = "auto"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationModeBilingualRuEs() {
        settings.translationMode = "bilingual_ru_es"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onApplyLiveTranslationPreset() {
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

    @objc func onSwapRuEsDirection() {
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

    @objc func onTranslationStyleNeutral() {
        settings.translationStyle = "neutral"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationStyleChat() {
        settings.translationStyle = "chat"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onTranslationStyleFormal() {
        settings.translationStyle = "formal"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onClipboardModeAlways() {
        settings.clipboardMode = "always_copy"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onClipboardModeOnFail() {
        settings.clipboardMode = "copy_on_fail"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onClipboardModeNever() {
        settings.clipboardMode = "never_copy"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
    }

    @objc func onHotkeyProfileDefault() {
        applyHotkeyProfile("default")
    }

    @objc func onHotkeyProfileMeeting() {
        applyHotkeyProfile("meeting")
    }

    @objc func onHotkeyProfileTranslation() {
        applyHotkeyProfile("translation")
    }

    @objc func onUpdateChannelStable() {
        settings.updateChannel = "stable"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        notify(title: "Krab Ear", body: "Канал обновлений: stable")
    }

    @objc func onUpdateChannelBeta() {
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

    @objc func onCopyLastResult() {
        guard let lastResult else {
            notify(title: "Krab Ear", body: "Пока нет последнего результата для копирования")
            return
        }
        pasteService.putToClipboard(lastResult.finalText)
        notify(title: "Krab Ear", body: "Последний результат скопирован в буфер")
    }

    @objc func onPasteLastResult() {
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

    @objc func onPasteLastOriginal() {
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

    @objc func onPasteLastTranslation() {
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

    @objc func onPasteLastPlainText() {
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

    @objc func onApplyTemplateRu() {
        applyTemplateAndPaste(templateKey: "follow_up_ru")
    }

    @objc func onApplyTemplateEs() {
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

    @objc func onQuit() {
        NSApp.terminate(nil)
    }

    @objc func onRestartAgent() {
        restartAgent()
    }

    @objc func onStopAgent() {
        stopAgent()
    }

    @objc func handleControlNotification(_ notification: Notification) {
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

    @objc func handleWorkspaceActivatedApp(_ notification: Notification) {
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

    // MARK: - Paste handling (see main+PasteHandling.swift)

    func openHistoryPanel(forceMenubar: Bool) {
        if forceMenubar && settings.mode != "menubar" {
            applyMode("menubar", persist: true)
        }
        historyPanel?.showPanel()
    }

    func loadSettings() -> AgentSettings {
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

    func persistSettingsPayload(_ payload: [String: Any]) {
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

    func updateSettingsFromPanel(_ payload: [String: Any]) -> AgentSettings {
        persistSettingsPayload(payload)
        rebuildStatusMenu()
        return settings
    }

    // MARK: - Status item & menu (see main+StatusMenu.swift)

    // MARK: - Realtime overlay (see main+RealtimeOverlay.swift)

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

    func isBackendNotRecordingError(_ error: Error) -> Bool {
        let lowered = error.localizedDescription.lowercased()
        return lowered.contains("запись не была запущена")
            || lowered.contains("recording was not started")
            || lowered.contains("not recording")
    }

    func stopAgent() {
        NSApp.terminate(nil)
    }

    func restartAgent() {
        let scriptPath = (options.projectRoot as NSString).appendingPathComponent("scripts/start_agent.command")
        let escaped = scriptPath.replacingOccurrences(of: "\"", with: "\\\"")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lc", "\"\(escaped)\" --show-history >/dev/null 2>&1 &"]
        try? process.run()
        NSApp.terminate(nil)
    }

    func showFatalAndTerminate(title: String, body: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.addButton(withTitle: "Закрыть")
        alert.runModal()
        NSApp.terminate(nil)
    }

    func openQuickStart() {
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
