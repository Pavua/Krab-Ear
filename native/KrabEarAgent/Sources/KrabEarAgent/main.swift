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

        // Bundled Python-рантайм внутри самого .app (задача упаковки, 2026-08-09):
        // scripts/build_bundled_runtime.command кладёт самодостаточный CPython
        // (.venv_krab_ear/) + копию KrabEar/ в Contents/Resources/vendor —
        // единственный источник backend'а для DMG-получателя без system Python
        // >= 3.12 и без git-репозитория рядом (T2/T3 из onboarding-аудита).
        //
        // Проверяется РАНЬШЕ cwd/walk-up намеренно: bundled-копия — самый
        // авторитетный источник для настоящей дистрибуции (в отличие от
        // 8-уровневого walk-up её нельзя случайно перепутать с чужим
        // dev-checkout'ом на диске), но explicit/env-override выше по цепочке
        // всё ещё обязаны выигрывать для dev/CI-сценариев.
        //
        // Строго требуем форму "Contents/MacOS/<исполняемый>" (grandparent
        // executablePath должен называться именно "Contents") — иначе
        // вычисление годится и для legacy-пути native/runtime/KrabEarAgent, и
        // тогда произвольный Resources/vendor рядом ложно подхватился бы.
        if let executablePath {
            let execURL = URL(fileURLWithPath: executablePath).standardizedFileURL
            let macOSDir = execURL.deletingLastPathComponent()
            let contentsDir = macOSDir.deletingLastPathComponent()
            if contentsDir.lastPathComponent == "Contents" {
                let vendorCandidate = contentsDir
                    .appendingPathComponent("Resources")
                    .appendingPathComponent("vendor")
                    .path
                if isProjectRoot(vendorCandidate) {
                    return vendorCandidate
                }
            }
        }

        // Текущий каталог процесса НАМЕРЕННО не кандидат (ревью E4 N2, 2026-11-16):
        // в active-режиме supervisor запускает `<projectRoot>/.venv_krab_ear/bin/python`,
        // и «корень» из чужого cwd означал бы исполнение чужого интерпретатора.
        // Dev-запуск из репозитория покрывают --project-root, KRAB_EAR_PROJECT_ROOT и
        // walk-up от исполняемого файла (native/runtime/KrabEarAgent лежит внутри репо).
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

        // Корень не найден. Раньше возвращался cwd, то есть недоверенный каталог;
        // теперь заведомо пустой корень: там нет ни backend, ни .venv_krab_ear
        // (под launchd cwd и так был «/»).
        return "/"
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
    let sentryPrivacy: SentryPrivacyLifecycle
    let backendSupervisor: BackendSupervisor
    let launchAgentManager: LaunchAgentManager
    var ipcClient: IPCClient

    // Создаём после окончательной настройки ipcClient; quit не создаёт новую сессию.
    private var storedPlaintextExportCoordinator: PlaintextExportCoordinator?
    var plaintextExportCoordinator: PlaintextExportCoordinator {
        if let coordinator = storedPlaintextExportCoordinator { return coordinator }
        let coordinator = PlaintextExportCoordinator(ipc: ipcClient)
        coordinator.setPrivacyRestricted(privacyModeEnabled)
        storedPlaintextExportCoordinator = coordinator
        return coordinator
    }
    func updatePlaintextExportPrivacy(_ restricted: Bool) {
        storedPlaintextExportCoordinator?.setPrivacyRestricted(restricted)
    }

    private var plaintextExportTerminationPending = false
    private var plaintextExportTerminationReplied = false

    let pasteService: PasteService
    let pasteTargetActivator: PasteTargetActivator
    let quickEditOverlay: any QuickEditPresenting
    let pasteActivationTarget: (NSRunningApplication) -> PasteActivationTarget
    let pasteFrontmostApplication: () -> NSRunningApplication?
    let playPasteCompletionSound: () -> Void
    let pasteStatusUpdater: ((String, String) -> Void)?
    let audioDuckingService: SystemAudioDuckingService
    private let notificationService: NotificationService
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
    var memoryDashboardWindowController: NSWindowController?
    // PR 1.5: Wake word listener (Porcupine)
    private var quickStartController: QuickStartWindowController?
    var realtimeOverlayTimer: Timer?

    var settings: AgentSettings = .default
    private let settingsOperations = AgentSettingsOperationQueue()
    private var pendingPrivacyMutations = 0
    private static let uncertainPrivacyKey = "KrabEar_UnconfirmedPrivacyWrite"
    var hasUnconfirmedPrivacyWrite: Bool { userDefaults.bool(forKey: Self.uncertainPrivacyKey) }
    private var privacyRevision = UUID()
    private(set) var privacyLease: ConfirmedPrivacyLease?
    private var privacyVerifiedAt: ContinuousClock.Instant?
    private var nextPrivacyPoll: ContinuousClock.Instant?
    private var privacyPoll: Task<Bool, Never>?
    var privacyNow: () -> ContinuousClock.Instant = { .now }

    // Период 5с, плюс 5с на ограниченное чтение. Старый ответ не продлевает срок.
    private static let privacyPollInterval: Duration = .seconds(5)
    private static let privacyLifetime: Duration = .seconds(10)
    var isRecording = false
    var isProcessing = false
    // Мини-волна 2026-08-11 (авто-дострел отложенного stop_recording):
    // armed — one-shot «стоп сдался, ждём первого здорового ping'а»;
    // budget — кап попыток на эпизод recoveryPending (спека §2.2/2.5).
    var dictationStopAutoRetryArmed = false
    var dictationStopAutoRetryBudget = DictationStopAutoRetryGate.fullBudget
    /// Не даёт двум async start разделить один snapshot audio ducking.
    let recordingStartGate = RecordingStartGate()
    /// Пока backend ещё не вернул token старта, отпускание hold/второй toggle
    /// запоминаются как stop-intent и исполняются сразу после ответа G1.
    var recordingStartInFlight = false
    /// true только после постановки start_recording в transport.
    var recordingStartRequestMayBeActive = false
    var dictationDuckingLease: AudioDuckingLease?
    struct PromotedMeetingDucking {
        let generationToken: String
        let startRequestID: String?
        let lease: AudioDuckingLease
        var confirmed: Bool
        var finished: Bool
    }
    var promotedMeetingDucking: PromotedMeetingDucking?
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
    /// Call Observer w1 T8 (spec §3 комп. 6): единственный владелец координатора —
    /// создаётся в setupCallObserver() (main+CallObserver.swift), останавливается
    /// в tearDownCallObserver() из applicationWillTerminate.
    var callObserverCoordinator: CallObserverCoordinator?
    /// Дискавери живых сессий VG поллингом (см. callObserverCoordinator выше).
    var callObserverWatcher: VGSessionWatcher?
    /// T9 (2б): пункт статус-меню «Звонок агента…» — создаётся в
    /// rebuildStatusMenu, enabled-состояние обновляется из menuWillOpen
    /// (тот же паттерн, что brainLeaseMenuItem, см. main+CallObserver.swift
    /// refreshCallObserverMenuItem()).
    var callObserverMenuItem: NSMenuItem?
    /// B3: инфо-строка «кто держит LM Studio» в status-меню — создаётся в
    /// rebuildStatusMenu, обновляется из menuWillOpen (main+BrainLease.swift).
    var brainLeaseMenuItem: NSMenuItem?
    /// T8 (Memory Conductor): инфо-строка «Память: …» СРАЗУ ПОСЛЕ brain-lease
    /// строки — создаётся в rebuildStatusMenu, обновляется из menuWillOpen
    /// (main+MemoryLine.swift).
    var memoryLineMenuItem: NSMenuItem?
    /// Последняя ошибка ErrorBus (например `system.unclean_restart`) — disabled
    /// строка status-меню, НЕ `NSStatusBarButton.toolTip`. Native toolTip на
    /// Tahoe следует за курсором, если mouseExited съедает StatusBarDropView.
    /// Создаётся в rebuildStatusMenu, обновляется из menuWillOpen
    /// (main+LastErrorLine.swift).
    var lastErrorMenuItem: NSMenuItem?
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

    init(
        options: LaunchOptions, userDefaults: UserDefaults = .standard,
        sentryPrivacy: SentryPrivacyLifecycle? = nil,
        audioDuckingService: SystemAudioDuckingService = SystemAudioDuckingService(),
        notificationService: NotificationService = NotificationService(),
        pasteService: PasteService = PasteService(),
        pasteTargetActivator: PasteTargetActivator? = nil,
        quickEditOverlay: (any QuickEditPresenting)? = nil,
        pasteActivationTarget: ((NSRunningApplication) -> PasteActivationTarget)? = nil,
        pasteFrontmostApplication: @escaping () -> NSRunningApplication? = { NSWorkspace.shared.frontmostApplication },
        pasteStatusUpdater: ((String, String) -> Void)? = nil,
        playPasteCompletionSound: @escaping () -> Void = {
            DispatchQueue.global(qos: .userInitiated).async { NSSound(named: "Purr")?.play() }
        }
    ) {
        self.quickEditOverlay = quickEditOverlay ?? QuickEditOverlay()
        self.pasteActivationTarget = pasteActivationTarget ?? { PasteActivationTarget(application: $0) }
        self.pasteFrontmostApplication = pasteFrontmostApplication
        self.pasteStatusUpdater = pasteStatusUpdater
        self.playPasteCompletionSound = playPasteCompletionSound
        self.pasteService = pasteService
        self.pasteTargetActivator = pasteTargetActivator ?? PasteTargetActivator()
        self.audioDuckingService = audioDuckingService
        self.notificationService = notificationService
        self.options = options
        self.userDefaults = userDefaults
        self.sentryPrivacy = sentryPrivacy ?? SentryConfig.lifecycle
        self.backendSupervisor = BackendSupervisor(projectRoot: options.projectRoot)
        self.launchAgentManager = LaunchAgentManager(projectRoot: options.projectRoot)
        self.ipcClient = IPCClient(socketPath: (NSString(string: "~/Library/Application Support/KrabEar").expandingTildeInPath as NSString).appendingPathComponent("krabear.sock"))
        super.init()
        // До первого чтения разрешений нет. Сам факт запуска не отзывает
        // кэш предыдущего процесса: его привязку проверит подтверждённый OFF.
        settings.privacyModeEnabled = true
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Phase C C.6: POSIX flock — единственная общая single-instance гарантия.
        // Новый экземпляр завершается сам; startup больше не ищет и не убивает
        // процессы только по имени `KrabEarAgent`.
        guard acquireFileLock(logger: logger) else {
            // Другой экземпляр Krab Ear уже запущен и держит lock.
            SentryConfig.recordTerminate(callsite: "acquire_file_lock_duplicate")
            NSApp.terminate(nil)
            return
        }

        // Wave 656: bootstrap stage — только владелец flock (duplicate-spawn не пишет шум).
        AgentRecoveryLogger.shared.logStage("app_launched")

        // Только LaunchServices cleanup вынесен в фон, чтобы синхронный Process-вызов
        // не блокировал applicationDidFinishLaunching. PID-based cleanup запрещён.

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

        let readStarted = privacyNow()
        applyConfirmedSettings(loadSettings(), readStarted: readStarted, updateUI: false)
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

        // Call Observer w1 T8 (spec §3 комп. 6): дискавери живых VG-сессий +
        // one-shot терминальный автомат. См. main+CallObserver.swift.
        setupCallObserver()

        // PermissionWizard удален, используем QuickStartWindowController
        historyPanel = HistoryPanelController(
            ipcClient: ipcClient,
            plaintextExportCoordinator: plaintextExportCoordinator,
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

        pasteUndoService = PasteUndoService(pasteService: pasteService)
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

        // Native NSStatusBarButton.toolTip must stay nil: the old 10s refresh
        // reassigned it and the help window followed the pointer.
        startTooltipRefresh()

        if !settings.onboardingCompleted && !options.launchedByLaunchd {
            openQuickStart()
        } else if options.showHistoryOnLaunch {
            openHistoryPanel(forceMenubar: true)
        }
    }

    // MARK: - Backend recovery (see main+IPCRecovery.swift)

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let coordinator = storedPlaintextExportCoordinator else { return .terminateNow }
        if plaintextExportTerminationReplied { return .terminateNow }
        if plaintextExportTerminationPending { return .terminateLater }
        // shutdown закрывает новые операции и очищает RAM синхронно.
        guard let revoke = coordinator.shutdown() else { return .terminateNow }
        plaintextExportTerminationPending = true
        Task { @MainActor [weak self] in
            await revoke.value
            self?.finishPlaintextExportTermination(sender)
        }
        // Выход приложения не зависит от ответа IPC, даже если transport завис.
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            self?.finishPlaintextExportTermination(sender)
        }
        return .terminateLater
    }

    private func finishPlaintextExportTermination(_ sender: NSApplication) {
        guard plaintextExportTerminationPending else { return }
        plaintextExportTerminationPending = false
        plaintextExportTerminationReplied = true
        sender.reply(toApplicationShouldTerminate: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        _ = storedPlaintextExportCoordinator?.shutdown()
        DistributedNotificationCenter.default().removeObserver(self)
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        hotkeyManager?.stop()
        wakeWordPoller?.deactivate()
        tearDownHealthMonitor()
        tearDownErrorBus()
        tearDownCallObserver()
        selectionTranslator?.stop()
        stopQuickCaptureHotkeyMonitor()
        pasteUndoService?.stop()
        stopLiveSubsCapture()
        stopRealtimeOverlayPolling()
        backendSupervisor.stopBackend()
        // Phase C C.6: release file lock so a subsequent launch can acquire it immediately.
        sentryPrivacy.finishProcess()
        releaseFileLock(logger: logger)
    }

    // MARK: - Wake Word (IPC-поллинг, spec 2026-07-05)

    /// Wake word через IPC-поллинг backend'а (openWakeWord).
    /// Дефолт: выключен (приватность). Включается в Settings → «Разговор с AI».
    /// Отзеркалить локальный тумблер wake word в backend-настройку.
    ///
    /// UserDefaults остаётся ИСТОЧНИКОМ ПРАВДЫ — это то, что пользователь
    /// переключает в Настройках. Но backend с 2026-07-29 гейтит
    /// `wake_word_start` по `wake_word_enabled` (F5), поэтому обязан знать
    /// значение: без зеркала settings.json продолжал бы расходиться с
    /// реальностью (живой инцидент: JSON показывал False, микрофон слушал).
    ///
    /// Строго off-main (AGENT-3) и без блокировки старта: провал синхронизации
    /// не должен ломать запуск агента — backend по умолчанию разрешает старт,
    /// так что худший исход промаха здесь прежнее поведение, не отказ фичи.
    func syncWakeWordEnabledToBackend(_ enabled: Bool) {
        let client = ipcClient
        DispatchQueue.global(qos: .utility).async { [weak self] in
            do {
                _ = try client.call(
                    method: "set_settings",
                    params: ["wake_word_enabled": enabled]
                )
            } catch {
                self?.logger.warn(
                    "Wake word: не удалось синхронизировать wake_word_enabled с backend"
                )
            }
        }
    }

    /// Имя функции сохранено для минимального диффа вызывающих мест.
    func setupWakeWordListenerIfEnabled() {
        let enabled = UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled")
        // Зеркалим ДО guard: выключенное состояние обязано доехать до backend
        // так же надёжно, как включённое — иначе выключить фичу было бы можно
        // только в UI, а settings.json остался бы врать.
        syncWakeWordEnabledToBackend(enabled)
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
                onStormTripped: {
                    NotificationService().notify(
                        title: "Слово-пробуждение приостановлено",
                        body: "Модель срабатывала слишком часто (ложные срабатывания). На 10 минут отключено; смените модель в Настройках → Голосовой ассистент.")
                },
                onWedgedEscalation: { [weak self] in
                    guard let self else { return }
                    self.logger.warn("Wake word: backend wedged — автоматический рестарт отложен")
                    DispatchQueue.global(qos: .utility).async { [weak self] in
                        guard let self else { return }
                        let ok = self.backendSupervisor.forceRestartBackend()
                        AgentRecoveryLogger.shared.logForceRestartBackend(
                            source: "wake_word_wedged",
                            success: ok,
                            deferred: true
                        )
                        DispatchQueue.main.async {
                            BackendToast.shared.show(
                                "⚠ Wake word недоступен — автоматический рестарт отложен до проверки активности",
                                duration: 10.0
                            )
                        }
                    }
                },
                onWedgedGiveUp: { [weak self] in
                    guard let self else { return }
                    self.logger.warn("Wake word: авто-восстановление отложено, wedged сохраняется")
                    BackendToast.shared.show(
                        "⚠ Wake word недоступен — проверьте микрофон или выключите wake word",
                        duration: 12.0
                    )
                }
            )
        }
        setupWakeWordConversationObservers()
        setupWakeWordTTSPlaybackObservers()
        wakeWordPoller?.activate(pausedForPrivacy: privacyModeEnabled)
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

    @objc func onOpenMemoryDashboard() {
        if memoryDashboardWindowController == nil {
            let wc = MemoryDashboardWindowController(ipcClient: ipcClient)
            self.memoryDashboardWindowController = wc
        }
        memoryDashboardWindowController?.showWindow(nil)
        memoryDashboardWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
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
        persistSettingsPayload(["quality_profile": "balanced"])
        rebuildStatusMenu()
    }

    @objc func onQualityMax() {
        persistSettingsPayload(["quality_profile": "max"])
        rebuildStatusMenu()
    }

    @objc func onAutoPasteToggle() {
        persistSettingsPayload(["auto_paste": !settings.autoPaste])
        rebuildStatusMenu()
    }

    @objc func onStartSoundToggle() {
        persistSettingsPayload(["play_start_sound": !settings.playStartSound])
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingToggle() {
        persistSettingsPayload(["audio_ducking_enabled": !settings.audioDuckingEnabled])
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent25() {
        persistSettingsPayload(["audio_ducking_percent": 25])
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent50() {
        persistSettingsPayload(["audio_ducking_percent": 50])
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent75() {
        persistSettingsPayload(["audio_ducking_percent": 75])
        rebuildStatusMenu()
    }

    @objc func onAudioDuckingPercent100() {
        persistSettingsPayload(["audio_ducking_percent": 100])
        rebuildStatusMenu()
    }

    @objc func onOverlayOpacity25() {
        persistSettingsPayload(["overlay_opacity_percent": 25])
        rebuildStatusMenu()
    }

    @objc func onOverlayOpacity45() {
        persistSettingsPayload(["overlay_opacity_percent": 45])
        rebuildStatusMenu()
    }

    @objc func onOverlayOpacity65() {
        persistSettingsPayload(["overlay_opacity_percent": 65])
        rebuildStatusMenu()
    }

    @objc func onAutostartToggle() {
        persistSettingsPayload(["auto_start_enabled": !settings.autoStartEnabled])
        rebuildStatusMenu()
    }

    @objc func onDockIconToggle() {
        persistSettingsPayload(["show_dock_icon": !settings.showDockIcon])
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
        persistSettingsPayload(["network_mode": "offline_default"])
        rebuildStatusMenu()
    }

    @objc func onNetworkOfflineStrict() {
        persistSettingsPayload(["network_mode": "offline_strict"])
        rebuildStatusMenu()
    }

    @objc func onNetworkOnlineOptIn() {
        persistSettingsPayload(["network_mode": "online_opt_in"])
        rebuildStatusMenu()
    }

    @objc func onTranslateAndPasteToggle() {
        persistSettingsPayload(["translate_and_paste": !settings.translateAndPaste])
        rebuildStatusMenu()
    }

    @objc func onTranslationModeOff() {
        persistSettingsPayload(["translation_mode": "off"])
        rebuildStatusMenu()
    }

    @objc func onTranslationModeRuToEs() {
        persistSettingsPayload(["translation_mode": "ru_to_es"])
        rebuildStatusMenu()
    }

    @objc func onTranslationModeEsToRu() {
        persistSettingsPayload(["translation_mode": "es_to_ru"])
        rebuildStatusMenu()
    }

    @objc func onTranslationModeEnToRu() {
        persistSettingsPayload(["translation_mode": "en_to_ru"])
        rebuildStatusMenu()
    }

    @objc func onTranslationModeAuto() {
        persistSettingsPayload(["translation_mode": "auto"])
        rebuildStatusMenu()
    }

    @objc func onTranslationModeBilingualRuEs() {
        persistSettingsPayload(["translation_mode": "bilingual_ru_es"])
        rebuildStatusMenu()
    }

    @objc func onApplyLiveTranslationPreset() {
        persistSettingsPayload([
            "translation_mode": "auto",
            "translation_style": "chat",
            "translate_and_paste": false,
            "realtime_preview_enabled": true,
            "network_mode": "offline_default",
        ], successMessage: "Live Translation preset включен: auto + chat + realtime.")
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
        persistSettingsPayload(
            ["translation_mode": nextMode],
            successMessage: "Направление перевода переключено: \(nextMode == "ru_to_es" ? "RU -> ES" : "ES -> RU")."
        )
    }

    @objc func onTranslationStyleNeutral() {
        persistSettingsPayload(["translation_style": "neutral"])
        rebuildStatusMenu()
    }

    @objc func onTranslationStyleChat() {
        persistSettingsPayload(["translation_style": "chat"])
        rebuildStatusMenu()
    }

    @objc func onTranslationStyleFormal() {
        persistSettingsPayload(["translation_style": "formal"])
        rebuildStatusMenu()
    }

    @objc func onClipboardModeAlways() {
        persistSettingsPayload(["clipboard_mode": "always_copy"])
        rebuildStatusMenu()
    }

    @objc func onClipboardModeOnFail() {
        persistSettingsPayload(["clipboard_mode": "copy_on_fail"])
        rebuildStatusMenu()
    }

    @objc func onClipboardModeNever() {
        persistSettingsPayload(["clipboard_mode": "never_copy"])
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

    private func applyHotkeyProfile(_ profile: String) {
        let patch: [String: Any]
        switch profile {
        case "meeting":
            patch = [
                "hotkey_profile": "meeting",
                "translation_mode": "auto_to_ru",
                "translation_style": "chat",
                "translate_and_paste": false,
                "auto_paste": false,
                "realtime_preview_enabled": true,
                "clipboard_mode": "copy_on_fail",
            ]
        case "translation":
            patch = [
                "hotkey_profile": "translation",
                "translation_mode": "auto",
                "translation_style": "chat",
                "translate_and_paste": true,
                "auto_paste": false,
                "realtime_preview_enabled": true,
                "clipboard_mode": "always_copy",
            ]
        default:
            patch = [
                "hotkey_profile": "default",
                "translation_mode": "off",
                "translation_style": "neutral",
                "translate_and_paste": false,
                "auto_paste": true,
                "realtime_preview_enabled": true,
                "clipboard_mode": "always_copy",
            ]
        }
        let title: String
        switch profile {
        case "meeting":
            title = "Meeting"
        case "translation":
            title = "Translation"
        default:
            title = "Default"
        }
        persistSettingsPayload(patch, successMessage: "Профиль hotkey переключен: \(title)")
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

    func loadSettings() -> [String: Any]? {
        let response = try? callWithRecovery(method: "get_settings", params: [:])
        guard confirmedPrivacyFlag(response?["ok"]) == true else { return nil }
        return response?["result"] as? [String: Any]
    }

    @discardableResult
    private func applyConfirmedSettings(
        _ payload: [String: Any]?, readStarted: ContinuousClock.Instant,
        privacyOnly: Bool = false, updateUI: Bool = true
    ) -> Bool {
        guard let payload, let confirmedPrivacy = confirmedPrivacyFlag(payload["privacy_mode_enabled"]),
              readStarted.duration(to: privacyNow()) < Self.privacyLifetime else {
            privacyVerifiedAt = nil
            setPrivacyMode(true)
            return false
        }
        let previous = settings
        if !privacyOnly { settings = AgentSettings(from: payload) }
        privacyVerifiedAt = readStarted
        let restricted = confirmedPrivacy || pendingPrivacyMutations > 0
            || hasUnconfirmedPrivacyWrite
        setPrivacyMode(restricted)
        if !restricted {
            let deadline = readStarted.advanced(by: Self.privacyLifetime)
            let epoch = (payload["live_subs_policy_epoch"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            if privacyLease?.liveSubsEpoch != epoch || privacyLease?.renew(until: deadline) != true {
                if privacyLease != nil { revokePrivacyLease() }
                privacyLease = ConfirmedPrivacyLease(until: deadline, liveSubsEpoch: epoch)
            }
            SentryConfig.initializeFromSettings(
                payload,
                environment: userDefaults.string(forKey: "KrabEar_SentryEnvironment") ?? "production",
                fallbackDSN: userDefaults.string(forKey: "KrabEar_SentryDSN"),
                lease: privacyLease, owner: sentryPrivacy
            )
        }
        if updateUI && !privacyOnly {
            applySettingsSideEffects(previous: previous, current: settings)
            if previous.mode != settings.mode || previous.showDockIcon != settings.showDockIcon {
                applyMode(settings.mode, persist: false)
            } else {
                rebuildStatusMenu()
            }
            historyPanel?.syncSettingsControls(using: settings)
        }
        return true
    }

    @discardableResult
    private func refreshSettings(privacyOnly: Bool = false) async -> Bool {
        let revision = privacyRevision
        let started = privacyNow()
        let response = try? await ipcClient.callAsync(method: "get_settings", timeoutSec: IPCClient.quickTimeoutSec)
        guard revision == privacyRevision else { return false }
        let payload = confirmedPrivacyFlag(response?["ok"]) == true ? response?["result"] as? [String: Any] : nil
        return applyConfirmedSettings(payload, readStarted: started, privacyOnly: privacyOnly)
    }

    // Не ставим неограниченную очередь чтений за медленной записью. Срок последнего
    // разрешения проверяется даже пока очередь занята или IPC ещё не вернул ответ.
    @discardableResult
    func tickPrivacyMonitor() -> Task<Bool, Never>? {
        let now = privacyNow()
        if let verified = privacyVerifiedAt, verified.duration(to: now) >= Self.privacyLifetime {
            privacyVerifiedAt = nil
            setPrivacyMode(true)
        }
        if let privacyPoll { return privacyPoll }
        if let nextPrivacyPoll, now < nextPrivacyPoll { return nil }
        nextPrivacyPoll = now.advanced(by: Self.privacyPollInterval)
        let poll = settingsOperations.enqueue { [weak self] in
            guard let self else { return false }
            defer { self.privacyPoll = nil }
            return await self.refreshSettings(privacyOnly: true)
        }
        privacyPoll = poll
        return poll
    }

    func revokePrivacyLease() {
        privacyLease?.revoke()
        privacyLease = nil
        stopLiveSubsCapture(flush: false)
        sentryPrivacy.suspend()
    }

    func invalidatePrivacyConnection() {
        privacyRevision = UUID()
        privacyVerifiedAt = nil
        nextPrivacyPoll = nil
        setPrivacyMode(true)
    }

    @discardableResult
    func performSettingsMutation(
        _ method: String, params: [String: Any], preservesPrivacy: Bool = false
    ) -> Task<Bool, Never> {
        let requestedPrivacy = confirmedPrivacyFlag(params["privacy_mode_enabled"])
        let affectsPrivacy = params["privacy_mode_enabled"] != nil
            || (method == "apply_config_preset" && !preservesPrivacy)
            || method == "restore_settings_backup"
        if affectsPrivacy {
            pendingPrivacyMutations += 1
            invalidatePrivacyConnection()
        }
        return settingsOperations.enqueue { [weak self] in
            guard let self else { return false }
            let alreadyUncertain = self.hasUnconfirmedPrivacyWrite
            let response: [String: Any]?
            if affectsPrivacy {
                // После timeout сервер ещё может применить запрос. Повтор и
                // произвольный get_settings=false не подтверждают завершение записи.
                self.userDefaults.set(true, forKey: Self.uncertainPrivacyKey)
                response = try? await self.ipcClient.callAsync(method: method, params: params)
                self.pendingPrivacyMutations -= 1
            } else {
                response = try? await self.callAsyncWithRecovery(method: method, params: params)
            }
            let result = confirmedPrivacyFlag(response?["ok"]) == true ? response?["result"] as? [String: Any] : nil
            let acknowledged: Bool
            if method == "apply_config_preset" {
                acknowledged = confirmedPrivacyFlag(result?["applied"]) == true
            } else {
                let payload = method == "restore_settings_backup"
                    ? result?["restored_settings"] as? [String: Any] : result
                let saved = confirmedPrivacyFlag(payload?["privacy_mode_enabled"])
                acknowledged = saved != nil && (requestedPrivacy == nil || saved == requestedPrivacy)
            }
            if affectsPrivacy && acknowledged && (!alreadyUncertain || requestedPrivacy == false) {
                self.userDefaults.removeObject(forKey: Self.uncertainPrivacyKey)
            }
            // Только свежий get_settings несёт проверенную backend privacy-проекцию.
            let refreshed = await self.refreshSettings()
            return acknowledged && refreshed
                && (requestedPrivacy == nil || self.settings.privacyModeEnabled == requestedPrivacy)
        }
    }

    func persistSettingsPayload(_ patch: [String: Any], successMessage: String? = nil) {
        guard !patch.isEmpty else { return }
        let operation = performSettingsMutation("set_settings", params: patch)
        Task { [weak self] in
            if await operation.value {
                if let successMessage { self?.notify(title: "Krab Ear", body: successMessage) }
            } else {
                self?.notify(title: "Krab Ear", body: "Не удалось подтвердить сохранение настроек")
            }
        }
    }

    func enqueueSettingsRefresh() {
        settingsOperations.enqueue { [weak self] in await self?.refreshSettings() ?? false }
    }

    func updateSettingsFromPanel(_ patch: [String: Any]) -> AgentSettings {
        persistSettingsPayload(patch)
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
                    self.persistSettingsPayload(["onboarding_completed": true])
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
        let step = WakeWordConsentStepController(ipcClient: ipcClient) { [weak self] outcome in
            guard let self = self else { return }
            self.wakeWordConsentStep = nil
            if outcome == .failed {
                // Тихий отказ оставил бы wake word выключенным без объяснения.
                NotificationService().notify(
                    title: "Слово-пробуждение не включено",
                    body: "Не удалось включить. Включите его в Настройках → Голосовой ассистент.")
            }
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
