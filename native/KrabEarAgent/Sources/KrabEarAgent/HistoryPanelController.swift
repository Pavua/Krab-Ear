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

/// Нативная панель истории с пагинацией, поиском, копированием и удалением.
final class HistoryPanelController: NSWindowController, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate, NSTabViewDelegate {
    private enum PanelTab: String {
        case dictation = "dictation"
        case liveTranslation = "live_translation"
        case history = "history"

        static func from(settingsValue: String) -> PanelTab {
            switch settingsValue {
            case PanelTab.dictation.rawValue:
                return .dictation
            case PanelTab.liveTranslation.rawValue:
                return .liveTranslation
            default:
                return .history
            }
        }
    }
    private struct ImportJob {
        let paths: [String]
        let sourceTag: String
        let audioCount: Int
        let folderCount: Int
        let totalBytes: Int
        let byExtension: [String: Int]
    }

    private struct ImportPreview {
        let audioCount: Int
        let folderCount: Int
        let sample: [String]
        let byExtension: [String: Int]
        let totalBytes: Int
    }

    private let ipcClient: IPCClient
    private let settingsProvider: () -> AgentSettings
    private let settingsUpdater: ([String: Any]) -> AgentSettings
    private let onToggleRecording: () -> Void
    private let onRestartAgent: () -> Void
    private let onStopAgent: () -> Void
    private let onPasteHistoryItem: (HistoryItem) -> Void
    private let onSwapRuEsDirection: () -> Void
    private let notificationService = NotificationService()

    private var items: [HistoryItem] = []
    private var nextCursor: String?
    private var currentQuery: String = ""
    private var isSyncingSettings = false
    private var importQueue: [ImportJob] = []
    private var importJobSignatures: Set<String> = []
    private var currentImportJob: ImportJob?
    private var isImportRunning = false
    private var isImportPaused = false
    private var importCancellationRequested = false
    private var importJobsPlanned = 0
    private var importJobsCompleted = 0
    private var importProcessedTotal = 0
    private var importErrorsTotal = 0
    private var importDurationTotalSec: Double = 0
    private var importSessionStartedAt: Date?
    private var lastImportReportPath: String?
    private var importSourceStats: [String: Int] = [:]
    private var importFormatStats: [String: Int] = [:]
    private var importFilesPlanned = 0
    private var importBytesPlanned = 0
    private var importElapsedTimer: Timer?
    private var currentImportJobStartedAt: Date?
    private var isSyncingTabs = false
    private var previewPollTick = 0
    private var isRecoveringHistoryFromFilters = false

    private let mainTabView = NSTabView()
    private let tableView = NSTableView()
    private let searchField = NSSearchField()
    private let historyPageSizeSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let historyDensitySelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let historyPasteStatusFilter = NSPopUpButton(frame: .zero, pullsDown: false)
    private let historyTranslationModeFilter = NSPopUpButton(frame: .zero, pullsDown: false)
    private let historyTranslationStatusFilter = NSPopUpButton(frame: .zero, pullsDown: false)
    private let historyFromDateField = NSTextField(frame: .zero)
    private let historyToDateField = NSTextField(frame: .zero)
    private let historyFocusModeButton = NSButton(title: "Фокус истории: ON", target: nil, action: nil)
    private let qualitySelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let cleanupSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let translationSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let translationStyleSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let networkSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let captureSourceSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let clipboardModeSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let audioDuckingButton = NSButton(checkboxWithTitle: "Приглушать звук", target: nil, action: nil)
    private let audioDuckingSlider = NSSlider(value: 50, minValue: 0, maxValue: 100, target: nil, action: nil)
    private let audioDuckingValueLabel = NSTextField(labelWithString: "50%")
    // D.10a: AI Settings Controls
    private let aiSectionLabel: NSTextField = {
        let label = NSTextField(labelWithString: "AI и обработка")
        label.font = .boldSystemFont(ofSize: 13)
        return label
    }()
    private let diarizationButton = NSButton(checkboxWithTitle: "Диаризация (определение говорящих)", target: nil, action: nil)
    private let llmRewriteButton = NSButton(checkboxWithTitle: "LLM постобработка текста", target: nil, action: nil)
    private let llmModelSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let overlayOpacitySlider = NSSlider(value: 45, minValue: 15, maxValue: 90, target: nil, action: nil)
    private let overlayOpacityValueLabel = NSTextField(labelWithString: "45%")
    private let modeSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let autoPasteButton = NSButton(checkboxWithTitle: "Автовставка", target: nil, action: nil)
    private let startSoundButton = NSButton(checkboxWithTitle: "Звук старта", target: nil, action: nil)
    private let realtimePreviewButton = NSButton(checkboxWithTitle: "Realtime превью", target: nil, action: nil)
    private let translateAndPasteButton = NSButton(checkboxWithTitle: "Перевод + вставка", target: nil, action: nil)
    private let callNotifyButton = NSButton(checkboxWithTitle: "Уведомлять собеседника", target: nil, action: nil)
    private let callAutoSummaryButton = NSButton(checkboxWithTitle: "Авто-summary звонка", target: nil, action: nil)
    private let voiceGatewayURLField = NSTextField(frame: .zero)
    private let voiceGatewayAPIKeyField = NSTextField(frame: .zero)
    private let voiceGatewayCheckButton = NSButton(title: "Проверить Gateway", target: nil, action: nil)
    private let autoStartButton = NSButton(checkboxWithTitle: "Автозапуск", target: nil, action: nil)
    private let dockIconButton = NSButton(checkboxWithTitle: "Иконка в Dock", target: nil, action: nil)
    private let hotkeySelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let hotkeyProfileSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let realtimeStatusLabel = NSTextField(labelWithString: "Realtime: ожидание")
    private let callAssistStatusLabel = NSTextField(labelWithString: "Call Assist: idle")
    private let realtimeTextView = NSTextView()
    private let dictationHistoryHintLabel = NSTextField(labelWithString: "История пока пустая. После первой транскрибации записи появятся здесь.")
    private let dictationHistoryOpenButton = NSButton(title: "Открыть историю", target: nil, action: nil)
    private let dictationHistoryPreviewView = NSTextView()
    private let callAssistStartButton = NSButton(title: "Старт звонка", target: nil, action: nil)
    private let callAssistStopButton = NSButton(title: "Стоп звонка", target: nil, action: nil)
    private let callPhrasePresetSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let callPhraseLoadButton = NSButton(title: "Загрузить фразы", target: nil, action: nil)
    private let callPhraseDirectionSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let callPhraseInputField = NSTextField(frame: .zero)
    private let callPhraseSendButton = NSButton(title: "Сказать фразу", target: nil, action: nil)
    private let callSummaryButton = NSButton(title: "Summary звонка", target: nil, action: nil)
    private let callDiagnosticsButton = NSButton(title: "Диагностика", target: nil, action: nil)
    private let callCostButton = NSButton(title: "Оценка стоимости", target: nil, action: nil)
    private let callTimelineButton = NSButton(title: "Timeline", target: nil, action: nil)
    private let callTimelineExportButton = NSButton(title: "Экспорт Timeline", target: nil, action: nil)
    private let callTimelineToHistoryButton = NSButton(title: "Timeline -> история", target: nil, action: nil)
    private let callTimelineClearButton = NSButton(title: "Очистить Timeline", target: nil, action: nil)
    private let callTimelineKeepLastSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let callAssistOutputView = NSTextView()
    private var callPhrasePresets: [[String: Any]] = []

