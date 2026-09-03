/*
 CallAutomationController — NSViewController для вкладки «Автозвонки».

 Отправляет IPC-команды (call_session_create, call_session_end, call_intervene) и отображает
 активную сессию: статус, живой транскрипт, длительность, текущую стоимость,
 историю последних 10 звонков.

 Если backend возвращает "gateway_not_configured" → показывает баннер настройки.

 Polish v2:
   1. Cost estimator preview (call_estimate_cost IPC) показывает ~$X.XXX/min (Country, Provider)
   2. Phone validation tooltip + красная обводка если не E.164
   3. Call history table: Date | To | Duration | Cost | Status icon | Goal (truncated)
      Click row → modal с полным транскриптом
   4. Emergency stop — большая красная кнопка ЭКСТРЕННО ПРЕРВАТЬ, всегда видна при активном звонке
   5. Provider switcher — segmented control Telnyx / Twilio, зелёная точка если оба ключа настроены
*/

import AppKit
import Foundation

// MARK: - CallSession model (клиентская сторона)

struct CallSession {
    enum Status: String {
        case idle        = "idle"
        case dialing     = "dialing"
        case connected   = "connected"
        case talking     = "talking"
        case ending      = "ending"
        case ended       = "ended"
        case error       = "error"

        var displayTitle: String {
            switch self {
            case .idle:      return "Ожидание"
            case .dialing:   return "Набор номера..."
            case .connected: return "Подключено"
            case .talking:   return "Разговор"
            case .ending:    return "Завершение..."
            case .ended:     return "Завершён"
            case .error:     return "Ошибка"
            }
        }

        var badgeColor: NSColor {
            switch self {
            case .idle, .ending, .ended: return .secondaryLabelColor
            case .dialing:               return .systemOrange
            case .connected, .talking:   return .systemGreen
            case .error:                 return .systemRed
            }
        }

        /// Иконка-символ для колонки истории
        var historyIcon: String {
            switch self {
            case .ended:            return "✓"
            case .error:            return "✗"
            case .ending:           return "⏱"
            default:                return "•"
            }
        }

        var historyIconColor: NSColor {
            switch self {
            case .ended:   return .systemGreen
            case .error:   return .systemRed
            case .ending:  return .systemOrange
            default:       return .secondaryLabelColor
            }
        }
    }

    var sessionID: String
    var status: Status
    var phone: String
    var goal: String
    var startedAt: Date?
    var endedAt: Date?
    var transcript: String
    var costUSD: Double
    var errorMessage: String?
}

struct CallHistoryItem {
    let sessionID: String
    let phone: String
    let goal: String
    let status: String
    let durationSec: Double
    let costUSD: Double
    let startedAt: String
    let summary: String

