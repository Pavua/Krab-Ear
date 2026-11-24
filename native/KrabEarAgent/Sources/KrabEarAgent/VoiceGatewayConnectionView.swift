/*
 VoiceGatewayConnectionView — карточка «Подключение к Voice Gateway» во вкладке «Разговор с AI».

 Зачем: владелец получал 401 от шлюза и не нашёл, где вводить ключ (поле жило строкой
 на «Live перевод», сохранялось только по Enter, а проверка «Gateway доступен» по /health
 выглядела как «ключ принят», хотя /health отвечает без ключа).

 Правила:
  - Ключ — в secure-поле, никогда не показывается и не пишется в логи/тесты.
  - Сохранение по потере фокуса и по Enter; ПУСТОЕ поле ключ не стирает (поле не заполняется
    сохранённым ключом — get_settings отдаёт REDACTED, — поэтому пустота = «не менять»).
  - Подписи над полями: на узком окне ничего не сжимается в горизонтальной строке.
  - Итог проверки — три коротких строки; сырой ответ шлюза — только по «Подробности».
  - Вью без IPC/сети: данные и действия — через замыкания, логика проверки — VoiceGatewayCheck.
*/

import AppKit

// MARK: - Итог проверки (чистая логика)

/// Итог проверки шлюза: адрес (по /health) и ключ (по запросу, ТРЕБУЮЩЕМУ авторизацию).
struct VoiceGatewayCheck: Equatable {
    enum Address: Equatable { case reachable, unreachable(String), badResponse(Int) }
    enum Key: Equatable { case accepted, rejected, notRequired, required, unknown }

    let address: Address
    let key: Key

    /// - healthStatus: HTTP-код /health (nil — сетевой сбой, тогда healthError).
    /// - authStatus: HTTP-код GET /v1/settings/conversation (маршрут с _auth_required в VG).
    /// - keyProvided: отправляли ли Bearer.
    static func evaluate(healthStatus: Int?, healthError: String?, authStatus: Int?, keyProvided: Bool) -> VoiceGatewayCheck {
        let address: Address
        if let code = healthStatus {
            address = (200..<300).contains(code) ? .reachable : .badResponse(code)
        } else {
            address = .unreachable(healthError ?? "нет ответа")
        }
        guard case .reachable = address, let auth = authStatus else {
            return VoiceGatewayCheck(address: address, key: .unknown)
        }
        let key: Key
        switch auth {
        case 200..<300: key = keyProvided ? .accepted : .notRequired
        case 401, 403: key = keyProvided ? .rejected : .required
        default: key = .unknown
        }
        return VoiceGatewayCheck(address: address, key: key)
    }

    var addressLine: String {
        switch address {
        case .reachable: return "Адрес: шлюз доступен"
        case .unreachable(let why): return "Адрес: шлюз недоступен (\(why))"
        case .badResponse(let code): return "Адрес: шлюз ответил ошибкой HTTP \(code)"
        }
    }

    var keyLine: String {
        switch key {
        case .accepted: return "Ключ: принят"
        case .rejected: return "Ключ: отклонён шлюзом — проверьте ключ"
        case .notRequired: return "Ключ: шлюз не требует ключ"
        case .required: return "Ключ: нужен — шлюз отвечает 401 без ключа"
        case .unknown: return "Ключ: не проверен"
        }
    }

    /// Можно ли разговаривать с AI с текущими настройками.
    var isReady: Bool {
        guard case .reachable = address else { return false }
        return key == .accepted || key == .notRequired
    }

    var summaryLine: String {
        isReady ? "Итог: всё готово к разговору с AI" : "Итог: разговор с AI работать не будет"
    }
}

// MARK: - Карточка

@MainActor
final class VoiceGatewayConnectionView: NSView, NSTextFieldDelegate {
    /// Состояние сохранённого ключа (по get_voice_gateway_credential, без раскрытия значения).
    enum KeyStatus: Equatable { case unknown, saved, missing }