    private let topBar = NSStackView()
    private let topSearchRow = NSStackView()
    private let topActionsRow = NSStackView()
    private let helpButton = NSButton(title: "Справка", target: nil, action: nil)
    private let liveTranslatePresetButton = NSButton(title: "Live Translation", target: nil, action: nil)
    private let filterRow1 = NSStackView()
    private let filterRow2 = NSStackView()
    private let historyQuickPresetRow = NSStackView()
    private let importRow = NSStackView()
    private let toolsRow = NSStackView()
    private let controlRow = NSStackView()
    private let bottomBar1 = NSStackView()
    private let bottomBar2 = NSStackView()
    private let settingsBar = NSStackView()
    private let settingsRow1 = NSStackView()
    private let settingsRow2 = NSStackView()
    private let settingsRow3 = NSStackView()
    private let settingsRow4 = NSStackView()
    private let settingsRow5 = NSStackView()
    private let settingsRow6 = NSStackView()
    private let settingsRow7 = NSStackView()
    private let aiSettingsRow1 = NSStackView()
    private let aiSettingsRow2 = NSStackView()
    private let startStopButton = NSButton(title: "Старт/Стоп", target: nil, action: nil)
    private let restartButton = NSButton(title: "Перезапуск", target: nil, action: nil)
    private let stopButton = NSButton(title: "Остановить", target: nil, action: nil)
    private let loadMoreButton = NSButton(title: "Показать ещё", target: nil, action: nil)
    private let jumpToLatestButton = NSButton(title: "К последней", target: nil, action: nil)
    private let loadAllButton = NSButton(title: "Загрузить всё", target: nil, action: nil)
    private let copyButton = NSButton(title: "Копировать", target: nil, action: nil)
    private let pasteSelectedButton = NSButton(title: "Вставить выбранное", target: nil, action: nil)
    private let copyOriginalButton = NSButton(title: "Копировать оригинал", target: nil, action: nil)
    private let copyTranslationButton = NSButton(title: "Копировать перевод", target: nil, action: nil)
    private let retranslateButton = NSButton(title: "Повторить перевод", target: nil, action: nil)
    private let summarizeSelectedButton = NSButton(title: "Summary выбранного", target: nil, action: nil)
    private let exportButton = NSButton(title: "Экспорт", target: nil, action: nil)
    private let exportNdjsonButton = NSButton(title: "Экспорт NDJSON", target: nil, action: nil)
    private let importNdjsonButton = NSButton(title: "Импорт NDJSON", target: nil, action: nil)
    private let deleteButton = NSButton(title: "Удалить", target: nil, action: nil)
    private let compactButton = NSButton(title: "Оптимизировать историю", target: nil, action: nil)
    private let openTranscriptsButton = NSButton(title: "Транскрипты", target: nil, action: nil)
    private let historyOverviewLabel = NSTextField(labelWithString: "")
    private let historyStatusLabel = NSTextField(labelWithString: "")
    private let glossaryStatusLabel = NSTextField(labelWithString: "Глоссарий: 0")
    private let importStatusLabel = NSTextField(labelWithString: "Импорт: idle")
    private let cancelImportButton = NSButton(title: "Отменить импорт", target: nil, action: nil)
    private let pauseImportButton = NSButton(title: "Пауза импорта", target: nil, action: nil)
    private let swapRuEsButton = NSButton(title: "Swap RU<->ES", target: nil, action: nil)
    private let openImportReportButton = NSButton(title: "Открыть отчёт", target: nil, action: nil)
    private let dropZoneView = ImportDropZoneView(frame: .zero)
    private var previewTimer: Timer?
    private var historyFocusManagedRows: [NSView] = []
    private var historyScrollMinHeightConstraint: NSLayoutConstraint?
    private let historyFiltersBadge = NSTextField(labelWithString: "Фильтры: 0")
    private let historyPreviewScroll = NSScrollView()
    private let historyPreviewTextView = NSTextView()
    private let historyPreviewHeader = NSTextField(labelWithString: "Последние транскрипты")
    // Promoted from local vars in setupUI() for applyVisualTheme() access
    private let liveSettingsBar = NSStackView()
    private let liveStack = NSStackView()
    private let historyStack = NSStackView()
    private let liveHeaderRow = NSStackView()
    private let voiceGatewayRow = NSStackView()
    private let callAssistConfigRow = NSStackView()
    private let callAssistControlRow = NSStackView()
    private let callPhrasePresetRow = NSStackView()
    private let callPhraseActionRow = NSStackView()
    private let callTimelineRow = NSStackView()
    private let callAssistOutputScroll = NSScrollView()
    private let realtimeScroll = NSScrollView()
    private let historyPreviewContainer = NSStackView()
    private let scrollView = NSScrollView()
    // Promoted from local vars in setupUI() for applyVisualTheme() access
    private let dictationStack = NSStackView()
    private let dictationHistoryHeaderRow = NSStackView()
    private let dictationHistoryPreviewScroll = NSScrollView()
    // MARK: - Collapsible section references
    private var dictationRecordingSection: CollapsibleSectionView?
    private var dictationSystemSection: CollapsibleSectionView?
    private var dictationAISection: CollapsibleSectionView?
    private var liveCallAssistSection: CollapsibleSectionView?
    private var historyFiltersSection: CollapsibleSectionView?
    private var historyAdvancedSection: CollapsibleSectionView?
    private var historyImportSection: CollapsibleSectionView?
    // MARK: - Tab selector
    private var tabSelector: NSSegmentedControl!
    // MARK: - Keyboard shortcut monitor
    nonisolated(unsafe) private var keyboardMonitor: Any?
    // MARK: - Reorganized History action rows
    private let primaryActionsRow = NSStackView()
    private let secondaryActionsRow = NSStackView()
    private let statusRow = NSStackView()
    // MARK: - Diagnostics & Metrics
    private var diagnosticsSection: CollapsibleSectionView?
    private let diagnosticsButton = NSButton(title: "Диагностика", target: nil, action: nil)
    private let metricsButton = NSButton(title: "Метрики", target: nil, action: nil)
    private let recordingStatsButton = NSButton(title: "Статистика", target: nil, action: nil)
    private let storageInfoButton = NSButton(title: "Хранилище", target: nil, action: nil)
    private let diagnosticsRow = NSStackView()
    private let diagnosticsOutputScroll = NSScrollView()
    private let diagnosticsOutputView = NSTextView()
    // MARK: - Profile Presets & Audio Devices
    private var profileAudioSection: CollapsibleSectionView?
    private let profilePresetSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let applyProfileButton = NSButton(title: "Применить", target: nil, action: nil)
    private let audioDeviceSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let testMicButton = NSButton(title: "Тест микрофона", target: nil, action: nil)
    private let micTestResultLabel = NSTextField(labelWithString: "")
    private let profileRow = NSStackView()
    private let audioDeviceRow = NSStackView()
    // MARK: - Clipboard History
    private var clipboardSection: CollapsibleSectionView?
    private let clipboardHistoryButton = NSButton(title: "Буфер обмена", target: nil, action: nil)
    private let repasteButton = NSButton(title: "Вставить повторно", target: nil, action: nil)
    private let clipboardRow = NSStackView()
    // MARK: - History enhancements
    private let exportSrtButton = NSButton(title: "Экспорт SRT", target: nil, action: nil)
    private let cleanupHistoryButton = NSButton(title: "Очистка старых", target: nil, action: nil)
    private let cleanupDaysSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let vocabSuggestionsButton = NSButton(title: "Словарь", target: nil, action: nil)
    private let glossarySuggestionsButton = NSButton(title: "Глоссарий авто", target: nil, action: nil)
    private let historyEnhancementsRow = NSStackView()

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
        window.title = "Krab Ear — Центр управления"
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
    }

    func showPanel() {
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        currentQuery = ""
        searchField.stringValue = ""
        
        // Сделать "История" дефолтной вкладкой при открытии
        mainTabView.selectTabViewItem(at: 2)
        
        syncSettingsControls()
        loadInitial()
        startPreviewPolling()
        refreshCallAssistState(silentOnError: false)
        onLoadCallPhraseLibrary()
        loadProfilePresets()
        loadAudioDevices()
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
        mainTabView.delegate = self
        mainTabView.translatesAutoresizingMaskIntoConstraints = false

        let tabSelector = NSSegmentedControl(labels: ["Диктовка", "Live перевод", "История"], trackingMode: .selectOne, target: self, action: #selector(onTabSelectorChanged))
        tabSelector.selectedSegment = 0
        tabSelector.translatesAutoresizingMaskIntoConstraints = false
        tabSelector.segmentStyle = .rounded
        tabSelector.controlSize = .regular
        self.tabSelector = tabSelector

        windowContentView.addSubview(tabSelector)
        windowContentView.addSubview(mainTabView)
        NSLayoutConstraint.activate([
            tabSelector.topAnchor.constraint(equalTo: windowContentView.topAnchor, constant: 8),
            tabSelector.centerXAnchor.constraint(equalTo: windowContentView.centerXAnchor),
            mainTabView.topAnchor.constraint(equalTo: tabSelector.bottomAnchor, constant: 8),
            mainTabView.leadingAnchor.constraint(equalTo: windowContentView.leadingAnchor, constant: 8),
            mainTabView.trailingAnchor.constraint(equalTo: windowContentView.trailingAnchor, constant: -8),
            mainTabView.bottomAnchor.constraint(equalTo: windowContentView.bottomAnchor, constant: -8),
        ])

        // Configure new history stack views
        for stack in [primaryActionsRow, secondaryActionsRow, statusRow] {
            stack.orientation = .horizontal
            stack.spacing = 8
            stack.alignment = .centerY
            stack.translatesAutoresizingMaskIntoConstraints = false
            stack.distribution = .fill
            // Prevent buttons from wrapping/stacking on narrow windows
            stack.setHuggingPriority(.defaultLow, for: .horizontal)
            stack.setClippingResistancePriority(.required, for: .horizontal)
        }

        let dictationContentView = NSView()
        dictationContentView.translatesAutoresizingMaskIntoConstraints = false
        let liveContentView = NSView()
        liveContentView.translatesAutoresizingMaskIntoConstraints = false
        // Note: liveSettingsBar is now a class property (promoted for applyVisualTheme)
        let historyContentView = NSView()
        historyContentView.translatesAutoresizingMaskIntoConstraints = false

        let dictationTab = NSTabViewItem(identifier: PanelTab.dictation.rawValue)
        dictationTab.label = "Диктовка"
        dictationTab.view = dictationContentView
        let liveTab = NSTabViewItem(identifier: PanelTab.liveTranslation.rawValue)
        liveTab.label = "Live перевод"
        liveTab.view = liveContentView
        let historyTab = NSTabViewItem(identifier: PanelTab.history.rawValue)
        historyTab.label = "История"
        historyTab.view = historyContentView
        mainTabView.addTabViewItem(dictationTab)
        mainTabView.addTabViewItem(liveTab)
        mainTabView.addTabViewItem(historyTab)

        topBar.orientation = .vertical
        topBar.spacing = 6
        topBar.alignment = .leading
        topBar.translatesAutoresizingMaskIntoConstraints = false

        topSearchRow.orientation = .horizontal
        topSearchRow.spacing = 8
        topSearchRow.alignment = .centerY
        topSearchRow.distribution = .fill
        topSearchRow.translatesAutoresizingMaskIntoConstraints = false

        topActionsRow.orientation = .horizontal
        topActionsRow.spacing = 8
        topActionsRow.alignment = .centerY
        topActionsRow.translatesAutoresizingMaskIntoConstraints = false

        filterRow1.orientation = .horizontal
        filterRow1.spacing = 8
        filterRow1.alignment = .centerY
        filterRow1.translatesAutoresizingMaskIntoConstraints = false

        filterRow2.orientation = .horizontal
        filterRow2.spacing = 8
        filterRow2.alignment = .centerY
        filterRow2.translatesAutoresizingMaskIntoConstraints = false

        historyQuickPresetRow.orientation = .horizontal
        historyQuickPresetRow.spacing = 8
        historyQuickPresetRow.alignment = .centerY
        historyQuickPresetRow.translatesAutoresizingMaskIntoConstraints = false

        importRow.orientation = .horizontal
        importRow.spacing = 8
        importRow.alignment = .centerY
        importRow.translatesAutoresizingMaskIntoConstraints = false

        toolsRow.orientation = .horizontal
        toolsRow.spacing = 8
        toolsRow.alignment = .centerY
        toolsRow.translatesAutoresizingMaskIntoConstraints = false

        controlRow.orientation = .horizontal
        controlRow.spacing = 8
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
        topSearchRow.addArrangedSubview(searchField)

        let clearSearch = NSButton(title: "Сбросить", target: self, action: #selector(onClearSearch))
        clearSearch.controlSize = .small
        clearSearch.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        clearSearch.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        topSearchRow.addArrangedSubview(clearSearch)
        let clearFiltersButton = NSButton(title: "Сбросить фильтры", target: self, action: #selector(onClearFilters))
        clearFiltersButton.controlSize = .small
        clearFiltersButton.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        clearFiltersButton.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        topSearchRow.addArrangedSubview(clearFiltersButton)
        
        historyFiltersBadge.textColor = .secondaryLabelColor
        historyFiltersBadge.font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        historyFiltersBadge.isHidden = true
        topSearchRow.addArrangedSubview(historyFiltersBadge)
        
        topSearchRow.addArrangedSubview(NSView())

        helpButton.target = self
        helpButton.action = #selector(onHelp)
        topActionsRow.addArrangedSubview(helpButton)

        liveTranslatePresetButton.target = self
        liveTranslatePresetButton.action = #selector(onEnableLiveTranslationPreset)
        topActionsRow.addArrangedSubview(liveTranslatePresetButton)
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
        historyFromDateField.font = .systemFont(ofSize: 11)
        historyFromDateField.widthAnchor.constraint(equalToConstant: 96).isActive = true
        filterRow2.addArrangedSubview(historyFromDateField)

        filterRow2.addArrangedSubview(NSTextField(labelWithString: "До:"))
        historyToDateField.placeholderString = "YYYY-MM-DD"
        historyToDateField.target = self
        historyToDateField.action = #selector(onHistoryFilterChanged)
        historyToDateField.controlSize = .small
        historyToDateField.font = .systemFont(ofSize: 11)
        historyToDateField.widthAnchor.constraint(equalToConstant: 96).isActive = true
        filterRow2.addArrangedSubview(historyToDateField)
        filterRow2.addArrangedSubview(NSView())

        historyQuickPresetRow.addArrangedSubview(NSTextField(labelWithString: "Быстрые фильтры:"))
        let historyTodayButton = NSButton(title: "Сегодня", target: self, action: #selector(onHistoryPresetToday))
        historyTodayButton.toolTip = "Показывает записи только за сегодня"
        historyQuickPresetRow.addArrangedSubview(historyTodayButton)
        let historyWeekButton = NSButton(title: "7 дней", target: self, action: #selector(onHistoryPresetLast7Days))
        historyWeekButton.toolTip = "Показывает записи за последние 7 дней"
        historyQuickPresetRow.addArrangedSubview(historyWeekButton)
        let historyErrorsButton = NSButton(title: "Ошибки перевода", target: self, action: #selector(onHistoryPresetTranslationErrors))
        historyErrorsButton.toolTip = "Фильтр по translation_status=translate_error"
        historyQuickPresetRow.addArrangedSubview(historyErrorsButton)
        let historyTranslatedButton = NSButton(title: "С переводом", target: self, action: #selector(onHistoryPresetTranslatedOnly))
        historyTranslatedButton.toolTip = "Показывает записи с успешным переводом (translation_status=ok)"
        historyQuickPresetRow.addArrangedSubview(historyTranslatedButton)
        let historyNoTranslationButton = NSButton(title: "Без перевода", target: self, action: #selector(onHistoryPresetNoTranslation))
        historyNoTranslationButton.toolTip = "Показывает записи, где translation_mode=off"
        historyQuickPresetRow.addArrangedSubview(historyNoTranslationButton)
        let historyPasteErrorsButton = NSButton(title: "Ошибки вставки", target: self, action: #selector(onHistoryPresetPasteFailed))
        historyPasteErrorsButton.toolTip = "Фильтр по paste_status=failed"
        historyQuickPresetRow.addArrangedSubview(historyPasteErrorsButton)
        let historyResetDatesButton = NSButton(title: "Сброс дат", target: self, action: #selector(onHistoryPresetResetDates))
        historyResetDatesButton.toolTip = "Очищает поля дат и перезагружает историю"
        historyQuickPresetRow.addArrangedSubview(historyResetDatesButton)
        historyQuickPresetRow.addArrangedSubview(NSView())

        let importAudioButton = NSButton(title: "Импорт аудио", target: self, action: #selector(onImportAudio))
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

        let addGlossaryButton = NSButton(title: "Добавить термин", target: self, action: #selector(onAddGlossaryTerm))
        toolsRow.addArrangedSubview(addGlossaryButton)

        let removeGlossaryButton = NSButton(title: "Удалить термин", target: self, action: #selector(onRemoveGlossaryTerm))
        toolsRow.addArrangedSubview(removeGlossaryButton)

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
        settingsBar.spacing = 6
        settingsBar.alignment = .leading
        settingsBar.translatesAutoresizingMaskIntoConstraints = false
        liveSettingsBar.orientation = .vertical
        liveSettingsBar.spacing = 6
        liveSettingsBar.alignment = .leading
        liveSettingsBar.translatesAutoresizingMaskIntoConstraints = false

        settingsRow1.orientation = .horizontal
        settingsRow1.spacing = 10
        settingsRow1.alignment = .centerY
        settingsRow1.translatesAutoresizingMaskIntoConstraints = false

        settingsRow2.orientation = .horizontal
        settingsRow2.spacing = 10
        settingsRow2.alignment = .centerY
        settingsRow2.translatesAutoresizingMaskIntoConstraints = false

        settingsRow3.orientation = .horizontal
        settingsRow3.spacing = 10
        settingsRow3.alignment = .centerY
        settingsRow3.translatesAutoresizingMaskIntoConstraints = false

        settingsRow4.orientation = .horizontal
        settingsRow4.spacing = 10
        settingsRow4.alignment = .centerY
        settingsRow4.translatesAutoresizingMaskIntoConstraints = false

        settingsRow5.orientation = .horizontal
        settingsRow5.spacing = 10
        settingsRow5.alignment = .centerY
        settingsRow5.translatesAutoresizingMaskIntoConstraints = false

        settingsRow6.orientation = .horizontal
        settingsRow6.spacing = 10
        settingsRow6.alignment = .centerY
        settingsRow6.translatesAutoresizingMaskIntoConstraints = false

        settingsRow7.orientation = .horizontal
        settingsRow7.spacing = 10
        settingsRow7.alignment = .centerY
        settingsRow7.translatesAutoresizingMaskIntoConstraints = false

        callAssistConfigRow.orientation = .horizontal
        callAssistConfigRow.spacing = 10
        callAssistConfigRow.alignment = .centerY
        callAssistConfigRow.translatesAutoresizingMaskIntoConstraints = false

        callAssistControlRow.orientation = .horizontal
        callAssistControlRow.spacing = 8
        callAssistControlRow.alignment = .centerY
        callAssistControlRow.translatesAutoresizingMaskIntoConstraints = false

        voiceGatewayRow.orientation = .horizontal
        voiceGatewayRow.spacing = 8
        voiceGatewayRow.alignment = .centerY
        voiceGatewayRow.translatesAutoresizingMaskIntoConstraints = false

        callPhrasePresetRow.orientation = .horizontal
        callPhrasePresetRow.spacing = 8
        callPhrasePresetRow.alignment = .centerY
        callPhrasePresetRow.translatesAutoresizingMaskIntoConstraints = false

        callPhraseActionRow.orientation = .horizontal
        callPhraseActionRow.spacing = 8
        callPhraseActionRow.alignment = .centerY
        callPhraseActionRow.translatesAutoresizingMaskIntoConstraints = false

        callTimelineRow.orientation = .horizontal
        callTimelineRow.spacing = 8
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

        startSoundButton.target = self
        startSoundButton.action = #selector(onStartSoundChanged)
        settingsRow3.addArrangedSubview(startSoundButton)

        realtimePreviewButton.target = self
        realtimePreviewButton.action = #selector(onRealtimePreviewChanged)
        settingsRow3.addArrangedSubview(realtimePreviewButton)

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
        llmModelSelector.addItems(withTitles: [
            "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
            "qwen3.5-4b-mlx",
            "qwen3.5-9b@6bit",
            "qwen2.5-coder-7b-instruct-mlx",
        ])

        diarizationButton.target = self
        diarizationButton.action = #selector(onDiarizationChanged)

        llmRewriteButton.target = self
        llmRewriteButton.action = #selector(onLlmRewriteChanged)

        llmModelSelector.target = self
        llmModelSelector.action = #selector(onLlmModelChanged)

        aiSettingsRow1.addArrangedSubview(diarizationButton)
        aiSettingsRow1.orientation = .horizontal
        aiSettingsRow1.alignment = .centerY

        aiSettingsRow2.addArrangedSubview(llmRewriteButton)
        aiSettingsRow2.addArrangedSubview(llmModelSelector)
        aiSettingsRow2.orientation = .horizontal
        aiSettingsRow2.spacing = 10
        aiSettingsRow2.alignment = .centerY

        settingsRow7.addArrangedSubview(NSTextField(labelWithString: "Прозрачность оверлея:"))
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
        realtimeScroll.hasVerticalScroller = true
        realtimeScroll.borderType = .noBorder
        realtimeScroll.wantsLayer = true
        realtimeScroll.layer?.cornerRadius = 8
        realtimeScroll.layer?.borderWidth = 0.5
        realtimeScroll.layer?.borderColor = NSColor.separatorColor.cgColor
        realtimeTextView.isEditable = false
        realtimeTextView.font = .systemFont(ofSize: 13)
        realtimeTextView.string = "Во время записи здесь появляется промежуточный текст."
        realtimeScroll.documentView = realtimeTextView

        dictationHistoryPreviewScroll.translatesAutoresizingMaskIntoConstraints = false
        dictationHistoryPreviewScroll.hasVerticalScroller = true
        dictationHistoryPreviewScroll.borderType = .noBorder
        dictationHistoryPreviewScroll.wantsLayer = true
        dictationHistoryPreviewScroll.layer?.cornerRadius = 8
        dictationHistoryPreviewScroll.layer?.borderWidth = 0.5
        dictationHistoryPreviewScroll.layer?.borderColor = NSColor.separatorColor.cgColor
        dictationHistoryPreviewView.isEditable = false
        dictationHistoryPreviewView.font = .systemFont(ofSize: 12)
        dictationHistoryPreviewView.string = "История пока пустая. После первой транскрибации записи появятся здесь."
        dictationHistoryPreviewScroll.documentView = dictationHistoryPreviewView

        callAssistOutputScroll.translatesAutoresizingMaskIntoConstraints = false
        callAssistOutputScroll.hasVerticalScroller = true
        callAssistOutputScroll.borderType = .noBorder
        callAssistOutputScroll.wantsLayer = true
        callAssistOutputScroll.layer?.cornerRadius = 8
        callAssistOutputScroll.layer?.borderWidth = 0.5
        callAssistOutputScroll.layer?.borderColor = NSColor.separatorColor.cgColor
        callAssistOutputView.isEditable = false
        callAssistOutputView.font = .systemFont(ofSize: 12)
        callAssistOutputView.string = "Здесь появятся результаты быстрых фраз, summary и диагностики звонка."
        callAssistOutputScroll.documentView = callAssistOutputView

        let tsColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("ts"))
        tsColumn.title = "Время"
        tsColumn.width = 132
        tsColumn.minWidth = 96

        let statusColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("status"))
        statusColumn.title = "Вставка"
        statusColumn.width = 82
        statusColumn.minWidth = 64

        let textColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("text"))
        textColumn.title = "Текст"
        textColumn.width = 560
        textColumn.minWidth = 160
        textColumn.resizingMask = .autoresizingMask

        tableView.addTableColumn(tsColumn)
        tableView.addTableColumn(statusColumn)
        tableView.addTableColumn(textColumn)
        tableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.delegate = self
        tableView.dataSource = self
        tableView.rowHeight = 28
        tableView.target = self
        tableView.doubleAction = #selector(onTableViewDoubleClick)

        scrollView.documentView = tableView

        bottomBar1.orientation = .horizontal
        bottomBar1.spacing = 8
        bottomBar1.alignment = .centerY
        bottomBar1.translatesAutoresizingMaskIntoConstraints = false

        bottomBar2.orientation = .horizontal
        bottomBar2.spacing = 8
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

        let dictationTitle = NSTextField(labelWithString: "Быстрые действия диктовки")
        dictationTitle.font = .systemFont(ofSize: 14, weight: .semibold)
        dictationHistoryHintLabel.lineBreakMode = .byTruncatingTail
        dictationHistoryHintLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        dictationHistoryHeaderRow.orientation = .horizontal
        dictationHistoryHeaderRow.spacing = 8
        dictationHistoryHeaderRow.alignment = .centerY
        dictationHistoryHeaderRow.translatesAutoresizingMaskIntoConstraints = false
        let dictationHistoryTitle = NSTextField(labelWithString: "Последние транскрибации")
        dictationHistoryTitle.font = .systemFont(ofSize: 13, weight: .semibold)
        dictationHistoryHeaderRow.addArrangedSubview(dictationHistoryTitle)
        dictationHistoryHeaderRow.addArrangedSubview(NSView())
        dictationHistoryOpenButton.target = self
        dictationHistoryOpenButton.action = #selector(onOpenHistoryTabFromDictation)
        dictationHistoryHeaderRow.addArrangedSubview(dictationHistoryOpenButton)

        let liveTitle = NSTextField(labelWithString: "Настройки live-перевода")
        liveTitle.font = .systemFont(ofSize: 14, weight: .semibold)

        liveHeaderRow.orientation = .horizontal
        liveHeaderRow.spacing = 8
        liveHeaderRow.alignment = .centerY
        liveHeaderRow.translatesAutoresizingMaskIntoConstraints = false
        liveHeaderRow.addArrangedSubview(NSTextField(labelWithString: "Realtime preview"))
        liveHeaderRow.addArrangedSubview(NSView())
        liveHeaderRow.addArrangedSubview(realtimeStatusLabel)

        dictationStack.orientation = .vertical
        dictationStack.spacing = 10
        dictationStack.alignment = .leading
        dictationStack.translatesAutoresizingMaskIntoConstraints = false
        dictationStack.addArrangedSubview(dictationTitle)
        dictationStack.addArrangedSubview(controlRow)
        dictationStack.addArrangedSubview(settingsBar)
        dictationStack.addArrangedSubview(dictationHistoryHeaderRow)
        dictationStack.addArrangedSubview(dictationHistoryHintLabel)
        dictationStack.addArrangedSubview(dictationHistoryPreviewScroll)
        dictationStack.addArrangedSubview(NSView())
        let dictationOuterScroll = NSScrollView()
        dictationOuterScroll.documentView = dictationStack
        dictationOuterScroll.hasVerticalScroller = true
        dictationOuterScroll.hasHorizontalScroller = false
        dictationOuterScroll.drawsBackground = false
        dictationOuterScroll.translatesAutoresizingMaskIntoConstraints = false
        dictationContentView.addSubview(dictationOuterScroll)
        NSLayoutConstraint.activate([
            dictationOuterScroll.topAnchor.constraint(equalTo: dictationContentView.topAnchor),
            dictationOuterScroll.leadingAnchor.constraint(equalTo: dictationContentView.leadingAnchor),
            dictationOuterScroll.trailingAnchor.constraint(equalTo: dictationContentView.trailingAnchor),
            dictationOuterScroll.bottomAnchor.constraint(equalTo: dictationContentView.bottomAnchor),
            dictationStack.topAnchor.constraint(equalTo: dictationOuterScroll.contentView.topAnchor, constant: 12),
            dictationStack.leadingAnchor.constraint(equalTo: dictationOuterScroll.contentView.leadingAnchor, constant: 12),
            dictationStack.trailingAnchor.constraint(equalTo: dictationOuterScroll.contentView.trailingAnchor, constant: -12),
            controlRow.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            settingsBar.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            dictationHistoryHeaderRow.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            dictationHistoryHintLabel.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            dictationHistoryPreviewScroll.widthAnchor.constraint(equalTo: dictationStack.widthAnchor),
            dictationHistoryPreviewScroll.heightAnchor.constraint(equalToConstant: 150),
        ])

        liveStack.orientation = .vertical
        liveStack.spacing = 10
        liveStack.alignment = .leading
        liveStack.translatesAutoresizingMaskIntoConstraints = false
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
        liveStack.addArrangedSubview(NSView())
        let liveOuterScroll = NSScrollView()
        liveOuterScroll.documentView = liveStack
        liveOuterScroll.hasVerticalScroller = true
        liveOuterScroll.hasHorizontalScroller = false
        liveOuterScroll.drawsBackground = false
        liveOuterScroll.translatesAutoresizingMaskIntoConstraints = false
        liveContentView.addSubview(liveOuterScroll)
        NSLayoutConstraint.activate([
            liveOuterScroll.topAnchor.constraint(equalTo: liveContentView.topAnchor),
            liveOuterScroll.leadingAnchor.constraint(equalTo: liveContentView.leadingAnchor),
            liveOuterScroll.trailingAnchor.constraint(equalTo: liveContentView.trailingAnchor),
            liveOuterScroll.bottomAnchor.constraint(equalTo: liveContentView.bottomAnchor),
            liveStack.topAnchor.constraint(equalTo: liveOuterScroll.contentView.topAnchor, constant: 12),
            liveStack.leadingAnchor.constraint(equalTo: liveOuterScroll.contentView.leadingAnchor, constant: 12),
            liveStack.trailingAnchor.constraint(equalTo: liveOuterScroll.contentView.trailingAnchor, constant: -12),
            liveSettingsBar.widthAnchor.constraint(equalTo: liveStack.widthAnchor),
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

        historyStack.orientation = .vertical
        historyStack.spacing = 8
        historyStack.alignment = .leading
        historyStack.translatesAutoresizingMaskIntoConstraints = false
        historyStack.addArrangedSubview(topBar)
        historyStack.addArrangedSubview(filterRow1)
        historyStack.addArrangedSubview(filterRow2)
        historyStack.addArrangedSubview(historyQuickPresetRow)
        historyPreviewContainer.orientation = .vertical
        historyPreviewContainer.spacing = 6
        historyPreviewContainer.translatesAutoresizingMaskIntoConstraints = false
        historyPreviewHeader.font = .systemFont(ofSize: 13, weight: .semibold)
        historyPreviewHeader.textColor = .labelColor
        historyPreviewTextView.isEditable = false
        historyPreviewTextView.font = .systemFont(ofSize: 12)
        historyPreviewTextView.string = "История загружается..."
        historyPreviewScroll.translatesAutoresizingMaskIntoConstraints = false
        historyPreviewScroll.hasVerticalScroller = true
        historyPreviewScroll.borderType = .noBorder
        historyPreviewScroll.wantsLayer = true
        historyPreviewScroll.layer?.cornerRadius = 8
        historyPreviewScroll.layer?.borderWidth = 0.5
        historyPreviewScroll.layer?.borderColor = NSColor.separatorColor.cgColor
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
        historyOuterScroll.documentView = historyStack
        historyOuterScroll.hasVerticalScroller = true
        historyOuterScroll.hasHorizontalScroller = false
        historyOuterScroll.drawsBackground = false
        historyOuterScroll.translatesAutoresizingMaskIntoConstraints = false
        historyContentView.addSubview(historyOuterScroll)
        let historyScrollMinHeightConstraint = scrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 180)
        historyScrollMinHeightConstraint.isActive = true
        self.historyScrollMinHeightConstraint = historyScrollMinHeightConstraint
        NSLayoutConstraint.activate([
            historyOuterScroll.topAnchor.constraint(equalTo: historyContentView.topAnchor),
            historyOuterScroll.leadingAnchor.constraint(equalTo: historyContentView.leadingAnchor),
            historyOuterScroll.trailingAnchor.constraint(equalTo: historyContentView.trailingAnchor),
            historyOuterScroll.bottomAnchor.constraint(equalTo: historyContentView.bottomAnchor),
            historyStack.topAnchor.constraint(equalTo: historyOuterScroll.contentView.topAnchor, constant: 12),
            historyStack.leadingAnchor.constraint(equalTo: historyOuterScroll.contentView.leadingAnchor, constant: 12),
            historyStack.trailingAnchor.constraint(equalTo: historyOuterScroll.contentView.trailingAnchor, constant: -12),
            dropZoneView.heightAnchor.constraint(equalToConstant: 42),
            historyPreviewScroll.heightAnchor.constraint(equalToConstant: 110),
        ])

        setupKeyboardShortcuts()
        applyVisualTheme()
    }

    // MARK: - Keyboard Shortcuts (Cmd+1/2/3, Cmd+F, Cmd+R, Cmd+D, Cmd+E, Cmd+I, Esc)

    private func setupKeyboardShortcuts() {
        keyboardMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, self.window?.isKeyWindow == true else { return event }

            if event.modifierFlags.contains(.command) {
                switch event.charactersIgnoringModifiers {
                case "1":
                    self.tabSelector.selectedSegment = 0
                    self.mainTabView.selectTabViewItem(at: 0)
                    return nil
                case "2":
                    self.tabSelector.selectedSegment = 1
                    self.mainTabView.selectTabViewItem(at: 1)
                    return nil
                case "3":
                    self.tabSelector.selectedSegment = 2
                    self.mainTabView.selectTabViewItem(at: 2)
                    return nil
                case "f":
                    self.tabSelector.selectedSegment = 2
                    self.mainTabView.selectTabViewItem(at: 2)
                    self.window?.makeFirstResponder(self.searchField)
                    return nil
                case "r":
                    self.loadInitial()
                    return nil
                case "d":
                    self.onDiagnostics()
                    return nil
                case "e":
                    self.onExportSrt()
                    return nil
                case "i":
                    self.onStorageInfo()
                    return nil
                default:
                    break
                }
            }

            // Escape — закрыть панель
            if event.keyCode == 53 {
                self.window?.orderOut(nil)
                return nil
            }

            return event
        }
    }

    @MainActor
    private func applyVisualTheme() {
        guard let window = self.window else { return }
        KrabEarTheme.applyTheme(to: window)

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

        let systemSection = CollapsibleSectionView(sectionId: "dictation_system", title: "Горячие клавиши и система", isExpanded: false)
        let systemCard = ThemeCardView()
        systemCard.contentStackView.addArrangedSubview(settingsRow4)
        systemCard.contentStackView.addArrangedSubview(settingsRow6)
        systemCard.contentStackView.addArrangedSubview(settingsRow7) // Moved from Live tab
        systemSection.contentStackView.addArrangedSubview(systemCard)
        self.dictationSystemSection = systemSection

        let aiSection = CollapsibleSectionView(sectionId: "dictation_ai", title: "AI и обработка", isExpanded: false)
        let aiCard = ThemeCardView()
        aiCard.contentStackView.addArrangedSubview(aiSettingsRow1)
        aiCard.contentStackView.addArrangedSubview(aiSettingsRow2)
        aiSection.contentStackView.addArrangedSubview(aiCard)
        self.dictationAISection = aiSection

        settingsBar.addArrangedSubview(recordingSection)
        settingsBar.addArrangedSubview(systemSection)
        settingsBar.addArrangedSubview(aiSection)

        // --- DIAGNOSTICS & METRICS SECTION ---
        let diagSection = CollapsibleSectionView(sectionId: "dictation_diagnostics", title: "Диагностика и метрики", isExpanded: false)
        let diagCard = ThemeCardView()
        diagnosticsRow.orientation = .horizontal
        diagnosticsRow.spacing = 8
        diagnosticsRow.alignment = .centerY
        diagnosticsRow.translatesAutoresizingMaskIntoConstraints = false
        diagnosticsButton.target = self
        diagnosticsButton.action = #selector(onDiagnostics)
        metricsButton.target = self
        metricsButton.action = #selector(onMetrics)
        recordingStatsButton.target = self
        recordingStatsButton.action = #selector(onRecordingStats)
        storageInfoButton.target = self
        storageInfoButton.action = #selector(onStorageInfo)
        diagnosticsRow.addArrangedSubview(diagnosticsButton)
        diagnosticsRow.addArrangedSubview(metricsButton)
        diagnosticsRow.addArrangedSubview(recordingStatsButton)
        diagnosticsRow.addArrangedSubview(storageInfoButton)
        diagCard.contentStackView.addArrangedSubview(diagnosticsRow)

        diagnosticsOutputView.isEditable = false
        diagnosticsOutputView.isSelectable = true
        diagnosticsOutputView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        diagnosticsOutputView.textColor = KrabEarTheme.Colors.textSecondary
        diagnosticsOutputView.backgroundColor = .clear
        diagnosticsOutputScroll.documentView = diagnosticsOutputView
        diagnosticsOutputScroll.hasVerticalScroller = true
        diagnosticsOutputScroll.translatesAutoresizingMaskIntoConstraints = false
        diagnosticsOutputScroll.heightAnchor.constraint(equalToConstant: 120).isActive = true
        diagCard.contentStackView.addArrangedSubview(diagnosticsOutputScroll)

        diagSection.contentStackView.addArrangedSubview(diagCard)
        self.diagnosticsSection = diagSection
        settingsBar.addArrangedSubview(diagSection)

        // --- PROFILE PRESETS & AUDIO DEVICES SECTION ---
        let profAudioSection = CollapsibleSectionView(sectionId: "dictation_profile_audio", title: "Профили и устройства", isExpanded: false)
        let profAudioCard = ThemeCardView()
        profileRow.orientation = .horizontal
        profileRow.spacing = 8
        profileRow.alignment = .centerY
        profileRow.translatesAutoresizingMaskIntoConstraints = false
        let profileLabel = NSTextField(labelWithString: "Профиль:")
        profileLabel.font = KrabEarTheme.Typography.controlLabel
        profilePresetSelector.removeAllItems()
        profilePresetSelector.addItem(withTitle: "Загрузка...")
        applyProfileButton.target = self
        applyProfileButton.action = #selector(onApplyProfile)
        profileRow.addArrangedSubview(profileLabel)
        profileRow.addArrangedSubview(profilePresetSelector)
        profileRow.addArrangedSubview(applyProfileButton)
        profAudioCard.contentStackView.addArrangedSubview(profileRow)

        audioDeviceRow.orientation = .horizontal
        audioDeviceRow.spacing = 8
        audioDeviceRow.alignment = .centerY
        audioDeviceRow.translatesAutoresizingMaskIntoConstraints = false
        let audioLabel = NSTextField(labelWithString: "Микрофон:")
        audioLabel.font = KrabEarTheme.Typography.controlLabel
        audioDeviceSelector.removeAllItems()
        audioDeviceSelector.addItem(withTitle: "По умолчанию")
        testMicButton.target = self
        testMicButton.action = #selector(onTestMicrophone)
        micTestResultLabel.font = KrabEarTheme.Typography.smallCaption
        micTestResultLabel.textColor = KrabEarTheme.Colors.textSecondary
        audioDeviceRow.addArrangedSubview(audioLabel)
        audioDeviceRow.addArrangedSubview(audioDeviceSelector)
        audioDeviceRow.addArrangedSubview(testMicButton)
        audioDeviceRow.addArrangedSubview(micTestResultLabel)
        profAudioCard.contentStackView.addArrangedSubview(audioDeviceRow)

        profAudioSection.contentStackView.addArrangedSubview(profAudioCard)
        self.profileAudioSection = profAudioSection
        settingsBar.addArrangedSubview(profAudioSection)

        // --- CLIPBOARD HISTORY SECTION ---
        let clipSection = CollapsibleSectionView(sectionId: "dictation_clipboard", title: "Буфер обмена", isExpanded: false)
        let clipCard = ThemeCardView()
        clipboardRow.orientation = .horizontal
        clipboardRow.spacing = 8
        clipboardRow.alignment = .centerY
        clipboardRow.translatesAutoresizingMaskIntoConstraints = false
        clipboardHistoryButton.target = self
        clipboardHistoryButton.action = #selector(onClipboardHistory)
        repasteButton.target = self
        repasteButton.action = #selector(onRepasteItem)
        clipboardRow.addArrangedSubview(clipboardHistoryButton)
        clipboardRow.addArrangedSubview(repasteButton)
        clipCard.contentStackView.addArrangedSubview(clipboardRow)
        clipSection.contentStackView.addArrangedSubview(clipCard)
        self.clipboardSection = clipSection
        settingsBar.addArrangedSubview(clipSection)

        dictationStack.addArrangedSubview(controlRow)
        dictationStack.addArrangedSubview(settingsBar)
        dictationStack.addArrangedSubview(dictationHistoryHeaderRow)
        dictationStack.addArrangedSubview(dictationHistoryHintLabel)
        dictationStack.addArrangedSubview(dictationHistoryPreviewScroll)

        // --- LIVE TRANSLATION TAB ---
        let translationSettingsCard = ThemeCardView()
        translationSettingsCard.title = "Настройки перевода"
        for view in [settingsRow2, toolsRow] as [NSView] {
            view.removeFromSuperview()
            translationSettingsCard.contentStackView.addArrangedSubview(view)
        }

        let gatewayCard = ThemeCardView()
        gatewayCard.title = "Voice Gateway"
        for view in [voiceGatewayRow, callAssistConfigRow] as [NSView] {
            view.removeFromSuperview()
            gatewayCard.contentStackView.addArrangedSubview(view)
        }

        let callAssistCard = ThemeCardView()
        callAssistCard.title = ""  // Title shown by CollapsibleSectionView
        for view in [callAssistControlRow, callPhrasePresetRow, callPhraseActionRow, callTimelineRow, callAssistOutputScroll] as [NSView] {
            view.removeFromSuperview()
            callAssistCard.contentStackView.addArrangedSubview(view)
        }

        let callAssistSection = CollapsibleSectionView(sectionId: "live_call_assist", title: "Call Assist", isExpanded: false)
        callAssistSection.contentStackView.addArrangedSubview(callAssistCard)
        self.liveCallAssistSection = callAssistSection

        liveStack.addArrangedSubview(liveHeaderRow)
        liveStack.addArrangedSubview(translationSettingsCard)
        liveStack.addArrangedSubview(gatewayCard)
        liveStack.addArrangedSubview(callAssistSection)
        liveStack.addArrangedSubview(realtimeScroll)

        // Width constraints for all live translation children (consistent with historyStack pattern)
        for child in liveStack.arrangedSubviews {
            child.widthAnchor.constraint(equalTo: liveStack.widthAnchor).isActive = true
        }

        // --- HISTORY TAB ---
        historyPreviewContainer.isHidden = true
        historyScrollMinHeightConstraint?.constant = 180

        let filtersSection = CollapsibleSectionView(sectionId: "history_filters", title: "Фильтры", isExpanded: false)
        let filtersCard = ThemeCardView()
        filtersCard.contentStackView.addArrangedSubview(filterRow1)
        filtersCard.contentStackView.addArrangedSubview(filterRow2)
        filtersCard.contentStackView.addArrangedSubview(historyQuickPresetRow)
        filtersSection.contentStackView.addArrangedSubview(filtersCard)
        self.historyFiltersSection = filtersSection

        primaryActionsRow.addArrangedSubview(loadMoreButton)
        primaryActionsRow.addArrangedSubview(jumpToLatestButton)
        primaryActionsRow.addArrangedSubview(copyButton)
        primaryActionsRow.addArrangedSubview(pasteSelectedButton)
        primaryActionsRow.addArrangedSubview(deleteButton)
        primaryActionsRow.addArrangedSubview(NSView()) // Spacer
        primaryActionsRow.addArrangedSubview(historyOverviewLabel)
        primaryActionsRow.addArrangedSubview(historyStatusLabel)

        let advancedSection = CollapsibleSectionView(sectionId: "history_advanced", title: "Расширенные действия", isExpanded: false)
        // Move rarely-used buttons from toolbar into advanced section
        let advancedToolbarRow = NSStackView()
        advancedToolbarRow.orientation = .horizontal
        advancedToolbarRow.spacing = 8
        advancedToolbarRow.alignment = .centerY
        advancedToolbarRow.distribution = .fill
        advancedToolbarRow.setHuggingPriority(.defaultLow, for: .horizontal)
        advancedToolbarRow.setClippingResistancePriority(.required, for: .horizontal)
        helpButton.removeFromSuperview()
        liveTranslatePresetButton.removeFromSuperview()
        advancedToolbarRow.addArrangedSubview(helpButton)
        advancedToolbarRow.addArrangedSubview(liveTranslatePresetButton)
        advancedToolbarRow.addArrangedSubview(openTranscriptsButton)
        advancedToolbarRow.addArrangedSubview(NSView()) // Spacer
        advancedSection.contentStackView.addArrangedSubview(advancedToolbarRow)

        secondaryActionsRow.addArrangedSubview(loadAllButton)
        secondaryActionsRow.addArrangedSubview(copyOriginalButton)
        secondaryActionsRow.addArrangedSubview(copyTranslationButton)
        secondaryActionsRow.addArrangedSubview(retranslateButton)
        secondaryActionsRow.addArrangedSubview(summarizeSelectedButton)
        secondaryActionsRow.addArrangedSubview(exportButton)
        secondaryActionsRow.addArrangedSubview(exportNdjsonButton)
        secondaryActionsRow.addArrangedSubview(importNdjsonButton)
        secondaryActionsRow.addArrangedSubview(compactButton)
        secondaryActionsRow.addArrangedSubview(NSView()) // Spacer
        advancedSection.contentStackView.addArrangedSubview(secondaryActionsRow)

        // History enhancements row
        historyEnhancementsRow.orientation = .horizontal
        historyEnhancementsRow.spacing = 8
        historyEnhancementsRow.alignment = .centerY
        historyEnhancementsRow.translatesAutoresizingMaskIntoConstraints = false
        historyEnhancementsRow.distribution = .fill
        historyEnhancementsRow.setHuggingPriority(.defaultLow, for: .horizontal)
        historyEnhancementsRow.setClippingResistancePriority(.required, for: .horizontal)
        exportSrtButton.target = self
        exportSrtButton.action = #selector(onExportSrt)
        cleanupDaysSelector.removeAllItems()
        cleanupDaysSelector.addItems(withTitles: ["30 дней", "60 дней", "90 дней", "180 дней", "365 дней"])
        cleanupHistoryButton.target = self
        cleanupHistoryButton.action = #selector(onCleanupHistory)
        vocabSuggestionsButton.target = self
        vocabSuggestionsButton.action = #selector(onVocabSuggestions)
        glossarySuggestionsButton.target = self
        glossarySuggestionsButton.action = #selector(onGlossarySuggestions)
        historyEnhancementsRow.addArrangedSubview(exportSrtButton)
        historyEnhancementsRow.addArrangedSubview(cleanupDaysSelector)
        historyEnhancementsRow.addArrangedSubview(cleanupHistoryButton)
        historyEnhancementsRow.addArrangedSubview(vocabSuggestionsButton)
        historyEnhancementsRow.addArrangedSubview(glossarySuggestionsButton)
        historyEnhancementsRow.addArrangedSubview(NSView()) // Spacer
        advancedSection.contentStackView.addArrangedSubview(historyEnhancementsRow)

        self.historyAdvancedSection = advancedSection

        let importSection = CollapsibleSectionView(sectionId: "history_import", title: "Импорт аудио", isExpanded: false)
        let importCard = ThemeCardView()
        importCard.contentStackView.addArrangedSubview(importRow)
        importCard.contentStackView.addArrangedSubview(dropZoneView)
        importSection.contentStackView.addArrangedSubview(importCard)
        self.historyImportSection = importSection

        statusRow.addArrangedSubview(glossaryStatusLabel)
        statusRow.addArrangedSubview(NSView()) // Spacer
        statusRow.addArrangedSubview(importStatusLabel)

        // Keep topSearchRow and topActionsRow as separate horizontal rows
        // (reverted merge that caused vertical stacking of toolbar items)
        historyStack.addArrangedSubview(topSearchRow)
        historyStack.addArrangedSubview(topActionsRow)
        historyStack.addArrangedSubview(filtersSection)
        historyStack.addArrangedSubview(scrollView)
        historyStack.addArrangedSubview(primaryActionsRow)
        historyStack.addArrangedSubview(advancedSection)
        historyStack.addArrangedSubview(importSection)
        historyStack.addArrangedSubview(statusRow)

        // Width constraints for history children
        for child in historyStack.arrangedSubviews {
            child.widthAnchor.constraint(equalTo: historyStack.widthAnchor).isActive = true
        }

        // Width constraints for settingsBar children (Dictation tab sections)
        for child in settingsBar.arrangedSubviews {
            child.widthAnchor.constraint(equalTo: settingsBar.widthAnchor).isActive = true
        }

        // --- BUTTON STYLING ---
        // Only true primary action buttons get accent color
        for button in [startStopButton, callAssistStartButton, callPhraseSendButton] {
            button.bezelStyle = .push
            button.isBordered = true
            button.bezelColor = KrabEarTheme.Colors.accent
            button.font = KrabEarTheme.Typography.controlLabel
        }

        // Secondary buttons — standard appearance
        for button in [restartButton, stopButton, loadMoreButton, jumpToLatestButton,
                       copyButton, pasteSelectedButton, deleteButton,
                       diagnosticsButton, metricsButton, recordingStatsButton, storageInfoButton,
                       applyProfileButton, testMicButton, clipboardHistoryButton, repasteButton,
                       exportSrtButton, cleanupHistoryButton, vocabSuggestionsButton, glossarySuggestionsButton] as [NSButton] {
            button.bezelStyle = .push
            button.isBordered = true
            button.bezelColor = nil
            button.font = KrabEarTheme.Typography.controlLabel
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
        alert.runModal()
    }

    @objc private func onToggleRecordFromPanel() {
        onToggleRecording()
    }

    @objc private func onSwapRuEsFromPanel() {
        onSwapRuEsDirection()
        syncSettingsControls()
    }

    @objc private func onEnableLiveTranslationPreset() {
        applySettingsPatch([
            "translation_mode": "auto",
            "translation_style": "chat",
            "translate_and_paste": false,
            "realtime_preview_enabled": true,
            "network_mode": "offline_default",
        ])
        showInfoAlert(
            title: "Live Translation",
            body: "Включен пресет: auto-перевод, чат-стиль, realtime preview, вставка оригинала."
        )
    }

    @objc private func onImportAudio() {
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

    @objc private func onOpenHistoryTabFromDictation() {
        // Если история есть, но текущие фильтры её скрывают, сразу сбрасываем фильтры.
        if items.isEmpty,
           let stats = fetchHistoryStats(),
           stats.activeCount > 0,
           hasActiveHistoryFiltersOrQuery() {
            onClearFilters()
        }
        mainTabView.selectTabViewItem(withIdentifier: PanelTab.history.rawValue)
        window?.makeFirstResponder(searchField)
    }

    @objc private func onCancelImport() {
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

    @objc private func onToggleImportPause() {
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

    @objc private func onOpenImportReport() {
        guard let lastImportReportPath else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: lastImportReportPath))
    }

    @objc private func onRestartFromPanel() {
        onRestartAgent()
    }

    @objc private func onStopFromPanel() {
        onStopAgent()
    }

    @objc private func onQualityChanged() {
        guard !isSyncingSettings else { return }
        let qualityProfile = qualitySelector.indexOfSelectedItem == 1 ? "max" : "balanced"
        applySettingsPatch(["quality_profile": qualityProfile])
    }

    @objc private func onModeChanged() {
        guard !isSyncingSettings else { return }
        let mode = modeSelector.indexOfSelectedItem == 1 ? "menubar" : "headless"
        applySettingsPatch(["mode": mode])
    }

    @objc private func onCleanupProfileChanged() {
        guard !isSyncingSettings else { return }
        let profile = cleanupSelector.indexOfSelectedItem == 1 ? "strict" : "soft"
        applySettingsPatch(["cleanup_profile": profile])
    }

    @objc private func onTranslationModeChanged() {
        guard !isSyncingSettings else { return }
        let mode: String
        switch translationSelector.indexOfSelectedItem {
        case 1:
            mode = "ru_to_es"
        case 2:
            mode = "es_to_ru"
        case 3:
            mode = "en_to_ru"
        case 4:
            mode = "auto"
        case 5:
            mode = "bilingual_ru_es"
        case 6:
            mode = "auto_to_ru"
        default:
            mode = "off"
        }
        applySettingsPatch(["translation_mode": mode])
    }

    @objc private func onHistoryPageSizeChanged() {
        guard !isSyncingSettings else { return }
        let raw = historyPageSizeSelector.titleOfSelectedItem ?? "50"
        let value = Int(raw) ?? 50
        applySettingsPatch(["history_page_size": value])
        loadInitial()
    }

    @objc private func onHistoryDensityChanged() {
        guard !isSyncingSettings else { return }
        let density = historyDensitySelector.indexOfSelectedItem == 1 ? "compact" : "normal"
        applySettingsPatch(["history_text_density": density])
        tableView.reloadData()
    }

    @objc private func onNetworkModeChanged() {
        guard !isSyncingSettings else { return }
        let mode: String
        switch networkSelector.indexOfSelectedItem {
        case 1:
            mode = "offline_strict"
        case 2:
            mode = "online_opt_in"
        default:
            mode = "offline_default"
        }
        applySettingsPatch(["network_mode": mode])
    }

    @objc private func onTranslationStyleChanged() {
        guard !isSyncingSettings else { return }
        let style: String
        switch translationStyleSelector.indexOfSelectedItem {
        case 1:
            style = "chat"
        case 2:
            style = "formal"
        default:
            style = "neutral"
        }
        applySettingsPatch(["translation_style": style])
    }

    @objc private func onAutoPasteChanged() {
        guard !isSyncingSettings else { return }
        let autoPaste = autoPasteButton.state == .on
        applySettingsPatch(["auto_paste": autoPaste])
    }

    @objc private func onStartSoundChanged() {
        guard !isSyncingSettings else { return }
        let playStartSound = startSoundButton.state == .on
        applySettingsPatch(["play_start_sound": playStartSound])
    }

    @objc private func onRealtimePreviewChanged() {
        guard !isSyncingSettings else { return }
        let enabled = realtimePreviewButton.state == .on
        applySettingsPatch(["realtime_preview_enabled": enabled])
        if !enabled {
            realtimeStatusLabel.stringValue = "Realtime: выключен"
            realtimeTextView.string = "Realtime preview отключен в настройках."
        } else {
            refreshRealtimePreview()
        }
    }

    @objc private func onTranslateAndPasteChanged() {
        guard !isSyncingSettings else { return }
        let enabled = translateAndPasteButton.state == .on
        applySettingsPatch(["translate_and_paste": enabled])
    }

    @objc private func onClipboardModeChanged() {
        guard !isSyncingSettings else { return }
        let mode: String
        switch clipboardModeSelector.indexOfSelectedItem {
        case 1:
            mode = "copy_on_fail"
        case 2:
            mode = "never_copy"
        default:
            mode = "always_copy"
        }
        applySettingsPatch(["clipboard_mode": mode])
    }

    @objc private func onAudioDuckingChanged() {
        guard !isSyncingSettings else { return }
        let enabled = audioDuckingButton.state == .on
        applySettingsPatch(["audio_ducking_enabled": enabled])
    }

    @objc private func onAudioDuckingPercentChanged() {
        guard !isSyncingSettings else { return }
        let percent = Int(audioDuckingSlider.doubleValue.rounded())
        audioDuckingValueLabel.stringValue = "\(percent)%"
        applySettingsPatch(["audio_ducking_percent": percent])
    }

    @objc private func onDiarizationChanged() {
        guard !isSyncingSettings else { return }
        let enabled = diarizationButton.state == .on
        applySettingsPatch(["diarization_enabled": enabled])
    }

    @objc private func onLlmRewriteChanged() {
        guard !isSyncingSettings else { return }
        let enabled = llmRewriteButton.state == .on
        llmModelSelector.isEnabled = enabled
        applySettingsPatch(["llm_rewrite_enabled": enabled])
    }

    @objc private func onLlmModelChanged() {
        guard !isSyncingSettings else { return }
        guard let selectedModel = llmModelSelector.titleOfSelectedItem else { return }
        applySettingsPatch(["llm_model": selectedModel])
    }

    @objc private func onOverlayOpacityChanged() {
        guard !isSyncingSettings else { return }
        let percent = Int(overlayOpacitySlider.doubleValue.rounded())
        overlayOpacityValueLabel.stringValue = "\(percent)%"
        applySettingsPatch(["overlay_opacity_percent": percent])
    }

    @objc private func onAutostartChanged() {
        guard !isSyncingSettings else { return }
        let autoStartEnabled = autoStartButton.state == .on
        applySettingsPatch(["auto_start_enabled": autoStartEnabled])
    }

    @objc private func onDockChanged() {
        guard !isSyncingSettings else { return }
        let showDockIcon = dockIconButton.state == .on
        applySettingsPatch(["show_dock_icon": showDockIcon])
    }

    @objc private func onHotkeyChanged() {
        guard !isSyncingSettings else { return }
        let idx = hotkeySelector.indexOfSelectedItem
        let val: String
        switch idx {
        case 1: val = "left_option"
        case 2: val = "any_option"
        default: val = "right_option"
        }
        applySettingsPatch(["hotkey": val])
    }

    @objc private func onHotkeyProfileChanged() {
        guard !isSyncingSettings else { return }
        let idx = hotkeyProfileSelector.indexOfSelectedItem
        let val: String
        switch idx {
        case 1: val = "meeting"
        case 2: val = "translation"
        default: val = "default"
        }
        applySettingsPatch(["hotkey_profile": val])
    }


    @objc private func onCaptureSourceModeChanged() {
        guard !isSyncingSettings else { return }
        applySettingsPatch(["capture_source_mode": selectedCaptureSourceMode()])
    }

    @objc private func onCallNotifyChanged() {
        guard !isSyncingSettings else { return }
        let enabled = callNotifyButton.state == .on
        applySettingsPatch(["call_notify_default": enabled])
    }

    @objc private func onCallAutoSummaryChanged() {
        guard !isSyncingSettings else { return }
        let enabled = callAutoSummaryButton.state == .on
        applySettingsPatch(["call_auto_summary": enabled])
    }

    @objc private func onVoiceGatewayURLChanged() {
        guard !isSyncingSettings else { return }
        let raw = voiceGatewayURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        applySettingsPatch(["voice_gateway_url": raw])
    }

    @objc private func onVoiceGatewayAPIKeyChanged() {
        guard !isSyncingSettings else { return }
        let raw = voiceGatewayAPIKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        applySettingsPatch(["voice_gateway_api_key": raw])
    }

    @objc private func onCheckVoiceGateway() {
        var url = voiceGatewayURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if url.isEmpty {
            url = "http://127.0.0.1:8090"
        }
        if url.hasSuffix("/") {
            url.removeLast()
        }
        guard let healthURL = URL(string: "\(url)/health") else {
            showInfoAlert(title: "Voice Gateway", body: "Некорректный URL: \(url)")
            return
        }

        voiceGatewayCheckButton.isEnabled = false
        voiceGatewayCheckButton.title = "Проверяю..."

        var request = URLRequest(url: healthURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3.0)
        request.httpMethod = "GET"

        let task = URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.voiceGatewayCheckButton.isEnabled = true
                self.voiceGatewayCheckButton.title = "Проверить Gateway"

                if let error {
                    self.showInfoAlert(title: "Voice Gateway", body: "Связь не установлена: \(error.localizedDescription)")
                    return
                }

                guard let http = response as? HTTPURLResponse else {
                    self.showInfoAlert(title: "Voice Gateway", body: "Gateway вернул некорректный ответ.")
                    return
                }

                let payloadPreview: String
                if let data, let text = String(data: data, encoding: .utf8), !text.isEmpty {
                    payloadPreview = text
                } else {
                    payloadPreview = "(пустой ответ)"
                }

                if 200 <= http.statusCode && http.statusCode < 300 {
                    self.showInfoAlert(
                        title: "Voice Gateway",
                        body: "Gateway доступен.\nURL: \(healthURL.absoluteString)\nHTTP: \(http.statusCode)\nОтвет: \(payloadPreview)"
                    )
                } else {
                    self.showInfoAlert(
                        title: "Voice Gateway",
                        body: "Gateway ответил с ошибкой.\nURL: \(healthURL.absoluteString)\nHTTP: \(http.statusCode)\nОтвет: \(payloadPreview)"
                    )
                }
            }
        }
        task.resume()
    }

    @objc private func onStartCallAssist() {
        let settings = settingsProvider()
        let captureMode = selectedCaptureSourceMode()
        let notifyMode = callNotifyButton.state == .on ? "auto_on" : "auto_off"
        let translationMode = settings.translationMode == "off" ? "auto_to_ru" : settings.translationMode

        guard
            let response = try? ipcClient.call(
                method: "start_call_assist",
                params: [
                    "capture_source_mode": captureMode,
                    "notify_mode": notifyMode,
                    "translation_mode": translationMode,
                    "tts_mode": "hybrid",
                    "auto_summary": callAutoSummaryButton.state == .on,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            showInfoAlert(title: "Call Assist", body: "Не удалось запустить звонковую сессию.")
            return
        }

        applySettingsPatch([
            "capture_source_mode": captureMode,
            "call_notify_default": callNotifyButton.state == .on,
            "call_auto_summary": callAutoSummaryButton.state == .on,
        ])
        applyCallAssistState(result)
    }

    @objc private func onStopCallAssist() {
        guard
            let response = try? ipcClient.call(
                method: "stop_call_assist",
                params: [
                    "auto_summary": callAutoSummaryButton.state == .on,
                    "summary_max_items": 60,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            showInfoAlert(title: "Call Assist", body: "Не удалось остановить звонковую сессию.")
            return
        }
        applyCallAssistState(result)
        if let summaryStatus = result["summary_status"] as? String {
            if summaryStatus == "ok", let summary = result["summary"] as? [String: Any] {
                appendCallAssistOutput(title: "Summary звонка", body: formatCallSummary(summary))
                if let historyId = result["summary_history_id"] as? String, !historyId.isEmpty {
                    appendCallAssistOutput(title: "Summary сохранён", body: "Добавлено в историю. id: \(historyId)")
                }
            } else if summaryStatus == "degraded" {
                let errorText = (result["summary_error"] as? String) ?? "unknown"
                appendCallAssistOutput(title: "Summary звонка", body: "Не удалось получить summary: \(errorText)")
            }
        }
    }

    @objc private func onLoadCallPhraseLibrary() {
        let pair = selectedCallPhraseDirection()
        guard
            let response = try? ipcClient.call(
                method: "list_call_assist_quick_phrases",
                params: [
                    "source_lang": pair.sourceLang,
                    "target_lang": pair.targetLang,
                    "category": "all",
                    "limit": 60,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Библиотека фраз", body: "Не удалось получить список быстрых фраз.")
            return
        }

        let items = (result["items"] as? [[String: Any]]) ?? []
        callPhrasePresets = items
        callPhrasePresetSelector.removeAllItems()
        if items.isEmpty {
            callPhrasePresetSelector.addItem(withTitle: "— фразы не найдены —")
            appendCallAssistOutput(title: "Библиотека фраз", body: "Список пуст.")
            return
        }
        for item in items {
            let text = (item["source_text"] as? String) ?? ""
            let category = (item["category"] as? String) ?? "base"
            callPhrasePresetSelector.addItem(withTitle: "[\(category)] \(text)")
        }
        callPhrasePresetSelector.selectItem(at: 0)
        onCallPhrasePresetSelected()
        appendCallAssistOutput(title: "Библиотека фраз", body: "Загружено фраз: \(items.count)")
    }

    @objc private func onCallPhrasePresetSelected() {
        let idx = callPhrasePresetSelector.indexOfSelectedItem
        guard idx >= 0, idx < callPhrasePresets.count else { return }
        let item = callPhrasePresets[idx]
        let text = ((item["source_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            callPhraseInputField.stringValue = text
        }
    }

    @objc private func onCallPhraseDirectionChanged() {
        onLoadCallPhraseLibrary()
    }

    @objc private func onSendCallPhrase() {
        let text = callPhraseInputField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            showInfoAlert(title: "Call Assist", body: "Введите фразу для отправки.")
            return
        }
        let pair = selectedCallPhraseDirection()
        let params: [String: Any] = [
            "text": text,
            "source_lang": pair.sourceLang,
            "target_lang": pair.targetLang,
            "voice": "default",
            "style": "chat",
        ]
        guard
            let response = try? ipcClient.call(method: "call_assist_quick_phrase", params: params),
            let result = response["result"] as? [String: Any],
            let quick = result["quick_phrase"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Quick Phrase", body: "Ошибка отправки фразы в Gateway.")
            return
        }

        let translated = (quick["translated_text"] as? String) ?? ""
        let audioURL = (quick["audio_url"] as? String) ?? "-"
        let cacheHit = (quick["cache_hit"] as? Bool) ?? false
        appendCallAssistOutput(
            title: "Quick Phrase",
            body: """
            \(pair.sourceLang) -> \(pair.targetLang)
            source: \(text)
            translated: \(translated)
            audio: \(audioURL)
            cache_hit: \(cacheHit)
            """
        )
    }

    @objc private func onFetchCallSummary() {
        guard
            let response = try? ipcClient.call(
                method: "call_assist_summary",
                params: ["max_items": 40]
            ),
            let result = response["result"] as? [String: Any],
            let summaryPayload = result["summary"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Summary", body: "Не удалось получить summary звонка.")
            return
        }
        appendCallAssistOutput(title: "Summary", body: formatCallSummary(summaryPayload))
    }

    @objc private func onFetchCallDiagnostics() {
        guard
            let response = try? ipcClient.call(
                method: "call_assist_diagnostics",
                params: ["include_why": true]
            ),
            let result = response["result"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Diagnostics", body: "Не удалось получить diagnostics.")
            return
        }
        let diagnostics = (result["diagnostics"] as? [String: Any]) ?? [:]
        let whyPayload = (result["why"] as? [String: Any]) ?? [:]
        let counters = (diagnostics["counters"] as? [String: Any]) ?? [:]
        let pipeline = (diagnostics["pipeline"] as? [String: Any]) ?? [:]
        let why = (whyPayload["why"] as? [String: Any]) ?? [:]
        let whyCode = (why["code"] as? String) ?? "-"
        let whyMessage = (why["message"] as? String) ?? "-"
        appendCallAssistOutput(
            title: "Diagnostics",
            body: """
            translation_partial: \(counters["translation_partial"] ?? 0)
            tts_ready: \(counters["tts_ready"] ?? 0)
            cache_hits: \(pipeline["cache_hits"] ?? 0)
            cache_misses: \(pipeline["cache_misses"] ?? 0)
            fallback: \(pipeline["last_fallback"] ?? "-")
            why: \(whyCode) — \(whyMessage)
            """
        )
    }

    @objc private func onEstimateCallCost() {
        let countryField = NSTextField(frame: NSRect(x: 0, y: 0, width: 90, height: 24))
        countryField.stringValue = "ES"
        countryField.placeholderString = "ISO2"

        let inboundField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        inboundField.stringValue = "200"
        let landlineField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        landlineField.stringValue = "100"
        let mobileField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        mobileField.stringValue = "100"
        let mediaField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        mediaField.stringValue = "400"

        let livePricingButton = NSButton(checkboxWithTitle: "Live pricing (Twilio API)", target: nil, action: nil)
        livePricingButton.state = .on

        let grid = NSGridView(views: [
            [NSTextField(labelWithString: "Страна:"), countryField],
            [NSTextField(labelWithString: "Inbound (мин):"), inboundField],
            [NSTextField(labelWithString: "Outbound landline (мин):"), landlineField],
            [NSTextField(labelWithString: "Outbound mobile (мин):"), mobileField],
            [NSTextField(labelWithString: "Media stream (мин):"), mediaField],
            [NSTextField(labelWithString: ""), livePricingButton],
        ])
        grid.rowSpacing = 6
        grid.columnSpacing = 10

        let alert = NSAlert()
        alert.messageText = "Оценка стоимости звонков"
        alert.informativeText = "Введите месячный микс минут. В live-режиме Gateway запросит Twilio Pricing API."
        alert.accessoryView = grid
        alert.addButton(withTitle: "Рассчитать")
        alert.addButton(withTitle: "Отмена")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let country = countryField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        let inbound = Double(inboundField.stringValue) ?? 200
        let landline = Double(landlineField.stringValue) ?? 100
        let mobile = Double(mobileField.stringValue) ?? 100
        let media = Double(mediaField.stringValue) ?? 400
        let useLivePricing = livePricingButton.state == .on

        guard
            let response = try? ipcClient.call(
                method: "call_assist_cost_estimate",
                params: [
                    "country": country,
                    "minutes_inbound": inbound,
                    "minutes_outbound_landline": landline,
                    "minutes_outbound_mobile": mobile,
                    "minutes_media_stream": media,
                    "use_live_pricing": useLivePricing,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Оценка стоимости", body: "Не удалось получить расчёт от Gateway.")
            return
        }

        let report = formatCallCostEstimate(result)
        appendCallAssistOutput(title: "Оценка стоимости", body: report)
        showInfoAlert(title: "Оценка стоимости", body: report)
    }

    @objc private func onFetchCallTimeline() {
        guard
            let response = try? ipcClient.call(
                method: "call_assist_timeline",
                params: ["limit": 50]
            ),
            let result = response["result"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Timeline", body: "Не удалось получить timeline.")
            return
        }

        let items = (result["items"] as? [[String: Any]]) ?? []
        if items.isEmpty {
            appendCallAssistOutput(title: "Timeline", body: "Пока событий нет.")
            return
        }
        var summaryText = ""
        if
            let summaryResponse = try? ipcClient.call(
                method: "call_assist_timeline_summary",
                params: ["limit": 200, "max_tasks": 5]
            ),
            let summaryResult = summaryResponse["result"] as? [String: Any]
        {
            let summary = ((summaryResult["summary"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !summary.isEmpty {
                summaryText = "summary: \(summary)\n\n"
            }
        }
        var statsText = ""
        if
            let statsResponse = try? ipcClient.call(
                method: "call_assist_timeline_stats",
                params: ["limit": 200]
            ),
            let statsResult = statsResponse["result"] as? [String: Any],
            let stats = statsResult["stats"] as? [String: Any]
        {
            let count = (stats["count"] as? Int) ?? 0
            let chars = (stats["text_chars"] as? Int) ?? 0
            var kindsChunk = ""
            if let byKind = stats["by_kind"] as? [String: Any], !byKind.isEmpty {
                let pairs = byKind.keys.sorted().map { key -> String in
                    let value = byKind[key] ?? 0
                    return "\(key)=\(value)"
                }
                kindsChunk = pairs.joined(separator: ", ")
            }
            statsText = "stats: count=\(count), text_chars=\(chars)\nby_kind: \(kindsChunk)\n\n"
        }
        let preview = formatCallTimelinePreview(items: Array(items.prefix(12)))
        appendCallAssistOutput(
            title: "Timeline",
            body: "Событий: \(items.count)\n\(summaryText)\(statsText)\(preview)"
        )
    }

    @objc private func onExportCallTimeline() {
        let formatAlert = NSAlert()
        formatAlert.messageText = "Экспорт Timeline"
        formatAlert.informativeText = "Выберите формат выгрузки текущей звонковой сессии."
        let formatSelector = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 220, height: 24), pullsDown: false)
        formatSelector.addItems(withTitles: ["Markdown (.md)", "NDJSON (.ndjson)"])
        formatSelector.selectItem(at: 0)
        formatAlert.accessoryView = formatSelector
        formatAlert.addButton(withTitle: "Экспорт")
        formatAlert.addButton(withTitle: "Отмена")
        guard formatAlert.runModal() == .alertFirstButtonReturn else { return }

        let selected = formatSelector.indexOfSelectedItem
        let exportFormat = selected == 1 ? "ndjson" : "md"
        guard
            let response = try? ipcClient.call(
                method: "call_assist_timeline_export",
                params: [
                    "format": exportFormat,
                    "limit": 400,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Timeline export", body: "Не удалось выгрузить timeline.")
            return
        }
        let content = (result["content"] as? String) ?? ""
        if content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            appendCallAssistOutput(title: "Timeline export", body: "Timeline пуст, экспортировать нечего.")
            return
        }

        let savePanel = NSSavePanel()
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let suffix = exportFormat == "ndjson" ? "ndjson" : "md"
        savePanel.nameFieldStringValue = "krab_call_timeline_\(formatter.string(from: Date())).\(suffix)"
        savePanel.canCreateDirectories = true
        if savePanel.runModal() != .OK {
            return
        }
        guard let url = savePanel.url else { return }
        do {
            try content.write(to: url, atomically: true, encoding: .utf8)
            appendCallAssistOutput(title: "Timeline export", body: "Сохранено: \(url.path)")
        } catch {
            appendCallAssistOutput(title: "Timeline export", body: "Ошибка записи файла: \(error.localizedDescription)")
        }
    }

    @objc private func onClearCallTimeline() {
        let keepLast = selectedCallTimelineKeepLast()
        guard
            let response = try? ipcClient.call(
                method: "call_assist_timeline_clear",
                params: ["keep_last": keepLast]
            ),
            let result = response["result"] as? [String: Any]
        else {
            appendCallAssistOutput(title: "Timeline clear", body: "Не удалось очистить timeline.")
            return
        }
        let before = (result["before"] as? Int) ?? -1
        let after = (result["after"] as? Int) ?? -1
        appendCallAssistOutput(
            title: "Timeline clear",
            body: "Очистка завершена. keep_last=\(keepLast), before=\(before), after=\(after)"
        )
    }

    @objc private func onSaveCallTimelineToHistory() {
        guard
            let response = try? ipcClient.call(
                method: "call_assist_timeline_to_history",
                params: [
                    "format": "md",
                    "limit": 500,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            appendCallAssistOutput(
                title: "Timeline -> история",
                body: "Не удалось сохранить timeline в историю."
            )
            return
        }
        let historyId = (result["history_id"] as? String) ?? "-"
        let chars = (result["chars"] as? Int) ?? 0
        appendCallAssistOutput(
            title: "Timeline -> история",
            body: "Сохранено в историю. id=\(historyId), chars=\(chars)"
        )
        loadInitial()
    }

    private func selectedCallTimelineKeepLast() -> Int {
        let raw = callTimelineKeepLastSelector.titleOfSelectedItem ?? "keep 1"
        let digits = raw.filter(\.isNumber)
        return Int(digits) ?? 1
    }

    private func formatCallTimelinePreview(items: [[String: Any]]) -> String {
        var lines: [String] = []
        for item in items {
            let ts = (item["ts"] as? String) ?? "-"
            let kind = (item["kind"] as? String) ?? "unknown"
            let text = ((item["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let shortText: String
            if text.isEmpty {
                shortText = "(без текста)"
            } else if text.count > 120 {
                shortText = String(text.prefix(120)) + "…"
            } else {
                shortText = text
            }
            lines.append("[\(ts)] \(kind): \(shortText)")
        }
        return lines.joined(separator: "\n")
    }

    private func formatCallSummary(_ payload: [String: Any]) -> String {
        let summaryText = ((payload["summary"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let rawTasks = (payload["tasks"] as? [Any]) ?? []
        var tasks: [String] = []
        for raw in rawTasks {
            if let dict = raw as? [String: Any] {
                let candidate = (
                    (dict["task"] as? String)
                    ?? (dict["title"] as? String)
                    ?? (dict["text"] as? String)
                    ?? ""
                ).trimmingCharacters(in: .whitespacesAndNewlines)
                if !candidate.isEmpty {
                    tasks.append(candidate)
                }
            } else if let rawText = raw as? String {
                let candidate = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !candidate.isEmpty {
                    tasks.append(candidate)
                }
            }
        }
        let safeSummary = summaryText.isEmpty ? "—" : summaryText
        let tasksText = tasks.isEmpty ? "- (нет задач)" : tasks.prefix(10).enumerated().map { "\($0 + 1). \($1)" }.joined(separator: "\n")
        return """
        \(safeSummary)
        tasks:
        \(tasksText)
        """
    }

    private func formatCallCostEstimate(_ payload: [String: Any]) -> String {
        let country = (payload["country"] as? String) ?? "n/a"
        let ratesSource = (payload["rates_source"] as? String) ?? "unknown"
        let ratesNote = ((payload["rates_note"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        let telephony = (payload["telephony_usd"] as? [String: Any]) ?? [:]
        let ai = (payload["ai_usd"] as? [String: Any]) ?? [:]
        let total = (payload["total_usd"] as? Double)
            ?? (payload["total_usd"] as? NSNumber)?.doubleValue
            ?? 0.0

        let telephonyTotal = (telephony["total"] as? Double)
            ?? (telephony["total"] as? NSNumber)?.doubleValue
            ?? 0.0
        let aiTotal = (ai["total"] as? Double)
            ?? (ai["total"] as? NSNumber)?.doubleValue
            ?? 0.0

        let noteLine = ratesNote.isEmpty ? "" : "\nrates_note: \(ratesNote)"
        return """
        country: \(country)
        rates_source: \(ratesSource)\(noteLine)
        telephony_total_usd: \(String(format: "%.3f", telephonyTotal))
        ai_total_usd: \(String(format: "%.3f", aiTotal))
        total_usd: \(String(format: "%.3f", total))
        """
    }

    @objc private func onLoadMore() {
        guard let nextCursor else {
            updateHistoryStatusLabel()
            return
        }
        let pageSize = settingsProvider().historyPageSize
        if currentQuery.isEmpty {
            appendPage(
                method: "get_history_page",
                params: buildHistoryQueryParams(cursor: nextCursor, limit: pageSize)
            )
        } else {
            var params = buildHistoryQueryParams(cursor: nextCursor, limit: pageSize)
            params["query"] = currentQuery
            appendPage(method: "search_history", params: params)
        }
    }

    @objc private func onLoadAll() {
        var guardLoops = 0
        while nextCursor != nil && guardLoops < 120 {
            guardLoops += 1
            onLoadMore()
        }
        if guardLoops >= 120 {
            showInfoAlert(
                title: "История",
                body: "Загружен лимит страниц за один проход. Сузьте фильтр или повторите действие."
            )
        }
    }

    @objc private func onJumpToLatest() {
        guard !items.isEmpty else { return }
        tableView.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        tableView.scrollRowToVisible(0)
    }

    @objc private func onCopy() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let text = items[selected].text
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    @objc private func onPasteSelected() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        onPasteHistoryItem(items[selected])
    }

    @objc private func onCopyOriginal() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        let text = item.sourceText.isEmpty ? item.text : item.sourceText
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    @objc private func onCopyTranslation() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        let text = item.translatedText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            showInfoAlert(title: "Копирование перевода", body: "Для этой записи перевод отсутствует.")
            return
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    @objc private func onExportHistory() {
        guard !items.isEmpty else {
            showInfoAlert(title: "Экспорт истории", body: "История пуста, экспортировать нечего.")
            return
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let suggestedName = "krab_ear_history_\(formatter.string(from: Date())).md"

        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = suggestedName
        panel.allowedContentTypes = [.plainText]
        panel.title = "Сохранить экспорт истории"
        panel.prompt = "Сохранить"

        guard panel.runModal() == .OK, let outputURL = panel.url else { return }

        let content = buildHistoryMarkdownExport()
        do {
            try content.write(to: outputURL, atomically: true, encoding: .utf8)
            showInfoAlert(
                title: "Экспорт истории",
                body: "Сохранено записей: \(items.count)\n\(outputURL.path)"
            )
        } catch {
            showInfoAlert(
                title: "Экспорт истории",
                body: "Не удалось сохранить файл: \(error.localizedDescription)"
            )
        }
        // Также сохраняем копию через IPC (export_history) в transcripts/
        if let ipcResponse = try? ipcClient.call(
            method: "export_history",
            params: ["format": "md", "save_to_file": true]
        ), let ipcResult = ipcResponse["result"] as? [String: Any],
           let serverPath = ipcResult["path"] as? String {
            notificationService.notify(title: "Krab Ear", body: "Серверная копия: \(serverPath)")
        }
    }

    @objc private func onExportHistoryNdjson() {
        guard !items.isEmpty else {
            showInfoAlert(title: "Экспорт NDJSON", body: "История пуста, экспортировать нечего.")
            return
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let suggestedName = "krab_ear_history_\(formatter.string(from: Date())).ndjson"

        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = suggestedName
        panel.allowedContentTypes = [.json]
        panel.title = "Сохранить экспорт NDJSON"
        panel.prompt = "Сохранить"

        guard panel.runModal() == .OK, let outputURL = panel.url else { return }
        let content = buildHistoryNdjsonExport()
        do {
            try content.write(to: outputURL, atomically: true, encoding: .utf8)
            showInfoAlert(
                title: "Экспорт NDJSON",
                body: "Сохранено записей: \(items.count)\n\(outputURL.path)"
            )
        } catch {
            showInfoAlert(
                title: "Экспорт NDJSON",
                body: "Не удалось сохранить файл: \(error.localizedDescription)"
            )
        }
    }

    @objc private func onImportHistoryNdjson() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.title = "Выберите NDJSON-файл истории"
        panel.prompt = "Импортировать"

        guard panel.runModal() == .OK, let inputURL = panel.url else { return }
        guard
            let response = try? ipcClient.call(
                method: "import_history_ndjson",
                params: ["path": inputURL.path]
            ),
            let result = response["result"] as? [String: Any]
        else {
            showInfoAlert(title: "Импорт NDJSON", body: "Ошибка при импорте файла.")
            return
        }

        let imported = (result["imported"] as? Int) ?? 0
        let skipped = (result["skipped"] as? Int) ?? 0
        let errors = (result["errors"] as? Int) ?? 0
        loadInitial()
        showInfoAlert(
            title: "Импорт NDJSON",
            body: "Импортировано: \(imported)\nПропущено дублей: \(skipped)\nОшибок: \(errors)"
        )
    }

    @objc private func onRetranslateSelected() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }

        let item = items[selected]
        let sourceText = item.sourceText.isEmpty ? item.text : item.sourceText
        let cleanSource = sourceText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanSource.isEmpty else {
            showInfoAlert(title: "Повторить перевод", body: "У выбранной записи нет исходного текста.")
            return
        }

        let targetMode: String
        switch translationSelector.indexOfSelectedItem {
        case 1:
            targetMode = "ru_to_es"
        case 2:
            targetMode = "es_to_ru"
        case 3:
            targetMode = "en_to_ru"
        case 4:
            targetMode = "auto"
        case 5:
            targetMode = "bilingual_ru_es"
        case 6:
            targetMode = "auto_to_ru"
        default:
            targetMode = "off"
        }

        guard targetMode != "off" else {
            showInfoAlert(title: "Повторить перевод", body: "Выберите режим перевода отличный от Off.")
            return
        }

        guard
            let translateResponse = try? ipcClient.call(
                method: "translate_text",
                params: [
                    "text": cleanSource,
                    "translation_mode": targetMode,
                    "translation_style": settingsProvider().translationStyle,
                    "network_mode": settingsProvider().networkMode,
                ]
            ),
            let translateResult = translateResponse["result"] as? [String: Any]
        else {
            showInfoAlert(title: "Повторить перевод", body: "Не удалось выполнить перевод.")
            return
        }

        let status = (translateResult["status"] as? String) ?? "unknown"
        let translatedText = ((translateResult["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if status != "ok" || translatedText.isEmpty {
            showInfoAlert(title: "Повторить перевод", body: "Перевод недоступен: \(status).")
            return
        }

        let shouldPasteTranslated = translateAndPasteButton.state == .on
        let newText = shouldPasteTranslated ? translatedText : cleanSource

        let _ = try? ipcClient.call(
            method: "add_history_item",
            params: [
                "text": newText,
                "paste_status": "failed",
                "source_text": cleanSource,
                "translated_text": translatedText,
                "translation_mode": targetMode,
                "translation_status": status,
                "translation_engine": (translateResult["engine"] as? String) ?? "",
                "source_lang": (translateResult["source_lang"] as? String) ?? "",
                "target_lang": (translateResult["target_lang"] as? String) ?? "",
            ]
        )

        loadInitial()
        showInfoAlert(
            title: "Повторить перевод",
            body: "Готово. Создана новая запись истории с обновлённым переводом."
        )
    }

    @objc private func onSummarizeSelected() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        let source = item.sourceText.isEmpty ? item.text : item.sourceText
        let cleanSource = source.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanSource.isEmpty else {
            showInfoAlert(title: "Summary", body: "У выбранной записи пустой текст.")
            return
        }
        guard
            let response = try? ipcClient.call(
                method: "summarize_text",
                params: [
                    "text": cleanSource,
                    "mode": "summary_short",
                    "max_points": 4,
                ]
            ),
            let result = response["result"] as? [String: Any]
        else {
            showInfoAlert(title: "Summary", body: "Не удалось построить summary.")
            return
        }
        let summary = (result["summary"] as? String) ?? ""
        let bullets = (result["bullets"] as? [String]) ?? []
        let bulletText = bullets.isEmpty ? "- (нет пунктов)" : bullets.prefix(6).map { "- \($0)" }.joined(separator: "\n")
        let text = """
        \(summary.isEmpty ? "—" : summary)
        
        Пункты:
        \(bulletText)
        """
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        showInfoAlert(title: "Summary", body: "\(text)\n\n(Скопировано в буфер)")
    }

    @objc private func onDelete() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        _ = try? ipcClient.call(method: "delete_history_item", params: ["id": item.id])
        items.remove(at: selected)
        tableView.reloadData()
    }

    @objc private func onCompact() {
        let response = try? ipcClient.call(method: "compact_history", params: [:])
        if let result = response?["result"] as? [String: Any] {
            let reclaimed = (result["reclaimed_bytes"] as? Int) ?? 0
            let beforeBytes = (result["before_total_bytes"] as? Int) ?? 0
            let afterBytes = (result["after_total_bytes"] as? Int) ?? 0
            let beforeActive = (result["before_active_count"] as? Int) ?? 0
            let afterActive = (result["after_active_count"] as? Int) ?? 0
            showInfoAlert(
                title: "Оптимизация истории",
                body: """
                Активных записей: \(beforeActive) -> \(afterActive)
                Размер: \(formatBytes(beforeBytes)) -> \(formatBytes(afterBytes))
                Освобождено: \(formatBytes(max(0, reclaimed)))
                """
            )
        }
        loadInitial()
    }

    @objc private func onOpenTranscripts() {
        let transcriptsPath = NSString(string: "~/Library/Application Support/KrabEar/transcripts").expandingTildeInPath
        let url = URL(fileURLWithPath: transcriptsPath, isDirectory: true)
        // Create directory if needed
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        NSWorkspace.shared.open(url)
    }

    private func enqueueImport(paths: [String], sourceTag: String) {
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

    private func processNextImportIfNeeded() {
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
            let errors = ((result?["errors"] as? [String]) ?? []).count
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

    private func finishImportQueueIfNeeded() {
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
        let totalSec = max(0, Int(importDurationTotalSec.rounded()))
        let summary = "Импорт завершён: файлов \(importProcessedTotal)/\(importFilesPlanned), ошибок \(importErrorsTotal), задач \(importJobsCompleted), время \(totalSec)с."
        importStatusLabel.stringValue = summary
        let reportPath = writeImportQueueReport(summary: summary)
        lastImportReportPath = reportPath
        openImportReportButton.isEnabled = (reportPath != nil)
        if let reportPath {
            showInfoAlert(title: "Импорт аудио", body: "\(summary)\nОтчёт: \(reportPath)")
        } else {
            showInfoAlert(title: "Импорт аудио", body: summary)
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
        importDurationTotalSec = 0
        importSessionStartedAt = nil
        importJobSignatures.removeAll()
        importSourceStats.removeAll()
        importFormatStats.removeAll()
        importFilesPlanned = 0
        importBytesPlanned = 0
    }

    private func sendImportNotification(filesProcessed: Int, errors: Int, duration: Int) {
        notificationService.notify(
            title: "Krab Ear — Импорт завершён",
            body: "Файлов: \(filesProcessed), ошибок: \(errors), время: \(duration)с"
        )
    }

    private func updateImportStatusLabel() {
        if isImportRunning {
            let current = min(importJobsPlanned, importJobsCompleted + 1)
            let avgSec = importJobsCompleted > 0 ? (importDurationTotalSec / Double(importJobsCompleted)) : 0
            let remainingJobs = max(0, importJobsPlanned - importJobsCompleted)
            let eta = Int((Double(remainingJobs) * avgSec).rounded())
            let currentFiles = currentImportJob?.audioCount ?? 0
            let currentFolders = currentImportJob?.folderCount ?? 0
            let elapsed = currentImportJobStartedAt.map { Int(Date().timeIntervalSince($0).rounded()) } ?? 0
            let folderSuffix = currentFolders > 1 ? " (\(currentFolders) папок)" : ""
            importStatusLabel.stringValue = "Импорт: задача \(current)/\(importJobsPlanned), файлов \(currentFiles)\(folderSuffix), \(elapsed)с" + (eta > 0 ? ", ETA ~\(eta)с" : "")
            return
        }
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

    private func startImportElapsedTimer() {
        stopImportElapsedTimer()
        importElapsedTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.isImportRunning else { return }
                self.updateImportStatusLabel()
            }
        }
    }

    private func stopImportElapsedTimer() {
        importElapsedTimer?.invalidate()
        importElapsedTimer = nil
    }

    private func normalizedImportSignature(_ paths: [String]) -> String {
        let normalized = paths
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .sorted()
        return normalized.joined(separator: "|")
    }

    private func previewImport(paths: [String]) -> ImportPreview {
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

    private func writeImportQueueReport(summary: String) -> String? {
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
        \(summary)
        """

        do {
            try body.write(toFile: reportPath, atomically: true, encoding: .utf8)
            return reportPath
        } catch {
            return nil
        }
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
        stack.spacing = 8

        let alert = NSAlert()
        alert.messageText = "Добавить термин в глоссарий"
        alert.informativeText = "Термин будет применяться к результату перевода локально."
        alert.accessoryView = stack
        alert.addButton(withTitle: "Сохранить")
        alert.addButton(withTitle: "Отмена")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let source = sourceField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let target = targetField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !source.isEmpty, !target.isEmpty else {
            showInfoAlert(title: "Глоссарий", body: "Поля не должны быть пустыми.")
            return
        }

        guard
            let response = try? ipcClient.call(
                method: "set_translation_glossary_item",
                params: ["source": source, "target": target]
            ),
            response["ok"] as? Bool == true
        else {
            showInfoAlert(title: "Глоссарий", body: "Не удалось сохранить термин.")
            return
        }

        var nextPayload = settingsProvider().toPayload()
        var glossary = settingsProvider().translationGlossary
        glossary[source] = target
        nextPayload["translation_glossary"] = glossary
        _ = settingsUpdater(nextPayload)
        syncSettingsControls()
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
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let source = sourceField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !source.isEmpty else {
            showInfoAlert(title: "Глоссарий", body: "Нужно указать термин.")
            return
        }

        guard
            let response = try? ipcClient.call(
                method: "remove_translation_glossary_item",
                params: ["source": source]
            ),
            response["ok"] as? Bool == true
        else {
            showInfoAlert(title: "Глоссарий", body: "Не удалось удалить термин.")
            return
        }

        var nextPayload = settingsProvider().toPayload()
        var glossary = settingsProvider().translationGlossary
        glossary.removeValue(forKey: source)
        nextPayload["translation_glossary"] = glossary
        _ = settingsUpdater(nextPayload)
        syncSettingsControls()
    }

    private func loadInitial() {
        items = []
        nextCursor = nil
        tableView.reloadData()
        jumpToLatestButton.isEnabled = false
        updateHistoryStatusLabel()
        updateDictationHistoryPreview()
        updateHistoryFiltersBadge()
        updateHistoryPreviewCard()

        let pageSize = settingsProvider().historyPageSize
        let hadActiveFilters = hasActiveHistoryFiltersOrQuery()
        if currentQuery.isEmpty {
            appendPage(method: "get_history_page", params: buildHistoryQueryParams(cursor: NSNull(), limit: pageSize))
        } else {
            var params = buildHistoryQueryParams(cursor: NSNull(), limit: pageSize)
            params["query"] = currentQuery
            appendPage(method: "search_history", params: params)
        }

        recoverHistoryIfFiltersHideAllRows(limit: pageSize, hadActiveFilters: hadActiveFilters)
    }

    private func appendPage(method: String, params: [String: Any]) {
        guard let response = try? ipcClient.call(method: method, params: params),
              let result = response["result"] as? [String: Any],
              let rawItems = result["items"] as? [[String: Any]] else {
            return
        }

        let mapped = rawItems.compactMap(HistoryItem.init(payload:))
        items.append(contentsOf: mapped)
        nextCursor = result["next_cursor"] as? String
        loadMoreButton.isEnabled = (nextCursor != nil)
        loadAllButton.isEnabled = (nextCursor != nil)
        updateLoadMoreButtonCaption()
        updateHistoryStatusLabel()
        updateDictationHistoryPreview()
        tableView.reloadData()
        updateHistoryPreviewCard()
        jumpToLatestButton.isEnabled = !items.isEmpty
    }

    private func hasActiveHistoryFiltersOrQuery() -> Bool {
        if !currentQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
        if historyPasteStatusFilter.indexOfSelectedItem > 0 {
            return true
        }
        if historyTranslationModeFilter.indexOfSelectedItem > 0 {
            return true
        }
        if historyTranslationStatusFilter.indexOfSelectedItem > 0 {
            return true
        }
        if !historyFromDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
        if !historyToDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
        return false
    }

    private func recoverHistoryIfFiltersHideAllRows(limit: Int, hadActiveFilters: Bool) {
        guard hadActiveFilters else { return }
        guard items.isEmpty else { return }
        guard !isRecoveringHistoryFromFilters else { return }
        guard let stats = fetchHistoryStats(), stats.activeCount > 0 else { return }

        // UX-страховка: если записи есть, но пользователь их "скрыл" фильтрами,
        // автоматически показываем последние элементы вместо пустой таблицы.
        isRecoveringHistoryFromFilters = true
        defer { isRecoveringHistoryFromFilters = false }

        currentQuery = ""
        searchField.stringValue = ""
        historyPasteStatusFilter.selectItem(at: 0)
        historyTranslationModeFilter.selectItem(at: 0)
        historyTranslationStatusFilter.selectItem(at: 0)
        historyFromDateField.stringValue = ""
        historyToDateField.stringValue = ""

        items = []
        nextCursor = nil
        tableView.reloadData()
        appendPage(method: "get_history_page", params: buildHistoryQueryParams(cursor: NSNull(), limit: limit))
        if !items.isEmpty {
            historyStatusLabel.stringValue = "Фильтры скрывали записи. Показаны последние \(items.count)."
        }
        updateDictationHistoryPreview()
    }

    private func applySettingsPatch(_ patch: [String: Any]) {
        var payload = settingsProvider().toPayload()
        for (key, value) in patch {
            payload[key] = value
        }
        let updated = settingsUpdater(payload)
        syncSettingsControls(using: updated)
    }

    private func selectedCaptureSourceMode() -> String {
        switch captureSourceSelector.indexOfSelectedItem {
        case 1:
            return "system_audio"
        case 2:
            return "mic_plus_system"
        default:
            return "mic"
        }
    }

    private func selectCaptureSourceMode(_ mode: String) {
        switch mode {
        case "system_audio":
            captureSourceSelector.selectItem(at: 1)
        case "mic_plus_system":
            captureSourceSelector.selectItem(at: 2)
        default:
            captureSourceSelector.selectItem(at: 0)
        }
    }

    private func selectedCallPhraseDirection() -> (sourceLang: String, targetLang: String) {
        switch callPhraseDirectionSelector.indexOfSelectedItem {
        case 1:
            return ("es", "ru")
        case 2:
            return ("auto", "ru")
        default:
            return ("ru", "es")
        }
    }

    private func appendCallAssistOutput(title: String, body: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        let chunk = "[\(ts)] \(title)\n\(body)\n\n"
        let existing = callAssistOutputView.string
        let combined = chunk + existing
        callAssistOutputView.string = String(combined.prefix(6000))
    }

    private func applyCallAssistState(_ state: [String: Any]) {
        let active = (state["active"] as? Bool) ?? false
        let status = ((state["status"] as? String) ?? (active ? "running" : "idle")).lowercased()
        let gatewayStatus = (state["gateway_status"] as? String) ?? ""
        let gatewayError = (state["gateway_error"] as? String) ?? ""
        let sessionId = (state["session_id"] as? String) ?? ""

        var chunks: [String] = []
        chunks.append("Call Assist: \(status)")
        if !sessionId.isEmpty {
            chunks.append("id \(sessionId)")
        }
        if !gatewayStatus.isEmpty {
            chunks.append("GW \(gatewayStatus)")
        }
        if !gatewayError.isEmpty {
            chunks.append("err \(gatewayError)")
        }
        callAssistStatusLabel.stringValue = chunks.joined(separator: " • ")
        callAssistStartButton.isEnabled = !active
        callAssistStopButton.isEnabled = active || status == "running"
    }

    private func refreshCallAssistState(silentOnError: Bool = true) {
        guard
            let response = try? ipcClient.call(method: "get_call_assist_state", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            if !silentOnError {
                callAssistStatusLabel.stringValue = "Call Assist: backend недоступен"
            }
            return
        }
        applyCallAssistState(result)
    }

    private func refreshCaptureSourceHint() {
        guard
            let response = try? ipcClient.call(method: "list_audio_inputs", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            captureSourceSelector.toolTip = "Список входных устройств недоступен."
            return
        }
        let count = (result["count"] as? Int) ?? 0
        let items = (result["items"] as? [[String: Any]]) ?? []
        let defaultName = items.first(where: { ($0["is_default"] as? Bool) == true })?["name"] as? String
        if let defaultName, !defaultName.isEmpty {
            captureSourceSelector.toolTip = "Доступно входов: \(count). По умолчанию: \(defaultName)"
        } else {
            captureSourceSelector.toolTip = "Доступно входов: \(count)"
        }
    }

    private func syncSettingsControls(using value: AgentSettings? = nil) {
        let settings = value ?? settingsProvider()

        isSyncingSettings = true
        qualitySelector.selectItem(at: settings.qualityProfile == "max" ? 1 : 0)
        cleanupSelector.selectItem(at: settings.cleanupProfile == "strict" ? 1 : 0)
        switch settings.translationMode {
        case "ru_to_es":
            translationSelector.selectItem(at: 1)
            swapRuEsButton.title = "Swap: RU -> ES"
        case "es_to_ru":
            translationSelector.selectItem(at: 2)
            swapRuEsButton.title = "Swap: ES -> RU"
        case "en_to_ru":
            translationSelector.selectItem(at: 3)
            swapRuEsButton.title = "Swap RU<->ES"
        case "auto":
            translationSelector.selectItem(at: 4)
            swapRuEsButton.title = "Swap RU<->ES"
        case "bilingual_ru_es":
            translationSelector.selectItem(at: 5)
            swapRuEsButton.title = "Swap RU<->ES"
        case "auto_to_ru":
            translationSelector.selectItem(at: 6)
            swapRuEsButton.title = "Swap RU<->ES"
        default:
            translationSelector.selectItem(at: 0)
            swapRuEsButton.title = "Swap RU<->ES"
        }
        swapRuEsButton.isEnabled = settings.translationMode == "ru_to_es"
            || settings.translationMode == "es_to_ru"
            || settings.translationMode == "auto"
            || settings.translationMode == "bilingual_ru_es"
        let normalizedPageSize = normalizePageSize(settings.historyPageSize)
        let idx = historyPageSizeSelector.indexOfItem(withTitle: "\(normalizedPageSize)")
        if idx >= 0 {
            historyPageSizeSelector.selectItem(at: idx)
        } else {
            historyPageSizeSelector.selectItem(withTitle: "50")
        }
        historyDensitySelector.selectItem(at: settings.historyTextDensity == "compact" ? 1 : 0)
        switch settings.networkMode {
        case "offline_strict":
            networkSelector.selectItem(at: 1)
        case "online_opt_in":
            networkSelector.selectItem(at: 2)
        default:
            networkSelector.selectItem(at: 0)
        }
        switch settings.translationStyle {
        case "chat":
            translationStyleSelector.selectItem(at: 1)
        case "formal":
            translationStyleSelector.selectItem(at: 2)
        default:
            translationStyleSelector.selectItem(at: 0)
        }
        modeSelector.selectItem(at: settings.mode == "menubar" ? 1 : 0)
        autoPasteButton.state = settings.autoPaste ? .on : .off
        startSoundButton.state = settings.playStartSound ? .on : .off
        realtimePreviewButton.state = settings.realtimePreviewEnabled ? .on : .off
        translateAndPasteButton.state = settings.translateAndPaste ? .on : .off
        autoStartButton.state = settings.autoStartEnabled ? .on : .off
        dockIconButton.state = settings.showDockIcon ? .on : .off
        callNotifyButton.state = settings.callNotifyDefault ? .on : .off
        callAutoSummaryButton.state = settings.callAutoSummary ? .on : .off
        voiceGatewayURLField.stringValue = settings.voiceGatewayURL
        voiceGatewayAPIKeyField.stringValue = settings.voiceGatewayAPIKey
        selectCaptureSourceMode(settings.captureSourceMode)
        switch settings.clipboardMode {
        case "copy_on_fail":
            clipboardModeSelector.selectItem(at: 1)
        case "never_copy":
            clipboardModeSelector.selectItem(at: 2)
        default:
            clipboardModeSelector.selectItem(at: 0)
        }
        audioDuckingButton.state = settings.audioDuckingEnabled ? .on : .off
        let safeDuckPercent = max(0, min(settings.audioDuckingPercent, 100))
        audioDuckingSlider.doubleValue = Double(safeDuckPercent)
        audioDuckingValueLabel.stringValue = "\(safeDuckPercent)%"
        let safeOverlayPercent = max(15, min(settings.overlayOpacityPercent, 90))
        overlayOpacitySlider.doubleValue = Double(safeOverlayPercent)
        overlayOpacityValueLabel.stringValue = "\(safeOverlayPercent)%"
        // D.10a: AI Settings Sync
        diarizationButton.state = settings.diarizationEnabled ? .on : .off
        llmRewriteButton.state = settings.llmRewriteEnabled ? .on : .off
        if let idx = llmModelSelector.itemTitles.firstIndex(of: settings.llmModel) {
            llmModelSelector.selectItem(at: idx)
        }
        llmModelSelector.isEnabled = settings.llmRewriteEnabled
        glossaryStatusLabel.stringValue = "Глоссарий: \(settings.translationGlossary.count)"

        switch settings.hotkey {
        case "left_option":
            hotkeySelector.selectItem(at: 1)
        case "any_option":
            hotkeySelector.selectItem(at: 2)
        default:
            hotkeySelector.selectItem(at: 0)
        }

        switch settings.hotkeyProfile {
        case "meeting":
            hotkeyProfileSelector.selectItem(at: 1)
        case "translation":
            hotkeyProfileSelector.selectItem(at: 2)
        default:
            hotkeyProfileSelector.selectItem(at: 0)
        }

        isSyncingTabs = true

        let tab = PanelTab.from(settingsValue: settings.uiLastTab)
        switch tab {
        case .dictation:
            mainTabView.selectTabViewItem(at: 0)
        case .liveTranslation:
            mainTabView.selectTabViewItem(at: 1)
        case .history:
            mainTabView.selectTabViewItem(at: 2)
        }
        isSyncingTabs = false
        applyHistoryFocusMode(settings.historyFocusMode)
        applyHistoryTextDensity(settings.historyTextDensity)
        isSyncingSettings = false
        updateLoadMoreButtonCaption()
        refreshCaptureSourceHint()
        refreshCallAssistState()
    }


    private func applyHistoryFocusMode(_ enabled: Bool) {
        // Collapse or expand all history collapsible sections
        historyFiltersSection?.setExpanded(!enabled, animated: true)
        historyAdvancedSection?.setExpanded(!enabled, animated: true)
        historyImportSection?.setExpanded(!enabled, animated: true)

        // Disable disclosure buttons in focus mode so user can't expand them
        historyFiltersSection?.disclosureButton.isEnabled = !enabled
        historyAdvancedSection?.disclosureButton.isEnabled = !enabled
        historyImportSection?.disclosureButton.isEnabled = !enabled

        historyScrollMinHeightConstraint?.constant = enabled ? 240 : 180
        historyFocusModeButton.title = enabled ? "Фокус истории: ON" : "Фокус истории: OFF"
    }

    private func applyHistoryTextDensity(_ density: String) {
        let compact = (density == "compact")
        tableView.rowHeight = compact ? 24 : 28
        historyDensitySelector.selectItem(at: compact ? 1 : 0)
    }

    private func historyBodyFont() -> NSFont {
        return settingsProvider().historyTextDensity == "compact"
            ? .systemFont(ofSize: 12)
            : .systemFont(ofSize: NSFont.systemFontSize)
    }

    private func historyMinRowHeight() -> CGFloat {
        return settingsProvider().historyTextDensity == "compact" ? 24 : 28
    }

    private func updateLoadMoreButtonCaption() {
        let pageSize = settingsProvider().historyPageSize
        loadMoreButton.title = "Показать ещё (\(pageSize))"
        loadAllButton.title = "Загрузить всё"
    }

    private func normalizePageSize(_ value: Int) -> Int {
        if value <= 25 { return 25 }
        if value <= 50 { return 50 }
        if value <= 100 { return 100 }
        return 200
    }

    private func updateHistoryStatusLabel() {
        let stats = fetchHistoryStats()
        let statsSuffix = stats.map { " • Активных: \($0.activeCount), \(formatBytes($0.totalBytes))" } ?? ""
        if items.isEmpty {
            historyStatusLabel.stringValue = "История пуста\(statsSuffix)"
        } else if nextCursor == nil {
            historyStatusLabel.stringValue = "Показаны все: \(items.count)\(statsSuffix)"
        } else {
            historyStatusLabel.stringValue = "Показано: \(items.count) (есть ещё)\(statsSuffix)"
        }
        historyOverviewLabel.stringValue = buildHistoryOverviewLabel()
    }

    private func updateDictationHistoryPreview() {
        let stats = fetchHistoryStats()
        let activeCount = stats?.activeCount ?? 0

        if items.isEmpty {
            dictationHistoryOpenButton.isEnabled = activeCount > 0
            if activeCount > 0 {
                let suffix = hasActiveHistoryFiltersOrQuery() ? " (фильтры/поиск активны)" : ""
                dictationHistoryHintLabel.stringValue = "В истории есть \(activeCount) записей\(suffix)."
                dictationHistoryPreviewView.string = "Записи есть, но текущая выборка пустая. Нажмите «Открыть историю»."
            } else {
                dictationHistoryHintLabel.stringValue = "История пока пустая. После первой транскрибации записи появятся здесь."
                dictationHistoryPreviewView.string = "Пока нет записей для предпросмотра."
            }
            return
        }

        dictationHistoryOpenButton.isEnabled = true
        dictationHistoryHintLabel.stringValue = "Показаны последние \(min(items.count, 5)) из \(max(activeCount, items.count)) записей."
        let lines = items.prefix(5).enumerated().map { index, item -> String in
            let raw = item.text.trimmingCharacters(in: .whitespacesAndNewlines)
            let shortText = raw.count > 120 ? String(raw.prefix(120)) + "…" : raw
            return "\(index + 1). [\(item.ts)] \(shortText)"
        }
        dictationHistoryPreviewView.string = lines.joined(separator: "\n")
    }

    private func updateHistoryPreviewCard() {
        if items.isEmpty {
            let message = hasActiveHistoryFiltersOrQuery()
                ? "Фильтры/поиск скрывают записи. Снимите фильтры или обновите поиск."
                : "История пуста. Запишите что-нибудь — запись появится здесь."
            historyPreviewTextView.string = message
            return
        }

        let previewLines = items.prefix(3).enumerated().map { index, item -> String in
            let snippet = item.text.trimmingCharacters(in: .whitespacesAndNewlines)
            let truncated = snippet.count > 150 ? String(snippet.prefix(150)) + "…" : snippet
            return "\(index + 1). [\(item.ts)] \(truncated)"
        }
        historyPreviewTextView.string = previewLines.joined(separator: "\n\n")
    }

    private func fetchHistoryStats() -> (activeCount: Int, totalBytes: Int)? {
        guard
            let response = try? ipcClient.call(method: "get_history_stats", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            return nil
        }
        return (
            activeCount: (result["active_count"] as? Int) ?? 0,
            totalBytes: (result["total_bytes"] as? Int) ?? 0
        )
    }

    private func buildHistoryOverviewLabel() -> String {
        guard
            let response = try? ipcClient.call(method: "get_history_overview", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            return "Обзор: недоступен"
        }
        let todayCount = (result["today_count"] as? Int) ?? 0
        let last24hCount = (result["last_24h_count"] as? Int) ?? 0
        let pasteOk = (result["paste_ok"] as? Int) ?? 0
        let pasteFailed = (result["paste_failed"] as? Int) ?? 0
        let translatedOk = (result["translated_ok"] as? Int) ?? 0
        let translatedError = (result["translated_error"] as? Int) ?? 0
        return "Обзор: сегодня \(todayCount), 24ч \(last24hCount), вставка ok/err \(pasteOk)/\(pasteFailed), перевод ok/err \(translatedOk)/\(translatedError)"
    }

    private func formatBytes(_ value: Int) -> String {
        let safe = max(0, value)
        if safe < 1024 {
            return "\(safe) B"
        }
        let kb = Double(safe) / 1024.0
        if kb < 1024 {
            return String(format: "%.1f KB", kb)
        }
        let mb = kb / 1024.0
        if mb < 1024 {
            return String(format: "%.1f MB", mb)
        }
        let gb = mb / 1024.0
        return String(format: "%.2f GB", gb)
    }

    private func buildHistoryQueryParams(cursor: Any, limit: Int) -> [String: Any] {
        var params: [String: Any] = [
            "cursor": cursor,
            "limit": limit,
        ]

        let pasteStatus = selectedHistoryPasteStatusFilter()
        if let pasteStatus {
            params["paste_status"] = pasteStatus
        }
        let translationMode = selectedHistoryTranslationModeFilter()
        if let translationMode {
            params["translation_mode"] = translationMode
        }
        let translationStatus = selectedHistoryTranslationStatusFilter()
        if let translationStatus {
            params["translation_status"] = translationStatus
        }
        let fromTs = historyFromDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !fromTs.isEmpty {
            params["from_ts"] = fromTs
        }
        let toTs = historyToDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !toTs.isEmpty {
            params["to_ts"] = toTs
        }
        return params
    }

    private func selectedHistoryPasteStatusFilter() -> String? {
        let idx = historyPasteStatusFilter.indexOfSelectedItem
        switch idx {
        case 1:
            return "ok"
        case 2:
            return "failed"
        default:
            return nil
        }
    }

    private func selectedHistoryTranslationModeFilter() -> String? {
        let idx = historyTranslationModeFilter.indexOfSelectedItem
        switch idx {
        case 1:
            return "off"
        case 2:
            return "ru_to_es"
        case 3:
            return "es_to_ru"
        case 4:
            return "en_to_ru"
        case 5:
            return "auto"
        case 6:
            return "auto_to_ru"
        case 7:
            return "bilingual_ru_es"
        default:
            return nil
        }
    }

    private func selectedHistoryTranslationStatusFilter() -> String? {
        let idx = historyTranslationStatusFilter.indexOfSelectedItem
        switch idx {
        case 1:
            return "ok"
        case 2:
            return "not_requested"
        case 3:
            return "model_unavailable_offline"
        case 4:
            return "model_unavailable_online"
        case 5:
            return "model_unavailable_cached"
        case 6:
            return "cannot_detect_language"
        case 7:
            return "already_target_language"
        case 8:
            return "translate_error"
        default:
            return nil
        }
    }

    private func buildHistoryNdjsonExport() -> String {
        var lines: [String] = []
        for item in items {
            let payload: [String: Any] = [
                "id": item.id,
                "ts": item.ts,
                "text": item.text,
                "paste_status": item.pasteStatus,
                "source_text": item.sourceText,
                "translated_text": item.translatedText,
                "translation_mode": item.translationMode,
                "translation_status": item.translationStatus,
            ]
            if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
               let raw = String(data: data, encoding: .utf8) {
                lines.append(raw)
            }
        }
        return lines.joined(separator: "\n") + "\n"
    }

    private func buildHistoryMarkdownExport() -> String {
        var lines: [String] = []
        lines.append("# Krab Ear History Export")
        lines.append("")
        lines.append("- exported_at: \(ISO8601DateFormatter().string(from: Date()))")
        lines.append("- items: \(items.count)")
        lines.append("- search_query: \(currentQuery.isEmpty ? "(none)" : currentQuery)")
        lines.append("")

        for (index, item) in items.enumerated() {
            lines.append("## \(index + 1). \(item.ts)")
            lines.append("")
            lines.append("- id: \(item.id)")
            lines.append("- paste_status: \(item.pasteStatus)")
            lines.append("- translation_mode: \(item.translationMode)")
            lines.append("- translation_status: \(item.translationStatus)")
            lines.append("")
            lines.append("### text")
            lines.append("")
            lines.append(item.text)
            lines.append("")
            if !item.sourceText.isEmpty {
                lines.append("### source_text")
                lines.append("")
                lines.append(item.sourceText)
                lines.append("")
            }
            if !item.translatedText.isEmpty {
                lines.append("### translated_text")
                lines.append("")
                lines.append(item.translatedText)
                lines.append("")
            }
        }

        return lines.joined(separator: "\n")
    }

    private func showInfoAlert(title: String, body: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    @objc private func onTabSelectorChanged() {
        let index = tabSelector.selectedSegment
        guard index >= 0, index < mainTabView.numberOfTabViewItems else { return }
        mainTabView.selectTabViewItem(at: index)
    }

    func tabView(_ tabView: NSTabView, didSelect tabViewItem: NSTabViewItem?) {
        guard tabView == mainTabView else { return }
        guard !isSyncingTabs, !isSyncingSettings else { return }
        if let item = tabViewItem {
            tabSelector.selectedSegment = mainTabView.indexOfTabViewItem(item)
        }
        let raw = String(describing: tabViewItem?.identifier ?? PanelTab.history.rawValue)
        let tab = PanelTab.from(settingsValue: raw)
        applySettingsPatch(["ui_last_tab": tab.rawValue])
    }

    func windowWillClose(_ notification: Notification) {
        stopPreviewPolling()
    }

    private func startPreviewPolling() {
        stopPreviewPolling()
        previewPollTick = 0
        previewTimer = Timer.scheduledTimer(withTimeInterval: 0.9, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.refreshRealtimePreview()
                self.previewPollTick += 1
                if self.previewPollTick % 3 == 0 {
                    self.refreshCallAssistState()
                }
            }
        }
        if let previewTimer {
            RunLoop.main.add(previewTimer, forMode: .common)
        }
        refreshRealtimePreview()
        refreshCallAssistState()
    }

    private func stopPreviewPolling() {
        previewTimer?.invalidate()
        previewTimer = nil
        previewPollTick = 0
    }

    private func refreshRealtimePreview() {
        let settings = settingsProvider()
        guard settings.realtimePreviewEnabled else {
            realtimeStatusLabel.stringValue = "Realtime: выключен"
            return
        }

        guard
            let response = try? ipcClient.call(method: "get_recording_state", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            return
        }

        let isRecording = (result["is_recording"] as? Bool) ?? false
        let durationSec = (result["duration_sec"] as? Double) ?? 0.0
        let previewText = ((result["preview_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let durationText = formatDuration(durationSec)

        if isRecording {
            realtimeStatusLabel.stringValue = "Realtime: запись \(durationText)"
            realtimeTextView.string = previewText.isEmpty
                ? "Слушаю... первые слова появятся через ~1-2 секунды."
                : previewText
        } else {
            realtimeStatusLabel.stringValue = "Realtime: idle"
            if previewText.isEmpty {
                realtimeTextView.string = "Запись не активна."
            } else {
                realtimeTextView.string = previewText
            }
        }
    }

    private func formatDuration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let minutes = total / 60
        let secs = total % 60
        return String(format: "%02d:%02d", minutes, secs)
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

    func numberOfRows(in tableView: NSTableView) -> Int {
        items.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row < items.count else { return nil }
        let item = items[row]

        let identifier = tableColumn?.identifier.rawValue ?? "unknown"
        let text: String
        switch identifier {
        case "ts":
            text = item.ts
        case "status":
            text = item.pasteStatus
        default:
            let translationBadge = buildTranslationBadge(item)
            text = translationBadge + item.text
        }

        let isTextColumn = identifier == "text"
        let bodyFont = historyBodyFont()
        let label = isTextColumn
            ? NSTextField(wrappingLabelWithString: text)
            : NSTextField(labelWithString: text)
        label.font = bodyFont
        label.maximumNumberOfLines = isTextColumn ? 0 : 1
        label.lineBreakMode = isTextColumn ? .byWordWrapping : .byTruncatingTail

        let cell = NSTableCellView()
        cell.addSubview(label)
        label.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            label.topAnchor.constraint(equalTo: cell.topAnchor, constant: 4),
            label.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -4),
        ])
        return cell
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        guard row < items.count else { return historyMinRowHeight() }

        let textColumn = tableView.tableColumns.first { $0.identifier.rawValue == "text" }
        let columnWidth = max(180, (textColumn?.width ?? 700) - 8)
        let sampleText = items[row].text as NSString

        let textHeight = sampleText.boundingRect(
            with: NSSize(width: columnWidth, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: historyBodyFont()],
            context: nil
        ).height

        return max(historyMinRowHeight(), ceil(textHeight) + 10)
    }

    private func buildTranslationBadge(_ item: HistoryItem) -> String {
        guard item.translationMode != "off" else { return "" }
        let statusMark: String
        switch item.translationStatus {
        case "ok":
            statusMark = "ok"
        case "not_requested":
            statusMark = "skip"
        case "model_unavailable_offline":
            statusMark = "offline"
        case "model_unavailable_online":
            statusMark = "online?"
        case "model_unavailable_cached":
            statusMark = "cached"
        case "cannot_detect_language":
            statusMark = "lang?"
        case "already_target_language":
            statusMark = "ru=ok"
        case "translate_error":
            statusMark = "error"
        default:
            statusMark = "warn"
        }
        return "[\(item.translationMode):\(statusMark)] "
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

    // MARK: - Diagnostics & Metrics handlers

    @objc private func onDiagnostics() {
        guard let response = try? ipcClient.call(method: "get_diagnostics", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить диагностику")
            return
        }
        showDiagnosticsOutput(formatNestedResult(result, title: "Диагностика"))
    }

    @objc private func onMetrics() {
        guard let response = try? ipcClient.call(method: "get_metrics_dashboard", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить метрики")
            return
        }
        showDiagnosticsOutput(formatNestedResult(result, title: "Метрики"))
    }

    @objc private func onRecordingStats() {
        guard let response = try? ipcClient.call(method: "get_recording_stats", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить статистику")
            return
        }
        showDiagnosticsOutput(formatNestedResult(result, title: "Статистика записей"))
    }

    @objc private func onStorageInfo() {
        guard let response = try? ipcClient.call(method: "get_storage_info", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить информацию о хранилище")
            return
        }
        showDiagnosticsOutput(formatNestedResult(result, title: "Хранилище"))
    }

    private func showDiagnosticsOutput(_ text: String) {
        diagnosticsOutputView.string = text
        diagnosticsSection?.setExpanded(true, animated: true)
        // Switch to Dictation tab if not already there
        if mainTabView.selectedTabViewItem?.identifier as? String != PanelTab.dictation.rawValue {
            mainTabView.selectTabViewItem(at: 0)
            tabSelector?.setSelected(true, forSegment: 0)
        }
    }

    private func formatNestedResult(_ result: [String: Any], title: String) -> String {
        var lines: [String] = ["=== \(title) ==="]
        for (key, value) in result.sorted(by: { $0.key < $1.key }) {
            if let dict = value as? [String: Any] {
                lines.append("\n[\(key)]")
                for (k, v) in dict.sorted(by: { $0.key < $1.key }) {
                    lines.append("  \(k): \(v)")
                }
            } else {
                lines.append("\(key): \(value)")
            }
        }
        return lines.joined(separator: "\n")
    }

    // MARK: - Profile Presets & Audio Devices handlers

    @objc private func onApplyProfile() {
        let selectedTitle = profilePresetSelector.titleOfSelectedItem ?? ""
        guard !selectedTitle.isEmpty, selectedTitle != "Загрузка..." else { return }
        let presetName = (profilePresetSelector.selectedItem?.representedObject as? String) ?? selectedTitle.lowercased()
        guard let response = try? ipcClient.call(method: "apply_profile_preset", params: ["preset": presetName]),
              let result = response["result"] as? [String: Any],
              result["applied"] as? Bool == true else {
            showDiagnosticsOutput("Ошибка: не удалось применить профиль '\(selectedTitle)'")
            return
        }
        showDiagnosticsOutput("Профиль '\(selectedTitle)' применён.")
        syncSettingsControls()
    }

    private func loadProfilePresets() {
        guard let response = try? ipcClient.call(method: "list_profile_presets", params: [:]),
              let result = response["result"] as? [String: Any],
              let presets = result["presets"] as? [[String: Any]] else { return }
        profilePresetSelector.removeAllItems()
        for preset in presets {
            if let name = preset["name"] as? String {
                let label = (preset["label"] as? String) ?? name
                profilePresetSelector.addItem(withTitle: label)
                profilePresetSelector.lastItem?.representedObject = name
            }
        }
    }

    private func loadAudioDevices() {
        guard let response = try? ipcClient.call(method: "get_audio_devices", params: [:]),
              let result = response["result"] as? [String: Any],
              let devices = result["devices"] as? [[String: Any]] else { return }
        audioDeviceSelector.removeAllItems()
        audioDeviceSelector.addItem(withTitle: "По умолчанию (системный)")
        for device in devices {
            if let name = device["name"] as? String {
                audioDeviceSelector.addItem(withTitle: name)
            }
        }
    }

    @objc private func onTestMicrophone() {
        micTestResultLabel.stringValue = "Тестирование..."
        micTestResultLabel.textColor = KrabEarTheme.Colors.textSecondary
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            guard let response = try? self.ipcClient.call(method: "test_microphone", params: ["duration_sec": 2]),
                  let result = response["result"] as? [String: Any] else {
                DispatchQueue.main.async {
                    self.micTestResultLabel.stringValue = "Ошибка теста"
                    self.micTestResultLabel.textColor = KrabEarTheme.Colors.error
                }
                return
            }
            let rms = result["rms"] as? Double ?? 0
            let peak = result["peak"] as? Double ?? 0
            let status = rms > 0.01 ? "OK" : "Тихо"
            DispatchQueue.main.async {
                self.micTestResultLabel.stringValue = String(format: "RMS: %.3f | Peak: %.3f | %@", rms, peak, status)
                self.micTestResultLabel.textColor = rms > 0.01 ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.warning
            }
        }
    }

    // MARK: - Clipboard History handlers

    @objc private func onClipboardHistory() {
        guard let response = try? ipcClient.call(method: "get_clipboard_history", params: [:]),
              let result = response["result"] as? [String: Any],
              let items = result["items"] as? [[String: Any]] else {
            showDiagnosticsOutput("Буфер обмена пуст")
            return
        }
        var lines: [String] = ["=== Буфер обмена (последние \(items.count)) ==="]
        for (i, item) in items.enumerated() {
            let text = String((item["text"] as? String ?? "").prefix(80))
            let ts = item["ts"] as? String ?? ""
            lines.append("\(i + 1). [\(ts)] \(text)")
        }
        showDiagnosticsOutput(lines.joined(separator: "\n"))
    }

    @objc private func onRepasteItem() {
        guard let response = try? ipcClient.call(method: "get_clipboard_history", params: [:]),
              let result = response["result"] as? [String: Any],
              let clipItems = result["items"] as? [[String: Any]],
              let firstItem = clipItems.first,
              let itemId = firstItem["id"] as? String else {
            notificationService.notify(title: "Krab Ear", body: "Нет элементов для вставки")
            return
        }
        guard let _ = try? ipcClient.call(method: "repaste_item", params: ["id": itemId]) else {
            notificationService.notify(title: "Krab Ear", body: "Ошибка повторной вставки")
            return
        }
        notificationService.notify(title: "Krab Ear", body: "Элемент вставлен повторно")
    }

    // MARK: - History Enhancement handlers

    @objc private func onExportSrt() {
        let selectedRow = tableView.selectedRow
        guard selectedRow >= 0, selectedRow < items.count else {
            notificationService.notify(title: "Krab Ear", body: "Выберите запись для экспорта SRT")
            return
        }
        let item = items[selectedRow]
        guard let response = try? ipcClient.call(method: "export_history_srt", params: ["id": item.id]),
              let result = response["result"] as? [String: Any],
              let path = result["path"] as? String else {
            notificationService.notify(title: "Krab Ear", body: "Ошибка экспорта SRT")
            return
        }
        notificationService.notify(title: "Krab Ear", body: "SRT сохранён")
        NSWorkspace.shared.selectFile(path, inFileViewerRootedAtPath: "")
    }

    @objc private func onCleanupHistory() {
        let daysMap = [30, 60, 90, 180, 365]
        let days = daysMap[cleanupDaysSelector.indexOfSelectedItem]
        let alert = NSAlert()
        alert.messageText = "Очистка истории"
        alert.informativeText = "Удалить записи старше \(days) дней? Это действие нельзя отменить."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Удалить")
        alert.addButton(withTitle: "Отмена")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        guard let response = try? ipcClient.call(method: "cleanup_old_history", params: ["days": days]),
              let result = response["result"] as? [String: Any],
              let deleted = result["deleted_count"] as? Int else {
            notificationService.notify(title: "Krab Ear", body: "Ошибка очистки")
            return
        }
        notificationService.notify(title: "Krab Ear", body: "Удалено записей: \(deleted)")
        loadInitial()
    }

    @objc private func onVocabSuggestions() {
        guard let response = try? ipcClient.call(method: "get_vocabulary_suggestions", params: [:]),
              let result = response["result"] as? [String: Any],
              let suggestions = result["suggestions"] as? [String] else {
            showDiagnosticsOutput("Нет предложений по словарю")
            return
        }
        var lines: [String] = ["=== Словарь (предложения) ==="]
        for (i, word) in suggestions.enumerated() {
            lines.append("\(i + 1). \(word)")
        }
        showDiagnosticsOutput(lines.joined(separator: "\n"))
    }

    @objc private func onGlossarySuggestions() {
        guard let response = try? ipcClient.call(method: "get_glossary_suggestions", params: [:]),
              let result = response["result"] as? [String: Any],
              let suggestions = result["suggestions"] as? [[String: Any]] else {
            showDiagnosticsOutput("Нет предложений по глоссарию")
            return
        }
        var lines: [String] = ["=== Глоссарий (авто-предложения) ==="]
        for (i, item) in suggestions.enumerated() {
            let source = item["source"] as? String ?? "?"
            let target = item["target"] as? String ?? "?"
            let count = item["count"] as? Int ?? 0
            lines.append("\(i + 1). \(source) → \(target) (встречалось: \(count))")
        }
        showDiagnosticsOutput(lines.joined(separator: "\n"))
    }

    // MARK: - get_history_item (double-click detail)

    @objc private func onTableViewDoubleClick() {
        let row = tableView.clickedRow
        guard row >= 0, row < items.count else { return }
        let item = items[row]
        guard let response = try? ipcClient.call(method: "get_history_item", params: ["id": item.id]),
              let result = response["result"] as? [String: Any] else {
            showInfoAlert(title: "Запись", body: "Не удалось загрузить детали записи.")
            return
        }
        let text = result["text"] as? String ?? item.text
        let ts = result["ts"] as? String ?? ""
        let wordCount = result["word_count"] as? Int ?? 0
        let transcriptFile = result["transcript_file"] as? String
        var info = """
        \(text)

        --- Метаданные ---
        ID: \(item.id)
        Время: \(ts)
        Слов: \(wordCount)
        """
        if let tf = transcriptFile {
            info += "\nТранскрипт: \(tf)"
        }
        let alert = NSAlert()
        alert.messageText = "Детали записи"
        alert.informativeText = info
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Скопировать")
        alert.addButton(withTitle: "Закрыть")
        let resp = alert.runModal()
        if resp == .alertFirstButtonReturn {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
    }

    // MARK: - summarize_item (single item by ID)

    @objc private func onSummarizeItem() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else {
            showDiagnosticsOutput("Выберите запись для summary.")
            return
        }
        let item = items[selected]
        guard let response = try? ipcClient.call(method: "summarize_item", params: ["id": item.id]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось построить summary для записи.")
            return
        }
        let summary = result["summary"] as? String ?? "(нет текста)"
        let isLLM = result["llm"] as? Bool ?? false
        let sourceChars = result["source_chars"] as? Int ?? 0
        let text = """
        === Summary (ID: \(item.id.prefix(8))…) ===
        \(summary)

        [LLM: \(isLLM ? "да" : "нет"), источник: \(sourceChars) символов]
        """
        showDiagnosticsOutput(text)
    }

    private func updateHistoryFiltersBadge() {
        var count = 0
        if !currentQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }
        if historyPasteStatusFilter.indexOfSelectedItem > 0 { count += 1 }
        if historyTranslationModeFilter.indexOfSelectedItem > 0 { count += 1 }
        if historyTranslationStatusFilter.indexOfSelectedItem > 0 { count += 1 }
        if !historyFromDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }
        if !historyToDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { count += 1 }
        
        if count > 0 {
            historyFiltersBadge.stringValue = "Фильтры: \(count)"
            historyFiltersBadge.textColor = .controlAccentColor
            historyFiltersBadge.isHidden = false
        } else {
            historyFiltersBadge.stringValue = "Фильтры: 0"
            historyFiltersBadge.isHidden = true
        }
    }
}

/// Drop zone для drag-and-drop импорта аудио/папок.
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
        layer?.cornerRadius = 8
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.cgColor
        layer?.backgroundColor = NSColor.controlBackgroundColor.withAlphaComponent(0.35).cgColor

        registerForDraggedTypes([.fileURL])

        hintLabel.translatesAutoresizingMaskIntoConstraints = false
        hintLabel.alignment = .center
        hintLabel.font = .systemFont(ofSize: 12)
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
            ? NSColor.systemBlue.withAlphaComponent(0.95).cgColor
            : NSColor.separatorColor.cgColor
        layer?.backgroundColor = value
            ? NSColor.systemBlue.withAlphaComponent(0.16).cgColor
            : NSColor.controlBackgroundColor.withAlphaComponent(0.35).cgColor
        hintLabel.textColor = value ? NSColor.systemBlue : NSColor.secondaryLabelColor
    }
}
