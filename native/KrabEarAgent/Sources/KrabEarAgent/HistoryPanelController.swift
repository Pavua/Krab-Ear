/*
 Нативная панель истории Krab Ear.

 Связи модуля:
 1) IPCClient: загрузка/поиск/удаление/компактация записей.
 2) AgentSettings: размер страницы и значения UI-настроек.
 3) main.swift: передаёт callback сохранения настроек.
*/

import AppKit
import Foundation
import UniformTypeIdentifiers

/// NSClipView с flipped coordinate system (y=0 сверху).
/// Нужен чтобы короткий document view оставался вверху scroll area,
/// а не проваливался вниз в non-flipped NSView (где y=0 внизу).
final class FlippedClipView: NSClipView {
    override var isFlipped: Bool { true }
}

/// Нативная панель истории с пагинацией, поиском, копированием и удалением.
final class HistoryPanelController: NSWindowController, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate, NSTabViewDelegate {
    enum PanelTab: String {
        case dictation = "dictation"
        case liveTranslation = "live_translation"
        case history = "history"
        /// Вкладка «Разговор с AI» — Phase 1.3 Voice Assistant Mode.
        case conversation = "conversation"
        /// Вкладка «Автозвонки» — Phase 3.4 Call Automation.
        case callAutomation = "call_automation"
        /// Вкладка «Диагностика» — Phase B.2 F6 Diagnostics Tab UI.
        case diagnostics = "diagnostics"
        /// Вкладка «Архив»
        case archive = "archive"

        static func from(settingsValue: String) -> PanelTab {
            switch settingsValue {
            case PanelTab.dictation.rawValue:
                return .dictation
            case PanelTab.liveTranslation.rawValue:
                return .liveTranslation
            case PanelTab.conversation.rawValue:
                return .conversation
            case PanelTab.callAutomation.rawValue:
                return .callAutomation
            case PanelTab.diagnostics.rawValue:
                return .diagnostics
            case PanelTab.archive.rawValue:
                return .archive
            default:
                return .history
            }
        }
    }
    struct ImportJob {
        let paths: [String]
        let sourceTag: String
        let audioCount: Int
        let folderCount: Int
        let totalBytes: Int
        let byExtension: [String: Int]
    }

    struct ImportPreview {
        let audioCount: Int
        let folderCount: Int
        let sample: [String]
        let byExtension: [String: Int]
        let totalBytes: Int
    }

    let ipcClient: IPCClient
    let settingsProvider: () -> AgentSettings
    let settingsUpdater: ([String: Any]) -> AgentSettings
    let onToggleRecording: () -> Void
    private let onRestartAgent: () -> Void
    private let onStopAgent: () -> Void
    let onPasteHistoryItem: (HistoryItem) -> Void
    private let onSwapRuEsDirection: () -> Void
    let notificationService = NotificationService()

    var rowHeightCache: [String: CGFloat] = [:]
    var items: [HistoryItem] = [] {
        didSet { rowHeightCache.removeAll() }
    }
    var nextCursor: String?
    var currentQuery: String = ""
    var isSyncingSettings = false
    /// Последнее известное значение mlx_available из list_stt_engines.
    /// syncSettingsControls не имеет доступа к свежему ответу IPC — использует
    /// это кэшированное значение; completion fetchAndRebuildSTTEnginesCard
    /// (Task 3 Step 5) обновляет его и вызывает syncGigaamTransportControls
    /// повторно с актуальным значением.
    var lastKnownGigaamMlxAvailable: Bool = false
    var importQueue: [ImportJob] = []
    var importJobSignatures: Set<String> = []
    var currentImportJob: ImportJob?
    var isImportRunning = false
    var isImportPaused = false
    var importCancellationRequested = false
    var importJobsPlanned = 0
    var importJobsCompleted = 0
    var importProcessedTotal = 0
    var importErrorsTotal = 0
    var importErrorMessages: [String] = []
    var importDurationTotalSec: Double = 0
    var importSessionStartedAt: Date?
    var lastImportReportPath: String?
    var importSourceStats: [String: Int] = [:]
    var importFormatStats: [String: Int] = [:]
    var importFilesPlanned = 0
    var importBytesPlanned = 0
    var importElapsedTimer: Timer?
    var currentImportJobStartedAt: Date?
    // Async transcribe (PR #14): job_id + polling timer for стадийный progress.
    var currentTranscribeJobID: String?
    var transcribeProgressTimer: Timer?
    var currentJobAudioDurationSec: Double = 0
    var transcribeProgressFailCount: Int = 0
    var isSyncingTabs = false
    var previewPollTick = 0
    // True while a Call Assist session is running. Gates the periodic
    // get_call_assist_state poll in the preview timer so it is NOT hammered
    // when no session is active (BACKEND-T over-poll / IPC rate-limit fix).
    // Kept in sync by applyCallAssistState(_:) on every start/stop/refresh.
    var callAssistActive = false
    var isRecoveringHistoryFromFilters = false

    // MARK: - Inline translation (per-item toggle)
    /// NSCache<NSString, NSString>: itemID → translated text.  Capacity 100 items.
    let inlineTranslationCache: NSCache<NSString, NSString> = {
        let c = NSCache<NSString, NSString>()
        c.countLimit = 100
        return c
    }()
    /// Set of item IDs currently showing their translation (toggled ON).
    var inlineTranslationVisible: Set<String> = []
    /// Set of item IDs that have an IPC request in-flight.
    var inlineTranslationLoading: Set<String> = []

    let mainTabView = NSTabView()
    let tableView = NSTableView()
    let historyEmptyStateContainer = NSStackView()
    let searchField = NSSearchField()
    let historyPageSizeSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let historyDensitySelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let historyPasteStatusFilter = NSPopUpButton(frame: .zero, pullsDown: false)
    let historyTranslationModeFilter = NSPopUpButton(frame: .zero, pullsDown: false)
    let historyTranslationStatusFilter = NSPopUpButton(frame: .zero, pullsDown: false)
    /// Filter: 0 = Все, 1 = Только с action items, 2 = Только без.
    /// Применяется client-side (action_items хранятся в HistoryItem fields из PR #295).
    let historyActionItemsFilter = NSPopUpButton(frame: .zero, pullsDown: false)
    let historyFromDateField = NSTextField(frame: .zero)
    let historyToDateField = NSTextField(frame: .zero)
    let historyFocusModeButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Фокус истории: ON", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let qualitySelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let cleanupSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let translationSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let translationStyleSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let networkSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let captureSourceSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let clipboardModeSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let audioDuckingButton = NSButton(checkboxWithTitle: "Приглушать звук", target: nil, action: nil)
    let audioDuckingSlider = NSSlider(value: 50, minValue: 0, maxValue: 100, target: nil, action: nil)
    let audioDuckingValueLabel = NSTextField(labelWithString: "50%")
    // D.10a: AI Settings Controls
    private let aiSectionLabel: NSTextField = {
        let label = NSTextField(labelWithString: "AI и обработка")
        label.font = KrabEarTheme.Typography.sectionTitle
        return label
    }()
    let diarizationButton = NSButton(checkboxWithTitle: "Диаризация (определение говорящих)", target: nil, action: nil)
    let llmRewriteButton = NSButton(checkboxWithTitle: "LLM постобработка текста", target: nil, action: nil)
    /// GigaAM-RNNT v2 — RU-специализированная STT модель (~2.5× меньше WER на русском).
    /// Требует ~/.venv_krab_ear_gigaam (см. scripts/install_gigaam_venv.command).
    /// Pre-flight check на наличие venv делается в onGigaamEnabledChanged.
    let gigaamEnabledButton = NSButton(checkboxWithTitle: "GigaAM-RNNT v2 (RU, опционально)", target: nil, action: nil)
    /// Показывает имя последнего использованного STT движка (из get_diagnostics stt.last_engine).
    let sttEngineLabel = NSTextField(labelWithString: "—")
    // --- C3a Quick Capture (быстрая голосовая заметка) Settings section, Task 3 ---
    // Ни один из трёх контролов не входит в кэшируемую AgentSettings — main+QuickCapture.swift
    // читает эти ключи ЖИВЬЁМ через get_settings (см. HistoryPanelController+Settings.swift
    // buildQuickCaptureSection / refreshQuickCaptureSectionState).
    let quickCaptureNotesButton = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    let quickCaptureObsidianButton = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    let quickCaptureHotkeySelector = NSPopUpButton(frame: .zero, pullsDown: false)
    /// C3b Task 2: показывать плавающую панель-скретчпад автоматически при
    /// старте быстрой заметки — quick_capture_show_panel (тот же live-паттерн,
    /// что и три соседних выше).
    let quickCaptureShowPanelButton = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    let llmModelSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let overlayOpacitySlider = NSSlider(value: 45, minValue: 15, maxValue: 90, target: nil, action: nil)
    let overlayOpacityValueLabel = NSTextField(labelWithString: "45%")
    let modeSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let autoPasteButton = NSButton(checkboxWithTitle: "Автовставка", target: nil, action: nil)
    let pasteUndoButton = NSButton(checkboxWithTitle: "Откат вставки (Cmd+Ctrl+Z)", target: nil, action: nil)
    let smartFieldFormatButton = NSButton(checkboxWithTitle: "Умная вставка по типу поля", target: nil, action: nil)
    let streamingPasteButton = NSButton(checkboxWithTitle: "Потоковая вставка (вживую)", target: nil, action: nil)
    let quickEditButton = NSButton(checkboxWithTitle: "Быстрое редактирование", target: nil, action: nil)
    let quickEditTimeoutStepper = NSStepper()
    let quickEditTimeoutValueLabel = NSTextField(labelWithString: "5 сек")
    /// Privacy Mode (D.5): when ON, disables Sentry telemetry + forces translation offline.
    let startSoundButton = NSButton(checkboxWithTitle: "Звук старта", target: nil, action: nil)
    let realtimePreviewButton = NSButton(checkboxWithTitle: "Realtime превью", target: nil, action: nil)
    let overlayFollowCursorButton = NSButton(checkboxWithTitle: "Оверлей за курсором", target: nil, action: nil)
    let translateAndPasteButton = NSButton(checkboxWithTitle: "Перевод + вставка", target: nil, action: nil)
    let callNotifyButton = NSButton(checkboxWithTitle: "Уведомлять собеседника", target: nil, action: nil)
    let callAutoSummaryButton = NSButton(checkboxWithTitle: "Авто-summary звонка", target: nil, action: nil)
    let voiceGatewayURLField = NSTextField(frame: .zero)
    let voiceGatewayAPIKeyField = NSTextField(frame: .zero)
    let voiceGatewayCheckButton = ThemeSecondaryButton(title: "Проверить Gateway", target: nil, action: nil)
    let autoStartButton = NSButton(checkboxWithTitle: "Автозапуск", target: nil, action: nil)
    let dockIconButton = NSButton(checkboxWithTitle: "Иконка в Dock", target: nil, action: nil)
    let hotkeySelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let hotkeyProfileSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let hotkeyModeToggleRadio = NSButton(radioButtonWithTitle: "Toggle (одно нажатие)", target: nil, action: nil)
    let hotkeyModeHoldRadio = NSButton(radioButtonWithTitle: "Hold (зажать → пишет)", target: nil, action: nil)
    let realtimeStatusLabel = NSTextField(labelWithString: "Realtime: ожидание")
    let callAssistStatusLabel = NSTextField(labelWithString: "Call Assist: idle")
    let realtimeTextView = NSTextView()
    let dictationHistoryHintLabel = NSTextField(labelWithString: "История пока пустая. После первой транскрибации записи появятся здесь.")
    let dictationHistoryOpenButton = ThemeSecondaryButton(title: "Открыть историю", target: nil, action: nil)
    let dictationHistoryPreviewView = NSTextView()
    let callAssistStartButton = ThemePrimaryButton(title: "Старт звонка", target: nil, action: nil)
    let callAssistStopButton = ThemeSecondaryButton(title: "Стоп звонка", target: nil, action: nil)
    let callPhrasePresetSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let callPhraseLoadButton = ThemeSecondaryButton(title: "Загрузить фразы", target: nil, action: nil)
    let callPhraseDirectionSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let callPhraseInputField = NSTextField(frame: .zero)
    private let callPhraseSendButton = ThemeSecondaryButton(title: "Сказать фразу", target: nil, action: nil)
    private let callSummaryButton = ThemeSecondaryButton(title: "Summary звонка", target: nil, action: nil)
    private let callDiagnosticsButton = ThemeSecondaryButton(title: "Диагностика", target: nil, action: nil)
    private let callCostButton = ThemeSecondaryButton(title: "Оценка стоимости", target: nil, action: nil)
    private let callTimelineButton = ThemeSecondaryButton(title: "Timeline", target: nil, action: nil)
    private let callTimelineExportButton = ThemeSecondaryButton(title: "Экспорт Timeline", target: nil, action: nil)
    private let callTimelineToHistoryButton = ThemeSecondaryButton(title: "Timeline -> история", target: nil, action: nil)
    private let callTimelineClearButton = ThemeSecondaryButton(title: "Очистить Timeline", target: nil, action: nil)
    let callTimelineKeepLastSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let callAssistOutputView = NSTextView()
    var callPhrasePresets: [[String: Any]] = []

    // PR 1.5 — Voice Assistant Settings controls
    let vaHotkeyToggle = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    let vaWakeWordToggle = NSButton(checkboxWithTitle: "", target: nil, action: nil)
    let vaEngineSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    // Выбор МОДЕЛИ мозга убран 2026-08-27: Voice Gateway не читает параметр
    // `brain` вовсе. Вместо него — режим (порядок провайдеров), который VG
    // валидирует и реально применяет.
    let vaBrainModeSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    // Wake word (openWakeWord, spec 2026-07-05): статус движка, модель, порог.
    let vaWakeWordStatusLabel = NSTextField(labelWithString: "openWakeWord: проверяю состояние")
    let vaWakeWordModelSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let vaWakeWordThresholdSlider = NSSlider(value: 0.5, minValue: 0.05, maxValue: 1.0, target: nil, action: nil)