    var onCommitURL: ((String) -> Void)?
    var onCommitKey: ((String) -> Void)?
    var onCheck: (() -> Void)?
    var onShowDetails: (() -> Void)?

    let urlField = NSTextField(frame: .zero)
    let keyField = NSSecureTextField(frame: .zero)
    let pasteButton = NSButton(title: "Вставить из буфера", target: nil, action: nil)
    let checkButton = NSButton(title: "Проверить подключение", target: nil, action: nil)
    let detailsButton = NSButton(title: "Подробности", target: nil, action: nil)
    private let keyStatusLabel = NSTextField(labelWithString: "")
    private let resultLabel = NSTextField(wrappingLabelWithString: "")
    private let card = ThemeCardView()
    private(set) var keyStatus: KeyStatus = .unknown
    private(set) var isChecking = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        card.title = "Подключение к Voice Gateway"
        card.translatesAutoresizingMaskIntoConstraints = false
        addSubview(card)

        urlField.placeholderString = "http://127.0.0.1:8090"
        urlField.font = Self.bodyFont
        urlField.delegate = self
        urlField.target = self
        urlField.action = #selector(onURLEnter)
        urlField.setAccessibilityLabel("Адрес Voice Gateway")

        keyField.font = Self.bodyFont
        keyField.delegate = self
        keyField.target = self
        keyField.action = #selector(onKeyEnter)
        keyField.setAccessibilityLabel("Ключ доступа Voice Gateway")

        for b in [pasteButton, checkButton, detailsButton] {
            b.bezelStyle = .rounded
            b.controlSize = .large
            b.target = self
        }
        pasteButton.action = #selector(onPastePressed)
        pasteButton.setAccessibilityLabel("Вставить ключ из буфера обмена")
        checkButton.action = #selector(onCheckPressed)
        detailsButton.action = #selector(onDetailsPressed)
        detailsButton.isHidden = true

        keyStatusLabel.font = Self.captionFont
        keyStatusLabel.textColor = KrabEarTheme.Colors.textSecondary
        keyStatusLabel.lineBreakMode = .byTruncatingTail
        resultLabel.font = Self.bodyFont
        resultLabel.textColor = KrabEarTheme.Colors.textPrimary
        resultLabel.isHidden = true
        resultLabel.setAccessibilityLabel("Итог проверки подключения")