    /// Отформатированная длительность mm:ss
    var durationFormatted: String {
        let total = Int(durationSec)
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

// MARK: - Controller

@MainActor
final class CallAutomationController: NSViewController {

    // MARK: - Dependencies

    let ipcClient: IPCClient

    // MARK: - State

    private var currentSession: CallSession?
    private var callHistory: [CallHistoryItem] = []
    private var durationTimer: Timer?
    private var pollTimer: Timer?

    /// Текущий выбранный провайдер
    /// Провайдер один: линия принадлежит Voice Gateway (волна консолидации
    /// 03.09.2026, спека docs/superpowers/specs/2026-09-03-telephony-consolidation.md).
    /// Собственные адаптеры Telnyx/Twilio/SIP удалены — они не совершили ни
    /// одного звонка и не были подключены к дороге вызова вовсе.
    enum CallProvider: Int {
        case gateway = 0
        var settingKey: String { "gateway" }
    }
    private var selectedProvider: CallProvider = .gateway

    // MARK: - UI: Provider switcher

    private let providerSegmented: NSSegmentedControl = {
        let sc = NSSegmentedControl(labels: ["Voice Gateway"], trackingMode: .selectOne, target: nil, action: nil)
        sc.selectedSegment = 0
        sc.font = KrabEarTheme.Typography.caption
        sc.translatesAutoresizingMaskIntoConstraints = false
        return sc
    }()

    private let providerStatusDot: NSImageView = {
        let img = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: "статус провайдера")
        let iv = NSImageView(image: img ?? NSImage())
        iv.contentTintColor = .systemGray
        iv.toolTip = "API key и from-number не настроены"
        iv.translatesAutoresizingMaskIntoConstraints = false
        return iv
    }()

    // MARK: - UI: Input

    private let phoneLabel: NSTextField = {
        let l = NSTextField(labelWithString: "Номер телефона (E.164):")
        l.font = KrabEarTheme.Typography.body
        return l
    }()
    private let phoneField: NSTextField = {
        let f = NSTextField(string: "")
        f.placeholderString = "+79991234567"
        f.font = KrabEarTheme.Typography.body
        f.isEditable = true
        return f
    }()
    private let phoneValidationLabel: NSTextField = {
        let l = NSTextField(labelWithString: "")
        l.font = KrabEarTheme.Typography.caption
        l.textColor = .systemRed
        return l
    }()

    private let goalLabel: NSTextField = {
        let l = NSTextField(labelWithString: "Цель звонка:")
        l.font = KrabEarTheme.Typography.body
        return l
    }()
    private let goalField: NSTextField = {
        let f = NSTextField(string: "")
        f.placeholderString = "Узнать график работы"
        f.font = KrabEarTheme.Typography.body
        f.isEditable = true
        return f
    }()

    // MARK: - UI: Cost estimator preview

    private let costEstimateLabel: NSTextField = {
        let l = NSTextField(labelWithString: "")
        l.font = KrabEarTheme.Typography.caption
        l.textColor = KrabEarTheme.Colors.textSecondary
        l.toolTip = "Примерная стоимость на основе номера и провайдера"
        return l
    }()

    private let startButton = ThemePrimaryButton(title: "Начать звонок", target: nil, action: nil)

    // MARK: - UI: Active call panel

    private let activeCallCard: NSVisualEffectView = {
        let v = NSVisualEffectView()
        v.material = .popover
        v.blendingMode = .behindWindow
        v.state = .active
        v.wantsLayer = true
        v.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        v.layer?.cornerCurve = .continuous
        v.translatesAutoresizingMaskIntoConstraints = false
        return v
    }()
    private let statusBadgeLabel: NSTextField = {
        let l = NSTextField(labelWithString: "Ожидание")
        l.font = KrabEarTheme.Typography.captionMedium
        l.textColor = .secondaryLabelColor
        return l
    }()
    private let durationLabel: NSTextField = {
        let l = NSTextField(labelWithString: "00:00")
        l.font = .monospacedDigitSystemFont(ofSize: 13, weight: .medium)
        l.textColor = KrabEarTheme.Colors.textPrimary
        l.isHidden = true
        return l
    }()
    private let costLabel: NSTextField = {
        let l = NSTextField(labelWithString: "")
        l.font = KrabEarTheme.Typography.caption
        l.textColor = KrabEarTheme.Colors.textSecondary
        l.isHidden = true
        return l
    }()

    private let transcriptScrollView: NSScrollView = {
        let sv = NSScrollView()
        sv.hasVerticalScroller = true
        sv.borderType = .noBorder
        sv.translatesAutoresizingMaskIntoConstraints = false
        sv.heightAnchor.constraint(equalToConstant: 120).isActive = true
        return sv
    }()
    private let transcriptTextView: NSTextView = {
        let tv = NSTextView()
        tv.isEditable = false
        tv.isSelectable = true
        tv.backgroundColor = .clear
        tv.drawsBackground = false
        tv.font = KrabEarTheme.Typography.body
        tv.textContainerInset = NSSize(width: 4, height: 4)
        return tv
    }()

    private let hangupButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Положить трубку", target: nil, action: nil)
        b.isHidden = true
        return b
    }()
    private let interveneButton: ThemeSecondaryButton = {
        let b = ThemeSecondaryButton(title: "Вмешаться", target: nil, action: nil)
        b.isHidden = true
        b.toolTip = "Заглушает бота, даёт оператору говорить напрямую"
        return b
    }()

    // MARK: - UI: Emergency stop button

    /// Большая красная кнопка — экстренный hangup без confirmation dialog.
    private let emergencyStopButton: NSButton = {
        let b = NSButton(title: "🛑 ЭКСТРЕННО ПРЕРВАТЬ", target: nil, action: nil)
        b.bezelStyle = .rounded
        b.font = .systemFont(ofSize: 13, weight: .bold)
        b.contentTintColor = .white
        b.wantsLayer = true
        b.layer?.backgroundColor = NSColor.systemRed.cgColor
        b.layer?.cornerRadius = 8
        b.isHidden = true
        b.translatesAutoresizingMaskIntoConstraints = false
        b.toolTip = "Немедленно завершить звонок без подтверждения"
        return b
    }()

    // MARK: - UI: Config-not-set banner

    private let configBannerCard: NSVisualEffectView = {
        let v = NSVisualEffectView()
        v.material = .popover
        v.blendingMode = .behindWindow
        v.state = .active
        v.wantsLayer = true
        v.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        v.layer?.cornerCurve = .continuous
        v.layer?.borderWidth = 0.5
        v.translatesAutoresizingMaskIntoConstraints = false
        v.isHidden = true
        return v
    }()
    private let configBannerLabel: NSTextField = {
        let l = NSTextField(wrappingLabelWithString:
            "Настройте Telnyx API key в разделе «Автозвонки» в Настройках, затем вернитесь сюда.")
        l.font = KrabEarTheme.Typography.body
        l.textColor = .secondaryLabelColor
        l.translatesAutoresizingMaskIntoConstraints = false
        return l
    }()

    // MARK: - UI: History

