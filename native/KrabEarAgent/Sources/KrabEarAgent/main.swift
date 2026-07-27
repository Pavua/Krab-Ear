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

        // Указатель bootstrap-инсталлятора (scripts/bootstrap_backend.command
        // пишет каталог установки в этот файл). Механизм последний намеренно:
        // на dev/prod машинах с репозиторием срабатывают cwd/walk-up выше, и
        // поведение не меняется; для .app из /Applications (cwd = «/», walk-up
        // пуст, env не наследуется при запуске из Finder) указатель —
        // единственный источник пути к backend.
        let pointerPath = NSString(
            string: "~/Library/Application Support/KrabEar/project_root"
        ).expandingTildeInPath
        if let raw = try? String(contentsOfFile: pointerPath, encoding: .utf8) {
            let candidate = NSString(
                string: raw.trimmingCharacters(in: .whitespacesAndNewlines)
            ).expandingTildeInPath
            if !candidate.isEmpty && isProjectRoot(candidate) {
                return candidate
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
    /// Хранилище настроек делегата; `.standard` — production-дефолт,
    /// отдельный suite защищает unit-тесты быстрых пресетов от живых настроек.
    let userDefaults: UserDefaults
    let backendSupervisor: BackendSupervisor
    let launchAgentManager: LaunchAgentManager
    var ipcClient: IPCClient

    let pasteService = PasteService()
    let quickEditOverlay = QuickEditOverlay()
    let audioDuckingService = SystemAudioDuckingService()
    private let notificationService = NotificationService()
    let realtimeOverlay = RealtimeOverlayController()
    let logger = AgentLogger.shared

    /// Wake word через IPC-поллинг backend (openWakeWord; spec 2026-07-05).
    var wakeWordPoller: WakeWordPoller?
    private var wakeWordConversationObservers: [NSObjectProtocol] = []
    private var wakeWordTTSPlaybackObservers: [NSObjectProtocol] = []

    /// Phase 2A: Selection translator — Cmd+Shift+T auto-translate selection.
    var selectionTranslator: SelectionTranslator?
    var pasteUndoService: PasteUndoService?
    /// Streaming live paste: вставка подтверждённых кусков текста по мере диктовки (opt-in).
    var streamingPasteController: StreamingPasteController?

    // Phase 2B live-subs state moved to main+LiveSubs.swift (associated objects).

    var historyPanel: HistoryPanelController?
    /// C2c: живая панель встречи (единственный инстанс, main+MeetingPanel.swift).
    var meetingPanelController: MeetingLivePanelController?
    var hotkeyManager: HotkeyManager?
    var statusItem: NSStatusItem?
    /// Ссылка на карточку «Сводка дня» в status-меню — хранится для refresh в NSMenuDelegate.
    /// Объявлена здесь, т.к. stored-property в extension Swift не поддерживает.
    var menuBarRecapView: MenuBarRecapView?
    // PR 1.5: Wake word listener (Porcupine)
    private var quickStartController: QuickStartWindowController?
    var realtimeOverlayTimer: Timer?

    var settings: AgentSettings = .default
    var isRecording = false
    var isProcessing = false
    /// Не даёт двум async start разделить один snapshot audio ducking.
    let recordingStartGate = RecordingStartGate()
    /// Пока backend ещё не вернул token старта, отпускание hold/второй toggle
    /// запоминаются как stop-intent и исполняются сразу после ответа G1.
    var recordingStartInFlight = false
    var recordingStopRequestedDuringStart = false
    /// Идентификатор конкретной попытки start_recording диктовки. Он передаётся
    /// backend без нормализации и позволяет отличить свой потерянный ответ G1
    /// от уже занятого рекордера другого клиента.
    var recordingStartRequestID: String?
    /// Transport-ошибка start не означает, что backend не открыл G1. Пока этот
    /// флаг истинен, новый старт запрещён: сначала нужен безопасный snapshot.
    var recordingStartAmbiguous = false
    /// Request ID неоднозначной попытки; хранится до доказанного idle/foreign
    /// состояния либо до принятия именно этого поколения.
    var recordingStartAmbiguousRequestID: String?
    /// Повторный toggle во время сохранённой неоднозначности — намерение
    /// остановить только подтверждённый собственный G1, не tokenless stop.
    var recordingStopRequestedDuringAmbiguousStart = false
    /// C3a: активна быстрая заметка (запись без вставки в активное окно, спека
    /// 2026-07-16-c3-quick-capture-design.md §2a). Подавляет streaming-paste
    /// и взаимно исключается с диктовкой/встречей.
    var quickCaptureActive = false
    /// UI-epoch незавершённого quick start. Это НЕ generation_token: нужен
    /// только чтобы опоздавший callback старого нажатия не присвоил состояние
    /// более новому start/stop-переходу.
    var quickCaptureStartRequestID: UUID?
    /// Quick Capture хранит ambiguity отдельно от UI-epoch: отменённый или
    /// потерянный Q1 не должен разрешить новый Q2 до его reconciliation.
    var quickCaptureStartAmbiguousRequestID: UUID?
    /// Отмена pending Quick Capture привязана к исходному request ID. До
    /// получения непустого token она не посылает в backend stop_recording.
    var quickCaptureStopRequestedStartID: UUID?
    /// Явный признак stop-intent в ambiguous Quick start нужен и для UI, и для
    /// тестов инварианта «не компенсировать запись без lease».
    var quickCaptureStopRequestedDuringAmbiguousStart = false
    /// Quick Capture удерживает общий start-gate до разбора единственного
    /// ответа Q1; это не даёт диктовке открыть G2 между pending start и cancel.
    var quickCaptureStartGateHeld = false
    /// C3a Task 2: глобальный хоткей-монитор Cmd+Shift+N — ОБЯЗАН сохраняться
    /// (урок main+QuickPresets.swift: несохранённый монитор не снять в teardown).
    var quickCaptureHotkeyMonitor: Any?
    /// C3a Task 2: подменю «Быстрые заметки» в status-меню — ссылка для refresh
    /// из menuWillOpen (stored property не поддерживается в extension Swift).
    var quickNotesSubmenu: NSMenu?
    /// C3b Task 2: единственный инстанс панели-скретчпада (лениво создаётся
    /// ensureQuickCapturePanelController() в main+QuickCapture.swift) — тот же
    /// паттерн, что meetingPanelController выше.
    var quickCapturePanelController: QuickCapturePanelController?
    /// B3: инфо-строка «кто держит LM Studio» в status-меню — создаётся в
    /// rebuildStatusMenu, обновляется из menuWillOpen (main+BrainLease.swift).
    var brainLeaseMenuItem: NSMenuItem?
    var lastToggleRequestAt: TimeInterval = 0
    let toggleDebounceSec: TimeInterval = 0.35
    var recordingTargetApp: NSRunningApplication?
    /// R2: opaque-token текущей локальной диктовки/быстрой заметки. Встреча
    /// хранит тот же generation отдельно в MeetingLivePanelController.
    var activeGenerationToken: String?
    /// Backend source, которому принадлежит локальный stop-route.
    var activeGenerationOwner: String?
    /// Версия владельца lease. Promote сохраняет token, но увеличивает это
    /// значение, поэтому поздний stop старой диктовки атомарно отвергается.
    var activeGenerationOwnerRevision: Int?
    /// Server-side идентичность start, из которого получен текущий lease.
    /// Не логируется: это opaque-значение, а не пользовательская метка.
    var activeGenerationStartRequestID: String?
    /// После неоднозначного/non-terminal stop следующий тап повторяет stop G1,
    /// а не пытается открыть свежее поколение G2.
    var recordingStopRecoveryPending = false
    /// Dictation могла быть повышена до meeting без нового physical capture.
    /// Тогда системный audio-ducking наследует встреча и восстанавливает ровно
    /// после её единственного terminal finished.
    var meetingInheritedDictationDucking = false
    /// meeting_start мог подтвердить promote G1 раньше, чем async dictation
    /// start вернул тот же token. Поздний ответ обязан завершить handoff,
    /// а не публиковать G1 обратно как локальную диктовку.
    var pendingMeetingPromotionToken: String?
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
    /// A3 adaptive backoff: consecutive silence ticks (RMS below threshold)
    var previewSilenceTickCount = 0
    /// A3 adaptive backoff: last observed audio RMS from get_recording_state
    var previewLastAudioRms: Double = 1.0

    init(options: LaunchOptions, userDefaults: UserDefaults = .standard) {
        self.options = options
        self.userDefaults = userDefaults
        self.backendSupervisor = BackendSupervisor(projectRoot: options.projectRoot)
        self.launchAgentManager = LaunchAgentManager(projectRoot: options.projectRoot)
        self.ipcClient = IPCClient(socketPath: (NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath as NSString).appendingPathComponent("krabear.sock"))
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Wave 656: log bootstrap stage — app_launched.
        AgentRecoveryLogger.shared.logStage("app_launched")

        // Phase C C.6: POSIX flock — единственная общая single-instance гарантия.
        // Новый экземпляр завершается сам; startup больше не ищет и не убивает
        // процессы только по имени `KrabEarAgent`.
        guard acquireFileLock(logger: logger) else {
            // Другой экземпляр Krab Ear уже запущен и держит lock.
            SentryConfig.recordTerminate(callsite: "acquire_file_lock_duplicate")
            NSApp.terminate(nil)
            return
        }

        // Только LaunchServices cleanup вынесен в фон, чтобы синхронный Process-вызов
        // не блокировал applicationDidFinishLaunching. PID-based cleanup запрещён.

        // Sentry / GlitchTip telemetry — no-op если DSN не задан в settings
        let sentryDsn = UserDefaults.standard.string(forKey: "KrabEar_SentryDSN") ?? ""
        let sentryEnv = UserDefaults.standard.string(forKey: "KrabEar_SentryEnvironment") ?? "production"
        SentryConfig.initialize(dsn: sentryDsn.isEmpty ? nil : sentryDsn, environment: sentryEnv)

        // AGENT-K fix: pre-warm BackendToast panel здесь, пока startup latency
        // ещё ожидается пользователем. NSVisualEffectView + ColorSync transform
        // выполняются один раз сейчас, а не при первом show() в showFatalAndTerminate.
        BackendToast.shared.prewarmPanel()

        // Sparkle автообновления — ДО backend-ожидания (ультракод-ревью C6):
        // у DMG-получателя со сломанным/неготовым backend'ом приложение
        // фатально завершается через showFatalAndTerminate; если updater
        // стартует только после готовности backend, обновление не может
        // привезти фикс именно тогда, когда оно нужнее всего. Sparkle не
        // зависит от IPC; dev-guard внутри (см. main+SparkleUpdater.swift).
        setupSparkleUpdater()

        logger.info("Старт агента. projectRoot=\(options.projectRoot), launchedByLaunchd=\(options.launchedByLaunchd)")
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

        // Backend wait перенесён в Task.detached — иначе main thread висит до 20 сек
        // на холодном старте Whisper/pyannote (Sentry KRAB-EAR-AGENT-4 AppHang).
        // applicationDidFinishLaunching возвращается сразу, NSApp.run цикл не блокируется,
        // оставшийся setup (UI, hotkey, history panel) выполняется по готовности backend.
        let projectRootURL = URL(fileURLWithPath: options.projectRoot)
        Task.detached { [weak self] in
            guard let self else { return }

            // POSIX flock остаётся единственной автоматической защитой экземпляра.
            // Legacy-процессы не завершаем по PID: macOS не даёт атомарного process
            // handle, а проверенный PID может быть переиспользован до отправки сигнала.
            self.logger.info("Single-instance guard: flock active, legacy PID cleanup disabled")

            cleanupWorktreeShadows(projectRoot: projectRootURL, logger: self.logger)

            self.logger.info("BackendSupervisor режим: \(self.backendSupervisor.supervisionMode == .passive ? "passive (launchd Variant B)" : "active (standalone)")")

            // Clean-Mac guard: текущая сборка НЕ несёт Python-backend внутри бандла —
            // backend живёт в каталоге проекта на диске. Если resolveProjectRoot
            // промахнулся (нет KrabEar/backend/service.py) И backend-сокета не
            // существует, ждать ensureBackendRunning бессмысленно: это гарантированный
            // 6–20-секундный timeout с загадочным «backend недоступен». Показываем
            // целевое сообщение сразу. Оба условия обязаны выполниться одновременно —
            // на dev/prod машинах с репозиторием guard не срабатывает никогда, а живой
            // сокет при невалидном projectRoot (launchd Variant B) продолжает работу.
            let backendScriptExists = FileManager.default.fileExists(
                atPath: projectRootURL.appendingPathComponent("KrabEar/backend/service.py").path)
            let backendSocketExists = FileManager.default.fileExists(atPath: self.backendSupervisor.socketPath)
            if !backendScriptExists && !backendSocketExists {
                await MainActor.run {
                    AgentRecoveryLogger.shared.logFatal("backend_payload_missing: projectRoot=\(projectRootURL.path)")
                    self.logger.error("Backend не установлен на этом Mac: projectRoot=\(projectRootURL.path) без service.py, сокет отсутствует")
                    var body = "Эта сборка не содержит Python-backend внутри приложения — он устанавливается отдельно.\n\nНа этом Mac не найден каталог проекта Krab Ear (KrabEar/backend/service.py), и backend-сервис не запущен."
                    // DMG-сборка несёт инсталлятор в Resources (кладёт
                    // build_distribution_dmg.command). Подсветка в Finder
                    // переживает наш terminate — двойной щелчок по .command
                    // откроет Terminal и поставит backend. Вне DMG (dev-бинарь
                    // без Resources) ресурса нет — остаётся ссылка на доку.
                    if let installer = Bundle.main.url(
                        forResource: "bootstrap_backend", withExtension: "command"
                    ) {
                        body += "\n\nАвтоустановка: в Finder подсвечен bootstrap_backend.command — запустите его двойным щелчком, дождитесь окончания и откройте Krab Ear заново."
                        NSWorkspace.shared.activateFileViewerSelecting([installer])
                    } else {
                        body += "\n\nИнструкция по установке: docs/DISTRIBUTION.md в репозитории Krab Ear (раздел «Требования на целевой машине»)."
                    }
                    self.showFatalAndTerminate(
                        title: "Krab Ear: backend не установлен",
                        body: body
                    )
                }
                return
            }

            // Wave 656: log IPC connect attempt.
            AgentRecoveryLogger.shared.logStage("ipc_connect_attempt")
            do {
                try await self.backendSupervisor.ensureBackendRunningAsync()
                await MainActor.run {
                    // Wave 656: log IPC connect success.
                    AgentRecoveryLogger.shared.logStage("ipc_connect_success")
                    self.logger.info("Backend доступен и готов к IPC")
                    self.completeStartupAfterBackendReady()
                }
            } catch {
                await MainActor.run {
                    // Wave 656: log IPC connect failure.
                    AgentRecoveryLogger.shared.logFatal("ipc_connect_fail: \(error.localizedDescription)")
                    self.logger.error("Backend недоступен: \(error.localizedDescription)")
                    self.showFatalAndTerminate(
                        title: "Krab Ear: backend недоступен",
                        body: error.localizedDescription
                    )
                }
            }
        }
    }

    /// Завершает startup после того как backend ответил на ping.
    /// Вызывается из Task.detached → MainActor.run по готовности.
    /// Содержит весь UI/hotkey/history setup, который требует working IPC.
    @MainActor
    func completeStartupAfterBackendReady() {
        ipcClient = IPCClient(socketPath: backendSupervisor.socketPath)

        // W798 fix: perform handshake once the socket is confirmed reachable.
        // Pass the real bundle version so backend can log version mismatches.
        let handshakeClient = ipcClient
        let agentVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown"
        Task.detached {
            await handshakeClient.performHandshake(swiftAgentVersion: agentVersion)
        }

        settings = loadSettings()
        realtimeOverlay.setOpacityPercent(settings.overlayOpacityPercent)
        logger.info(
            "Настройки загружены: mode=\(settings.mode), autoPaste=\(settings.autoPaste), quality=\(settings.qualityProfile), translation=\(settings.translationMode)"
        )

        // 2026-07-05: setupHealthMonitor() существовал с Phase A, но НИКОГДА не
        // вызывался отсюда (та же декоративная проводка, что у setupErrorBus
        // ниже — найдено при её расследовании). BackendSupervisor умеет
        // проверить/перезапустить backend (актуатор), но сам НЕ мониторит
        // непрерывно — единственный continuous-ping триггер это HealthMonitor.
        // Без вызова здесь: (1) menu-bar status dot никогда не отражал
        // реальное здоровье backend (обновлялся только побочно при переключении
        // privacy mode, всегда показывая дефолтный .stopped цвет); (2) не было
        // НИКАКОГО проактивного обнаружения зависшего backend в простое —
        // только реактивный путь (main+IPCRecovery.swift, срабатывает лишь
        // когда пользователь САМ вызывает IPC и получает ошибку соединения).
        // См. main+HealthMonitor.swift.
        setupHealthMonitor()

        // 2026-07-05: setupErrorBus() существовал с Phase B.1, но НИКОГДА не
        // вызывался отсюда (декоративная проводка — найдено при фиксе IPC-
        // поллинга krab_error). Тосты об ошибках были мертвы в проде. См.
        // main+Errors.swift.
        setupErrorBus(toastPresenter: ErrorToastPresenter())

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

        // Фабрика подключает ВСЕ callback'и. Тот же путь используется при
        // смене клавиши/режима, поэтому runtime-reinstall не обедняет менеджер.
        hotkeyManager = makeHotkeyManager(settings: settings)
        // 2026-05-09: Pre-flight Accessibility check. CGEventTap silently fails
        // (returns nil) если AX permission not granted → hotkey monitor не работает,
        // user не понимает что произошло. Explicitly ask via AXIsProcessTrustedWithOptions
        // когда permission missing — macOS shows стандартный системный prompt
        // с кнопкой "Open System Settings", сразу выбирая правильный path.
        let axTrusted = AXIsProcessTrusted()
        logger.info("Accessibility AX trusted at startup: \(axTrusted)")
        if !axTrusted {
            logger.warn("Accessibility permission missing — prompting user via AXIsProcessTrustedWithOptions")
            let options: NSDictionary = ["AXTrustedCheckOptionPrompt": true]
            _ = AXIsProcessTrustedWithOptions(options as CFDictionary)
            // System dialog появится; user должен grant и потом restart agent.
            // Hotkey reg ниже всё равно попытается, но silent fail если still no perm.
        }

        hotkeyManager?.start()
        logger.info("Глобальный hotkey активирован (mode=\(settings.hotkeyMode))")

        selectionTranslator = SelectionTranslator(
            ipcClient: ipcClient,
            notificationService: notificationService
        )
        selectionTranslator?.start()
        logger.info("SelectionTranslator запущен (Cmd+Shift+T)")

        startQuickCaptureHotkeyMonitor()
        logger.info("Quick Capture hotkey активирован (Cmd+Shift+N)")

        pasteUndoService = PasteUndoService()
        pasteUndoService?.pasteUndoEnabled = settings.pasteUndoEnabled
        pasteUndoService?.start()
        logger.info("PasteUndoService запущен (Cmd+Ctrl+Z). enabled=\(settings.pasteUndoEnabled)")

        // Streaming live paste: создаём контроллер, применяем начальный флаг из настроек.
        streamingPasteController = StreamingPasteController(
            pasteService: PasteServiceStreamingAdapter(pasteService: pasteService)
        )
        streamingPasteController?.isEnabled = settings.streamingPasteEnabled
        logger.info("StreamingPasteController ready. enabled=\(settings.streamingPasteEnabled)")

        // Smart field-aware paste: применяем стартовое значение из настроек и вешаем callback
        // для тихого уведомления при пропуске вставки в защищённое поле.
        pasteService.smartFieldFormatEnabled = settings.smartFieldFormatEnabled
        pasteService.onSecureFieldSkipped = { [weak self] in
            self?.handlePasteFailure(reason: "secure_field_skipped")
        }
        logger.info("SmartFieldPaste ready. enabled=\(settings.smartFieldFormatEnabled)")

        // S34 / Fable-ревью (double-notify): пользовательское уведомление НЕ отсюда —
        // каждый вызывающий putToClipboard(_:) уже проверяет Bool-результат и сам решает,
        // что сказать пользователю (continueTranscriptionResult always_copy-ветка,
        // handlePasteFailure — и напрямую, и через pasteToFrontmostApp → propagated
        // reason "concealed_clipboard_skipped"). Второй notify отсюда дублировал бы их.
        // Closure — только для телеметрии/дебага по путям, которые сами не решают
        // (напр. streaming per-chunk — там намеренно тихо, как и для любых других
        // причин провала чанка, см. StreamingPasteController.appendChunk).
        pasteService.onConcealedClipboardSkipped = { [weak self] in
            self?.logger.info("[Clipboard] Concealed content skip signalled (handled by caller)")
        }

        if UserDefaults.standard.string(forKey: "KrabEar_ActivePreset") == nil {
            UserDefaults.standard.set("default", forKey: "KrabEar_ActivePreset")
        }
        startPresetHotkeyMonitor()
        logger.info("Quick preset hotkey monitor запущен (Cmd+Shift+P)")

        if settings.bookmarksHotkeyEnabled {
            startBookmarkHotkeyMonitor()
            logger.info("Bookmark hotkey активирован (Cmd+Shift+B)")
        }

        startLiveSubsHotkeyMonitor()
        logger.info("Live Subs hotkey активирован (Cmd+Option+Shift+L)")

        startQuickReplaceHotkeyMonitor()
        logger.info("Quick Replace hotkey активирован (Cmd+Shift+R)")

        setupWakeWordListenerIfEnabled()

        applyMode(settings.mode, persist: false)

        // Start dynamic menu bar tooltip refresh (10 s interval).
        startTooltipRefresh()

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
        wakeWordPoller?.deactivate()
        tearDownHealthMonitor()
        tearDownErrorBus()
        selectionTranslator?.stop()
        stopQuickCaptureHotkeyMonitor()
        pasteUndoService?.stop()
        stopLiveSubsCapture()
        stopRealtimeOverlayPolling()
        backendSupervisor.stopBackend()
        // Phase C C.6: release file lock so a subsequent launch can acquire it immediately.
        releaseFileLock(logger: logger)
    }

    // MARK: - Wake Word (IPC-поллинг, spec 2026-07-05)

    /// Wake word через IPC-поллинг backend'а (openWakeWord).
    /// Дефолт: выключен (приватность). Включается в Settings → «Разговор с AI».
    /// Имя функции сохранено для минимального диффа вызывающих мест.
    func setupWakeWordListenerIfEnabled() {
        let enabled = UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled")
        guard enabled else {
            logger.info("Wake word: выключен (UserDefaults KrabEar_WakeWordEnabled=false)")
            return
        }
        if wakeWordPoller == nil {
            wakeWordPoller = WakeWordPoller(
                ipcProvider: { [weak self] in self?.ipcClient },
                isToggleEnabled: { UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled") },
                onDetection: { [weak self] in
                    self?.historyPanel?.triggerConversationFromWakeWord()
                },
                onWedgedEscalation: { [weak self] in
                    guard let self else { return }
                    self.logger.warn("Wake word: backend wedged — принудительный рестарт backend")
                    BackendToast.shared.show("Wake word завис — перезапускаю backend...", duration: 5.0)
                    DispatchQueue.global(qos: .utility).async { [weak self] in
                        guard let self else { return }
                        let ok = self.backendSupervisor.forceRestartBackend()
                        DispatchQueue.main.async {
                            BackendToast.shared.show(
                                ok ? "Backend перезапущен (wake word)"
                                   : "⚠ Рестарт backend не удался — перезапустите Krab Ear вручную",
                                duration: ok ? 3.0 : 10.0
                            )
                        }
                    }
                },
                onWedgedGiveUp: { [weak self] in
                    guard let self else { return }
                    self.logger.warn("Wake word: авто-рестарты исчерпаны (wedged держится) — нужно ручное вмешательство")
                    BackendToast.shared.show(
                        "⚠ Wake word не оживает после 3 рестартов — проверьте микрофон (громкость входа) или выключите тумблер wake word",
                        duration: 12.0
                    )
                }
            )
        }
        setupWakeWordConversationObservers()
        setupWakeWordTTSPlaybackObservers()
        wakeWordPoller?.activate()
        // Тумблер включили, пока privacy mode активен: сразу паузим (backend
        // всё равно отвергнет старт — гейт живой; агентская пауза убирает
        // цикл повторных попыток и лог-шум с обеих сторон).
        if privacyModeEnabled {
            wakeWordPoller?.pause(.privacyMode)
        }
    }

    /// Перезапустить wake word с новым значением enabled.
    /// Вызывается из HistoryPanelController+Settings при изменении тогглера.
    func applyWakeWordEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: "KrabEar_WakeWordEnabled")
        if enabled {
            setupWakeWordListenerIfEnabled()
        } else {
            wakeWordPoller?.deactivate()
        }
    }

    /// Разговор занимает микрофон: пауза wake word на время разговора.
    /// Notification'ы шлёт ConversationViewController (start/stopConversation) —
    /// единственная воронка всех путей старта/останова разговора.
    private func setupWakeWordConversationObservers() {
        guard wakeWordConversationObservers.isEmpty else { return }
        let nc = NotificationCenter.default
        wakeWordConversationObservers.append(
            nc.addObserver(forName: .krabConversationStarted, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.wakeWordPoller?.pause(.conversation) }
            }
        )
        wakeWordConversationObservers.append(
            nc.addObserver(forName: .krabConversationStopped, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.wakeWordPoller?.resume(.conversation) }
            }
        )
    }

    /// Собственный TTS (озвучка ошибки «Разговора с AI») звучит через те же
    /// колонки, что слушает микрофон — пауза wake word на РЕАЛЬНОЕ время
    /// воспроизведения, а не на границы conversation-сессии: ошибка озвучивается
    /// уже ПОСЛЕ stopConversation()/.krabConversationStopped, когда .conversation-
    /// пауза уже снята (живой инцидент ложных срабатываний на своё же эхо, T5b).
    /// Notification'ы шлёт ConversationErrorAnnouncer.playWav().
    private func setupWakeWordTTSPlaybackObservers() {
        guard wakeWordTTSPlaybackObservers.isEmpty else { return }
        let nc = NotificationCenter.default
        wakeWordTTSPlaybackObservers.append(
            nc.addObserver(forName: .krabTTSPlaybackStarted, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.wakeWordPoller?.pause(.ttsPlayback) }
            }
        )
        wakeWordTTSPlaybackObservers.append(
            nc.addObserver(forName: .krabTTSPlaybackFinished, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.wakeWordPoller?.resume(.ttsPlayback) }
            }
        )
    }

    /// Включить/выключить Right Option double-tap hotkey для Разговора с AI.
    func applyConversationHotkeyEnabled(_ enabled: Bool) {
        let supported = HotkeyManager.supportsConversationDoubleTap(variant: settings.hotkey)
        let effectiveEnabled = enabled && supported
        if effectiveEnabled {
            hotkeyManager?.onConversationDoubleTap = { [weak self] in
                DispatchQueue.main.async {
                    self?.historyPanel?.triggerConversationToggle()
                }
            }
        } else {
            hotkeyManager?.onConversationDoubleTap = nil
        }
        let state = effectiveEnabled
            ? "включён"
            : (enabled ? "не поддерживается вариантом \(settings.hotkey)" : "выключен")
        logger.info("Conversation hotkey double-tap: \(state)")
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
        // IPC compact может занять до нескольких секунд → на background.
        // showPanel() — UI, оставляем на main, открываем сразу (не ждём compact).
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async {
            _ = try? ipcClient.call(method: "compact_history", params: [:])
        }
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
        // S34 / Fable-ревью F3: explicit user-initiated copy — не через concealed-guard.
        pasteService.putToClipboardUserInitiated(lastResult.finalText)
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
        SentryConfig.recordTerminate(callsite: "onQuit")
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
            SentryConfig.recordTerminate(callsite: "handleControlNotification_quit")
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

    /// Фабрика HotkeyManager: конфигурирует режим и полный набор callback'ов.
    ///
    /// Это единственная точка создания менеджера и на startup, и при runtime-
    /// смене клавиши/режима. Так новый экземпляр не теряет quick replay,
    /// conflict-reporting и сохранённый флаг conversation hotkey.
    func makeHotkeyManager(settings: AgentSettings) -> HotkeyManager {
        let manager = HotkeyManager(
            variant: settings.hotkey,
            onToggle: { [weak self] in
                DispatchQueue.main.async { self?.handleRecordToggleRequest() }
            },
            mode: settings.hotkeyMode,
            holdMinDurationMs: 200
        )
        // Hold-режим: DOWN вооружает отложенный старт, UP после порога останавливает запись.
        manager.onHoldStart = { [weak self] in
            DispatchQueue.main.async {
                guard let self, !self.isRecording else { return }
                Task { @MainActor [weak self] in
                    // Hold-start проходит тот же owner/state gate: прямой start
                    // мог восстановить ducking живой promoted meeting после
                    // ожидаемого backend already_recording.
                    await self?.performRecordToggle(
                        wasRecordingLocally: false
                    )
                }
            }
        }
        manager.onHoldStop = { [weak self] in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.recordingStartInFlight {
                    self.recordingStopRequestedDuringStart = true
                    return
                }
                guard self.isRecording else { return }
                // Hold и toggle обязаны проходить один owner/state-гейт:
                // прямой stop после dictation→meeting убивал чужую встречу.
                Task { @MainActor [weak self] in
                    await self?.performRecordToggle(wasRecordingLocally: true)
                }
            }
        }
        // Явно сохранённый false обязан переживать restart. Если ключ ещё не
        // создан, ConversationHotkeyPolicy сохраняет исторический дефолт ON.
        if ConversationHotkeyPolicy.isEnabled(in: .standard)
            && HotkeyManager.supportsConversationDoubleTap(variant: settings.hotkey) {
            manager.onConversationDoubleTap = { [weak self] in
                DispatchQueue.main.async {
                    self?.historyPanel?.triggerConversationToggle()
                }
            }
        }

        manager.onQuickReplay = { [weak self] in
            DispatchQueue.main.async {
                self?.handleQuickReplayPaste()
            }
        }

        // Phase B.2 F9: fire-and-forget IPC when RegisterEventHotKey возвращает
        // eventHotKeyExistsErr — chord уже занят другим приложением.
        let ipcClientForHotkey = ipcClient
        manager.reportHotkeyConflictHandler = { chord in
            DispatchQueue.global(qos: .utility).async {
                _ = try? ipcClientForHotkey.call(
                    method: "report_hotkey_conflict",
                    params: ["chord": chord],
                    timeoutSec: IPCClient.quickTimeoutSec
                )
            }
        }
        return manager
    }

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
            // Phase B.2 F10: re-init Sentry with IPC settings so sentry_dsn_agent
            // (agent-specific project) takes precedence over generic sentry_dsn.
            let sentryEnv = UserDefaults.standard.string(forKey: "KrabEar_SentryEnvironment") ?? "production"
            SentryConfig.initializeFromSettings(result, environment: sentryEnv)
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
        // Уведомляющий звук о старте записи (off main thread: AudioQueueXPC.Start синхронный).
        DispatchQueue.global(qos: .userInitiated).async {
            if let sound = NSSound(named: "Glass") {
                sound.play()
            } else {
                NSSound.beep()
            }
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
        SentryConfig.recordTerminate(callsite: "stopAgent")
        NSApp.terminate(nil)
    }

    func restartAgent() {
        // Phase C.6.2 root-cause fix: open the canonical .app bundle instead of
        // shelling out to start_agent.command → runtime/KrabEarAgent.
        // This ensures launchd/Dock/TCC grants all reference the same binary path.
        let bundleURL: URL
        let bundleMain = Bundle.main.bundleURL
        if bundleMain.pathExtension == "app" {
            // Running from inside the .app bundle (production path).
            bundleURL = bundleMain
        } else {
            // Running from native/runtime/ binary directly (dev path) — construct
            // bundle URL from project root so we still open the canonical .app.
            bundleURL = URL(fileURLWithPath: options.projectRoot, isDirectory: true)
                .appendingPathComponent("Krab Ear.app", isDirectory: true)
        }
        NSWorkspace.shared.open(bundleURL)
        SentryConfig.recordTerminate(callsite: "restartAgent")
        NSApp.terminate(nil)
    }

    func showFatalAndTerminate(title: String, body: String) {
        // AGENT-H fix: NSAlert.runModal() blocks main thread → Sentry ANR ≥2 s.
        // Use BackendToast (non-modal, floating panel) so main thread stays free,
        // then terminate after 3 s — enough time for user to read the message.
        // AGENT-K: breadcrumb добавляется ДО show() — если ColorSync всё-таки
        // заблокирует (panel не был pre-warmed), breadcrumb уже будет в Sentry.
        SentryConfig.recordBreadcrumb(
            category: "lifecycle",
            message: "showFatalAndTerminate called",
            data: ["title": title]
        )
        logger.error("FATAL: \(title) — \(body)")
        BackendToast.shared.show("FATAL: \(title)\n\(body)", duration: 3.0)
        SentryConfig.recordTerminate(callsite: "showFatalAndTerminate")
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
            NSApp.terminate(nil)
        }
    }

    func openQuickStart() {
        if quickStartController == nil {
            quickStartController = QuickStartWindowController(
                ipcClient: ipcClient,
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
    private let ipcClient: IPCClient
    private let onComplete: () -> Void
    private let onOpenPanel: () -> Void
    private let launchAgentManager: LaunchAgentManager
    private let autostartCheckbox = NSButton(checkboxWithTitle: "Запускать Krab Ear при входе в систему", target: nil, action: nil)
    private let microphoneStatusLabel = NSTextField(labelWithString: "Микрофон: ...")
    private let accessibilityStatusLabel = NSTextField(labelWithString: "Accessibility: ...")
    /// Шаг загрузки STT-модели — strong ref пока sheet активен.
    private var modelDownloadStep: ModelDownloadStepController?
    /// A1: шаг превью рекомендованной настройки — strong ref пока sheet активен.
    private var recommendedSetupStep: RecommendedSetupStepController?
    /// Решение 9.4: отдельный consent-экран wake word — strong ref пока sheet активен.
    private var wakeWordConsentStep: WakeWordConsentStepController?

    init(
        ipcClient: IPCClient,
        onComplete: @escaping () -> Void,
        onOpenPanel: @escaping () -> Void,
        launchAgentManager: LaunchAgentManager
    ) {
        self.ipcClient = ipcClient
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
        runModelDownloadStepThenComplete()
    }

    /// Перед завершением онбординга: (1) STT-модель если не в кэше, (2) рекомендованная
    /// настройка (A1, dry_run превью -> apply/skip), (3) wake word consent (решение 9.4,
    /// отдельно от apply_recommended_setup). Любой исход каждого шага -> следующий шаг;
    /// финал -> onComplete().
    private func runModelDownloadStepThenComplete() {
        guard let parent = self.window else {
            // Нет окна — не блокируем завершение онбординга.
            onComplete()
            return
        }
        let step = ModelDownloadStepController(ipcClient: ipcClient) { [weak self] _ in
            guard let self = self else { return }
            self.modelDownloadStep = nil
            self.runRecommendedSetupStepThenWakeWord(over: parent)
        }
        self.modelDownloadStep = step
        step.start(over: parent)
    }

    private func runRecommendedSetupStepThenWakeWord(over parent: NSWindow) {
        let step = RecommendedSetupStepController(ipcClient: ipcClient) { [weak self] _ in
            guard let self = self else { return }
            self.recommendedSetupStep = nil
            self.runWakeWordConsentStep(over: parent)
        }
        self.recommendedSetupStep = step
        step.start(over: parent)
    }

    private func runWakeWordConsentStep(over parent: NSWindow) {
        let step = WakeWordConsentStepController(ipcClient: ipcClient) { [weak self] _ in
            guard let self = self else { return }
            self.wakeWordConsentStep = nil
            self.onComplete()
        }
        self.wakeWordConsentStep = step
        step.start(over: parent)
    }
}

// MARK: - Defense-in-depth: runtime → bundle self-redirect (Phase C root-cause fix)
//
// Структурное исправление two-binary drift, задокументированного в:
//   memory/blocker_two_binary_drift_2026-05-03.md
//
// Парное исправление с scripts/start_agent.command redirect (commit 6781a4e):
//   - scripts/start_agent.command закрывает ВНЕШНИЙ источник runtime-запусков
//     (script-driven start → теперь `exec /usr/bin/open -W "Krab Ear.app"`)
//   - Этот guard закрывает ВНУТРЕННИЙ путь: если runtime-бинарь всё-таки запущен
//     (устаревший launchd plist, прямой shell-скрипт, тест), он сам перенаправляется
//     в bundle-версию и завершается.
//
// Поведение:
//   1. Проверяем argv[0] — совпадает ли с `<root>/native/runtime/KrabEarAgent`
//   2. Если да — ищем `<root>/Krab Ear.app/Contents/MacOS/KrabEarAgent`
//   3. Если bundle-бинарь существует и исполняем → запускаем его через Process
//      с теми же аргументами, затем exit(0)
//   4. Если bundle не найден → fallthrough (dev-режим: `swift run` или тесты)
private func redirectRuntimeToBundleIfPresent() {
    let exePath = ProcessInfo.processInfo.arguments[0]
    let exeURL = URL(fileURLWithPath: exePath).resolvingSymlinksInPath()

    // Проверяем: путь должен заканчиваться на .../native/runtime/KrabEarAgent
    let pathComponents = exeURL.pathComponents
    guard pathComponents.count >= 3,
          pathComponents[pathComponents.count - 1] == "KrabEarAgent",
          pathComponents[pathComponents.count - 2] == "runtime",
          pathComponents[pathComponents.count - 3] == "native" else {
        return  // не из runtime/ — редирект не нужен
    }

    // Строим путь к bundle: <root>/Krab Ear.app/Contents/MacOS/KrabEarAgent
    let rootURL = exeURL
        .deletingLastPathComponent()  // .../runtime
        .deletingLastPathComponent()  // .../native
        .deletingLastPathComponent()  // .../<root>
    let bundleExe = rootURL
        .appendingPathComponent("Krab Ear.app")
        .appendingPathComponent("Contents")
        .appendingPathComponent("MacOS")
        .appendingPathComponent("KrabEarAgent")

    guard FileManager.default.isExecutableFile(atPath: bundleExe.path) else {
        NSLog("[KrabEarAgent] WARN: running from native/runtime/, no bundle at %@. Continuing in dev mode.",
              bundleExe.path)
        return  // dev-режим — bundle не найден, продолжаем as-is
    }

    NSLog("[KrabEarAgent] Two-binary drift detected: runtime path → redirecting to bundle: %@",
          bundleExe.path)

    // Запускаем bundle с теми же аргументами (без argv[0])
    let task = Process()
    task.executableURL = bundleExe
    let originalArgs = ProcessInfo.processInfo.arguments
    task.arguments = originalArgs.count > 1 ? Array(originalArgs.dropFirst()) : []

    do {
        try task.run()
        NSLog("[KrabEarAgent] Bundle launched (pid=%d), runtime exiting.", task.processIdentifier)
        exit(0)
    } catch {
        NSLog("[KrabEarAgent] Failed to launch bundle (%@) — falling back to runtime mode.",
              error.localizedDescription)
        // Fall through — лучше запустить runtime, чем упасть совсем
    }
}

// Самое первое исполняемое выражение — до NSApp setup, до SingleInstanceGuard,
// до SentryConfig, до всего остального.
redirectRuntimeToBundleIfPresent()

// Fixes KRAB-EAR-AGENT-F: write() в закрытый Unix-сокет (после restart backend
// или во время Phase C.2 reconnect) посылал SIGPIPE → fatal kill агента.
// Игнорируем сигнал; write() вернёт -1/EPIPE, что уже обрабатывает IPCError.writeFailed.
signal(SIGPIPE, SIG_IGN)

// MainActor.assumeIsolated: top-level main.swift runs on main thread synchronously,
// но nominally nonisolated. AgentAppDelegate.init(options:) и NSApplication setup
// помечены @MainActor в Foundation/AppKit (Swift 6 SDK). Без этой обёртки CI ловит:
// "call to main actor-isolated initializer 'init(options:)' in synchronous nonisolated context".
MainActor.assumeIsolated {
    let options = LaunchOptions(arguments: CommandLine.arguments)
    let app = NSApplication.shared
    let delegate = AgentAppDelegate(options: options)
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