        let stack = card.contentStackView
        stack.spacing = KrabEarTheme.Metrics.standard
        let urlTitle = Self.caption("Адрес шлюза")
        let keyTitle = Self.caption("Ключ доступа")
        let keyRow = NSStackView(views: [keyField, pasteButton])
        keyRow.orientation = .horizontal
        keyRow.spacing = KrabEarTheme.Metrics.standard
        keyRow.alignment = .centerY
        let actionRow = NSStackView(views: [checkButton, detailsButton])
        actionRow.orientation = .horizontal
        actionRow.spacing = KrabEarTheme.Metrics.standard
        actionRow.alignment = .centerY
        for v in [urlTitle, urlField, keyTitle, keyRow, keyStatusLabel, actionRow, resultLabel] as [NSView] {
            stack.addArrangedSubview(v)
        }
        // Явные ширины детей (урок #2240): иначе вертикальный стек центрирует по intrinsic size.
        var constraints: [NSLayoutConstraint] = [
            card.topAnchor.constraint(equalTo: topAnchor),
            card.leadingAnchor.constraint(equalTo: leadingAnchor),
            card.trailingAnchor.constraint(equalTo: trailingAnchor),
            card.bottomAnchor.constraint(equalTo: bottomAnchor),
            stack.widthAnchor.constraint(equalTo: card.widthAnchor, constant: -2 * KrabEarTheme.Metrics.cardPadding),
            keyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 160),
        ]
        for v in [urlField, keyRow, keyStatusLabel, resultLabel] as [NSView] {
            constraints.append(v.widthAnchor.constraint(equalTo: stack.widthAnchor))
        }
        NSLayoutConstraint.activate(constraints)
        pasteButton.setContentHuggingPriority(.required, for: .horizontal)
        pasteButton.setContentCompressionResistancePriority(.required, for: .horizontal)
        apply(keyStatus: .unknown)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    /// Крупный текст редизайна (15/13 pt). После слияния токенов PR 0 заменить на
    /// Typography.bodyLarge / captionLarge — значения те же.
    private static let bodyFont = NSFont.systemFont(ofSize: 15)
    private static let captionFont = NSFont.systemFont(ofSize: 13)

    private static func caption(_ text: String) -> NSTextField {
        let l = NSTextField(labelWithString: text)
        l.font = .systemFont(ofSize: 13, weight: .medium)
        l.textColor = KrabEarTheme.Colors.textSecondary
        return l
    }

    // MARK: Состояние

    var keyStatusText: String { keyStatusLabel.stringValue }
    var resultText: String { resultLabel.stringValue }

    func apply(keyStatus status: KeyStatus) {
        keyStatus = status
        switch status {
        case .saved:
            keyStatusLabel.stringValue = "Ключ сохранён (скрыт). Чтобы заменить — вставьте новый."
            keyField.placeholderString = "сохранён — вставьте новый, чтобы заменить"
        case .missing:
            keyStatusLabel.stringValue = "Ключ не задан. Если шлюз требует ключ, разговор с AI не заработает."
            keyField.placeholderString = "нужен, если шлюз требует ключ"
        case .unknown:
            keyStatusLabel.stringValue = "Проверяю, сохранён ли ключ…"
            keyField.placeholderString = "ключ доступа"
        }
    }

    func setChecking(_ checking: Bool) {
        isChecking = checking
        checkButton.isEnabled = !checking
        checkButton.title = checking ? "Проверяю…" : "Проверить подключение"
    }

    func apply(check: VoiceGatewayCheck, hasDetails: Bool) {
        // Цветом выделен только итог (и он же сказан словами); строки адреса/ключа — обычным текстом.
        let text = NSMutableAttributedString(
            string: check.addressLine + "\n" + check.keyLine + "\n",
            attributes: [.font: Self.bodyFont, .foregroundColor: KrabEarTheme.Colors.textPrimary])
        text.append(NSAttributedString(string: check.summaryLine, attributes: [
            .font: NSFont.systemFont(ofSize: Self.bodyFont.pointSize, weight: .semibold),
            .foregroundColor: check.isReady ? NSColor.systemGreen : KrabEarTheme.Colors.error,
        ]))
        resultLabel.attributedStringValue = text
        resultLabel.isHidden = false
        detailsButton.isHidden = !hasDetails
    }

    // MARK: Ввод

    @objc private func onURLEnter() { commitURL() }
    @objc private func onKeyEnter() { commitKey() }

    func controlTextDidEndEditing(_ obj: Notification) {
        guard let field = obj.object as? NSTextField else { return }
        if field === keyField { commitKey() } else if field === urlField { commitURL() }
    }

    private var lastCommittedURL: String?

    private func commitURL() {
        let raw = urlField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard raw != lastCommittedURL else { return }
        lastCommittedURL = raw
        onCommitURL?(raw)
    }

    /// Пустое поле не сохраняется: это «не менять», а не «стереть ключ».
    private func commitKey() {
        let raw = keyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return }
        keyField.stringValue = ""
        onCommitKey?(raw)
    }

    /// URL из настроек (без повторного сохранения).
    func setURL(_ url: String) {
        lastCommittedURL = url
        urlField.stringValue = url
    }

    @objc private func onPastePressed() {
        // Читаем буфер только по явному нажатию; значение никуда не логируется.
        guard let text = NSPasteboard.general.string(forType: .string)?
            .trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
            keyStatusLabel.stringValue = "В буфере обмена нет текста."
            return
        }
        keyField.stringValue = text
        commitKey()
    }

    @objc private func onCheckPressed() { onCheck?() }
    @objc private func onDetailsPressed() { onShowDetails?() }
}