    private let historyLabel: NSTextField = {
        let l = NSTextField(labelWithString: "ПОСЛЕДНИЕ ЗВОНКИ")
        l.font = .systemFont(ofSize: 10, weight: .semibold)
        l.textColor = KrabEarTheme.Colors.textSecondary
        return l
    }()
    private let historyTableView = NSTableView()
    private let historyScrollView: NSScrollView = {
        let sv = NSScrollView()
        sv.hasVerticalScroller = true
        sv.borderType = .noBorder
        sv.translatesAutoresizingMaskIntoConstraints = false
        return sv
    }()

    // MARK: - Init

    init(ipcClient: IPCClient) {
        self.ipcClient = ipcClient
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:)")
    }

    // MARK: - Lifecycle

    override func loadView() {
        self.view = NSView()
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        buildUI()
        wireTargets()
        updateSessionUI(session: nil)
        loadCallHistory()
        refreshProviderStatus()
    }

    // MARK: - Build UI

    private func buildUI() {
        view.wantsLayer = true

        let scrollOuter = NSScrollView()
        scrollOuter.hasVerticalScroller = true
        scrollOuter.drawsBackground = false
        scrollOuter.borderType = .noBorder
        scrollOuter.translatesAutoresizingMaskIntoConstraints = false

        let outerStack = NSStackView()
        outerStack.orientation = .vertical
        outerStack.spacing = KrabEarTheme.Metrics.standard
        outerStack.alignment = .leading
        outerStack.translatesAutoresizingMaskIntoConstraints = false
        outerStack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)

        // ---- Provider switcher row ----

        let providerRow = NSStackView(views: [providerSegmented, providerStatusDot, NSView()])
        providerRow.orientation = .horizontal
        providerRow.spacing = 6
        providerRow.alignment = .centerY
        outerStack.addArrangedSubview(providerRow)

        // ---- Emergency stop (fixed above input, visible during active call) ----

        outerStack.addArrangedSubview(emergencyStopButton)
        emergencyStopButton.heightAnchor.constraint(equalToConstant: 36).isActive = true

        // ---- Input section ----

        let inputCard = makeCard()
        let inputStack = NSStackView()
        inputStack.orientation = .vertical
        inputStack.spacing = KrabEarTheme.Metrics.tight
        inputStack.alignment = .leading
        inputStack.translatesAutoresizingMaskIntoConstraints = false

        let phoneLabelRow = makeRow(label: phoneLabel, control: phoneField)
        inputStack.addArrangedSubview(phoneLabelRow)
        inputStack.addArrangedSubview(phoneValidationLabel)
        inputStack.addArrangedSubview(costEstimateLabel)
        let goalLabelRow = makeRow(label: goalLabel, control: goalField)
        inputStack.addArrangedSubview(goalLabelRow)

        startButton.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        inputStack.addArrangedSubview(startButton)

        let inputCardInner = inputCard.subviews[0]
        inputCardInner.addSubview(inputStack)
        NSLayoutConstraint.activate([
            inputStack.topAnchor.constraint(equalTo: inputCardInner.topAnchor, constant: 12),
            inputStack.leadingAnchor.constraint(equalTo: inputCardInner.leadingAnchor, constant: 12),
            inputStack.trailingAnchor.constraint(equalTo: inputCardInner.trailingAnchor, constant: -12),
            inputStack.bottomAnchor.constraint(equalTo: inputCardInner.bottomAnchor, constant: -12),
        ])
        outerStack.addArrangedSubview(inputCard)

        // ---- Config banner ----

        configBannerCard.layer?.borderColor = NSColor.systemOrange.withAlphaComponent(0.4).cgColor
        let bannerInner = makeCardInner()
        bannerInner.addSubview(configBannerLabel)
        NSLayoutConstraint.activate([
            configBannerLabel.topAnchor.constraint(equalTo: bannerInner.topAnchor, constant: 10),
            configBannerLabel.leadingAnchor.constraint(equalTo: bannerInner.leadingAnchor, constant: 12),
            configBannerLabel.trailingAnchor.constraint(equalTo: bannerInner.trailingAnchor, constant: -12),
            configBannerLabel.bottomAnchor.constraint(equalTo: bannerInner.bottomAnchor, constant: -10),
        ])
        configBannerCard.addSubview(bannerInner)
        NSLayoutConstraint.activate([
            bannerInner.topAnchor.constraint(equalTo: configBannerCard.topAnchor),
            bannerInner.leadingAnchor.constraint(equalTo: configBannerCard.leadingAnchor),
            bannerInner.trailingAnchor.constraint(equalTo: configBannerCard.trailingAnchor),
            bannerInner.bottomAnchor.constraint(equalTo: configBannerCard.bottomAnchor),
        ])
        outerStack.addArrangedSubview(configBannerCard)

        // ---- Active call card ----

        let activeCardInner = makeCardInner()
        activeCallCard.addSubview(activeCardInner)
        NSLayoutConstraint.activate([
            activeCardInner.topAnchor.constraint(equalTo: activeCallCard.topAnchor),
            activeCardInner.leadingAnchor.constraint(equalTo: activeCallCard.leadingAnchor),
            activeCardInner.trailingAnchor.constraint(equalTo: activeCallCard.trailingAnchor),
            activeCardInner.bottomAnchor.constraint(equalTo: activeCallCard.bottomAnchor),
        ])

        let activeStack = NSStackView()
        activeStack.orientation = .vertical
        activeStack.spacing = KrabEarTheme.Metrics.tight
        activeStack.alignment = .leading
        activeStack.translatesAutoresizingMaskIntoConstraints = false
        activeStack.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)

        // Status row
        let statusRow = NSStackView(views: [statusBadgeLabel, NSView(), durationLabel, costLabel])
        statusRow.orientation = .horizontal
        statusRow.spacing = KrabEarTheme.Metrics.standard
        statusRow.alignment = .centerY

        let transcriptHeader = NSTextField(labelWithString: "Транскрипт:")
        transcriptHeader.font = KrabEarTheme.Typography.captionMedium
        transcriptHeader.textColor = KrabEarTheme.Colors.textSecondary

        transcriptScrollView.documentView = transcriptTextView
        transcriptTextView.autoresizingMask = [.width]

        // Buttons row
        let callButtonsRow = NSStackView(views: [hangupButton, interveneButton, NSView()])
        callButtonsRow.orientation = .horizontal
        callButtonsRow.spacing = KrabEarTheme.Metrics.standard
        callButtonsRow.alignment = .centerY

        activeStack.addArrangedSubview(statusRow)
        activeStack.addArrangedSubview(transcriptHeader)
        activeStack.addArrangedSubview(transcriptScrollView)
        activeStack.addArrangedSubview(callButtonsRow)

        activeCardInner.addSubview(activeStack)
        NSLayoutConstraint.activate([
            activeStack.topAnchor.constraint(equalTo: activeCardInner.topAnchor),
            activeStack.leadingAnchor.constraint(equalTo: activeCardInner.leadingAnchor),
            activeStack.trailingAnchor.constraint(equalTo: activeCardInner.trailingAnchor),
            activeStack.bottomAnchor.constraint(equalTo: activeCardInner.bottomAnchor),
            transcriptScrollView.widthAnchor.constraint(equalTo: activeStack.widthAnchor),
        ])

        outerStack.addArrangedSubview(activeCallCard)

        // ---- History ----

        outerStack.addArrangedSubview(historyLabel)

        historyTableView.style = .inset
        historyTableView.intercellSpacing = NSSize(width: 0, height: 2)
        historyTableView.rowHeight = 32
        historyTableView.backgroundColor = .clear
        historyTableView.gridStyleMask = []
        historyTableView.dataSource = self
        historyTableView.delegate = self
        historyTableView.doubleAction = #selector(onHistoryRowDoubleClick)
        historyTableView.target = self

        // Columns: Date/Time | To | Duration | Cost | Status | Goal
        let colDate = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("date"))
        colDate.title = "Дата"
        colDate.width = 100

        let colPhone = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("phone"))
        colPhone.title = "Куда"
        colPhone.width = 120

        let colDuration = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("duration"))
        colDuration.title = "Длит."
        colDuration.width = 60

        let colCost = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("cost"))
        colCost.title = "Стоим."
        colCost.width = 65

        let colStatus = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("status"))
        colStatus.title = "Ст."
        colStatus.width = 30

        let colGoal = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("goal"))
        colGoal.title = "Цель"
        colGoal.width = 160

        for col in [colDate, colPhone, colDuration, colCost, colStatus, colGoal] {
            historyTableView.addTableColumn(col)
        }

        historyTableView.headerView = NSTableHeaderView()

        historyScrollView.documentView = historyTableView
        historyScrollView.heightAnchor.constraint(equalToConstant: 200).isActive = true
        outerStack.addArrangedSubview(historyScrollView)

        // Set widths
        for sub in [inputCard, configBannerCard, activeCallCard, historyScrollView] {
            sub.widthAnchor.constraint(equalTo: outerStack.widthAnchor).isActive = true
        }
        for sub in [historyLabel, providerRow, emergencyStopButton] {
            sub.widthAnchor.constraint(equalTo: outerStack.widthAnchor).isActive = true
        }

        // Embed in outer scroll
        scrollOuter.documentView = outerStack

        view.addSubview(scrollOuter)
        NSLayoutConstraint.activate([
            scrollOuter.topAnchor.constraint(equalTo: view.topAnchor),
            scrollOuter.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollOuter.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollOuter.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            outerStack.widthAnchor.constraint(equalTo: scrollOuter.widthAnchor),
        ])
    }

    // MARK: - View helpers

    private func makeCard() -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = .popover
        v.blendingMode = .behindWindow
        v.state = .active
        v.wantsLayer = true
        v.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        v.layer?.cornerCurve = .continuous
        v.layer?.borderWidth = 0.5
        v.layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        v.translatesAutoresizingMaskIntoConstraints = false
        let inner = NSView()
        inner.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(inner)
        NSLayoutConstraint.activate([
            inner.topAnchor.constraint(equalTo: v.topAnchor),
            inner.leadingAnchor.constraint(equalTo: v.leadingAnchor),
            inner.trailingAnchor.constraint(equalTo: v.trailingAnchor),
            inner.bottomAnchor.constraint(equalTo: v.bottomAnchor),
        ])
        return v
    }

    private func makeCardInner() -> NSView {
        let v = NSView()
        v.translatesAutoresizingMaskIntoConstraints = false
        return v
    }

    private func makeRow(label: NSView, control: NSView) -> NSStackView {
        label.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        let row = NSStackView(views: [label, control])
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.standard
        row.alignment = .centerY
        control.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        return row
    }

    // MARK: - Targets

    private func wireTargets() {
        startButton.target = self
        startButton.action = #selector(onStartCall)
        hangupButton.target = self
        hangupButton.action = #selector(onHangup)
        interveneButton.target = self
        interveneButton.action = #selector(onIntervene)
        phoneField.target = self
        phoneField.action = #selector(onPhoneFieldCommit)
        phoneField.delegate = self as? NSTextFieldDelegate
        providerSegmented.target = self
        providerSegmented.action = #selector(onProviderChanged)
        emergencyStopButton.target = self
        emergencyStopButton.action = #selector(onEmergencyStop)
    }

    // MARK: - Actions

    @objc private func onPhoneFieldCommit() {
        validateAndPreviewCost()
    }

    /// Вызывается после каждого изменения номера: валидация + cost preview
    private func validateAndPreviewCost() {
        let raw = phoneField.stringValue.trimmingCharacters(in: .whitespaces)
        let valid = isValidE164(raw)

        // Обводка
        phoneField.layer?.borderWidth = 0
        if raw.isEmpty {
            phoneValidationLabel.stringValue = ""
            costEstimateLabel.stringValue = ""
        } else if valid {
            phoneValidationLabel.stringValue = "✓ Корректный формат E.164"
            phoneValidationLabel.textColor = .systemGreen
            phoneField.layer?.borderWidth = 0
            fetchCostEstimate(phone: raw)
        } else {
            phoneValidationLabel.stringValue = "Use E.164 format: +34612345678"
            phoneValidationLabel.textColor = .systemRed
            phoneField.wantsLayer = true
            phoneField.layer?.borderWidth = 1.5
            phoneField.layer?.borderColor = NSColor.systemRed.cgColor
            phoneField.layer?.cornerRadius = 3
            phoneField.toolTip = "Use E.164 format: +34612345678"
            costEstimateLabel.stringValue = ""
        }
        if raw.isEmpty || valid {
            phoneField.layer?.borderWidth = 0
            phoneField.toolTip = nil
        }
    }

    /// IPC вызов call_estimate_cost → показывает ~$X.XXX/min (Country, Provider)
    private func fetchCostEstimate(phone: String) {
        let provider = selectedProvider.settingKey
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let result = try? self.ipcClient.call(
                method: "call_estimate_cost",
                params: ["phone": phone, "provider": provider]
            )
            DispatchQueue.main.async {
                self.applyCostEstimate(result)
            }
        }
    }

    private func applyCostEstimate(_ response: [String: Any]?) {
        guard let result = response?["result"] as? [String: Any] else {
            costEstimateLabel.stringValue = ""
            return
        }
        let costPerMin  = (result["minute_rate_usd"] as? Double) ?? 0
        let country     = (result["destination"] as? String) ?? ""
        let provider    = (result["provider"] as? String) ?? selectedProvider.settingKey.capitalized

        if costPerMin > 0 {
            let providerTitle = provider.capitalized
            let countryStr = country.isEmpty ? "" : " \(country),"
            costEstimateLabel.stringValue = String(format: "~$%.3f/min (%@%@ %@)", costPerMin, countryStr.isEmpty ? "" : "", country, providerTitle)
            // Более читаемый вариант
            if country.isEmpty {
                costEstimateLabel.stringValue = String(format: "~$%.3f/min (%@)", costPerMin, providerTitle)
            } else {
                costEstimateLabel.stringValue = String(format: "~$%.3f/min (%@, %@)", costPerMin, country, providerTitle)
            }
        } else {
            costEstimateLabel.stringValue = ""
        }
    }

    @objc private func onStartCall() {
        let phone = phoneField.stringValue.trimmingCharacters(in: .whitespaces)
        let goal  = goalField.stringValue.trimmingCharacters(in: .whitespaces)

        guard isValidE164(phone) else {
            phoneValidationLabel.stringValue = "Use E.164 format: +34612345678"
            phoneValidationLabel.textColor = .systemRed
            phoneField.wantsLayer = true
            phoneField.layer?.borderWidth = 1.5
            phoneField.layer?.borderColor = NSColor.systemRed.cgColor
            phoneField.layer?.cornerRadius = 3
            return
        }

        startButton.isEnabled = false
        startButton.title = "Набираем..."

        let providerKey = self.selectedProvider.settingKey
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            // 🔴 До 03.09.2026 кнопка звала call_session_create — журнальную
            // запись, которая НЕ звонила: дороги от IPC до провайдера не
            // существовало. call_start — настоящий набор через Voice Gateway.
            let result = try? self.ipcClient.call(
                method: "call_start",
                params: ["phone": phone, "goal_text": goal, "prompt": goal,
                         "provider": providerKey]
            )
            DispatchQueue.main.async {
                self.handleDialResponse(result, phone: phone, goal: goal)
            }
        }
    }

    @objc private func onHangup() {
        guard let sessionID = currentSession?.sessionID else { return }
        hangupButton.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let _ = try? self.ipcClient.call(
                method: "call_session_end",
                params: ["id": sessionID, "reason": "completed"]
            )
            DispatchQueue.main.async {
                self.stopSessionPolling()
                var s = self.currentSession
                s?.status = .ended
                s?.endedAt = Date()
                self.currentSession = s
                self.updateSessionUI(session: self.currentSession)
                self.loadCallHistory()
            }
        }
    }

    /// Экстренная остановка — hangup БЕЗ confirmation dialog
    @objc private func onEmergencyStop() {
        guard let sessionID = currentSession?.sessionID else { return }
        emergencyStopButton.isEnabled = false
        stopSessionPolling()
        var s = currentSession
        s?.status = .ending
        currentSession = s
        updateSessionUI(session: currentSession)

        DispatchQueue.global(qos: .userInteractive).async { [weak self] in
            guard let self else { return }
            let _ = try? self.ipcClient.call(
                method: "call_session_end",
                params: ["id": sessionID, "reason": "completed"]
            )
            DispatchQueue.main.async {
                var s2 = self.currentSession
                s2?.status = .ended
                s2?.endedAt = Date()
                self.currentSession = s2
                self.updateSessionUI(session: self.currentSession)
                self.emergencyStopButton.isEnabled = true
                self.loadCallHistory()
            }
        }
    }

    @objc private func onIntervene() {
        guard let sessionID = currentSession?.sessionID else { return }
        let isActive = interveneButton.title == "Вмешаться"
        let method = isActive ? "call_intervene" : "call_resume_bot"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let _ = try? self.ipcClient.call(
                method: method,
                params: ["session_id": sessionID]
            )
            DispatchQueue.main.async {
                self.interveneButton.title = isActive ? "Передать боту" : "Вмешаться"
            }
        }
    }

    @objc private func onProviderChanged() {
        // Выбор из одного: провайдер остаётся шлюзом при любом сегменте.
        selectedProvider = .gateway
        refreshProviderStatus()
        // Если уже введён корректный номер — обновить estimate
        let raw = phoneField.stringValue.trimmingCharacters(in: .whitespaces)
        if isValidE164(raw) { fetchCostEstimate(phone: raw) }
    }

    /// Click по строке истории → показать полный транскрипт в modal
    @objc private func onHistoryRowDoubleClick() {
        let row = historyTableView.clickedRow
        guard row >= 0 && row < callHistory.count else { return }
        let item = callHistory[row]
        showTranscriptModal(for: item)
    }

    // MARK: - Provider status

    /// Опрашивает IPC get_settings → проверяет настроены ли параметры провайдера
    private func refreshProviderStatus() {
        let provider = selectedProvider.settingKey
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let result = try? self.ipcClient.call(method: "get_settings", params: [:])
            DispatchQueue.main.async {
                self.applyProviderStatus(result, provider: provider)
            }
        }
    }

    private func applyProviderStatus(_ response: [String: Any]?, provider: String) {
        guard let settings = response?["result"] as? [String: Any] else { return }
        // Готовность = настройки Voice Gateway. Ключи удалённых адаптеров
        // (telnyx_*, twilio_*, sip_*) больше ничего не значат.
        let url = (settings["voice_gateway_url"] as? String) ?? ""
        let key = (settings["voice_gateway_api_key"] as? String) ?? ""
        let isConfigured = !url.isEmpty && !key.isEmpty
        providerStatusDot.contentTintColor = isConfigured ? NSColor.systemGreen : NSColor.systemGray
        providerStatusDot.toolTip = isConfigured
            ? "Voice Gateway настроен"
            : "Voice Gateway: адрес или ключ не заданы в Настройках"
    }

    // MARK: - Transcript modal

    private func showTranscriptModal(for item: CallHistoryItem) {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 520, height: 380),
                           styleMask: [.titled, .closable, .resizable],
                           backing: .buffered, defer: false)
        panel.title = "Транскрипт — \(item.phone)"
        panel.isReleasedWhenClosed = false

        let scroll = NSScrollView(frame: NSRect(x: 12, y: 48, width: 496, height: 320))
        scroll.autoresizingMask = [.width, .height]
        let tv = NSTextView(frame: scroll.bounds)
        tv.isEditable = false
        let text = item.summary.isEmpty ? "(нет записей транскрипта)" : item.summary
        tv.string = "Цель: \(item.goal)\nНомер: \(item.phone)\nДлительность: \(item.durationFormatted)\nСтоимость: \(item.costUSD > 0 ? String(format: "$%.3f", item.costUSD) : "—")\n\n\(text)"
        tv.font = KrabEarTheme.Typography.body
        scroll.documentView = tv

        let closeBtn = ThemeSecondaryButton(title: "Закрыть", target: nil, action: nil)
        closeBtn.frame = NSRect(x: 420, y: 12, width: 88, height: 28)
        closeBtn.target = self
        closeBtn.action = #selector(onCloseTranscriptModal(_:))

        panel.contentView?.addSubview(scroll)
        panel.contentView?.addSubview(closeBtn)
        panel.center()
        panel.makeKeyAndOrderFront(nil)
    }

    @objc private func onCloseTranscriptModal(_ sender: NSButton) {
        sender.window?.close()
    }

    // MARK: - Dial response handler

    private func handleDialResponse(_ response: [String: Any]?, phone: String, goal: String) {
        startButton.isEnabled = true
        startButton.title = "Начать звонок"

        guard let response else {
            showError("Нет ответа от backend. Убедитесь, что сервис запущен.")
            return
        }

        // Не настроен провайдер — показать баннер, а не молчать.
        // 🔴 Коды сверены с backend буква в букву: gateway_not_configured
        // возвращает GatewayCallProvider, call_provider_unavailable —
        // CallSessionService, когда фабрика не отдала провайдера вовсе.
        // Коды удалённых адаптеров (telnyx_/twilio_/sip_local_not_configured)
        // больше не приходят.
        let notConfigured: Set<String> = ["gateway_not_configured", "call_provider_unavailable"]
        if let error = response["error"] as? [String: Any],
           let code = error["code"] as? String, notConfigured.contains(code) {
            showConfigBanner(true)
            return
        }
        if let result = response["result"] as? [String: Any],
           (result["ok"] as? Bool) == false,
           let code = result["error"] as? String, notConfigured.contains(code) {
            // call_start отвечает отказом В PAYLOAD, а не конвертом ошибки:
            // проверять только error-конверт значило бы пропустить этот случай.
            showConfigBanner(true)
            return
        }

        if let result = response["result"] as? [String: Any] {
            let sessionID = (result["session_id"] as? String) ?? UUID().uuidString
            let statusRaw  = (result["status"] as? String) ?? "dialing"
            let session = CallSession(
                sessionID: sessionID,
                status: CallSession.Status(rawValue: statusRaw) ?? .dialing,
                phone: phone,
                goal: goal,
                startedAt: Date(),
                endedAt: nil,
                transcript: "",
                costUSD: 0,
                errorMessage: nil
            )
            currentSession = session
            updateSessionUI(session: session)
            startSessionPolling(sessionID: sessionID)
        } else {
            showError("Не удалось начать звонок. Проверьте настройки Telnyx.")
        }
    }

    // MARK: - Session polling

    private func startSessionPolling(sessionID: String) {
        durationTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updateDurationLabel() }
        }
        pollTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.pollSessionStatus(sessionID: sessionID) }
        }
    }

    private func stopSessionPolling() {
        durationTimer?.invalidate()
        durationTimer = nil
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func pollSessionStatus(sessionID: String) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let result = try? self.ipcClient.call(
                method: "call_session_get",
                params: ["id": sessionID]
            )
            DispatchQueue.main.async {
                self.applySessionPoll(result)
            }
        }
    }

    private func applySessionPoll(_ response: [String: Any]?) {
        guard var session = currentSession,
              let result = response?["result"] as? [String: Any] else { return }

        let statusRaw  = (result["status"] as? String) ?? session.status.rawValue
        let entries = result["transcript_history"] as? [[String: Any]] ?? []
        let transcript = entries.isEmpty
            ? session.transcript
            : entries.map { "\($0["speaker"] as? String ?? "?"): \($0["text"] as? String ?? "")" }.joined(separator: "\n")
        let cost       = (result["cost_usd"] as? Double) ?? session.costUSD

        session.status    = CallSession.Status(rawValue: statusRaw) ?? session.status
        session.transcript = transcript
        session.costUSD   = cost

        if session.status == .ended || session.status == .error {
            session.endedAt = Date()
            stopSessionPolling()
            loadCallHistory()
        }

        currentSession = session
        updateSessionUI(session: session)
    }

    // MARK: - UI update

    private func updateSessionUI(session: CallSession?) {
        guard let session else {
            statusBadgeLabel.stringValue = "●  Ожидание"
            statusBadgeLabel.textColor = .secondaryLabelColor
            durationLabel.isHidden = true
            costLabel.isHidden = true
            hangupButton.isHidden = true
            interveneButton.isHidden = true
            emergencyStopButton.isHidden = true
            transcriptTextView.string = ""
            return
        }

        statusBadgeLabel.stringValue = "●  \(session.status.displayTitle)"
        statusBadgeLabel.textColor = session.status.badgeColor
        durationLabel.isHidden = session.startedAt == nil
        costLabel.isHidden = session.costUSD == 0

        if session.costUSD > 0 {
            costLabel.stringValue = String(format: "$%.3f", session.costUSD)
        }

        transcriptTextView.string = session.transcript.isEmpty
            ? (session.status == .idle ? "" : "Транскрипт появится здесь...")
            : session.transcript

        let isActive = session.status == .dialing || session.status == .connected || session.status == .talking
        hangupButton.isHidden = !isActive
        interveneButton.isHidden = !isActive
        hangupButton.isEnabled = true

        // Кнопка экстренного прерывания — видна при ЛЮБОМ активном звонке
        emergencyStopButton.isHidden = !isActive
    }

    private func updateDurationLabel() {
        guard let startedAt = currentSession?.startedAt,
              currentSession?.endedAt == nil else { return }
        let elapsed = Int(Date().timeIntervalSince(startedAt))
        let m = elapsed / 60
        let s = elapsed % 60
        durationLabel.stringValue = String(format: "%02d:%02d", m, s)
    }

    // MARK: - History

    private func loadCallHistory() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let response = try? self.ipcClient.call(
                method: "call_session_list",
                params: ["limit": 10]
            )
            DispatchQueue.main.async {
                self.applyHistoryResponse(response)
            }
        }
    }

    private func applyHistoryResponse(_ response: [String: Any]?) {
        guard let items = (response?["result"] as? [String: Any])?["sessions"] as? [[String: Any]] else {
            return
        }
        callHistory = items.compactMap { dict -> CallHistoryItem? in
            guard let id = dict["id"] as? String else { return nil }
            return CallHistoryItem(
                sessionID: id,
                phone:    (dict["phone_number"] as? String) ?? "",
                goal:     (dict["goal_text"]    as? String) ?? "",
                status:   (dict["status"]   as? String) ?? "unknown",
                durationSec: (dict["duration_sec"] as? Double) ?? 0,
                costUSD:  (dict["cost_usd"] as? Double) ?? 0,
                startedAt:(dict["started_at"] as? String) ?? "",
                summary:  (dict["summary"]  as? String) ?? ""
            )
        }
        historyTableView.reloadData()
    }

    // MARK: - Helpers

    func isValidE164(_ s: String) -> Bool {
        let pattern = #"^\+[1-9]\d{6,14}$"#
        return s.range(of: pattern, options: .regularExpression) != nil
    }

    private func showError(_ msg: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Ошибка автозвонка"
        alert.informativeText = msg
        alert.addButton(withTitle: "OK")
        if let window = view.window {
            alert.beginSheetModal(for: window, completionHandler: nil)
        } else {
            // Без окна runModal() блокирует main thread → AppHang (KRAB-EAR-AGENT-E).
            // Call automation может выполняться в фоне, окно может быть не на переднем плане.
            NSLog("[KrabEar] CallAutomation showError bail (no window): %@", msg)
        }
    }

    func showConfigBanner(_ show: Bool) {
        if show {
            let providerName = selectedProvider.settingKey.capitalized
            configBannerLabel.stringValue = "Настройте параметры \(providerName) в разделе «Автозвонки» в Настройках, затем вернитесь сюда."
        }
        configBannerCard.isHidden = !show
    }
}

