/*
 CallAutomationController — NSViewController для вкладки «Автозвонки».

 Отправляет IPC-команды (call_dial, call_hangup, call_intervene) и отображает
 активную сессию: статус, живой транскрипт, длительность, текущую стоимость,
 историю последних 10 звонков.

 Если backend возвращает "telnyx_not_configured" → показывает баннер настройки.
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
}

// MARK: - Controller

@MainActor
final class CallAutomationController: NSViewController {

    // MARK: - Dependencies

    nonisolated(unsafe) let ipcClient: IPCClient

    // MARK: - State

    private var currentSession: CallSession?
    private var callHistory: [CallHistoryItem] = []
    private var durationTimer: Timer?
    private var pollTimer: Timer?

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
        let l = NSTextField(labelWithString: "●  Ожидание")
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
        historyTableView.rowHeight = 40
        historyTableView.headerView = nil
        historyTableView.backgroundColor = .clear
        historyTableView.gridStyleMask = []
        historyTableView.dataSource = self
        historyTableView.delegate = self

        let colPhone = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("phone"))
        colPhone.title = "Номер"
        colPhone.width = 120
        let colGoal = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("goal"))
        colGoal.title = "Цель"
        colGoal.width = 200
        let colStatus = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("status"))
        colStatus.title = "Статус"
        colStatus.width = 80
        let colCost = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("cost"))
        colCost.title = "Стоимость"
        colCost.width = 80
        let colDate = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("date"))
        colDate.title = "Дата"
        colDate.width = 120
        for col in [colPhone, colGoal, colStatus, colCost, colDate] {
            historyTableView.addTableColumn(col)
        }

        historyScrollView.documentView = historyTableView
        historyScrollView.heightAnchor.constraint(equalToConstant: 200).isActive = true
        outerStack.addArrangedSubview(historyScrollView)

        // Set widths
        for sub in [inputCard, configBannerCard, activeCallCard, historyScrollView] {
            sub.widthAnchor.constraint(equalTo: outerStack.widthAnchor).isActive = true
        }
        historyLabel.widthAnchor.constraint(equalTo: outerStack.widthAnchor).isActive = true

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
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
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
        phoneField.action = #selector(onPhoneFieldChanged)
    }

    // MARK: - Actions

    @objc private func onPhoneFieldChanged() {
        let raw = phoneField.stringValue.trimmingCharacters(in: .whitespaces)
        if raw.isEmpty {
            phoneValidationLabel.stringValue = ""
        } else if isValidE164(raw) {
            phoneValidationLabel.stringValue = "✓ Корректный формат E.164"
            phoneValidationLabel.textColor = .systemGreen
        } else {
            phoneValidationLabel.stringValue = "Введите номер в формате +7XXXXXXXXXX"
            phoneValidationLabel.textColor = .systemRed
        }
    }

    @objc private func onStartCall() {
        let phone = phoneField.stringValue.trimmingCharacters(in: .whitespaces)
        let goal  = goalField.stringValue.trimmingCharacters(in: .whitespaces)

        guard isValidE164(phone) else {
            phoneValidationLabel.stringValue = "Введите номер в формате E.164 (+7XXXXXXXXXX)"
            phoneValidationLabel.textColor = .systemRed
            return
        }

        startButton.isEnabled = false
        startButton.title = "Набираем..."

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = try? self.ipcClient.call(
                method: "call_dial",
                params: ["phone": phone, "goal": goal]
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
                method: "call_hangup",
                params: ["session_id": sessionID]
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

    // MARK: - IPC response handling

    private func handleDialResponse(_ response: [String: Any]?, phone: String, goal: String) {
        startButton.isEnabled = true
        startButton.title = "Начать звонок"

        guard let response else {
            showError("Нет ответа от backend. Убедитесь, что сервис запущен.")
            return
        }

        // Graceful: telnyx_not_configured
        if let error = response["error"] as? [String: Any],
           let code = error["code"] as? String,
           code == "telnyx_not_configured" {
            showConfigBanner(true)
            return
        }

        if let result = response["result"] as? [String: Any] {
            let sessionID = (result["session_id"] as? String) ?? UUID().uuidString
            let statusRaw  = (result["status"] as? String) ?? "dialing"
            var session = CallSession(
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
                method: "call_session_status",
                params: ["session_id": sessionID]
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
        let transcript = (result["transcript"] as? String) ?? session.transcript
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
                method: "list_call_sessions",
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
            guard let id = dict["session_id"] as? String else { return nil }
            return CallHistoryItem(
                sessionID: id,
                phone:    (dict["phone"]    as? String) ?? "",
                goal:     (dict["goal"]     as? String) ?? "",
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

    private func isValidE164(_ s: String) -> Bool {
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
            alert.runModal()
        }
    }

    func showConfigBanner(_ show: Bool) {
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
        cell.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        switch tableColumn?.identifier.rawValue {
        case "phone":  label.stringValue = item.phone
        case "goal":   label.stringValue = item.goal
        case "status": label.stringValue = item.status
        case "cost":
            label.stringValue = item.costUSD > 0 ? String(format: "$%.3f", item.costUSD) : "—"
        case "date":
            label.stringValue = String(item.startedAt.prefix(10))
        default: break
        }
        return cell
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat { 30 }
}
