/*
 ConversationStatusOverlay — плавающий HUD статуса «Разговора с AI» (Волна 3c).

 Показывается, когда сессия активна, а главное окно не в фокусе/скрыто —
 пользователь видит «Слушает/Думает/Говорит» поверх любых приложений и может
 прервать ответ кнопкой, не возвращаясь в окно.

 Паттерн: NSPanel floating/non-activating/draggable, позиция в UserDefaults —
 1-в-1 как LiveSubtitlesOverlay (без SSE: данные пушит ConversationViewController
 напрямую через update(state:)/pushLevel(_:)).

 Глиф-гейт: статусные эмодзи берутся ИЗ ConversationState.localizedLabel
 (уже в кодовой базе) — новых non-ASCII глифов файл не вводит.
*/

import AppKit

@MainActor
final class ConversationStatusOverlay: NSObject {

    // MARK: - UI

    private let panel: NSPanel
    private let statusLabel = NSTextField(labelWithString: "⚪ Готов")
    private let levelMeter = MicLevelMeterView(frame: NSRect(x: 0, y: 0, width: 120, height: 18))
    let interruptButton = ThemeSecondaryButton(title: "Прервать", target: nil, action: nil)

    /// Колбэк кнопки «Прервать» — ConversationViewController подвязывает interruptAI().
    var onInterrupt: (() -> Void)?

    private(set) var isVisible = false
    private let positionKey = "KrabEar_ConversationStatusHUDPosition"

    // MARK: - Test hooks

    var _testPanelLevel: NSWindow.Level { panel.level }
    var _testPanelIsDraggable: Bool { panel.isMovableByWindowBackground }
    var _testStatusText: String { statusLabel.stringValue }
    var _testPanelOrigin: NSPoint { panel.frame.origin }

    // MARK: - Init

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 280, height: 64),
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        super.init()
        setupPanel()
        restorePosition()
    }

    // MARK: - Public API

    func show() {
        panel.orderFront(nil)
        isVisible = true
    }

    func hide() {
        panel.orderOut(nil)
        isVisible = false
    }

    /// Обновить статус (вызывается из applyState VC при каждом изменении состояния).
    func update(state: ConversationState) {
        statusLabel.stringValue = state.localizedLabel
        interruptButton.isHidden = (state != .speaking)
    }

    /// Прокинуть нормализованный mic-уровень (из computeAndPushLevel VC).
    func pushLevel(_ normalized: CGFloat) {
        levelMeter.updateLevel(normalized)
    }

    // MARK: - Setup

    private func setupPanel() {
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
        panel.alphaValue = 0.95

        statusLabel.font = KrabEarTheme.Typography.body
        statusLabel.textColor = KrabEarTheme.Colors.textPrimary
        statusLabel.isBordered = false
        statusLabel.drawsBackground = false

        interruptButton.target = self
        interruptButton.action = #selector(onInterruptTapped)
        interruptButton.isHidden = true

        levelMeter.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            levelMeter.widthAnchor.constraint(equalToConstant: 120),
            levelMeter.heightAnchor.constraint(equalToConstant: 18),
        ])

        let row = NSStackView(views: [statusLabel, levelMeter, interruptButton])
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.comfortable
        row.alignment = .centerY
        row.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.comfortable,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.comfortable,
            right: KrabEarTheme.Metrics.spacious
        )
        row.translatesAutoresizingMaskIntoConstraints = false

        let backdrop = NSVisualEffectView()
        backdrop.material = .popover
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.wantsLayer = true
        backdrop.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        backdrop.layer?.cornerCurve = .continuous
        backdrop.layer?.masksToBounds = true

        backdrop.addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: backdrop.topAnchor),
            row.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            row.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            row.bottomAnchor.constraint(equalTo: backdrop.bottomAnchor),
        ])

        backdrop.frame = panel.contentView!.bounds
        backdrop.autoresizingMask = [.width, .height]
        panel.contentView = backdrop

        let drag = NSPanGestureRecognizer(target: self, action: #selector(handleDrag(_:)))
        backdrop.addGestureRecognizer(drag)
    }

    @objc private func onInterruptTapped() {
        onInterrupt?()
    }

    // MARK: - Position persistence (паттерн LiveSubtitlesOverlay + M2-guard RealtimeOverlayController)

    private func placeTopRight() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        let x = vf.maxX - size.width - 24
        let y = vf.maxY - size.height - 24
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }

    /// Валидна ли candidate-позиция — хотя бы 80% площади пересекается с visibleFrame
    /// какого-нибудь ТЕКУЩЕГО экрана. Портировано из
    /// RealtimeOverlayController.restoreSavedPosition() (M2, ~строки 757-782): без этой
    /// проверки отключение второго монитора навсегда прячет панель за экраном — сохранённая
    /// позиция ссылается на уже не существующий экран, а UserDefaults применяется безусловно.
    private func isOnScreen(_ candidate: NSRect) -> Bool {
        let totalArea = candidate.width * candidate.height
        guard totalArea > 0 else { return false }
        return NSScreen.screens.contains { screen in
            let intersection = candidate.intersection(screen.visibleFrame)
            let coveredArea = intersection.width * intersection.height
            return coveredArea / totalArea >= 0.80
        }
    }

    private func restorePosition() {
        if let saved = UserDefaults.standard.string(forKey: positionKey),
           let data = saved.data(using: .utf8),
           let dict = try? JSONSerialization.jsonObject(with: data) as? [String: CGFloat],
           let x = dict["x"], let y = dict["y"] {
            let size = panel.frame.size
            let candidate = NSRect(x: x, y: y, width: size.width, height: size.height)
            if isOnScreen(candidate) {
                panel.setFrame(candidate, display: false)
                return
            }
        }
        // Нет сохранённой позиции ИЛИ она вне видимых экранов (guard выше) — дефолт.
        placeTopRight()
    }

    private func savePosition() {
        let origin = panel.frame.origin
        let dict: [String: CGFloat] = ["x": origin.x, "y": origin.y]
        if let data = try? JSONSerialization.data(withJSONObject: dict),
           let str = String(data: data, encoding: .utf8) {
            UserDefaults.standard.set(str, forKey: positionKey)
        }
    }

    @objc private func handleDrag(_ gr: NSPanGestureRecognizer) {
        if gr.state == .ended || gr.state == .changed {
            savePosition()
        }
    }
}