// MARK: - NSTableViewDataSource & Delegate

extension CallAutomationController: NSTableViewDataSource, NSTableViewDelegate {

    func numberOfRows(in tableView: NSTableView) -> Int { callHistory.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let item = callHistory[row]
        let cell = NSTableCellView()
        let label = NSTextField(labelWithString: "")
        label.font = KrabEarTheme.Typography.caption
        label.translatesAutoresizingMaskIntoConstraints = false
        label.lineBreakMode = .byTruncatingTail
        cell.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])

        switch tableColumn?.identifier.rawValue {
        case "date":
            // Показываем дату+время если есть
            let dateStr = item.startedAt
            label.stringValue = dateStr.count >= 16 ? String(dateStr.prefix(16)) : String(dateStr.prefix(10))

        case "phone":
            label.stringValue = item.phone

        case "duration":
            label.stringValue = item.durationFormatted

        case "cost":
            label.stringValue = item.costUSD > 0 ? String(format: "$%.3f", item.costUSD) : "—"

        case "status":
            // Иконка статуса
            let st = CallSession.Status(rawValue: item.status)
            let icon = st?.historyIcon ?? "•"
            label.stringValue = icon
            label.textColor = st?.historyIconColor ?? .secondaryLabelColor
            label.font = .systemFont(ofSize: 14)
            label.alignment = .center

        case "goal":
            label.stringValue = item.goal

        default:
            break
        }
        return cell
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat { 28 }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { true }
}