    private let topBar = NSStackView()
    let topSearchRow = NSStackView()
    let topActionsRow = NSStackView()
    let helpButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Справка", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let liveTranslatePresetButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Live Translation", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    /// C2c: открывает/показывает живую панель встречи через AgentAppDelegate.
    let meetingPanelButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Встреча", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let filterRow1 = NSStackView()
    let filterRow2 = NSStackView()
    let historyQuickPresetRow = NSStackView()
    let importRow = NSStackView()
    let toolsRow = NSStackView()
    private let controlRow = NSStackView()
    private let bottomBar1 = NSStackView()
    private let bottomBar2 = NSStackView()
    private let settingsBar = NSStackView()
    /// Claude Design A/B variant stack (populated by buildClaudeDesignSettingsSections).
    /// Kept as a var so buildClaudeDesignSettingsSections can replace it on toggle.
    var settingsBarCD = NSStackView()
    private let settingsRow1 = NSStackView()
    private let settingsRow2 = NSStackView()
    private let settingsRow3 = NSStackView()
    private let settingsRow4 = NSStackView()
    private let settingsRow5 = NSStackView()
    private let settingsRow6 = NSStackView()
    private let settingsRow7 = NSStackView()
    private let aiSettingsRow1 = NSStackView()
    private let aiSettingsRow2 = NSStackView()
    private let startStopButton = ThemePrimaryButton(title: "Старт/Стоп", target: nil, action: nil)
    private let restartButton = ThemeSecondaryButton(title: "Перезапуск", target: nil, action: nil)
    private let stopButton = ThemeSecondaryButton(title: "Остановить", target: nil, action: nil)
    let loadMoreButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Показать ещё", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let jumpToLatestButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "К последней", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let loadAllButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Загрузить всё", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let copyButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Копировать", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let pasteSelectedButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Вставить выбранное", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let copyOriginalButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Копировать оригинал", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let copyTranslationButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Копировать перевод", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let retranslateButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Повторить перевод", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let summarizeSelectedButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Summary выбранного", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let exportButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Экспорт", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let exportNdjsonButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Экспорт NDJSON", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let importNdjsonButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Импорт NDJSON", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let deleteButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Удалить", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let compactButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Оптимизировать историю", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let openTranscriptsButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Транскрипты", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let historyOverviewLabel = NSTextField(labelWithString: "")
    let historyStatusLabel = NSTextField(labelWithString: "")
    let glossaryStatusLabel = NSTextField(labelWithString: "Глоссарий: 0")
    let importStatusLabel = NSTextField(labelWithString: "Импорт: idle")
    private(set) var importProgressBar: NSProgressIndicator = {
        let bar = NSProgressIndicator()
        bar.style = .bar
        bar.isIndeterminate = false
        bar.minValue = 0
        bar.maxValue = 100
        bar.doubleValue = 0
        bar.isHidden = true
        bar.translatesAutoresizingMaskIntoConstraints = false
        return bar
    }()
    let cancelImportButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Отменить импорт", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let pauseImportButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Пауза импорта", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let swapRuEsButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Swap RU<->ES", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let openImportReportButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Открыть отчёт", target: nil, action: nil)
        b.isTransparentStyle = true
        return b
    }()
    let dropZoneView = ImportDropZoneView(frame: .zero)
    var previewTimer: Timer?
    private var historyFocusManagedRows: [NSView] = []
    var historyScrollMinHeightConstraint: NSLayoutConstraint?
    // Явные width/height constraints текущего selected tab-item view →
    // mainTabView. NSTabView (.noTabsNoBorder + translatesAutoresizingMaskIntoConstraints
    // = false дочерних view) не гарантированно подгоняет ВЫСОТУ view таба,
    // ставшего selected программно ПОСЛЕ начальной установки — таб 0
    // (Диктовка) получает реальную высоту "бесплатно" (viewable с первого
    // появления окна), остальные табы (silent-restore ui_last_tab ИЛИ клик
    // tabSelector) оставались с frame.height == 0, пряча полностью корректно
    // построенный контент (см. tabView(_:didSelect:) в +LiveTranslation.swift).
    // Деактивируем предыдущую пару перед активацией новой — иначе стейл-
    // constraint на view, которое NSTabView уже убрал из иерархии, кидает
    // NSGenericException 'no common ancestor' (KRAB-EAR-AGENT-2 класс).
    var activeTabSizeConstraints: [NSLayoutConstraint] = []
    let historyFiltersBadge = NSTextField(labelWithString: "Фильтры: 0")
    private let historyPreviewScroll = NSScrollView()
    let historyPreviewTextView = NSTextView()
    private let historyPreviewHeader = NSTextField(labelWithString: "Последние транскрипты")
    // Promoted from local vars in setupUI() for applyVisualTheme() access
    private let liveSettingsBar = NSStackView()
    let liveStack = NSStackView()
    let historyStack = NSStackView()
    let liveHeaderRow = NSStackView()
    let voiceGatewayRow = NSStackView()
    let callAssistConfigRow = NSStackView()
    let callAssistControlRow = NSStackView()
    let callPhrasePresetRow = NSStackView()
    let callPhraseActionRow = NSStackView()
    let callTimelineRow = NSStackView()
    let callAssistOutputScroll = NSScrollView()
    let realtimeScroll = NSScrollView()
    let historyPreviewContainer = NSStackView()
    let scrollView = NSScrollView()
    // Promoted from local vars in setupUI() for applyVisualTheme() access
    private let dictationStack = NSStackView()
    private let dictationHistoryHeaderRow = NSStackView()
    private let dictationHistoryPreviewScroll = NSScrollView()
    // Width constraints recreated by applyVisualTheme() on each invocation.
    // Tracked here so previous batch can be deactivated before new one is built —
    // иначе после повторного applyVisualTheme() стэйл-constraints держат ссылки на
    // отдельные view и валятся NSGenericException 'no common ancestor' (KRAB-EAR-AGENT-2).
    var themeWidthConstraints: [NSLayoutConstraint] = []
    // MARK: - Collapsible section references
    private var dictationRecordingSection: CollapsibleSectionView?
    private var dictationSystemSection: CollapsibleSectionView?
    private var dictationAISection: CollapsibleSectionView?
    var liveCallAssistSection: CollapsibleSectionView?
    var historyFiltersSection: CollapsibleSectionView?
    var historyAdvancedSection: CollapsibleSectionView?
    var historyImportSection: CollapsibleSectionView?
    // MARK: - Tab selector
    var tabSelector: NSSegmentedControl!
    // MARK: - Global status bar (visible on all tabs)
    /// Liquid Glass pill above tabSelector — отображает текущую long-running операцию
    /// backend (transcribe job / Obsidian sync). Подписан на SSE `app.status`.
    let globalStatusBar = GlobalStatusBar()
    // MARK: - Keyboard shortcut monitor
    /// Keyboard event monitor (private use, но `internal` access нужен для
    /// HistoryPanelController+KeyboardShortcuts.swift extension которое set'ит
    /// и убирает monitor.
    nonisolated(unsafe) var keyboardMonitor: Any?
    // MARK: - Reorganized History action rows
    let primaryActionsRow = NSStackView()
    let secondaryActionsRow = NSStackView()
    let statusRow = NSStackView()
    // MARK: - Diagnostics & Metrics
    var diagnosticsSection: CollapsibleSectionView?
    let diagnosticsButton = ThemeSecondaryButton(title: "Диагностика", target: nil, action: nil)
    let metricsButton = ThemeSecondaryButton(title: "Метрики", target: nil, action: nil)
    let recordingStatsButton = ThemeSecondaryButton(title: "Статистика", target: nil, action: nil)
    let storageInfoButton = ThemeSecondaryButton(title: "Хранилище", target: nil, action: nil)
    let diagnosticsRow = NSStackView()
    let diagnosticsOutputScroll = NSScrollView()
    let diagnosticsOutputView = NSTextView()
    // MARK: - Profile Presets & Audio Devices
    var profileAudioSection: CollapsibleSectionView?
    let profilePresetSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let applyProfileButton = ThemePrimaryButton(title: "Применить", target: nil, action: nil)
    let audioDeviceSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let testMicButton = ThemeSecondaryButton(title: "Тест микрофона", target: nil, action: nil)
    let micTestResultLabel = NSTextField(labelWithString: "")
    let profileRow = NSStackView()
    let audioDeviceRow = NSStackView()
    // MARK: - Clipboard History
    var clipboardSection: CollapsibleSectionView?
    let clipboardHistoryButton = ThemeSecondaryButton(title: "Буфер обмена", target: nil, action: nil)
    let repasteButton = ThemeSecondaryButton(title: "Вставить повторно", target: nil, action: nil)
    let clipboardRow = NSStackView()
    // MARK: - History enhancements
    let exportSrtButton = ThemeSecondaryButton(title: "Экспорт SRT", target: nil, action: nil)
    let cleanupHistoryButton = ThemeSecondaryButton(title: "Очистка старых", target: nil, action: nil)
    let cleanupDaysSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    let vocabSuggestionsButton = ThemeSecondaryButton(title: "Словарь", target: nil, action: nil)
    let glossarySuggestionsButton = ThemeSecondaryButton(title: "Глоссарий авто", target: nil, action: nil)
    let sendToAppleNotesButton = ThemeSecondaryButton(title: "Apple Notes", target: nil, action: nil)
    let sendToRemindersButton = ThemeSecondaryButton(title: "Reminders", target: nil, action: nil)
    let createCalendarEventButton = ThemeSecondaryButton(title: "В Календарь", target: nil, action: nil)
    let sendToImessageButton = ThemeSecondaryButton(title: "iMessage", target: nil, action: nil)
    let sendToTelegramButton = ThemeSecondaryButton(title: "Отправить в Telegram", target: nil, action: nil)
    let historyEnhancementsRow = NSStackView()

    // MARK: - Glossary search (GlossarySearch extension)
    /// Search field placed above the glossary list in the Live Translation tab.
    let glossarySearchField = NSSearchField()
    /// Vertical stack that holds one row per glossary entry. Rebuilt by
    /// reloadGlossaryList(glossary:query:) whenever the glossary or query changes.
    let glossaryListStack = NSStackView()

    init(
        ipcClient: IPCClient,
        settingsProvider: @escaping () -> AgentSettings,
        settingsUpdater: @escaping ([String: Any]) -> AgentSettings,
        onToggleRecording: @escaping () -> Void,
        onRestartAgent: @escaping () -> Void,
        onStopAgent: @escaping () -> Void,
        onPasteHistoryItem: @escaping (HistoryItem) -> Void,
        onSwapRuEsDirection: @escaping () -> Void
    ) {
        self.ipcClient = ipcClient
        self.settingsProvider = settingsProvider
        self.settingsUpdater = settingsUpdater
        self.onToggleRecording = onToggleRecording
        self.onRestartAgent = onRestartAgent
        self.onStopAgent = onStopAgent
        self.onPasteHistoryItem = onPasteHistoryItem
        self.onSwapRuEsDirection = onSwapRuEsDirection

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 860, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Krab Ear"
        // Явно разрешаем сильное сужение по ширине для небольших экранов.
        window.minSize = NSSize(width: 360, height: 520)
        window.contentMinSize = NSSize(width: 360, height: 520)
        window.setFrameAutosaveName("KrabEarControlPanelFrame")
        super.init(window: window)
        window.delegate = self
        setupUI()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) не поддерживается")
    }

    deinit {
        if let monitor = keyboardMonitor {
            NSEvent.removeMonitor(monitor)
        }
        globalStatusBar.stop()
    }

    func showPanel() {
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        currentQuery = ""
        searchField.stringValue = ""

        // Сначала показываем «Историю» как безопасный fallback; syncSettingsControls()
        // ниже может восстановить сохранённую ui_last_tab без обхода всех вкладок.
        mainTabView.selectTabViewItem(at: 2)

        syncSettingsControls()
        layoutVisiblePanelTab()
        loadInitial()
        startPreviewPolling()
        refreshCallAssistState(silentOnError: false)
        onLoadCallPhraseLibrary()
        loadProfilePresets()
        loadAudioDevices()
    }

    private func layoutVisiblePanelTab() {
        guard let contentView = window?.contentView else { return }
        // AGE-51 / KRAB-EAR-AGENT-P: не прогреваем все вкладки через selectTabViewItem.
        // На Sequoia такое последовательное переключение заставляет AppKit синхронно
        // строить тяжёлые content view вкладок на главном потоке. Достаточно пересчитать только
        // реально видимую вкладку после syncSettingsControls().
        mainTabView.needsLayout = true
        mainTabView.selectedTabViewItem?.view?.needsLayout = true
        contentView.layoutSubtreeIfNeeded()
    }

    /// Вызывается агентом после новой транскрибации/обновления статуса вставки.
    func onHistoryDidUpdate() {
        guard window?.isVisible == true else { return }
        loadInitial()
    }

    private func setupUI() {
        guard let windowContentView = window?.contentView else { return }
        // Tab view setup
        mainTabView.tabViewType = .noTabsNoBorder
        mainTabView.drawsBackground = false  // Liquid Glass: прозрачный фон, чтобы cardBackground просвечивал
        mainTabView.delegate = self
        mainTabView.translatesAutoresizingMaskIntoConstraints = false
        // Layer-backed + .onSetNeedsDisplay redraw policy уменьшает мерцание
        // NSVisualEffectView при переключении табов (AppKit bug workaround).
        mainTabView.wantsLayer = true
        mainTabView.layer?.backgroundColor = NSColor.clear.cgColor
        mainTabView.layerContentsRedrawPolicy = .onSetNeedsDisplay

        let tabSelector = NSSegmentedControl(labels: ["Диктовка", "Live перевод", "История", "Разговор с AI", "Автозвонки", "Диагностика", "Архив"], trackingMode: .selectOne, target: self, action: #selector(onTabSelectorChanged))
        tabSelector.selectedSegment = 0
        tabSelector.translatesAutoresizingMaskIntoConstraints = false
        tabSelector.segmentStyle = .rounded
        // Liquid Glass: let cardBackground show through (NSSegmentedControl draws
        // opaque white by default; кnown workaround — wrap в wantsLayer + clear).
        tabSelector.wantsLayer = true
        tabSelector.layer?.backgroundColor = NSColor.clear.cgColor
        tabSelector.controlSize = .regular
        self.tabSelector = tabSelector

        windowContentView.addSubview(globalStatusBar)
        windowContentView.addSubview(tabSelector)
        windowContentView.addSubview(mainTabView)
        NSLayoutConstraint.activate([
            // Global status bar — над tabSelector, видим со всех вкладок.
            globalStatusBar.topAnchor.constraint(equalTo: windowContentView.topAnchor, constant: 4),
            globalStatusBar.leadingAnchor.constraint(equalTo: windowContentView.leadingAnchor, constant: 8),
            globalStatusBar.trailingAnchor.constraint(equalTo: windowContentView.trailingAnchor, constant: -8),
            // tabSelector — теперь под globalStatusBar (раньше topAnchor шёл к windowContentView).
            tabSelector.topAnchor.constraint(equalTo: globalStatusBar.bottomAnchor, constant: 4),
            tabSelector.centerXAnchor.constraint(equalTo: windowContentView.centerXAnchor),
            mainTabView.topAnchor.constraint(equalTo: tabSelector.bottomAnchor, constant: 8),
            mainTabView.leadingAnchor.constraint(equalTo: windowContentView.leadingAnchor, constant: 8),
            mainTabView.trailingAnchor.constraint(equalTo: windowContentView.trailingAnchor, constant: -8),
            mainTabView.bottomAnchor.constraint(equalTo: windowContentView.bottomAnchor, constant: -8),
        ])
        // Запускаем подписку на SSE — сразу, не ждём показа панели. Если backend
        // ещё не поднят, SSESessionDelegate проигнорирует ошибку connect, heartbeat
        // оставит pill скрытым. При следующем live-event подключение реактивируется
        // (URLSession сам делает retry для long-poll dataTask).
        globalStatusBar.start()

        // Configure new history stack views
        for stack in [primaryActionsRow, secondaryActionsRow, statusRow] {
            stack.orientation = .horizontal
            stack.spacing = KrabEarTheme.Metrics.standard
            stack.alignment = .centerY
            stack.translatesAutoresizingMaskIntoConstraints = false
            stack.distribution = .fill
            // Prevent buttons from wrapping/stacking on narrow windows
            stack.setHuggingPriority(.defaultLow, for: .horizontal)
            stack.setClippingResistancePriority(.required, for: .horizontal)
        }

        let dictationContentView = NSView()
        dictationContentView.translatesAutoresizingMaskIntoConstraints = false
        dictationContentView.wantsLayer = true
        dictationContentView.layerContentsRedrawPolicy = .onSetNeedsDisplay
        let liveContentView = NSView()
        liveContentView.translatesAutoresizingMaskIntoConstraints = false
        liveContentView.wantsLayer = true
        liveContentView.layerContentsRedrawPolicy = .onSetNeedsDisplay
        // Note: liveSettingsBar is now a class property (promoted for applyVisualTheme)
        let historyContentView = NSView()
        historyContentView.translatesAutoresizingMaskIntoConstraints = false
        historyContentView.wantsLayer = true
        historyContentView.layerContentsRedrawPolicy = .onSetNeedsDisplay
        // Вкладка «Разговор с AI» — Phase 1.3.
        let voiceContentView = NSView()
        voiceContentView.translatesAutoresizingMaskIntoConstraints = false
        voiceContentView.wantsLayer = true
        voiceContentView.layerContentsRedrawPolicy = .onSetNeedsDisplay
        // Вкладка «Автозвонки» — Phase 3.4.
        let callAutoContentView = NSView()
        callAutoContentView.translatesAutoresizingMaskIntoConstraints = false
        callAutoContentView.wantsLayer = true
        callAutoContentView.layerContentsRedrawPolicy = .onSetNeedsDisplay
        // Вкладка «Диагностика» — Phase B.2 F6.
        let diagnosticsContentView = NSView()
        diagnosticsContentView.translatesAutoresizingMaskIntoConstraints = false
        diagnosticsContentView.wantsLayer = true
        diagnosticsContentView.layerContentsRedrawPolicy = .onSetNeedsDisplay
        // Вкладка «Архив»
        let archiveContentView = NSView()
        archiveContentView.translatesAutoresizingMaskIntoConstraints = false
        archiveContentView.wantsLayer = true
        archiveContentView.layerContentsRedrawPolicy = .onSetNeedsDisplay

        // Pre-warm all tabs: make all tab views layer-backed и attached to the view
        // hierarchy before user sees them. Это предотвращает мерцание при первом
        // переключении на таб — NSVisualEffectView уже rendered и активен.
        // Без этого вложенные NSVisualEffectView карточки перерисовываются
        // при первом показе таба, вызывая visual «flash».

        let dictationTab = NSTabViewItem(identifier: PanelTab.dictation.rawValue)
        dictationTab.label = "Диктовка"
        dictationTab.view = dictationContentView
        let liveTab = NSTabViewItem(identifier: PanelTab.liveTranslation.rawValue)
        liveTab.label = "Live перевод"
        liveTab.view = liveContentView
        let historyTab = NSTabViewItem(identifier: PanelTab.history.rawValue)
        historyTab.label = "История"
        historyTab.view = historyContentView
        let conversationTab = NSTabViewItem(identifier: PanelTab.conversation.rawValue)
        conversationTab.label = "Разговор с AI"
        conversationTab.view = voiceContentView
        mainTabView.addTabViewItem(dictationTab)
        mainTabView.addTabViewItem(liveTab)
        mainTabView.addTabViewItem(historyTab)
        mainTabView.addTabViewItem(conversationTab)
        let callAutoTab = NSTabViewItem(identifier: PanelTab.callAutomation.rawValue)
        callAutoTab.label = "Автозвонки"
        callAutoTab.view = callAutoContentView
        mainTabView.addTabViewItem(callAutoTab)
        let diagnosticsTab = NSTabViewItem(identifier: PanelTab.diagnostics.rawValue)
        diagnosticsTab.label = "Диагностика"
        diagnosticsTab.view = diagnosticsContentView
        mainTabView.addTabViewItem(diagnosticsTab)
        
        let archiveTab = NSTabViewItem(identifier: PanelTab.archive.rawValue)
        archiveTab.label = "Архив"
        archiveTab.view = archiveContentView
        mainTabView.addTabViewItem(archiveTab)
        
        // Встроить ConversationViewController в voiceContentView.
        setupConversationTab(contentView: voiceContentView)
        // Встроить CallAutomationController в callAutoContentView. Phase 3.4.
        setupCallAutomationTab(contentView: callAutoContentView)
        // Встроить DiagnosticsTabViewController в diagnosticsContentView. Phase B.2 F6.
        setupDiagnosticsTab(contentView: diagnosticsContentView)
        // Встроить ArchiveTabViewController в archiveContentView.
        setupArchiveTab(contentView: archiveContentView)

        topBar.orientation = .vertical
        topBar.spacing = KrabEarTheme.Metrics.tight
        topBar.alignment = .leading
        topBar.translatesAutoresizingMaskIntoConstraints = false

        topSearchRow.orientation = .horizontal
        topSearchRow.spacing = KrabEarTheme.Metrics.standard
        topSearchRow.alignment = .centerY
        topSearchRow.distribution = .fill
        topSearchRow.translatesAutoresizingMaskIntoConstraints = false

        topActionsRow.orientation = .horizontal
        topActionsRow.spacing = KrabEarTheme.Metrics.standard
        topActionsRow.alignment = .centerY
        topActionsRow.translatesAutoresizingMaskIntoConstraints = false

        filterRow1.orientation = .horizontal
        filterRow1.spacing = KrabEarTheme.Metrics.standard
        filterRow1.alignment = .centerY
        filterRow1.translatesAutoresizingMaskIntoConstraints = false

        filterRow2.orientation = .horizontal
        filterRow2.spacing = KrabEarTheme.Metrics.standard
        filterRow2.alignment = .centerY
        filterRow2.translatesAutoresizingMaskIntoConstraints = false

        historyQuickPresetRow.orientation = .horizontal
        historyQuickPresetRow.spacing = KrabEarTheme.Metrics.standard
        historyQuickPresetRow.alignment = .centerY
        historyQuickPresetRow.translatesAutoresizingMaskIntoConstraints = false

        importRow.orientation = .horizontal
        importRow.spacing = KrabEarTheme.Metrics.standard
        importRow.alignment = .centerY
        importRow.translatesAutoresizingMaskIntoConstraints = false

        toolsRow.orientation = .horizontal
        toolsRow.spacing = KrabEarTheme.Metrics.standard
        toolsRow.alignment = .centerY
        toolsRow.translatesAutoresizingMaskIntoConstraints = false

        controlRow.orientation = .horizontal
        controlRow.spacing = KrabEarTheme.Metrics.standard
        controlRow.alignment = .centerY
        controlRow.translatesAutoresizingMaskIntoConstraints = false
        controlRow.distribution = .fill
        controlRow.setHuggingPriority(.defaultLow, for: .horizontal)
        controlRow.setClippingResistancePriority(.required, for: .horizontal)

        searchField.placeholderString = "Поиск по истории"
        searchField.target = self
        searchField.action = #selector(onSearch)
        searchField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        searchField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        // Автодополнение: недавние запросы из backend (см. +SearchSuggestions).
        setupSearchSuggestionsMenu()
        refreshSearchSuggestions()
        topSearchRow.addArrangedSubview(searchField)

        let clearSearch: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Сбросить", target: self, action: #selector(onClearSearch))
            b.isTransparentStyle = true
            return b
        }()
        clearSearch.controlSize = .small
        clearSearch.applyThemeSecondary()
        clearSearch.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        clearSearch.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        topSearchRow.addArrangedSubview(clearSearch)
        let clearFiltersButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Сбросить фильтры", target: self, action: #selector(onClearFilters))
            b.isTransparentStyle = true
            return b
        }()
        clearFiltersButton.controlSize = .small
        clearFiltersButton.applyThemeSecondary()
        clearFiltersButton.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        clearFiltersButton.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        topSearchRow.addArrangedSubview(clearFiltersButton)
        
        historyFiltersBadge.textColor = .secondaryLabelColor
        historyFiltersBadge.font = KrabEarTheme.Typography.captionMedium.tabular()
        historyFiltersBadge.isHidden = true
        topSearchRow.addArrangedSubview(historyFiltersBadge)
        
        topSearchRow.addArrangedSubview(NSView())

        helpButton.target = self
        helpButton.action = #selector(onHelp)
        topActionsRow.addArrangedSubview(helpButton)

        liveTranslatePresetButton.target = self
        liveTranslatePresetButton.action = #selector(onEnableLiveTranslationPreset)
        topActionsRow.addArrangedSubview(liveTranslatePresetButton)

        meetingPanelButton.target = self
        meetingPanelButton.action = #selector(onOpenMeetingPanel)
        topActionsRow.addArrangedSubview(meetingPanelButton)

        topActionsRow.addArrangedSubview(NSTextField(labelWithString: "Страница:"))
        historyPageSizeSelector.addItems(withTitles: ["25", "50", "100", "200"])
        historyPageSizeSelector.controlSize = .small
        historyPageSizeSelector.target = self
        historyPageSizeSelector.action = #selector(onHistoryPageSizeChanged)
        topActionsRow.addArrangedSubview(historyPageSizeSelector)
        topActionsRow.addArrangedSubview(NSTextField(labelWithString: "Плотность:"))
        historyDensitySelector.addItems(withTitles: ["Normal", "Compact"])
        historyDensitySelector.controlSize = .small
        historyDensitySelector.target = self
        historyDensitySelector.action = #selector(onHistoryDensityChanged)
        topActionsRow.addArrangedSubview(historyDensitySelector)
        historyFocusModeButton.target = self
        historyFocusModeButton.action = #selector(onToggleHistoryFocusMode)
        historyFocusModeButton.toolTip = "Сворачивает вторичные блоки и отдаёт максимум места таблице истории"
        topActionsRow.addArrangedSubview(historyFocusModeButton)
        topActionsRow.addArrangedSubview(NSView())
        topBar.addArrangedSubview(topSearchRow)
        topBar.addArrangedSubview(topActionsRow)

        filterRow1.addArrangedSubview(NSTextField(labelWithString: "Вставка:"))
        historyPasteStatusFilter.addItems(withTitles: ["Все", "ok", "failed"])
        historyPasteStatusFilter.target = self
        historyPasteStatusFilter.action = #selector(onHistoryFilterChanged)
        filterRow1.addArrangedSubview(historyPasteStatusFilter)

        filterRow1.addArrangedSubview(NSTextField(labelWithString: "Перевод:"))
        historyTranslationModeFilter.addItems(withTitles: ["Все", "off", "ru_to_es", "es_to_ru", "en_to_ru", "auto", "auto_to_ru", "bilingual_ru_es"])
        historyTranslationModeFilter.target = self
        historyTranslationModeFilter.action = #selector(onHistoryFilterChanged)
        filterRow1.addArrangedSubview(historyTranslationModeFilter)

        filterRow1.addArrangedSubview(NSTextField(labelWithString: "Статус:"))
        historyTranslationStatusFilter.addItems(
            withTitles: [
                "Все",
                "ok",
                "not_requested",
                "model_unavailable_offline",
                "model_unavailable_online",
                "model_unavailable_cached",
                "cannot_detect_language",
                "already_target_language",
                "translate_error",
            ]
        )
        historyTranslationStatusFilter.target = self
        historyTranslationStatusFilter.action = #selector(onHistoryFilterChanged)
        filterRow1.addArrangedSubview(historyTranslationStatusFilter)
        filterRow1.addArrangedSubview(NSView())

        filterRow2.addArrangedSubview(NSTextField(labelWithString: "От:"))
        historyFromDateField.placeholderString = "YYYY-MM-DD"
        historyFromDateField.target = self
        historyFromDateField.action = #selector(onHistoryFilterChanged)
        historyFromDateField.controlSize = .small
        historyFromDateField.font = KrabEarTheme.Typography.caption
        historyFromDateField.widthAnchor.constraint(equalToConstant: 96).isActive = true
        filterRow2.addArrangedSubview(historyFromDateField)

        filterRow2.addArrangedSubview(NSTextField(labelWithString: "До:"))
        historyToDateField.placeholderString = "YYYY-MM-DD"
        historyToDateField.target = self
        historyToDateField.action = #selector(onHistoryFilterChanged)
        historyToDateField.controlSize = .small
        historyToDateField.font = KrabEarTheme.Typography.caption
        historyToDateField.widthAnchor.constraint(equalToConstant: 96).isActive = true
        filterRow2.addArrangedSubview(historyToDateField)

        // Action items filter (PR feat/action-items-filter-ui, depends on #295 HistoryItem fields).
        // Применяется client-side через applyClientActionItemsFilter — backend
        // get_history_page не поддерживает has_action_items query.
        filterRow2.addArrangedSubview(NSTextField(labelWithString: "Action items:"))
        historyActionItemsFilter.addItems(
            withTitles: ["Все", "Только с action items", "Только без"]
        )
        historyActionItemsFilter.target = self
        historyActionItemsFilter.action = #selector(onActionItemsFilterChanged)
        filterRow2.addArrangedSubview(historyActionItemsFilter)
        filterRow2.addArrangedSubview(NSView())

        historyQuickPresetRow.addArrangedSubview(NSTextField(labelWithString: "Быстрые фильтры:"))
        let historyTodayButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Сегодня", target: self, action: #selector(onHistoryPresetToday))
            b.isTransparentStyle = true
            return b
        }()
        historyTodayButton.applyThemeSecondary()
        historyTodayButton.toolTip = "Показывает записи только за сегодня"
        historyQuickPresetRow.addArrangedSubview(historyTodayButton)
        let historyWeekButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "7 дней", target: self, action: #selector(onHistoryPresetLast7Days))
            b.isTransparentStyle = true
            return b
        }()
        historyWeekButton.applyThemeSecondary()
        historyWeekButton.toolTip = "Показывает записи за последние 7 дней"
        historyQuickPresetRow.addArrangedSubview(historyWeekButton)
        let historyErrorsButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Ошибки перевода", target: self, action: #selector(onHistoryPresetTranslationErrors))
            b.isTransparentStyle = true
            return b
        }()
        historyErrorsButton.applyThemeSecondary()
        historyErrorsButton.toolTip = "Фильтр по translation_status=translate_error"
        historyQuickPresetRow.addArrangedSubview(historyErrorsButton)
        let historyTranslatedButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "С переводом", target: self, action: #selector(onHistoryPresetTranslatedOnly))
            b.isTransparentStyle = true
            return b
        }()
        historyTranslatedButton.applyThemeSecondary()
        historyTranslatedButton.toolTip = "Показывает записи с успешным переводом (translation_status=ok)"
        historyQuickPresetRow.addArrangedSubview(historyTranslatedButton)
        let historyNoTranslationButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Без перевода", target: self, action: #selector(onHistoryPresetNoTranslation))
            b.isTransparentStyle = true
            return b
        }()
        historyNoTranslationButton.applyThemeSecondary()
        historyNoTranslationButton.toolTip = "Показывает записи, где translation_mode=off"
        historyQuickPresetRow.addArrangedSubview(historyNoTranslationButton)
        let historyPasteErrorsButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Ошибки вставки", target: self, action: #selector(onHistoryPresetPasteFailed))
            b.isTransparentStyle = true
            return b
        }()
        historyPasteErrorsButton.applyThemeSecondary()
        historyPasteErrorsButton.toolTip = "Фильтр по paste_status=failed"
        historyQuickPresetRow.addArrangedSubview(historyPasteErrorsButton)
        let historyResetDatesButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Сброс дат", target: self, action: #selector(onHistoryPresetResetDates))
            b.isTransparentStyle = true
            return b
        }()
        historyResetDatesButton.applyThemeSecondary()
        historyResetDatesButton.toolTip = "Очищает поля дат и перезагружает историю"
        historyQuickPresetRow.addArrangedSubview(historyResetDatesButton)
        historyQuickPresetRow.addArrangedSubview(NSView())

        let importAudioButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Импорт аудио", target: self, action: #selector(onImportAudio))
            b.isTransparentStyle = true
            return b
        }()
        importAudioButton.applyThemeSecondary()
        importRow.addArrangedSubview(importAudioButton)
        cancelImportButton.target = self
        cancelImportButton.action = #selector(onCancelImport)
        cancelImportButton.isEnabled = false
        cancelImportButton.toolTip = "Останавливает очередь после текущей активной задачи"
        importRow.addArrangedSubview(cancelImportButton)
        pauseImportButton.target = self
        pauseImportButton.action = #selector(onToggleImportPause)
        pauseImportButton.isEnabled = false
        pauseImportButton.toolTip = "Пауза/продолжение очереди импорта"
        importRow.addArrangedSubview(pauseImportButton)
        openImportReportButton.target = self
        openImportReportButton.action = #selector(onOpenImportReport)
        openImportReportButton.isEnabled = false
        importRow.addArrangedSubview(openImportReportButton)
        importRow.addArrangedSubview(importStatusLabel)
        importRow.addArrangedSubview(NSView())

        swapRuEsButton.target = self
        swapRuEsButton.action = #selector(onSwapRuEsFromPanel)
        swapRuEsButton.toolTip = "Быстро переключает направление перевода RU<->ES"
        toolsRow.addArrangedSubview(swapRuEsButton)

        let addGlossaryButton = ThemePrimaryButton(title: "Добавить термин", target: self, action: #selector(onAddGlossaryTerm))
        addGlossaryButton.applyThemeSecondary()
        toolsRow.addArrangedSubview(addGlossaryButton)

        let removeGlossaryButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Удалить термин", target: self, action: #selector(onRemoveGlossaryTerm))
            b.isTransparentStyle = true
            return b
        }()
        removeGlossaryButton.applyThemeSecondary()
        toolsRow.addArrangedSubview(removeGlossaryButton)

        let exportGlossaryButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Экспорт CSV…", target: self, action: #selector(onExportGlossary))
            b.isTransparentStyle = true
            b.toolTip = "Сохранить глоссарий в CSV-файл для редактирования в Excel / Numbers"
            return b
        }()
        exportGlossaryButton.applyThemeSecondary()
        toolsRow.addArrangedSubview(exportGlossaryButton)

        let importGlossaryButton: ThemeSecondaryButton = {
            let b = ThemeSecondaryButton(title: "Импорт CSV…", target: self, action: #selector(onImportGlossary))
            b.isTransparentStyle = true
            b.toolTip = "Загрузить глоссарий из CSV-файла (merge или replace)"
            return b
        }()
        importGlossaryButton.applyThemeSecondary()
        toolsRow.addArrangedSubview(importGlossaryButton)

        toolsRow.addArrangedSubview(glossaryStatusLabel)
        toolsRow.addArrangedSubview(NSView())

        startStopButton.target = self
        startStopButton.action = #selector(onToggleRecordFromPanel)
        controlRow.addArrangedSubview(startStopButton)

        restartButton.target = self
        restartButton.action = #selector(onRestartFromPanel)
        controlRow.addArrangedSubview(restartButton)

        stopButton.target = self
        stopButton.action = #selector(onStopFromPanel)
        controlRow.addArrangedSubview(stopButton)
        controlRow.addArrangedSubview(NSView())

        settingsBar.orientation = .vertical
        settingsBar.spacing = KrabEarTheme.Metrics.tight
        settingsBar.alignment = .leading
        settingsBar.translatesAutoresizingMaskIntoConstraints = false
        liveSettingsBar.orientation = .vertical
        liveSettingsBar.spacing = KrabEarTheme.Metrics.tight
        liveSettingsBar.alignment = .leading
        liveSettingsBar.translatesAutoresizingMaskIntoConstraints = false

        settingsRow1.orientation = .horizontal
        settingsRow1.spacing = KrabEarTheme.Metrics.standard
        settingsRow1.alignment = .centerY
        settingsRow1.translatesAutoresizingMaskIntoConstraints = false

        settingsRow2.orientation = .horizontal
        settingsRow2.spacing = KrabEarTheme.Metrics.standard
        settingsRow2.alignment = .centerY
        settingsRow2.translatesAutoresizingMaskIntoConstraints = false

        settingsRow3.orientation = .horizontal
        settingsRow3.spacing = KrabEarTheme.Metrics.standard
        settingsRow3.alignment = .centerY
        settingsRow3.translatesAutoresizingMaskIntoConstraints = false

        settingsRow4.orientation = .horizontal
        settingsRow4.spacing = KrabEarTheme.Metrics.standard
        settingsRow4.alignment = .centerY
        settingsRow4.translatesAutoresizingMaskIntoConstraints = false

        settingsRow5.orientation = .horizontal
        settingsRow5.spacing = KrabEarTheme.Metrics.standard
        settingsRow5.alignment = .centerY
        settingsRow5.translatesAutoresizingMaskIntoConstraints = false

        settingsRow6.orientation = .horizontal
        settingsRow6.spacing = KrabEarTheme.Metrics.standard
        settingsRow6.alignment = .centerY
        settingsRow6.translatesAutoresizingMaskIntoConstraints = false

        settingsRow7.orientation = .horizontal
        settingsRow7.spacing = KrabEarTheme.Metrics.standard
        settingsRow7.alignment = .centerY
        settingsRow7.translatesAutoresizingMaskIntoConstraints = false

        callAssistConfigRow.orientation = .horizontal
        callAssistConfigRow.spacing = KrabEarTheme.Metrics.standard
        callAssistConfigRow.alignment = .centerY
        callAssistConfigRow.translatesAutoresizingMaskIntoConstraints = false

        callAssistControlRow.orientation = .horizontal
        callAssistControlRow.spacing = KrabEarTheme.Metrics.standard
        callAssistControlRow.alignment = .centerY
        callAssistControlRow.translatesAutoresizingMaskIntoConstraints = false

        voiceGatewayRow.orientation = .horizontal
        voiceGatewayRow.spacing = KrabEarTheme.Metrics.standard
        voiceGatewayRow.alignment = .centerY
        voiceGatewayRow.translatesAutoresizingMaskIntoConstraints = false

        callPhrasePresetRow.orientation = .horizontal
        callPhrasePresetRow.spacing = KrabEarTheme.Metrics.standard
        callPhrasePresetRow.alignment = .centerY
        callPhrasePresetRow.translatesAutoresizingMaskIntoConstraints = false

        callPhraseActionRow.orientation = .horizontal
        callPhraseActionRow.spacing = KrabEarTheme.Metrics.standard
        callPhraseActionRow.alignment = .centerY
        callPhraseActionRow.translatesAutoresizingMaskIntoConstraints = false

        callTimelineRow.orientation = .horizontal
        callTimelineRow.spacing = KrabEarTheme.Metrics.standard
        callTimelineRow.alignment = .centerY
        callTimelineRow.translatesAutoresizingMaskIntoConstraints = false

        settingsRow1.addArrangedSubview(NSTextField(labelWithString: "Качество:"))
        qualitySelector.addItems(withTitles: ["Balanced", "Max"])
        qualitySelector.target = self
        qualitySelector.action = #selector(onQualityChanged)
        settingsRow1.addArrangedSubview(qualitySelector)

        settingsRow1.addArrangedSubview(NSTextField(labelWithString: "Очистка хвоста:"))
        cleanupSelector.addItems(withTitles: ["Soft", "Strict"])
        cleanupSelector.target = self
        cleanupSelector.action = #selector(onCleanupProfileChanged)
        settingsRow1.addArrangedSubview(cleanupSelector)

        settingsRow1.addArrangedSubview(NSTextField(labelWithString: "Режим:"))
        modeSelector.addItems(withTitles: ["Headless", "Menu Bar"])
        modeSelector.target = self
        modeSelector.action = #selector(onModeChanged)
        settingsRow1.addArrangedSubview(modeSelector)

        settingsRow2.addArrangedSubview(NSTextField(labelWithString: "Перевод:"))
        translationSelector.addItems(withTitles: ["Off", "RU -> ES", "ES -> RU", "EN -> RU", "Auto", "Bilingual RU<->ES", "Auto -> RU"])
        translationSelector.target = self
        translationSelector.action = #selector(onTranslationModeChanged)
        settingsRow2.addArrangedSubview(translationSelector)

        settingsRow2.addArrangedSubview(NSTextField(labelWithString: "Сеть:"))
        networkSelector.addItems(withTitles: ["Offline default", "Offline strict", "Online opt-in"])
        networkSelector.target = self
        networkSelector.action = #selector(onNetworkModeChanged)
        settingsRow2.addArrangedSubview(networkSelector)

        settingsRow2.addArrangedSubview(NSTextField(labelWithString: "Стиль:"))
        translationStyleSelector.addItems(withTitles: ["Neutral", "Chat", "Formal"])
        translationStyleSelector.target = self
        translationStyleSelector.action = #selector(onTranslationStyleChanged)
        settingsRow2.addArrangedSubview(translationStyleSelector)

        autoPasteButton.target = self
        autoPasteButton.action = #selector(onAutoPasteChanged)
        settingsRow3.addArrangedSubview(autoPasteButton)

        pasteUndoButton.target = self
        pasteUndoButton.action = #selector(onPasteUndoChanged)
        settingsRow3.addArrangedSubview(pasteUndoButton)

        smartFieldFormatButton.target = self
        smartFieldFormatButton.action = #selector(onSmartFieldFormatChanged)
        settingsRow3.addArrangedSubview(smartFieldFormatButton)

        streamingPasteButton.target = self
        streamingPasteButton.action = #selector(onStreamingPasteChanged)
        settingsRow3.addArrangedSubview(streamingPasteButton)

        quickEditButton.target = self
        quickEditButton.action = #selector(onQuickEditChanged)
        settingsRow3.addArrangedSubview(quickEditButton)

        // Timeout stepper (legacy variant — compact inline)
        quickEditTimeoutStepper.minValue = 1
        quickEditTimeoutStepper.maxValue = 30
        quickEditTimeoutStepper.increment = 1
        quickEditTimeoutStepper.valueWraps = false
        quickEditTimeoutStepper.integerValue = 5
        quickEditTimeoutStepper.target = self
        quickEditTimeoutStepper.action = #selector(onQuickEditTimeoutChanged(_:))
        quickEditTimeoutValueLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        quickEditTimeoutValueLabel.textColor = .secondaryLabelColor
        quickEditTimeoutValueLabel.alignment = .right
        quickEditTimeoutValueLabel.setContentHuggingPriority(.required, for: .horizontal)
        quickEditTimeoutValueLabel.stringValue = "5 сек"
        settingsRow3.addArrangedSubview(quickEditTimeoutStepper)
        settingsRow3.addArrangedSubview(quickEditTimeoutValueLabel)

        startSoundButton.target = self
        startSoundButton.action = #selector(onStartSoundChanged)
        settingsRow3.addArrangedSubview(startSoundButton)

        realtimePreviewButton.target = self
        realtimePreviewButton.action = #selector(onRealtimePreviewChanged)
        settingsRow3.addArrangedSubview(realtimePreviewButton)

        overlayFollowCursorButton.target = self
        overlayFollowCursorButton.action = #selector(onOverlayFollowCursorChanged)
        settingsRow3.addArrangedSubview(overlayFollowCursorButton)

        translateAndPasteButton.target = self
        translateAndPasteButton.action = #selector(onTranslateAndPasteChanged)
        settingsRow3.addArrangedSubview(translateAndPasteButton)

        settingsRow3.addArrangedSubview(NSView())

        settingsRow4.addArrangedSubview(NSView())

        autoStartButton.target = self
        autoStartButton.action = #selector(onAutostartChanged)
        settingsRow4.addArrangedSubview(autoStartButton)

        dockIconButton.target = self
        dockIconButton.action = #selector(onDockChanged)
        settingsRow4.addArrangedSubview(dockIconButton)
        
        settingsRow4.addArrangedSubview(NSTextField(labelWithString: "Hotkey:"))
        hotkeySelector.addItems(withTitles: ["Right Option", "Left Option", "Any Option"])
        hotkeySelector.target = self
        hotkeySelector.action = #selector(onHotkeyChanged)
        settingsRow4.addArrangedSubview(hotkeySelector)

        settingsRow4.addArrangedSubview(NSTextField(labelWithString: "Profile:"))
        hotkeyProfileSelector.addItems(withTitles: ["Default", "Meeting", "Translation"])
        hotkeyProfileSelector.target = self
        hotkeyProfileSelector.action = #selector(onHotkeyProfileChanged)
        settingsRow4.addArrangedSubview(hotkeyProfileSelector)
        
        settingsRow4.addArrangedSubview(NSView())

        settingsRow5.addArrangedSubview(NSTextField(labelWithString: "Буфер:"))
        clipboardModeSelector.addItems(withTitles: ["Always copy", "Copy on fail", "Never copy"])
        clipboardModeSelector.target = self
        clipboardModeSelector.action = #selector(onClipboardModeChanged)
        // S34: риск-предупреждение — режим управляет автозаменой системного буфера.
        clipboardModeSelector.toolTip = "Always copy: каждая диктовка заменяет буфер обмена "
            + "транскриптом (пароли и другой защищённый контент не затираются). "
            + "Copy on fail: буфер заменяется только если вставка в приложение не удалась "
            + "(пароли не затираются). Never copy: буфер обмена диктовкой не используется."
        settingsRow5.addArrangedSubview(clipboardModeSelector)
        settingsRow5.addArrangedSubview(NSView())

        audioDuckingButton.target = self
        audioDuckingButton.action = #selector(onAudioDuckingChanged)
        settingsRow6.addArrangedSubview(audioDuckingButton)

        settingsRow6.addArrangedSubview(NSTextField(labelWithString: "Громкость при записи:"))
        audioDuckingSlider.target = self
        audioDuckingSlider.action = #selector(onAudioDuckingPercentChanged)
        audioDuckingSlider.numberOfTickMarks = 6
        audioDuckingSlider.allowsTickMarkValuesOnly = false
        audioDuckingSlider.controlSize = .small
        settingsRow6.addArrangedSubview(audioDuckingSlider)
        settingsRow6.addArrangedSubview(audioDuckingValueLabel)
        settingsRow6.addArrangedSubview(NSView())

        // D.10a: AI Settings Row Setup
        // Список выверенных моделей по результатам бенчмарков Round 1-13 (2026-04-28).
        // Все verified outputs на тестовом наборе (RU/ES/EN + мат + brand recognition + dedup).
        llmModelSelector.addItems(withTitles: [
            // 🥇 Production rewriter (fastest, abliterated, Qwen 3 family — MLX native)
            "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
            // 🥈 Mid-tier challenger (13B uncensored GGUF, sustains мат)
            "mythomax-l2-lora-assemble-13b",
            // 🆕 Round 13 NEW WINNER: Qwen 2.5 14B Uncensored MLX (Apr 2026)
            // 7/7 clean, brand recognition (commit Latin sохранил), dedup Whisper повторов, ~7.7s
            "qwen2.5-14b-uncensored-mlx",
            // 🏆 Best quality для VA brain (slow ~7s, GGUF через llama.cpp)
            "qwen/qwen3.6-27b",
            // Mistral Devstral 2 24B (Dec 2025 fresh, Mistral arch)
            "mistralai/devstral-small-2-2512",
            // RU-native specialty (Yandex 8B, brand recognition авто)
            "yandexgpt-5-lite-8b-pretrain",
            // OpenHermes Mistral 7B (быстрый но удаляет filler слова)
            "openhermes-2.5-mistral-7b-mlx-393a7",
            // Microsoft Fara 7B
            "microsoft_fara-7b",
        ])

        diarizationButton.target = self
        diarizationButton.action = #selector(onDiarizationChanged)

        llmRewriteButton.target = self
        llmRewriteButton.action = #selector(onLlmRewriteChanged)

        gigaamEnabledButton.target = self
        gigaamEnabledButton.action = #selector(onGigaamEnabledChanged)

        llmModelSelector.target = self
        llmModelSelector.action = #selector(onLlmModelChanged)

        aiSettingsRow1.addArrangedSubview(diarizationButton)
        aiSettingsRow1.orientation = .horizontal
        aiSettingsRow1.alignment = .centerY

        aiSettingsRow2.addArrangedSubview(llmRewriteButton)
        aiSettingsRow2.addArrangedSubview(llmModelSelector)
        aiSettingsRow2.orientation = .horizontal
        aiSettingsRow2.spacing = KrabEarTheme.Metrics.standard
        aiSettingsRow2.alignment = .centerY

        settingsRow7.addArrangedSubview(NSTextField(labelWithString: "Прозрачность Live Preview:"))
        overlayOpacitySlider.target = self
        overlayOpacitySlider.action = #selector(onOverlayOpacityChanged)
        overlayOpacitySlider.numberOfTickMarks = 6
        overlayOpacitySlider.allowsTickMarkValuesOnly = false
        overlayOpacitySlider.controlSize = .small
        settingsRow7.addArrangedSubview(overlayOpacitySlider)
        settingsRow7.addArrangedSubview(overlayOpacityValueLabel)
        settingsRow7.addArrangedSubview(NSView())

        callAssistConfigRow.addArrangedSubview(NSTextField(labelWithString: "Источник звонка:"))
        captureSourceSelector.addItems(withTitles: ["Микрофон", "Системное аудио", "Микрофон + система"])
        captureSourceSelector.target = self
        captureSourceSelector.action = #selector(onCaptureSourceModeChanged)
        callAssistConfigRow.addArrangedSubview(captureSourceSelector)

        callNotifyButton.target = self
        callNotifyButton.action = #selector(onCallNotifyChanged)
        callAssistConfigRow.addArrangedSubview(callNotifyButton)

        callAutoSummaryButton.target = self
        callAutoSummaryButton.action = #selector(onCallAutoSummaryChanged)
        callAssistConfigRow.addArrangedSubview(callAutoSummaryButton)
        callAssistConfigRow.addArrangedSubview(NSView())

        voiceGatewayRow.addArrangedSubview(NSTextField(labelWithString: "Gateway URL:"))
        voiceGatewayURLField.placeholderString = "http://127.0.0.1:8090"
        voiceGatewayURLField.target = self
        voiceGatewayURLField.action = #selector(onVoiceGatewayURLChanged)
        voiceGatewayURLField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        voiceGatewayURLField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        voiceGatewayURLField.widthAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        voiceGatewayRow.addArrangedSubview(voiceGatewayURLField)

        voiceGatewayRow.addArrangedSubview(NSTextField(labelWithString: "API key:"))
        voiceGatewayAPIKeyField.placeholderString = "опционально"
        voiceGatewayAPIKeyField.target = self
        voiceGatewayAPIKeyField.action = #selector(onVoiceGatewayAPIKeyChanged)
        voiceGatewayAPIKeyField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        voiceGatewayAPIKeyField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        voiceGatewayAPIKeyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 140).isActive = true
        voiceGatewayRow.addArrangedSubview(voiceGatewayAPIKeyField)

        voiceGatewayCheckButton.target = self
        voiceGatewayCheckButton.action = #selector(onCheckVoiceGateway)
        voiceGatewayRow.addArrangedSubview(voiceGatewayCheckButton)
        voiceGatewayRow.addArrangedSubview(NSView())

        callAssistStartButton.target = self
        callAssistStartButton.action = #selector(onStartCallAssist)
        callAssistControlRow.addArrangedSubview(callAssistStartButton)

        callAssistStopButton.target = self
        callAssistStopButton.action = #selector(onStopCallAssist)
        callAssistStopButton.isEnabled = false
        callAssistControlRow.addArrangedSubview(callAssistStopButton)

        callAssistStatusLabel.lineBreakMode = .byTruncatingTail
        callAssistStatusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        callAssistControlRow.addArrangedSubview(callAssistStatusLabel)
        callAssistControlRow.addArrangedSubview(NSView())

        callPhrasePresetRow.addArrangedSubview(NSTextField(labelWithString: "Быстрые фразы:"))
        callPhrasePresetSelector.addItem(withTitle: "— загрузите библиотеку —")
        callPhrasePresetSelector.target = self
        callPhrasePresetSelector.action = #selector(onCallPhrasePresetSelected)
        callPhrasePresetSelector.widthAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        callPhrasePresetRow.addArrangedSubview(callPhrasePresetSelector)

        callPhraseLoadButton.target = self
        callPhraseLoadButton.action = #selector(onLoadCallPhraseLibrary)
        callPhrasePresetRow.addArrangedSubview(callPhraseLoadButton)
        callPhrasePresetRow.addArrangedSubview(NSView())

        callPhraseActionRow.addArrangedSubview(NSTextField(labelWithString: "Реплика:"))
        callPhraseInputField.placeholderString = "Введите фразу для мгновенного перевода/озвучки"
        callPhraseInputField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        callPhraseInputField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        callPhraseActionRow.addArrangedSubview(callPhraseInputField)

        callPhraseDirectionSelector.addItems(withTitles: ["RU -> ES", "ES -> RU", "Auto -> RU"])
        callPhraseDirectionSelector.selectItem(at: 0)
        callPhraseDirectionSelector.target = self
        callPhraseDirectionSelector.action = #selector(onCallPhraseDirectionChanged)
        callPhraseActionRow.addArrangedSubview(callPhraseDirectionSelector)

        callPhraseSendButton.target = self
        callPhraseSendButton.action = #selector(onSendCallPhrase)
        callPhraseActionRow.addArrangedSubview(callPhraseSendButton)

        callSummaryButton.target = self
        callSummaryButton.action = #selector(onFetchCallSummary)
        callPhraseActionRow.addArrangedSubview(callSummaryButton)

        callDiagnosticsButton.target = self
        callDiagnosticsButton.action = #selector(onFetchCallDiagnostics)
        callPhraseActionRow.addArrangedSubview(callDiagnosticsButton)

        callCostButton.target = self
        callCostButton.action = #selector(onEstimateCallCost)
        callPhraseActionRow.addArrangedSubview(callCostButton)
        callPhraseActionRow.addArrangedSubview(NSView())

        callTimelineRow.addArrangedSubview(NSTextField(labelWithString: "Timeline:"))
        callTimelineButton.target = self
        callTimelineButton.action = #selector(onFetchCallTimeline)
        callTimelineRow.addArrangedSubview(callTimelineButton)

        callTimelineExportButton.target = self
        callTimelineExportButton.action = #selector(onExportCallTimeline)
        callTimelineRow.addArrangedSubview(callTimelineExportButton)

        callTimelineToHistoryButton.target = self
        callTimelineToHistoryButton.action = #selector(onSaveCallTimelineToHistory)
        callTimelineRow.addArrangedSubview(callTimelineToHistoryButton)

        callTimelineKeepLastSelector.addItems(withTitles: ["keep 0", "keep 1", "keep 5", "keep 20"])
        callTimelineKeepLastSelector.selectItem(at: 1)
        callTimelineRow.addArrangedSubview(callTimelineKeepLastSelector)

        callTimelineClearButton.target = self
        callTimelineClearButton.action = #selector(onClearCallTimeline)
        callTimelineRow.addArrangedSubview(callTimelineClearButton)
        callTimelineRow.addArrangedSubview(NSView())

        settingsBar.addArrangedSubview(settingsRow1)
        settingsBar.addArrangedSubview(settingsRow3)
        settingsBar.addArrangedSubview(settingsRow4)
        settingsBar.addArrangedSubview(settingsRow5)
        settingsBar.addArrangedSubview(settingsRow6)
        settingsBar.addArrangedSubview(aiSectionLabel)
        settingsBar.addArrangedSubview(aiSettingsRow1)
        settingsBar.addArrangedSubview(aiSettingsRow2)
        liveSettingsBar.addArrangedSubview(settingsRow2)
        liveSettingsBar.addArrangedSubview(settingsRow7)
        liveSettingsBar.addArrangedSubview(voiceGatewayRow)
        liveSettingsBar.addArrangedSubview(callAssistConfigRow)

        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        // Liquid Glass: table scroll view должен быть прозрачным,
        // чтобы видно было frosted glass tableCard background.
        scrollView.drawsBackground = false
        scrollView.backgroundColor = .clear

        let realtimeTitle = NSTextField(labelWithString: "Realtime preview")
        realtimeTitle.translatesAutoresizingMaskIntoConstraints = false
        realtimeStatusLabel.translatesAutoresizingMaskIntoConstraints = false
        realtimeStatusLabel.lineBreakMode = .byTruncatingTail
        realtimeStatusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        importStatusLabel.lineBreakMode = .byTruncatingTail
        importStatusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        glossaryStatusLabel.lineBreakMode = .byTruncatingTail
        glossaryStatusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        historyOverviewLabel.lineBreakMode = .byTruncatingTail
        historyOverviewLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        historyStatusLabel.lineBreakMode = .byTruncatingTail
        historyStatusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        dropZoneView.translatesAutoresizingMaskIntoConstraints = false
        dropZoneView.onPathsDropped = { [weak self] paths in
            self?.enqueueImport(paths: paths, sourceTag: "drag_drop")
        }

        realtimeScroll.translatesAutoresizingMaskIntoConstraints = false
        realtimeScroll.drawsBackground = false
        realtimeScroll.hasVerticalScroller = true
        realtimeScroll.borderType = .noBorder
        realtimeScroll.wantsLayer = true
        realtimeScroll.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        realtimeScroll.layer?.borderWidth = 0.5
        realtimeScroll.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        realtimeTextView.isEditable = false
        realtimeTextView.font = KrabEarTheme.Typography.body
        realtimeTextView.string = "Во время записи здесь появляется промежуточный текст."
        realtimeTextView.backgroundColor = .clear
        realtimeTextView.drawsBackground = false
        realtimeScroll.documentView = realtimeTextView

        dictationHistoryPreviewScroll.translatesAutoresizingMaskIntoConstraints = false
        dictationHistoryPreviewScroll.drawsBackground = false
        dictationHistoryPreviewScroll.hasVerticalScroller = true
        dictationHistoryPreviewScroll.borderType = .noBorder
        dictationHistoryPreviewScroll.wantsLayer = true
        dictationHistoryPreviewScroll.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        dictationHistoryPreviewScroll.layer?.borderWidth = 0.5
        dictationHistoryPreviewScroll.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        dictationHistoryPreviewView.isEditable = false
        dictationHistoryPreviewView.font = KrabEarTheme.Typography.body
        dictationHistoryPreviewView.string = "История пока пустая. После первой транскрибации записи появятся здесь."
        dictationHistoryPreviewView.backgroundColor = .clear
        dictationHistoryPreviewView.drawsBackground = false
        dictationHistoryPreviewScroll.documentView = dictationHistoryPreviewView

        callAssistOutputScroll.translatesAutoresizingMaskIntoConstraints = false
        callAssistOutputScroll.drawsBackground = false
        callAssistOutputScroll.hasVerticalScroller = true
        callAssistOutputScroll.borderType = .noBorder
        callAssistOutputScroll.wantsLayer = true
        callAssistOutputScroll.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        callAssistOutputScroll.layer?.borderWidth = 0.5
        callAssistOutputScroll.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        callAssistOutputView.isEditable = false
        callAssistOutputView.font = KrabEarTheme.Typography.body
        callAssistOutputView.string = "Здесь появятся результаты быстрых фраз, summary и диагностики звонка."
        callAssistOutputView.backgroundColor = .clear
        callAssistOutputView.drawsBackground = false
        callAssistOutputScroll.documentView = callAssistOutputView

        let tsColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("ts"))
        tsColumn.title = "Время"
        tsColumn.width = 0
        tsColumn.minWidth = 0
        tsColumn.isHidden = true

        let statusColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("status"))
        statusColumn.title = "Вставка"
        statusColumn.width = 0
        statusColumn.minWidth = 0
        statusColumn.isHidden = true

        let textColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("text"))
        textColumn.title = "Текст"
        textColumn.width = 560
        textColumn.minWidth = 160
        textColumn.resizingMask = .autoresizingMask

        tableView.addTableColumn(tsColumn)
        tableView.addTableColumn(statusColumn)
        tableView.addTableColumn(textColumn)
        tableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        tableView.usesAlternatingRowBackgroundColors = false
        tableView.delegate = self
        tableView.dataSource = self
        tableView.rowHeight = 64
        tableView.target = self
        tableView.doubleAction = #selector(onTableViewDoubleClick)
        tableView.backgroundColor = .clear
        tableView.usesAlternatingRowBackgroundColors = false
        tableView.gridColor = KrabEarTheme.Colors.border
        // Header row прозрачный
        tableView.headerView?.wantsLayer = true
        tableView.headerView?.layer?.backgroundColor = NSColor.clear.cgColor
        tableView.selectionHighlightStyle = .regular
        tableView.style = .plain

        scrollView.documentView = tableView

        bottomBar1.orientation = .horizontal
        bottomBar1.spacing = KrabEarTheme.Metrics.standard
        bottomBar1.alignment = .centerY
        bottomBar1.translatesAutoresizingMaskIntoConstraints = false

        bottomBar2.orientation = .horizontal
        bottomBar2.spacing = KrabEarTheme.Metrics.standard
        bottomBar2.alignment = .centerY
        bottomBar2.translatesAutoresizingMaskIntoConstraints = false

        loadMoreButton.target = self
        loadMoreButton.action = #selector(onLoadMore)
        loadMoreButton.toolTip = "Загружает следующую страницу истории"
        jumpToLatestButton.target = self
        jumpToLatestButton.action = #selector(onJumpToLatest)
        jumpToLatestButton.toolTip = "Выделяет и показывает самую свежую запись истории"
        loadAllButton.target = self
        loadAllButton.action = #selector(onLoadAll)
        loadAllButton.toolTip = "Догружает всю историю по страницам"

        copyButton.target = self
        copyButton.action = #selector(onCopy)

        pasteSelectedButton.target = self
        pasteSelectedButton.action = #selector(onPasteSelected)

        copyOriginalButton.target = self
        copyOriginalButton.action = #selector(onCopyOriginal)

        copyTranslationButton.target = self
        copyTranslationButton.action = #selector(onCopyTranslation)

        retranslateButton.target = self
        retranslateButton.action = #selector(onRetranslateSelected)

        summarizeSelectedButton.target = self
        summarizeSelectedButton.action = #selector(onSummarizeSelected)

        exportButton.target = self
        exportButton.action = #selector(onExportHistory)

        exportNdjsonButton.target = self
        exportNdjsonButton.action = #selector(onExportHistoryNdjson)

        importNdjsonButton.target = self
        importNdjsonButton.action = #selector(onImportHistoryNdjson)

        deleteButton.target = self
        deleteButton.action = #selector(onDelete)

        compactButton.target = self
        compactButton.action = #selector(onCompact)
        compactButton.toolTip = "Сжимает журналы истории: удаляет служебный мусор после удалений/обновлений"

        openTranscriptsButton.target = self
        openTranscriptsButton.action = #selector(onOpenTranscripts)
        openTranscriptsButton.toolTip = "Открыть папку с сохранёнными транскриптами"

        bottomBar1.addArrangedSubview(loadMoreButton)
        bottomBar1.addArrangedSubview(jumpToLatestButton)
        bottomBar1.addArrangedSubview(loadAllButton)
        bottomBar1.addArrangedSubview(copyButton)
        bottomBar1.addArrangedSubview(pasteSelectedButton)
        bottomBar1.addArrangedSubview(copyOriginalButton)
        bottomBar1.addArrangedSubview(copyTranslationButton)
        bottomBar1.addArrangedSubview(retranslateButton)
        bottomBar1.addArrangedSubview(summarizeSelectedButton)
        bottomBar1.addArrangedSubview(NSView())

        bottomBar2.addArrangedSubview(exportButton)
        bottomBar2.addArrangedSubview(exportNdjsonButton)
        bottomBar2.addArrangedSubview(importNdjsonButton)
        bottomBar2.addArrangedSubview(deleteButton)
        bottomBar2.addArrangedSubview(compactButton)
        bottomBar2.addArrangedSubview(NSView())
        bottomBar2.addArrangedSubview(historyOverviewLabel)
        bottomBar2.addArrangedSubview(historyStatusLabel)

        func relaxHorizontalCompression(_ row: NSStackView) {
            row.setContentHuggingPriority(.defaultLow, for: .horizontal)
            row.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            for subview in row.arrangedSubviews {
                subview.setContentHuggingPriority(.defaultLow, for: .horizontal)
                subview.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            }
        }

        [
            topBar, topSearchRow, topActionsRow, filterRow1, filterRow2, importRow, toolsRow, controlRow,
            settingsRow1, settingsRow2, settingsRow3, settingsRow4, settingsRow5, settingsRow6, settingsRow7,
            voiceGatewayRow,
            callAssistConfigRow, callAssistControlRow, callPhrasePresetRow, callPhraseActionRow, callTimelineRow,
            historyQuickPresetRow, bottomBar1, bottomBar2,
        ].forEach(relaxHorizontalCompression)

        // Критично: таблица истории должна забирать остаток высоты, а не схлопываться.
        for fixedRow in [topBar, filterRow1, filterRow2, historyQuickPresetRow, importRow, bottomBar1, bottomBar2] {
            fixedRow.setContentHuggingPriority(.required, for: .vertical)
            fixedRow.setContentCompressionResistancePriority(.required, for: .vertical)
        }
        scrollView.setContentHuggingPriority(.defaultLow, for: .vertical)
        scrollView.setContentCompressionResistancePriority(.defaultLow, for: .vertical)

        setupDictationTab(dictationContentView)
        setupLiveTranslationTab(liveContentView)
        setupHistoryTab(historyContentView)
        setupKeyboardShortcuts()
        applyVisualTheme()
    }

    // MARK: - Tab Setup Helpers

    private func setupDictationTab(_ contentView: NSView) {
        let dictationTitle = NSTextField(labelWithString: "Быстрые действия диктовки")
        dictationTitle.font = KrabEarTheme.Typography.sectionTitle
        dictationHistoryHintLabel.lineBreakMode = .byTruncatingTail
        dictationHistoryHintLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        dictationHistoryHeaderRow.orientation = .horizontal
        dictationHistoryHeaderRow.spacing = KrabEarTheme.Metrics.standard
        dictationHistoryHeaderRow.alignment = .centerY
        dictationHistoryHeaderRow.translatesAutoresizingMaskIntoConstraints = false
        let dictationHistoryTitle = NSTextField(labelWithString: "Последние транскрибации")
        dictationHistoryTitle.font = KrabEarTheme.Typography.sectionTitle
        dictationHistoryHeaderRow.addArrangedSubview(dictationHistoryTitle)
        dictationHistoryHeaderRow.addArrangedSubview(NSView())
        dictationHistoryOpenButton.target = self
        dictationHistoryOpenButton.action = #selector(onOpenHistoryTabFromDictation)
        dictationHistoryHeaderRow.addArrangedSubview(dictationHistoryOpenButton)

        dictationStack.orientation = .vertical
        dictationStack.spacing = KrabEarTheme.Metrics.standard
        dictationStack.alignment = .leading
        dictationStack.translatesAutoresizingMaskIntoConstraints = false
        dictationStack.setHuggingPriority(.required, for: .vertical)
        dictationStack.setContentCompressionResistancePriority(.required, for: .vertical)
        dictationStack.addArrangedSubview(dictationTitle)
        dictationStack.addArrangedSubview(controlRow)
        dictationStack.addArrangedSubview(settingsBar)
        dictationStack.addArrangedSubview(dictationHistoryHeaderRow)
        dictationStack.addArrangedSubview(dictationHistoryHintLabel)
        dictationStack.addArrangedSubview(dictationHistoryPreviewScroll)
        dictationStack.addArrangedSubview(NSView())
        let dictationOuterScroll = NSScrollView()
        let dictationClipView = FlippedClipView()
        dictationClipView.drawsBackground = false
        dictationOuterScroll.contentView = dictationClipView
        dictationOuterScroll.documentView = dictationStack
        dictationOuterScroll.hasVerticalScroller = true
        dictationOuterScroll.hasHorizontalScroller = false
        dictationOuterScroll.drawsBackground = false
        dictationOuterScroll.automaticallyAdjustsContentInsets = false
        dictationOuterScroll.contentInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)
        dictationOuterScroll.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(dictationOuterScroll)
        NSLayoutConstraint.activate([
            dictationOuterScroll.topAnchor.constraint(equalTo: contentView.topAnchor),
            dictationOuterScroll.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            dictationOuterScroll.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            dictationOuterScroll.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            dictationStack.topAnchor.constraint(equalTo: dictationOuterScroll.contentView.topAnchor, constant: 12),
            dictationStack.leadingAnchor.constraint(equalTo: dictationOuterScroll.contentView.leadingAnchor, constant: 12),
            dictationStack.trailingAnchor.constraint(equalTo: dictationOuterScroll.contentView.trailingAnchor, constant: -12),
            // NOTE: controlRow, settingsBar, settingsBarCD width constraints are applied in
            // applyVisualTheme() AFTER each view is added to dictationStack via addArrangedSubview.
            // Activating them here risks "no common ancestor" if the hierarchy hasn't been
            // established yet (KRAB-EAR-AGENT-2). applyVisualTheme loops handle all dictationStack
            // child width constraints uniformly.
            dictationHistoryHeaderRow.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            dictationHistoryHintLabel.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            dictationHistoryPreviewScroll.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            dictationHistoryPreviewScroll.heightAnchor.constraint(equalToConstant: 150),
        ])
    }

    private func setupLiveTranslationTab(_ contentView: NSView) {
        let liveTitle = NSTextField(labelWithString: "Настройки live-перевода")
        liveTitle.font = KrabEarTheme.Typography.sectionTitle

        liveHeaderRow.orientation = .horizontal
        liveHeaderRow.spacing = KrabEarTheme.Metrics.standard
        liveHeaderRow.alignment = .centerY
        liveHeaderRow.translatesAutoresizingMaskIntoConstraints = false
        liveHeaderRow.addArrangedSubview(NSTextField(labelWithString: "Realtime preview"))
        liveHeaderRow.addArrangedSubview(NSView())
        liveHeaderRow.addArrangedSubview(realtimeStatusLabel)

        liveStack.orientation = .vertical
        liveStack.spacing = KrabEarTheme.Metrics.standard
        liveStack.alignment = .leading
        liveStack.translatesAutoresizingMaskIntoConstraints = false
        liveStack.setHuggingPriority(.required, for: .vertical)
        liveStack.setContentCompressionResistancePriority(.required, for: .vertical)
        liveStack.addArrangedSubview(liveTitle)
        liveStack.addArrangedSubview(liveSettingsBar)
        liveStack.addArrangedSubview(toolsRow)
        liveStack.addArrangedSubview(callAssistControlRow)
        liveStack.addArrangedSubview(callPhrasePresetRow)
        liveStack.addArrangedSubview(callPhraseActionRow)
        liveStack.addArrangedSubview(callTimelineRow)
        liveStack.addArrangedSubview(callAssistOutputScroll)
        liveStack.addArrangedSubview(liveHeaderRow)
        liveStack.addArrangedSubview(realtimeScroll)
        let liveOuterScroll = NSScrollView()
        let liveClipView = FlippedClipView()
        liveClipView.drawsBackground = false
        liveOuterScroll.contentView = liveClipView
        liveOuterScroll.documentView = liveStack
        liveOuterScroll.hasVerticalScroller = true
        liveOuterScroll.hasHorizontalScroller = false
        liveOuterScroll.drawsBackground = false
        liveOuterScroll.automaticallyAdjustsContentInsets = false
        liveOuterScroll.contentInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)
        liveOuterScroll.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(liveOuterScroll)
        NSLayoutConstraint.activate([
            liveOuterScroll.topAnchor.constraint(equalTo: contentView.topAnchor),
            liveOuterScroll.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            liveOuterScroll.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            liveOuterScroll.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            liveStack.topAnchor.constraint(equalTo: liveOuterScroll.contentView.topAnchor, constant: 12),
            liveStack.leadingAnchor.constraint(equalTo: liveOuterScroll.contentView.leadingAnchor, constant: 12),
            liveStack.trailingAnchor.constraint(equalTo: liveOuterScroll.contentView.trailingAnchor, constant: -12),
            // NOTE: liveSettingsBar is intentionally excluded — applyVisualTheme() removes
            // liveSettingsBar from liveStack (it becomes orphaned), so a liveSettingsBar ↔ liveStack
            // constraint would become invalid (no common ancestor) after the first applyVisualTheme
            // call. Residual stale constraints are a source of KRAB-EAR-AGENT-2-style exceptions.
            // toolsRow, callAssistControlRow etc. are constrained in applyVisualTheme via the
            // liveStack.arrangedSubviews loop (after they've been wrapped in card views).
            toolsRow.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            callAssistControlRow.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            callPhrasePresetRow.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            callPhraseActionRow.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            callTimelineRow.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            callAssistOutputScroll.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            liveHeaderRow.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            realtimeScroll.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
            callAssistOutputScroll.heightAnchor.constraint(equalToConstant: 110),
            realtimeScroll.heightAnchor.constraint(equalToConstant: 132),
        ])
    }

    private func setupHistoryTab(_ contentView: NSView) {
        historyStack.orientation = .vertical
        historyStack.spacing = KrabEarTheme.Metrics.standard
        historyStack.alignment = .leading
        historyStack.translatesAutoresizingMaskIntoConstraints = false
        historyStack.setHuggingPriority(.required, for: .vertical)
        historyStack.setContentCompressionResistancePriority(.required, for: .vertical)
        historyStack.addArrangedSubview(topBar)
        historyStack.addArrangedSubview(filterRow1)
        historyStack.addArrangedSubview(filterRow2)
        historyStack.addArrangedSubview(historyQuickPresetRow)
        historyPreviewContainer.orientation = .vertical
        historyPreviewContainer.spacing = KrabEarTheme.Metrics.tight
        historyPreviewContainer.translatesAutoresizingMaskIntoConstraints = false
        historyPreviewHeader.font = KrabEarTheme.Typography.sectionTitle
        historyPreviewHeader.textColor = .labelColor
        historyPreviewTextView.isEditable = false
        historyPreviewTextView.font = KrabEarTheme.Typography.body
        historyPreviewTextView.string = "История загружается..."
        historyPreviewTextView.backgroundColor = .clear
        historyPreviewTextView.drawsBackground = false
        historyPreviewScroll.translatesAutoresizingMaskIntoConstraints = false
        historyPreviewScroll.drawsBackground = false
        historyPreviewScroll.hasVerticalScroller = true
        historyPreviewScroll.borderType = .noBorder
        historyPreviewScroll.wantsLayer = true
        historyPreviewScroll.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        historyPreviewScroll.layer?.borderWidth = 0.5
        historyPreviewScroll.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        historyPreviewScroll.documentView = historyPreviewTextView
        historyPreviewContainer.addArrangedSubview(historyPreviewHeader)
        historyPreviewContainer.addArrangedSubview(historyPreviewScroll)
        historyStack.addArrangedSubview(historyPreviewContainer)
        historyStack.addArrangedSubview(importRow)
        historyStack.addArrangedSubview(dropZoneView)
        historyStack.addArrangedSubview(scrollView)
        historyStack.addArrangedSubview(bottomBar1)
        historyStack.addArrangedSubview(bottomBar2)
        historyFocusManagedRows = [
            filterRow1,
            filterRow2,
            historyQuickPresetRow,
            importRow,
            dropZoneView,
        ]
        // Wrap historyStack in a scroll view so the History tab scrolls on small windows
        let historyOuterScroll = NSScrollView()
        let historyClipView = FlippedClipView()
        historyClipView.drawsBackground = false
        historyOuterScroll.contentView = historyClipView
        historyOuterScroll.documentView = historyStack
        historyOuterScroll.hasVerticalScroller = true
        historyOuterScroll.hasHorizontalScroller = false
        historyOuterScroll.drawsBackground = false
        historyOuterScroll.automaticallyAdjustsContentInsets = false
        historyOuterScroll.contentInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)
        historyOuterScroll.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(historyOuterScroll)
        let historyScrollMinHeightConstraint = scrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 180)
        historyScrollMinHeightConstraint.isActive = true
        self.historyScrollMinHeightConstraint = historyScrollMinHeightConstraint
        NSLayoutConstraint.activate([
            historyOuterScroll.topAnchor.constraint(equalTo: contentView.topAnchor),
            historyOuterScroll.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            historyOuterScroll.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            historyOuterScroll.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            historyStack.topAnchor.constraint(equalTo: historyOuterScroll.contentView.topAnchor, constant: 12),
            historyStack.leadingAnchor.constraint(equalTo: historyOuterScroll.contentView.leadingAnchor, constant: 12),
            historyStack.trailingAnchor.constraint(equalTo: historyOuterScroll.contentView.trailingAnchor, constant: -12),
            dropZoneView.heightAnchor.constraint(equalToConstant: 42),
            historyPreviewScroll.heightAnchor.constraint(equalToConstant: 110),
        ])
    }

    // Keyboard shortcuts moved → HistoryPanelController+KeyboardShortcuts.swift
    // (setupKeyboardShortcuts + showKeyboardShortcutsHelp + helper text)

    @MainActor
    private func applyVisualTheme() {
        guard let window = self.window else { return }
        KrabEarTheme.applyTheme(to: window)

        // Деактивируем width constraints из прошлого вызова — view уже могут быть
        // удалены из иерархии, и активация новых поверх стейл-constraint крашит
        // NSGenericException 'no common ancestor' (KRAB-EAR-AGENT-2).
        NSLayoutConstraint.deactivate(themeWidthConstraints)
        themeWidthConstraints.removeAll()

        // Clear existing layouts
        for stack in [dictationStack, liveStack, historyStack, settingsBar, primaryActionsRow, secondaryActionsRow, statusRow] {
            for view in stack.arrangedSubviews {
                stack.removeArrangedSubview(view)
                view.removeFromSuperview()
            }
        }

        // --- DICTATION TAB ---
        let recordingSection = CollapsibleSectionView(sectionId: "dictation_recording", title: "Запись и вставка", isExpanded: true)
        let recordingCard = ThemeCardView()
        recordingCard.contentStackView.addArrangedSubview(settingsRow1)
        recordingCard.contentStackView.addArrangedSubview(settingsRow3)
        recordingCard.contentStackView.addArrangedSubview(settingsRow5)
        recordingSection.contentStackView.addArrangedSubview(recordingCard)
        self.dictationRecordingSection = recordingSection

        // --- HOTKEY SECTION (Path A: makeSettingRow helpers) ---
        let hotkeySection = buildHotkeySection()
        // Wire targets/actions (controls are declared as class properties)
        hotkeySelector.target = self
        hotkeySelector.action = #selector(onHotkeyChanged)
        hotkeyProfileSelector.target = self
        hotkeyProfileSelector.action = #selector(onHotkeyProfileChanged)

        // --- SYSTEM SECTION (Path A: makeSwitchRow / makeSettingRow helpers) ---
        let builtSystemSection = buildSystemSection()
        // Wire targets/actions for controls reparented from settingsRow4/6/7
        audioDuckingButton.target = self
        audioDuckingButton.action = #selector(onAudioDuckingChanged)
        audioDuckingSlider.target = self
        audioDuckingSlider.action = #selector(onAudioDuckingPercentChanged)
        overlayOpacitySlider.target = self
        overlayOpacitySlider.action = #selector(onOverlayOpacityChanged)
        autoStartButton.target = self
        autoStartButton.action = #selector(onAutostartChanged)
        dockIconButton.target = self
        dockIconButton.action = #selector(onDockChanged)
        self.dictationSystemSection = builtSystemSection

        // --- AI / LLM SECTION (Path A: makeSwitchRow / makeSettingRow helpers) ---
        let llmSection = buildLLMSection()
        // Wire targets/actions (diarizationButton wired in buildAudioPipelineSection call below)
        llmRewriteButton.target = self
        llmRewriteButton.action = #selector(onLlmRewriteChanged)
        llmModelSelector.target = self
        llmModelSelector.action = #selector(onLlmModelChanged)
        self.dictationAISection = llmSection

        // Hero card — Phase 2 IA refactor (Gemini 3.1 Pro design 2026-04-26):
        // visual hierarchy для частых параметров вверху таба, всегда видим.
        settingsBar.addArrangedSubview(buildDictationHeroCard())

        // Group: Базовые (recording, hotkey, system).
        settingsBar.addArrangedSubview(makeCategoryHeader(text: "Базовые"))
        settingsBar.addArrangedSubview(recordingSection)
        settingsBar.addArrangedSubview(hotkeySection)
        settingsBar.addArrangedSubview(builtSystemSection)

        // Group: Аудио (audio pipeline + profile/devices).
        settingsBar.addArrangedSubview(makeCategoryHeader(text: "Аудио"))
        let audioPipelineSection = buildAudioPipelineSection()
        settingsBar.addArrangedSubview(audioPipelineSection)
        let profAudioSection = setupDictationProfileAudioSection()
        settingsBar.addArrangedSubview(profAudioSection)

        // STT-движки (доступность + enable/disable каждого движка).
        let sttEnginesSection = buildSTTEnginesSection()
        settingsBar.addArrangedSubview(sttEnginesSection)

        // Калибровка (аппаратно-зависимая рекомендация STT-модели).
        let calibrationSection = buildCalibrationSection()
        settingsBar.addArrangedSubview(calibrationSection)

        // A1: рекомендованная настройка в один тап (dry_run превью + применить/отменить).
        let recommendedSetupSection = buildRecommendedSetupSection()
        settingsBar.addArrangedSubview(recommendedSetupSection)

        // Словарь STT (hotwords): добавление/удаление терминов + предложения из истории.
        let sttVocabSection = buildSTTVocabularySection()
        settingsBar.addArrangedSubview(sttVocabSection)

        // Пресеты конфигурации: именованные шаблоны настроек (встроенные + кастомные).
        let configPresetsSection = buildConfigPresetsSection()
        settingsBar.addArrangedSubview(configPresetsSection)

        // Профили резюмирования: встроенные + кастомные стили для summarize_item.
        let summaryProfilesSection = buildSummaryProfilesSection()
        settingsBar.addArrangedSubview(summaryProfilesSection)

        // Запланированные записи
        let schedulerSection = buildRecordingSchedulerSection()
        settingsBar.addArrangedSubview(schedulerSection)

        // Webhooks (внешние интеграции)
        let webhookManagerSection = buildWebhookManagerSection()
        settingsBar.addArrangedSubview(webhookManagerSection)

        // Голосовые команды: включить/выключить + строгий режим + справочник команд.
        let voiceCommandsSection = buildVoiceCommandsSection()
        settingsBar.addArrangedSubview(voiceCommandsSection)

        // Наблюдатель звонков агента (Call Observer w1 T9): HUD/панель + автопрослушка.
        let callObserverSettingsSection = buildCallObserverSettingsSection()
        settingsBar.addArrangedSubview(callObserverSettingsSection)

        // Текстовые сниппеты
        let textSnippetsSection = buildTextSnippetsSection()
        settingsBar.addArrangedSubview(textSnippetsSection)

        // Фонетический словарь
        let phoneticVocabSection = buildPhoneticVocabSection()
        settingsBar.addArrangedSubview(phoneticVocabSection)

        // Шаблоны вывода: управление пользовательскими шаблонами (TemplateManager).
        let outputTemplatesSection = buildOutputTemplatesSection()
        settingsBar.addArrangedSubview(outputTemplatesSection)

        // Хранение истории: авто-удаление старых записей.
        let retentionSection = buildRetentionSettingsSection()
        settingsBar.addArrangedSubview(retentionSection)

        // Безопасность: шифрование истории на диске (AES-256-GCM, Keychain).
        let securitySection = buildSecuritySettingsSection()
        settingsBar.addArrangedSubview(securitySection)

        // Приватность и данные: read-only сводка (режим приватности, шифрование,
        // хранилище, авто-очистка/purge, аудит приватности).
        let privacyDashboardSection = buildPrivacyDashboardSection()
        settingsBar.addArrangedSubview(privacyDashboardSection)

        // Group: Нейросетевые функции (LLM + VA + Selection Translator + Quick Preset).
        settingsBar.addArrangedSubview(makeCategoryHeader(text: "Нейросетевые функции"))
        settingsBar.addArrangedSubview(llmSection)
        
        let cloudRewriterSection = buildCloudRewriterSection()
        settingsBar.addArrangedSubview(cloudRewriterSection)

        // Voice Assistant — wire targets/actions.
        let vaSection = buildVoiceAssistantSection()
        vaHotkeyToggle.target = self
        vaHotkeyToggle.action = #selector(onVAHotkeyToggleChanged)
        vaWakeWordToggle.target = self
        vaWakeWordToggle.action = #selector(onVAWakeWordToggleChanged)
        vaWakeWordModelSelector.target = self
        vaWakeWordModelSelector.action = #selector(onVAWakeWordModelChanged)
        vaWakeWordThresholdSlider.target = self
        vaWakeWordThresholdSlider.action = #selector(onVAWakeWordThresholdChanged)
        vaEngineSelector.target = self
        vaEngineSelector.action = #selector(onVAEngineSelectorChanged)
        vaBrainModeSelector.target = self
        vaBrainModeSelector.action = #selector(onVABrainModeSelectorChanged)
        settingsBar.addArrangedSubview(vaSection)

        let selTransSection = buildSelectionTranslatorSection()
        settingsBar.addArrangedSubview(selTransSection)

        let quickPresetSection = buildQuickPresetSection()
        settingsBar.addArrangedSubview(quickPresetSection)

        // Быстрые заметки (C3a Task 3): opt-in Notes/Obsidian + хоткей-дропдаун.
        let quickCaptureSection = buildQuickCaptureSection()
        settingsBar.addArrangedSubview(quickCaptureSection)

        // Group: Разработчик / Отладка (diagnostics, clipboard).
        settingsBar.addArrangedSubview(makeCategoryHeader(text: "Разработчик / Отладка"))

        let (diagSection, diagCard) = setupDictationDiagnosticsSection()
        settingsBar.addArrangedSubview(diagSection)
        let diagWidthC = diagnosticsOutputScroll.widthAnchor.constraint(
            equalTo: diagCard.contentStackView.widthAnchor
        )
        diagWidthC.isActive = true
        themeWidthConstraints.append(diagWidthC)

        let clipSection = setupDictationClipboardSection()
        settingsBar.addArrangedSubview(clipSection)

        let controlCard = ThemeCardView()
        controlCard.contentStackView.addArrangedSubview(controlRow)
        dictationStack.addArrangedSubview(controlCard)

        // A/B variant: Claude Design or Gemini Design settings sections.
        // UserDefaults key "KrabEar_UseClaudeDesign" selects the active variant.
        if UserDefaults.standard.useClaudeDesignVariant {
            // Build 5 Claude Design sections into settingsBarCD, then add to stack.
            buildClaudeDesignSettingsSections()
            settingsBarCD.translatesAutoresizingMaskIntoConstraints = false
            settingsBar.isHidden = true
            dictationStack.addArrangedSubview(settingsBarCD)
            // Width constraint must be activated AFTER addArrangedSubview so that
            // settingsBarCD and dictationStack share a common ancestor. (Fixes KRAB-EAR-AGENT-2)
            let cdWidthC = settingsBarCD.widthAnchor.constraint(equalTo: dictationStack.widthAnchor)
            cdWidthC.isActive = true
            themeWidthConstraints.append(cdWidthC)
        } else {
            settingsBarCD.isHidden = true
            dictationStack.addArrangedSubview(settingsBar)
        }

        // Gemini 3.1 Pro: аналитика + здоровье
        let (analyticsSection, healthSection) = setupAnalyticsSections()
        dictationStack.addArrangedSubview(analyticsSection)
        dictationStack.addArrangedSubview(healthSection)
        dictationStack.addArrangedSubview(dictationHistoryHeaderRow)
        dictationStack.addArrangedSubview(dictationHistoryHintLabel)
        dictationStack.addArrangedSubview(dictationHistoryPreviewScroll)

        // --- LIVE TRANSLATION TAB ---
        // Extracted в HistoryPanelController+ApplyTheme+LiveTab.swift
        // (continuing PR #327 incremental split pattern).
        setupLiveTranslationTab()

        // --- HISTORY TAB ---
        // Extracted в HistoryPanelController+ApplyTheme+HistoryTab.swift
        // (continuing PR #327 / #328 incremental split pattern).
        setupHistoryTab()

        // Width constraints for settingsBar children (Dictation tab sections)
        for child in settingsBar.arrangedSubviews {
            let c = child.widthAnchor.constraint(equalTo: settingsBar.widthAnchor)
            c.isActive = true
            themeWidthConstraints.append(c)
        }

        // Width constraints for settingsBarCD children (Claude Design variant).
        // Guard: only activate when settingsBarCD is actually in the view hierarchy
        // (i.e. CD variant is active). Activating against a detached NSStackView
        // would leave stale constraints referencing views with no common ancestor
        // once settingsBarCD is later replaced — partial guard against KRAB-EAR-AGENT-2.
        if settingsBarCD.superview != nil {
            for child in settingsBarCD.arrangedSubviews {
                let c = child.widthAnchor.constraint(equalTo: settingsBarCD.widthAnchor)
                c.isActive = true
                themeWidthConstraints.append(c)
            }
        }

        for child in dictationStack.arrangedSubviews {
            let c = child.widthAnchor.constraint(equalTo: dictationStack.widthAnchor)
            c.isActive = true
            themeWidthConstraints.append(c)
        }

        // --- BUTTON STYLING ---
        // Primary action buttons
        startStopButton.applyThemePrimary()
        callAssistStartButton.applyThemePrimary()
        callPhraseSendButton.applyThemePrimary()

        // Secondary buttons — standard appearance
        for button in [restartButton, stopButton, loadMoreButton, jumpToLatestButton,
                       loadAllButton, copyButton, pasteSelectedButton, copyOriginalButton,
                       copyTranslationButton, retranslateButton, summarizeSelectedButton,
                       exportButton, exportNdjsonButton, importNdjsonButton, deleteButton,
                       compactButton, openTranscriptsButton, cancelImportButton, pauseImportButton,
                       swapRuEsButton, openImportReportButton,
                       callAssistStopButton, callPhraseLoadButton, callSummaryButton,
                       callDiagnosticsButton, callCostButton, callTimelineButton,
                       callTimelineExportButton, callTimelineToHistoryButton, callTimelineClearButton,
                       helpButton, liveTranslatePresetButton, historyFocusModeButton,
                       voiceGatewayCheckButton, dictationHistoryOpenButton,
                       diagnosticsButton, metricsButton, recordingStatsButton, storageInfoButton,
                       applyProfileButton, testMicButton, clipboardHistoryButton, repasteButton,
                       exportSrtButton, cleanupHistoryButton, vocabSuggestionsButton, glossarySuggestionsButton,
                       sendToAppleNotesButton, sendToRemindersButton, createCalendarEventButton,
                       sendToImessageButton, sendToTelegramButton] as [NSButton] {
            button.applyThemeSecondary()
        }

        // Checkbox buttons
        for button in [audioDuckingButton, diarizationButton, llmRewriteButton,
                       autoPasteButton, pasteUndoButton, smartFieldFormatButton, streamingPasteButton,
                       quickEditButton, startSoundButton, realtimePreviewButton,
                       overlayFollowCursorButton,
                       translateAndPasteButton, callNotifyButton, callAutoSummaryButton,
                       autoStartButton, dockIconButton] as [NSButton] {
            button.applyThemeCheckbox()
        }

        // Style header labels
        if let headerLabel = liveHeaderRow.arrangedSubviews.first(where: { $0 is NSTextField }) as? NSTextField {
            headerLabel.font = KrabEarTheme.Typography.sectionTitle
        }
        historyPreviewHeader.font = KrabEarTheme.Typography.sectionTitle
    }

    @objc private func onSearch() {
        currentQuery = searchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        loadInitial()
        // Обновляем «недавние поиски» (backend записал запрос в loadInitial →
        // search_history → record_search). currentQuery кладём оптимистично.
        if !currentQuery.isEmpty {
            refreshSearchSuggestions(prepending: currentQuery)
        }
    }

    @objc private func onClearSearch() {
        currentQuery = ""
        searchField.stringValue = ""
        loadInitial()
    }

    @objc private func onClearFilters() {
        historyPasteStatusFilter.selectItem(at: 0)
        historyTranslationModeFilter.selectItem(at: 0)
        historyTranslationStatusFilter.selectItem(at: 0)
        historyFromDateField.stringValue = ""
        historyToDateField.stringValue = ""
        loadInitial()
    }

    @objc private func onToggleHistoryFocusMode() {
        let nextValue = !settingsProvider().historyFocusMode
        applySettingsPatch(["history_focus_mode": nextValue])
    }

    @objc private func onHistoryFilterChanged() {
        loadInitial()
    }

    @objc private func onHistoryPresetToday() {
        let today = isoDateString(daysOffset: 0)
        historyFromDateField.stringValue = today
        historyToDateField.stringValue = today
        loadInitial()
    }

    @objc private func onHistoryPresetLast7Days() {
        historyFromDateField.stringValue = isoDateString(daysOffset: -6)
        historyToDateField.stringValue = isoDateString(daysOffset: 0)
        loadInitial()
    }

    @objc private func onHistoryPresetTranslationErrors() {
        historyTranslationStatusFilter.selectItem(at: 8) // translate_error
        loadInitial()
    }

    @objc private func onHistoryPresetTranslatedOnly() {
        historyTranslationStatusFilter.selectItem(at: 1) // ok
        loadInitial()
    }

    @objc private func onHistoryPresetNoTranslation() {
        historyTranslationModeFilter.selectItem(at: 1) // off
        historyTranslationStatusFilter.selectItem(at: 0) // Все
        loadInitial()
    }

    @objc private func onHistoryPresetPasteFailed() {
        historyPasteStatusFilter.selectItem(at: 2) // failed
        loadInitial()
    }

    @objc private func onHistoryPresetResetDates() {
        historyFromDateField.stringValue = ""
        historyToDateField.stringValue = ""
        loadInitial()
    }

    /// C2c: кнопка «Встреча» в topActionsRow — роутит в единственную точку входа
    /// панели встречи на AgentAppDelegate (та же, что и пункт меню-бара).
    @objc private func onOpenMeetingPanel() {
        (NSApp.delegate as? AgentAppDelegate)?.onMeetingPanelToggle()
    }

    @objc private func onHelp() {
        let alert = NSAlert()
        alert.messageText = "Krab Ear: кнопки панели"
        alert.informativeText = """
        Показать ещё — загружает следующую страницу истории.
        К последней — быстро выделяет самую свежую запись истории.
        Загрузить всё — догружает историю по страницам до конца.
        Оптимизировать историю — сжимает файлы истории и удаляет служебный мусор после удалений/обновлений.
        Удалить — логически удаляет выбранную запись из истории.
        Повторить перевод — создаёт новую запись с переводом выбранного исходного текста.
        Импорт аудио — выбирает файлы/папки и транскрибирует их пакетно.
        Фильтры сверху позволяют сузить историю по статусу вставки и режиму перевода.
        Экспорт NDJSON — выгрузка в формат для скриптов/других моделей.
        Импорт NDJSON — добавляет записи истории из внешнего файла (без дублей по id).
        Summary выбранного — локально строит краткую сводку выбранной записи и копирует в буфер.
        Добавить/Удалить термин — управление глоссарием перевода (локальные замены).
        Быстрые фильтры (Сегодня/7 дней/Ошибки перевода/С переводом/Без перевода) — ускоряют навигацию по истории.
        Ошибки вставки — быстрый фильтр записей, где автовставка не сработала.
        Обзор истории внизу показывает качество за сессию (сегодня/24ч, вставка и перевод).
        Live Translation — пресет для разговорного перевода в реальном времени (RU<->ES, EN->RU).
        Swap RU<->ES — быстро меняет направление перевода между русским и испанским.
        Фокус истории — скрывает вторичные блоки и увеличивает видимую область таблицы.
        Плотность истории — Compact показывает больше строк на экране.
        Вкладки: Диктовка — базовые настройки записи, Live перевод — realtime перевод и оверлей, История — поиск/экспорт.
        Последние транскрибации на вкладке "Диктовка" показывают быстрый срез истории и помогают не терять записи.
        Открыть историю (в Диктовке) — мгновенно переводит на вкладку истории; если записи скрыты фильтрами, они будут сброшены.
        Отменить импорт — завершает текущий файл и останавливает очередь.
        Зона drag-and-drop под настройками добавляет задачи в очередь импорта.
        Открыть отчёт — открывает последний отчёт очереди импорта в Finder.
        Realtime превью — во время записи показывает промежуточный текст.
        Очистка хвоста: Soft = мягко, Strict = агрессивнее убирает повтор последней фразы.
        Качество: Balanced = whisper-large-v3-turbo, Max = настраиваемая heavy-модель (по умолчанию тоже turbo).
        Перевод: RU <-> ES и EN -> RU работают локально (offline-first).
        Auto -> RU: автоматически пытается переводить в русский язык для звонкового сценария.
        Bilingual RU<->ES — формирует двуязычную строку вида "RU: ... / ES: ...".
        Источник звонка: выбирает режим захвата для Call Assist (микрофон/системный звук/оба канала).
        Gateway URL / API key: настройки подключения к Krab Voice Gateway для звонковых сценариев.
        Проверить Gateway: быстрый health-check endpoint `/health`.
        Старт звонка / Стоп звонка — управляет звонковой сессией через локальный Voice Gateway.
        Уведомлять собеседника — default режим уведомления о переводе при запуске звонка.
        Авто-summary звонка — при остановке сессии автоматически запрашивает summary и сохраняет его в историю.
        Загрузить фразы — подтягивает библиотеку коротких реплик RU/ES для мгновенной отправки.
        Сказать фразу — отправляет quick phrase в активную сессию (перевод + подготовка озвучки).
        Summary звонка — краткий summary разговора + извлечённые задачи.
        Диагностика — счётчики и объяснение "почему не перевелось".
        Оценка стоимости — расчёт telephony + AI бюджета (live Twilio или manual rates).
        Timeline — показывает последние события STT/перевода/озвучки текущего звонка.
        Экспорт Timeline — сохраняет журнал событий в Markdown или NDJSON.
        Timeline -> история — сохраняет экспорт текущего timeline как запись в истории Krab Ear.
        Очистить Timeline — удаляет старые события, оставляя только хвост keep_last.
        Перевод + вставка: если включено, в поле вставляется перевод, иначе оригинал.
        Сеть: Offline default = сеть не используется по умолчанию; Online opt-in = можно подтянуть модели из сети.
        Стиль: лёгкая пост-обработка перевода (neutral/chat/formal).
        Буфер: режим копирования в системный clipboard.
        Приглушать звук + Громкость при записи: снижает системный звук, чтобы он меньше попадал в диктовку.
        Прозрачность оверлея: регулирует заметность realtime-окна во время диктовки.
        Страница истории: определяет, сколько записей подгружать на один шаг.
        """
        alert.addButton(withTitle: "OK")
        presentAlertSheet(alert, for: self.window) { _ in }
    }

    @objc private func onToggleRecordFromPanel() {
        onToggleRecording()
    }

    @objc private func onSwapRuEsFromPanel() {
        onSwapRuEsDirection()
        syncSettingsControls()
    }

    @objc private func onOpenHistoryTabFromDictation() {
        // Если история есть, но текущие фильтры её скрывают, сразу сбрасываем фильтры.
        // Stats fetch is async — switch tab immediately, reset filters after stats arrive.
        mainTabView.selectTabViewItem(withIdentifier: PanelTab.history.rawValue)
        window?.makeFirstResponder(searchField)
        if items.isEmpty, hasActiveHistoryFiltersOrQuery() {
            fetchHistoryStatsAsync { [weak self] stats in
                guard let self = self, let stats = stats, stats.activeCount > 0 else { return }
                self.onClearFilters()
            }
        }
    }

    @objc private func onRestartFromPanel() {
        onRestartAgent()
    }

    @objc private func onStopFromPanel() {
        onStopAgent()
    }


    @objc private func onAddGlossaryTerm() {
        let sourceField = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        let targetField = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        let stack = NSStackView(views: [
            NSTextField(labelWithString: "Исходный термин:"),
            sourceField,
            NSTextField(labelWithString: "Замена:"),
            targetField,
        ])
        stack.orientation = .vertical
        stack.spacing = KrabEarTheme.Metrics.standard

        let alert = NSAlert()
        alert.messageText = "Добавить термин в глоссарий"
        alert.informativeText = "Термин будет применяться к результату перевода локально."
        alert.accessoryView = stack
        alert.addButton(withTitle: "Сохранить")
        alert.addButton(withTitle: "Отмена")
        presentAlertSheet(alert, for: self.window) { [weak self] resp in
        guard let self, resp == .alertFirstButtonReturn else { return }

        let source = sourceField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let target = targetField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !source.isEmpty, !target.isEmpty else {
            self.showInfoAlert(title: "Глоссарий", body: "Поля не должны быть пустыми.")
            return
        }

        // Async IPC чтобы NSAlert dismiss не блокировал main thread (AppHang risk).
        let ipc = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let ok = (try? ipc.call(
                method: "set_translation_glossary_item",
                params: ["source": source, "target": target]
            ))?["ok"] as? Bool == true
            DispatchQueue.main.async {
                guard let self else { return }
                if !ok {
                    self.showInfoAlert(title: "Глоссарий", body: "Не удалось сохранить термин.")
                    return
                }
                var nextPayload = self.settingsProvider().toPayload()
                var glossary = self.settingsProvider().translationGlossary
                glossary[source] = target
                nextPayload["translation_glossary"] = glossary
                _ = self.settingsUpdater(nextPayload)
                self.syncSettingsControls()
            }
        }
        }  // закрывает completion presentAlertSheet
    }

    @objc private func onRemoveGlossaryTerm() {
        let sourceField = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        sourceField.placeholderString = "Термин для удаления"
        let alert = NSAlert()
        alert.messageText = "Удалить термин из глоссария"
        alert.informativeText = "Введите исходный термин (ключ)."
        alert.accessoryView = sourceField
        alert.addButton(withTitle: "Удалить")
        alert.addButton(withTitle: "Отмена")
        presentAlertSheet(alert, for: self.window) { [weak self] resp in
        guard let self, resp == .alertFirstButtonReturn else { return }

        let source = sourceField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !source.isEmpty else {
            self.showInfoAlert(title: "Глоссарий", body: "Нужно указать термин.")
            return
        }

        // Async IPC чтобы NSAlert dismiss не блокировал main thread (AppHang risk).
        let ipc = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let ok = (try? ipc.call(
                method: "remove_translation_glossary_item",
                params: ["source": source]
            ))?["ok"] as? Bool == true
            DispatchQueue.main.async {
                guard let self else { return }
                if !ok {
                    self.showInfoAlert(title: "Глоссарий", body: "Не удалось удалить термин.")
                    return
                }
                var nextPayload = self.settingsProvider().toPayload()
                var glossary = self.settingsProvider().translationGlossary
                glossary.removeValue(forKey: source)
                nextPayload["translation_glossary"] = glossary
                _ = self.settingsUpdater(nextPayload)
                self.syncSettingsControls()
            }
        }
        }  // закрывает completion presentAlertSheet
    }

    @objc private func onExportGlossary() {
        let ipc = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            guard let resp = try? ipc.call(method: "export_glossary_csv", params: [:]),
                  let result = resp["result"] as? [String: Any],
                  let csv = result["csv"] as? String else {
                DispatchQueue.main.async {
                    self.showInfoAlert(title: "Экспорт глоссария", body: "Не удалось получить данные глоссария.")
                }
                return
            }
            DispatchQueue.main.async {
                let panel = NSSavePanel()
                panel.nameFieldStringValue = "krab-ear-glossary.csv"
                panel.allowedContentTypes = [.commaSeparatedText]
                panel.title = "Сохранить глоссарий"
                presentPanelSheet(panel, for: self.window) { resp in
                    guard resp == .OK, let url = panel.url else { return }
                    do {
                        try csv.write(to: url, atomically: true, encoding: .utf8)
                    } catch {
                        self.showInfoAlert(title: "Экспорт глоссария", body: "Ошибка записи файла: \(error.localizedDescription)")
                    }
                }
            }
        }
    }

    @objc private func onImportGlossary() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.commaSeparatedText]
        panel.title = "Загрузить глоссарий из CSV"
        panel.message = "Файл должен содержать заголовок source,target"
        presentPanelSheet(panel, for: self.window) { [weak self] resp in
            guard let self, resp == .OK, let url = panel.url else { return }
            guard let csv = try? String(contentsOf: url, encoding: .utf8) else {
                self.showInfoAlert(title: "Импорт глоссария", body: "Не удалось прочитать файл.")
                return
            }

            // Предложить режим: merge или replace
            let modeAlert = NSAlert()
            modeAlert.messageText = "Режим импорта"
            modeAlert.informativeText = "Merge — добавить/обновить термины; Replace — полностью заменить глоссарий."
            modeAlert.addButton(withTitle: "Merge")
            modeAlert.addButton(withTitle: "Replace")
            modeAlert.addButton(withTitle: "Отмена")
            // Второй sheet показываем из completion первого (панель уже закрыта).
            presentAlertSheet(modeAlert, for: self.window) { modeResp in
                guard let modeResp, modeResp != .alertThirdButtonReturn else { return }
                let mode = modeResp == .alertFirstButtonReturn ? "merge" : "replace"
                self.performGlossaryImport(csv: csv, mode: mode, onConflict: "skip")
            }
        }
    }

    /// Выполняет IPC-вызов import_glossary_csv и обрабатывает конфликты.
    /// Если обнаружены конфликты, показывает NSAlert со списком (до 10) и
    /// предлагает повторить импорт с on_conflict=overwrite.
    private func performGlossaryImport(csv: String, mode: String, onConflict: String) {
        let ipc = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = (try? ipc.call(
                method: "import_glossary_csv",
                params: ["csv": csv, "mode": mode, "on_conflict": onConflict]
            ))?["result"] as? [String: Any]
            DispatchQueue.main.async {
                if let err = result?["error"] as? String {
                    self.showInfoAlert(title: "Импорт глоссария", body: "Ошибка: \(err)")
                    return
                }
                let imported = result?["imported_count"] as? Int ?? 0
                let skipped = result?["skipped_count"] as? Int ?? 0
                let total = result?["total"] as? Int ?? 0
                let conflictCount = result?["conflict_count"] as? Int ?? 0
                let conflicts = result?["conflicts"] as? [[String: Any]] ?? []

                if conflictCount > 0 && mode == "merge" {
                    // Show conflict summary and offer to overwrite
                    let shown = Array(conflicts.prefix(10))
                    var lines = shown.map { c -> String in
                        let src = c["source"] as? String ?? "?"
                        let existing = c["existing_target"] as? String ?? "?"
                        let new = c["new_target"] as? String ?? "?"
                        return "• \(src): «\(existing)» → «\(new)»"
                    }
                    if conflictCount > 10 {
                        lines.append("…ещё \(conflictCount - 10) конфликтов")
                    }
                    let conflictAlert = NSAlert()
                    conflictAlert.messageText = "Конфликты глоссария (\(conflictCount))"
                    conflictAlert.informativeText = "Найдены термины с другим переводом:\n"
                        + lines.joined(separator: "\n")
                        + "\n\nПерезаписать существующие записи?"
                    conflictAlert.addButton(withTitle: "Перезаписать")
                    conflictAlert.addButton(withTitle: "Сохранить существующие")
                    presentAlertSheet(conflictAlert, for: self.window) { resp in
                        if resp == .alertFirstButtonReturn {
                            self.performGlossaryImport(csv: csv, mode: mode, onConflict: "overwrite")
                            return  // syncSettingsControls сделает рекурсивный вызов
                        }
                        // User chose to keep existing — show final summary
                        self.showInfoAlert(
                            title: "Импорт глоссария",
                            body: "Импортировано: \(imported), конфликтов пропущено: \(conflictCount), пропущено строк: \(skipped), итого: \(total)."
                        )
                        self.syncSettingsControls()
                    }
                } else {
                    self.showInfoAlert(
                        title: "Импорт глоссария",
                        body: "Импортировано: \(imported), пропущено: \(skipped), итого в глоссарии: \(total)."
                    )
                    self.syncSettingsControls()
                }
            }
        }
    }

    func showInfoAlert(title: String, body: String) {
        // Делегируем в showInfoSheet — безопасная версия без runModal().
        // Если window == nil (окно закрыто/свёрнуто) — тихо логируем вместо
        // вызова blocking runModal без window → AppHang (KRAB-EAR-AGENT-E).
        showInfoSheet(window: self.window, title: title, body: body)
    }

    func windowWillClose(_ notification: Notification) {
        stopPreviewPolling()
        // Закрытие окна завершает активный разговор с AI: иначе микрофон и
        // WebSocket живут без UI, а wake word остаётся на вечной паузе —
        // pause(.conversation) без парного resume (ревью-находка волны
        // wake word). stopConversation() идемпотентен (guard isSessionActive)
        // и постит .krabConversationStopped → resume.
        conversationVC?.stopConversation()
    }

    private func isoDateString(daysOffset: Int) -> String {
        let now = Date()
        let date = Calendar.current.date(byAdding: .day, value: daysOffset, to: now) ?? now
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone.current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }

    func windowDidResize(_ notification: Notification) {
        let width = window?.frame.width ?? 0
        let isVeryNarrow = width < 900
        
        topSearchRow.orientation = isVeryNarrow ? .vertical : .horizontal
        topSearchRow.alignment = isVeryNarrow ? .leading : .centerY
        
        filterRow1.orientation = isVeryNarrow ? .vertical : .horizontal
        filterRow2.orientation = isVeryNarrow ? .vertical : .horizontal
        historyQuickPresetRow.orientation = isVeryNarrow ? .vertical : .horizontal
        importRow.orientation = isVeryNarrow ? .vertical : .horizontal
        bottomBar1.orientation = isVeryNarrow ? .vertical : .horizontal
        bottomBar2.orientation = isVeryNarrow ? .vertical : .horizontal
        
        // Повышаем читаемость: скрываем лишний статус при узком окне
        historyOverviewLabel.isHidden = isVeryNarrow
        
        // Компактные кнопки для узкого окна
        if isVeryNarrow {
            loadMoreButton.title = "Ещё"
            jumpToLatestButton.title = "Свежее"
            copyButton.title = "Copy"
            deleteButton.title = "Del"
        } else {
            updateLoadMoreButtonCaption()
            jumpToLatestButton.title = "К последней"
            copyButton.title = "Копировать"
            deleteButton.title = "Удалить"
        }
    }
    
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.contains(.command) {
            switch event.charactersIgnoringModifiers {
            case "1": onHistoryPresetToday(); return true
            case "2": onHistoryPresetTranslatedOnly(); return true
            case "3": onHistoryPresetPasteFailed(); return true
            default: break
            }
        }
        return super.performKeyEquivalent(with: event)
    }
}
