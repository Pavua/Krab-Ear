/*
 Экранный realtime-оверлей Krab Ear.

 Связи модуля:
 1) main.swift: показывает и обновляет оверлей во время активной записи.
 2) IPC get_recording_state: источник промежуточного текста и таймера.

 Редизайн: Liquid Glass (NSVisualEffectView + привязка к курсору + анимация появления).
 - Целевая платформа macOS 13+, строгая многопоточность Swift 6.0.
 - Индикатор записи: точка и мягкий halo строго на CALayer (без NSBox и глифов).
 - Подложка: surfaceView с Colors.cardBackground и динамический border.
 - Типографика: Typography.display и Typography.captionMedium.
*/

import AppKit
import Foundation
import QuartzCore

// MARK: - Состояния оверлея

private enum OverlayState {
    case hidden
    case live        // Активная запись: текст и индикатор записи
    case reveal      // 3-стадийный прогресс после stop_recording
}

// MARK: - DynamicTintView

/// NSView с динамическим cgColor — корректно перерисовывается при смене Light/Dark темы.
@MainActor
private final class DynamicTintView: NSView {
    var tintColor: NSColor = .clear {
        didSet { needsDisplay = true }
    }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        super.updateLayer()
        layer?.backgroundColor = tintColor.cgColor
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

// MARK: - OverlayEffectView

/// Фоновый эффект Liquid Glass с синхронизацией рамки и смены темы.
@MainActor
private final class OverlayEffectView: NSVisualEffectView {
    var onLayout: (() -> Void)?
    var onAppearanceChanged: (() -> Void)?

    override func layout() {
        super.layout()
        onLayout?()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        onAppearanceChanged?()
    }
}

// MARK: - StageBadgeView

/// Капсула (pill) для индикации стадии обработки ("Распознано" / "Очищено" / "LLM").
@MainActor
private final class StageBadgeView: NSView {
    private let label = NSTextField(labelWithString: "")

    var stageText: String = "" {
        didSet {
            label.stringValue = stageText
            isHidden = stageText.isEmpty
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setupUI()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func setupUI() {
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.tight

        label.font = KrabEarTheme.Typography.captionMedium
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.translatesAutoresizingMaskIntoConstraints = false

        addSubview(label)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            label.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2),
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            label.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
        ])
    }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        super.updateLayer()
        layer?.backgroundColor = KrabEarTheme.Colors.textSecondary.withAlphaComponent(0.12).cgColor
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

// MARK: - RealtimeOverlayController

/// Плавающий Liquid Glass оверлей для realtime-превью диктовки.
/// Позиционирование возле курсора, анимированное появление/исчезновение,
/// поддержка 3-стадийного reveal после окончания записи.
@MainActor
public final class RealtimeOverlayController: NSObject {

    // MARK: Панель и представления

    private let panel: NSPanel

    /// Фоновое стекло (Liquid Glass) с виброй
    private let effectView: OverlayEffectView

    /// Поверхность карточки (0.5 alpha cardBackground)
    private let surfaceView: DynamicTintView

    /// Динамический оверлей подсветки (красный при записи / акцентный при транскрипции)
    private let tintView: DynamicTintView

    /// Тонкая внутренняя граница поверх effectView
    private let borderLayer = CALayer()

    /// Строка статуса: длительность и режим
    private let statusLabel  = NSTextField(labelWithString: "00:00")
    private let modeLabel    = NSTextField(labelWithString: "—")

    /// Индикатор записи: точка и мягкий halo строго на CALayer
    private let recordingDot = NSView()
    private let recordingDotLayer = CALayer()
    private let recordingDotHalo = CALayer()

    /// Плашка этапа (pill) для анимации reveal ("Распознано" / "Очищено" / "LLM")
    private let stageBadge   = StageBadgeView()

    /// Основной текст (превью / текст этапа)
    /// internal — доступен из RealtimeOverlayController+PartialSSE.swift
    let primaryLabel = NSTextField(wrappingLabelWithString: "")

    // MARK: Состояние

    private var overlayState: OverlayState = .hidden
    private var opacityPercent: Int = 100

    /// Флаг — текущий текст является частичной транскрипцией (SSE partial).
    /// internal — используется RealtimeOverlayController+PartialSSE.swift
    var _isShowingPartial: Bool = false

    private var targetAlpha: CGFloat {
        CGFloat(opacityPercent) / 100.0
    }

    /// Таск для управления стадиями reveal анимации
    private var revealTask: Task<Void, Never>?

    /// Флаг — нужно ли уважать reduce-motion
    private var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    // MARK: Сохранение позиции

    /// Ключ UserDefaults для сохранённых координат оверлея
    private let savedOriginKey = "RealtimeOverlay_LastOrigin"
    /// Глобальный монитор NSEvent для отслеживания перетаскивания оверлея
    private var dragMonitor: Any?
    /// Точка начала перетаскивания относительно окна
    private var dragStartWindowLocation: NSPoint = .zero

    // MARK: Кольцевой буфер строк

    /// Максимальное количество одновременно отображаемых строк
    private let maxVisibleLines = 4
    /// Кольцевой буфер строк транскрипта
    private var lineRingBuffer: [String] = []

    // MARK: Константы разметки

    private let minWidth:   CGFloat = 420
    private let maxWidth:   CGFloat = 640
    private let minHeight:  CGFloat = 80
    private let maxHeight:  CGFloat = 180
    private let cornerRadius: CGFloat = KrabEarTheme.Metrics.cardCornerRadius

    // Ключи анимаций CABasicAnimation
    private let dotPulseKey       = "krabEarDotPulse"
    private let labelPulseKey     = "krabEarLabelPulse"
    private let breathingKey      = "krabEarBreathing"

    // MARK: Инициализация

    public override init() {
        let initialRect = NSRect(x: 0, y: 0, width: 520, height: 80)
        self.panel = NSPanel(
            contentRect: initialRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        self.effectView = OverlayEffectView(frame: initialRect)
        self.surfaceView = DynamicTintView(frame: initialRect)
        self.tintView   = DynamicTintView(frame: initialRect)

        super.init()
        setupPanel()
        setupEffectView()
        setupUI()
        // Примечание: затемнение при наведении убрано — panel.ignoresMouseEvents = true
        // означает, что события NSTrackingArea не приходят. Перетаскивание использует
        // глобальный монитор NSEvent.
    }

    // MARK: - Публичный API

    public func show() {
        revealTask?.cancel()
        guard overlayState == .hidden else { return }
        overlayState = .live
        lineRingBuffer = []  // Сброс кольцевого буфера при каждой новой записи
        stageBadge.isHidden = true
        tintView.tintColor = KrabEarTheme.Colors.error.withAlphaComponent(0.04)
        // Восстанавливаем позицию, куда владелец перетащил оверлей.
        // Но включённое следование за курсором сильнее: это более свежее и
        // более явное распоряжение, чем перетаскивание когда-то в прошлом.
        if followCursorEnabled {
            positionNearCursor()
        } else if !restoreSavedPosition() {
            positionNearCursor()
        }
        panel.alphaValue = 0
        panel.orderFront(nil)
        animateShow()
        startDotPulse()     // Пульсация точки записи
        startLabelPulse()
        startBreathing()    // Фоновое дыхание подсветки
        recordingDot.isHidden = false
        recordingDotHalo.opacity = 0.3
        startDragMonitor()  // Отслеживание перемещения пользователем
    }

    public func hide() {
        revealTask?.cancel()
        stopAllPulse()
        stopBreathing()     // Отключение дыхания подсветки
        stopDragMonitor()   // Остановка отслеживания перетаскивания
        if overlayState == .hidden { return }
        overlayState = .hidden
        recordingDot.isHidden = true
        recordingDotHalo.transform = CATransform3DIdentity
        recordingDotHalo.opacity = 0.0
        tintView.tintColor = .clear
        animateHide { [weak self] in
            Task { @MainActor [weak self] in
                self?.panel.orderOut(nil)
            }
        }
    }

    public func update(previewText: String, translatedText: String?, durationText: String, modeHint: String) {
        guard overlayState == .live else { return }

        statusLabel.stringValue = durationText
        modeLabel.stringValue   = modeHint.isEmpty ? "—" : modeHint

        let clean = previewText.trimmingCharacters(in: .whitespacesAndNewlines)
        if clean.isEmpty {
            setPrimaryText("Слушаю…")
        } else {
            // ЗАМЕНА, а не накопление (жалоба владельца: «каждое новое слово копирует всю диктовку»).
            // Раз текст уже кумулятивный и обрезан бэкендом до 900 знаков —
            // просто показываем его. Рост по высоте берёт на себя adjustHeight().
            let cleanTrans = (translatedText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let line = cleanTrans.isEmpty ? clean : "\(clean)  ↔  \(cleanTrans)"
            lineRingBuffer = [line]
            // Панель фиксирована по высоте, а текст накапливается — показываем
            // хвост, иначе видно только застывшее начало диктовки.
            let font = primaryLabel.font ?? KrabEarTheme.Typography.display
            let textWidth = 520 - KrabEarTheme.Metrics.comfortable * 2
            setPrimaryText(Self.tailFitting(line, available: availableTextHeight()) { candidate in
                self.heightForString(candidate, font: font, width: textWidth)
            })
        }
        if panel.isVisible {
            adjustHeight()
            if followCursorEnabled {
                positionNearCursor()
            }
        }
    }

    public func setOpacityPercent(_ value: Int) {
        let safe = max(15, min(100, value))
        opacityPercent = safe
        if panel.isVisible && overlayState != .hidden {
            panel.alphaValue = targetAlpha
        }
    }

    public func setAudioLevel(_ rms: Float) {
        guard overlayState == .live else { return }
        guard !reduceMotion else {
            recordingDotHalo.transform = CATransform3DIdentity
            recordingDotHalo.opacity = 0.0
            return
        }
        let clamped = max(0.0, min(1.0, rms))
        // Мягкий halo без тяжелой перерисовки: только трансформ и прозрачность CALayer
        let scale = 1.0 + CGFloat(clamped) * 2.2
        let haloOpacity = Float(0.25 + clamped * 0.45)
        
        CATransaction.begin()
        CATransaction.setAnimationDuration(KrabEarTheme.Motion.Duration.micro)
        CATransaction.setAnimationTimingFunction(KrabEarTheme.Motion.Easing.easeOut)
        recordingDotHalo.transform = CATransform3DMakeScale(scale, scale, 1.0)
        recordingDotHalo.opacity = haloOpacity
        CATransaction.commit()
    }

    // MARK: - Настройка панели и представлений

    private func setupPanel() {
        panel.level               = .statusBar
        panel.isFloatingPanel     = true
        panel.collectionBehavior  = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate   = false
        panel.isOpaque            = false
        panel.backgroundColor     = .clear
        panel.hasShadow           = false
        // Пропуск кликов — оверлей передает клики нижележащим окнам
        panel.ignoresMouseEvents  = true
    }

    private func setupEffectView() {
        effectView.material      = .popover
        effectView.blendingMode  = .behindWindow
        effectView.state         = .active
        effectView.isEmphasized  = true
        effectView.wantsLayer    = true

        effectView.layer?.cornerRadius  = cornerRadius
        effectView.layer?.cornerCurve   = .continuous
        effectView.layer?.masksToBounds = true

        panel.contentView?.wantsLayer = true
        if let rootLayer = panel.contentView?.layer {
            rootLayer.masksToBounds   = false
            KrabEarTheme.Elevation.applyOverlay(to: rootLayer)
        }

        borderLayer.borderColor = KrabEarTheme.Colors.border.cgColor
        borderLayer.borderWidth = 1.0
        borderLayer.cornerRadius = cornerRadius
        borderLayer.cornerCurve = .continuous
        borderLayer.frame = effectView.bounds

        effectView.layer?.addSublayer(borderLayer)

        effectView.onLayout = { [weak self] in
            guard let self else { return }
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            self.borderLayer.frame = self.effectView.bounds
            CATransaction.commit()
        }

        effectView.onAppearanceChanged = { [weak self] in
            guard let self else { return }
            self.borderLayer.borderColor = KrabEarTheme.Colors.border.cgColor
            self.updateDotAppearance()
        }

        panel.contentView?.addSubview(effectView)
        effectView.translatesAutoresizingMaskIntoConstraints = false
        if let cv = panel.contentView {
            NSLayoutConstraint.activate([
                effectView.topAnchor.constraint(equalTo: cv.topAnchor),
                effectView.leadingAnchor.constraint(equalTo: cv.leadingAnchor),
                effectView.trailingAnchor.constraint(equalTo: cv.trailingAnchor),
                effectView.bottomAnchor.constraint(equalTo: cv.bottomAnchor),
            ])
        }
    }

    private func setupUI() {
        // Подложка поверхности (Liquid Glass)
        surfaceView.wantsLayer = true
        surfaceView.layer?.cornerRadius = cornerRadius
        surfaceView.layer?.cornerCurve = .continuous
        surfaceView.tintColor = KrabEarTheme.Colors.cardBackground
        surfaceView.translatesAutoresizingMaskIntoConstraints = false
        effectView.addSubview(surfaceView)

        // Динамический оверлей подсветки (красный при записи / акцентный при транскрипции)
        tintView.wantsLayer = true
        tintView.layer?.cornerRadius = cornerRadius
        tintView.layer?.cornerCurve = .continuous
        tintView.tintColor = .clear
        tintView.translatesAutoresizingMaskIntoConstraints = false
        effectView.addSubview(tintView)

        NSLayoutConstraint.activate([
            surfaceView.topAnchor.constraint(equalTo: effectView.topAnchor),
            surfaceView.leadingAnchor.constraint(equalTo: effectView.leadingAnchor),
            surfaceView.trailingAnchor.constraint(equalTo: effectView.trailingAnchor),
            surfaceView.bottomAnchor.constraint(equalTo: effectView.bottomAnchor),

            tintView.topAnchor.constraint(equalTo: effectView.topAnchor),
            tintView.leadingAnchor.constraint(equalTo: effectView.leadingAnchor),
            tintView.trailingAnchor.constraint(equalTo: effectView.trailingAnchor),
            tintView.bottomAnchor.constraint(equalTo: effectView.bottomAnchor),
        ])

        statusLabel.font      = KrabEarTheme.Typography.captionMedium.tabular()
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.alignment = .right
        statusLabel.isBordered = false
        statusLabel.drawsBackground = false
        statusLabel.isEditable = false
        statusLabel.isSelectable = false
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        modeLabel.font      = KrabEarTheme.Typography.captionMedium
        modeLabel.textColor = KrabEarTheme.Colors.textSecondary
        modeLabel.alignment = .left
        modeLabel.isBordered = false
        modeLabel.drawsBackground = false
        modeLabel.isEditable = false
        modeLabel.isSelectable = false
        modeLabel.translatesAutoresizingMaskIntoConstraints = false

        // Индикатор записи: точка и мягкий halo строго на CALayer
        recordingDot.wantsLayer = true
        recordingDot.isHidden = true
        recordingDot.translatesAutoresizingMaskIntoConstraints = false

        let dotBounds = CGRect(x: 0, y: 0, width: 8, height: 8)

        recordingDotHalo.frame = dotBounds
        recordingDotHalo.cornerRadius = 4
        recordingDotHalo.backgroundColor = KrabEarTheme.Colors.error.withAlphaComponent(0.35).cgColor
        recordingDotHalo.opacity = 0.0

        recordingDotLayer.frame = dotBounds
        recordingDotLayer.cornerRadius = 4
        recordingDotLayer.backgroundColor = KrabEarTheme.Colors.error.cgColor

        recordingDot.layer?.addSublayer(recordingDotHalo)
        recordingDot.layer?.addSublayer(recordingDotLayer)

        // Плашка этапа (pill) для анимации reveal
        stageBadge.isHidden = true
        stageBadge.translatesAutoresizingMaskIntoConstraints = false

        primaryLabel.font            = KrabEarTheme.Typography.display
        primaryLabel.textColor       = KrabEarTheme.Colors.textPrimary
        primaryLabel.alignment       = .left
        primaryLabel.maximumNumberOfLines = 0
        primaryLabel.lineBreakMode   = .byWordWrapping
        primaryLabel.wantsLayer      = true
        primaryLabel.translatesAutoresizingMaskIntoConstraints = false
        setPrimaryText("Слушаю…")

        effectView.addSubview(recordingDot)
        effectView.addSubview(modeLabel)
        effectView.addSubview(statusLabel)
        effectView.addSubview(stageBadge)
        effectView.addSubview(primaryLabel)

        NSLayoutConstraint.activate([
            recordingDot.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            recordingDot.topAnchor.constraint(equalTo: effectView.topAnchor, constant: KrabEarTheme.Metrics.comfortable),
            recordingDot.widthAnchor.constraint(equalToConstant: 8),
            recordingDot.heightAnchor.constraint(equalToConstant: 8),

            modeLabel.centerYAnchor.constraint(equalTo: recordingDot.centerYAnchor),
            modeLabel.leadingAnchor.constraint(equalTo: recordingDot.trailingAnchor, constant: KrabEarTheme.Metrics.standard),

            statusLabel.centerYAnchor.constraint(equalTo: modeLabel.centerYAnchor),
            statusLabel.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -KrabEarTheme.Metrics.comfortable),

            // Плашка этапа — ниже верхней строки при reveal
            stageBadge.topAnchor.constraint(equalTo: modeLabel.bottomAnchor, constant: KrabEarTheme.Metrics.standard),
            stageBadge.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),

            primaryLabel.topAnchor.constraint(equalTo: stageBadge.bottomAnchor, constant: KrabEarTheme.Metrics.tight),
            primaryLabel.leadingAnchor.constraint(equalTo: effectView.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            primaryLabel.trailingAnchor.constraint(equalTo: effectView.trailingAnchor, constant: -KrabEarTheme.Metrics.comfortable),
            primaryLabel.bottomAnchor.constraint(lessThanOrEqualTo: effectView.bottomAnchor, constant: -KrabEarTheme.Metrics.comfortable),
        ])
    }

    /// Обновление цветов точки записи при смене темы
    private func updateDotAppearance() {
        recordingDotHalo.backgroundColor = KrabEarTheme.Colors.error.withAlphaComponent(0.35).cgColor
        recordingDotLayer.backgroundColor = KrabEarTheme.Colors.error.cgColor
    }

    // MARK: - Анимации

    private func animateShow() {
        if reduceMotion {
            panel.alphaValue = targetAlpha
            return
        }
        if let layer = panel.contentView?.layer {
            let scaleT = CATransform3DMakeScale(0.98, 0.98, 1.0)
            let translateT = CATransform3DMakeTranslation(0, -10, 0)
            layer.transform = CATransform3DConcat(scaleT, translateT)
        }
        panel.alphaValue = 0

        KrabEarTheme.Motion.animate(
            duration: KrabEarTheme.Motion.Duration.short,
            easing: KrabEarTheme.Motion.Easing.easeInOut
        ) {
            self.panel.animator().alphaValue = self.targetAlpha
            self.panel.contentView?.layer?.transform = CATransform3DIdentity
        }
    }

    private func animateHide(completion: @escaping @Sendable () -> Void) {
        if reduceMotion {
            panel.alphaValue = 0
            completion()
            return
        }
        
        let hideDuration = KrabEarTheme.Motion.Duration.short
        KrabEarTheme.Motion.animate(
            duration: hideDuration,
            easing: KrabEarTheme.Motion.Easing.easeInOut
        ) {
            self.panel.animator().alphaValue = 0
            if let layer = self.panel.contentView?.layer {
                var t = CATransform3DIdentity
                t = CATransform3DScale(t, 0.96, 0.96, 1.0)
                t = CATransform3DTranslate(t, 0, 8, 0)
                layer.transform = t
            }
        }
        
        DispatchQueue.main.asyncAfter(deadline: .now() + hideDuration) {
            completion()
        }
    }

    // MARK: - Пульсация индикатора записи (CABasicAnimation)

    private func startDotPulse() {
        guard !reduceMotion else {
            recordingDotLayer.opacity = 1.0
            return
        }
        recordingDotLayer.removeAnimation(forKey: dotPulseKey)

        let pulse = CABasicAnimation(keyPath: "opacity")
        pulse.fromValue = 0.5
        pulse.toValue   = 1.0
        pulse.duration  = KrabEarTheme.Motion.Duration.long
        pulse.autoreverses   = true
        pulse.repeatCount    = .infinity
        pulse.timingFunction = KrabEarTheme.Motion.Easing.easeInOut

        recordingDotLayer.add(pulse, forKey: dotPulseKey)
    }

    private func stopDotPulse() {
        recordingDotLayer.removeAnimation(forKey: dotPulseKey)
        recordingDotLayer.opacity = 1.0
    }

    // MARK: - Пульсация текста (зарезервировано)

    private func startLabelPulse() {
        primaryLabel.layer?.opacity = 1.0
    }

    private func stopLabelPulse() {
        primaryLabel.layer?.removeAnimation(forKey: labelPulseKey)
        primaryLabel.layer?.opacity = 1.0
    }

    private func stopAllPulse() {
        stopDotPulse()
        stopLabelPulse()
    }

    // MARK: - Позиционирование

    /// Следовать ли за курсором на каждом тике обновления.
    /// Выключено по умолчанию — включается настройкой `overlay_follow_cursor`.
    var followCursorEnabled: Bool = false

    private func positionNearCursor() {
        let cursor = NSEvent.mouseLocation

        let screen = NSScreen.screens.first { $0.frame.contains(cursor) }
                  ?? NSScreen.main
                  ?? NSScreen.screens.first

        guard let screen else { return }
        let visible = screen.visibleFrame

        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let height = currentPanelHeight()

        // Смещение относительно курсора: 16pt вправо, 24pt вниз
        var x = cursor.x + 16
        var y = cursor.y - 24 - height

        if x + width > visible.maxX {
            x = cursor.x - width - 10
        }
        if x < visible.minX {
            x = visible.minX + 8
        }
        if y < visible.minY {
            y = cursor.y + 30
        }
        if y + height > visible.maxY {
            y = visible.maxY - height - 8
        }

        panel.setFrame(NSRect(x: x, y: y, width: width, height: height), display: true)
        borderLayer.frame = effectView.bounds
    }

    /// internal — доступен из RealtimeOverlayController+PartialSSE.swift
    func adjustHeight() {
        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let insets: CGFloat = KrabEarTheme.Metrics.comfortable * 2
        let topRowH: CGFloat = KrabEarTheme.Metrics.comfortable + 16
        let stageLabelH: CGFloat = stageBadge.isHidden ? 0 : (18 + KrabEarTheme.Metrics.standard)
        let padding: CGFloat = KrabEarTheme.Metrics.tight + KrabEarTheme.Metrics.comfortable
        let textWidth = width - insets

        let textH = heightForString(primaryLabel.stringValue, font: primaryLabel.font ?? KrabEarTheme.Typography.display, width: textWidth)
        let total = topRowH + stageLabelH + padding + textH
        let height = clamp(value: total, min: minHeight, max: maxHeight)

        let oldFrame = panel.frame
        let fixedHeight = maxHeight
        if abs(oldFrame.size.height - fixedHeight) > 0.5 {
            // Однократно: увеличиваем панель до фиксированной высоты с привязкой к левому верхнему углу.
            let topLeft = NSPoint(x: oldFrame.minX, y: oldFrame.maxY)
            panel.setContentSize(NSSize(width: oldFrame.size.width, height: fixedHeight))
            panel.setFrameTopLeftPoint(topLeft)
        }
        // Параметр height игнорируется — панель всегда зафиксирована на maxHeight.
        _ = height

        borderLayer.frame = effectView.bounds
    }

    private func currentPanelHeight() -> CGFloat {
        let width: CGFloat = 520
        let insets: CGFloat = KrabEarTheme.Metrics.comfortable * 2
        let topRowH: CGFloat = KrabEarTheme.Metrics.comfortable + 16
        let stageLabelH: CGFloat = stageBadge.isHidden ? 0 : (18 + KrabEarTheme.Metrics.standard)
        let padding: CGFloat = KrabEarTheme.Metrics.tight + KrabEarTheme.Metrics.comfortable
        let textWidth = width - insets
        let textH = heightForString(primaryLabel.stringValue, font: primaryLabel.font ?? KrabEarTheme.Typography.display, width: textWidth)
        let total = topRowH + stageLabelH + padding + textH
        return clamp(value: total, min: minHeight, max: maxHeight)
    }

    // MARK: - Фоновая анимация подсветки (дыхание)

    private func startBreathing() {
        guard !reduceMotion else { return }
        tintView.layer?.removeAnimation(forKey: breathingKey)
        let breathing = CABasicAnimation(keyPath: "opacity")
        breathing.fromValue  = 0.03
        breathing.toValue    = 0.08
        breathing.duration   = KrabEarTheme.Motion.Duration.long * 2
        breathing.autoreverses = true
        breathing.repeatCount  = .infinity
        breathing.timingFunction = KrabEarTheme.Motion.Easing.easeInOut
        tintView.layer?.add(breathing, forKey: breathingKey)
    }

    private func stopBreathing() {
        tintView.layer?.removeAnimation(forKey: breathingKey)
    }

    // MARK: - Типографика: трекинг символов

    /// Устанавливает текст primaryLabel с кернингом 0.3pt для лучшей читаемости.
    func setPrimaryText(_ text: String) {
        let font = primaryLabel.font ?? KrabEarTheme.Typography.display
        let attrs: [NSAttributedString.Key: Any] = [
            .kern: 0.3 as NSNumber,
            .font: font,
            .foregroundColor: KrabEarTheme.Colors.textPrimary
        ]
        primaryLabel.attributedStringValue = NSAttributedString(string: text, attributes: attrs)
    }

    // MARK: - Память позиции и монитор перетаскивания

    /// Возвращает true и восстанавливает позицию панели, если сохраненная точка валидна для текущих экранов.
    @discardableResult
    private func restoreSavedPosition() -> Bool {
        guard let dict = UserDefaults.standard.dictionary(forKey: savedOriginKey),
              let x = dict["x"] as? CGFloat,
              let y = dict["y"] as? CGFloat
        else { return false }

        let origin = NSPoint(x: x, y: y)
        let width = clamp(value: 520, min: minWidth, max: maxWidth)
        let height = currentPanelHeight()
        let candidate = NSRect(origin: origin, size: CGSize(width: width, height: height))

        // Проверяем: хотя бы 80% фрейма должно быть на одном из экранов (защита при отключении монитора).
        let isOnScreen = NSScreen.screens.contains { screen in
            let intersection = candidate.intersection(screen.visibleFrame)
            let coveredArea = intersection.width * intersection.height
            let totalArea = width * height
            return coveredArea / totalArea >= 0.80
        }
        guard isOnScreen else { return false }

        panel.setFrame(candidate, display: true)
        borderLayer.frame = effectView.bounds
        return true
    }

    /// Сохраняет текущую позицию панели в UserDefaults.
    private func saveCurrentPosition() {
        let origin = panel.frame.origin
        UserDefaults.standard.set(["x": origin.x, "y": origin.y], forKey: savedOriginKey)
    }

    /// Устанавливает глобальный монитор NSEvent для отслеживания перетаскивания оверлея.
    /// Так как `ignoresMouseEvents = true`, глобальный монитор позволяет пользователю перетаскивать окно.
    private func startDragMonitor() {
        stopDragMonitor()

        // Перехватываем leftMouseDown + leftMouseDragged на глобальном уровне.
        // Когда mouseDown внутри фрейма панели, временно разрешаем обработку мыши для перетаскивания.
        var isDragging = false
        var dragStartMouseLocation: NSPoint = .zero
        var dragStartFrameOrigin: NSPoint = .zero

        dragMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .leftMouseDragged, .leftMouseUp]
        ) { [weak self] event in
            guard let self else { return }
            Task { @MainActor [weak self] in
                guard let self else { return }
                let mouseLocation = NSEvent.mouseLocation
                switch event.type {
                case .leftMouseDown:
                    // Проверяем, попал ли клик в область панели (с запасом 8pt для удобного захвата)
                    let panelFrame = self.panel.frame.insetBy(dx: -8, dy: -8)
                    if panelFrame.contains(mouseLocation) {
                        isDragging = true
                        dragStartMouseLocation = mouseLocation
                        dragStartFrameOrigin = self.panel.frame.origin
                        // Временно включаем события мыши на время перетаскивания
                        self.panel.ignoresMouseEvents = false
                    }
                case .leftMouseDragged where isDragging:
                    let dx = mouseLocation.x - dragStartMouseLocation.x
                    let dy = mouseLocation.y - dragStartMouseLocation.y
                    let newOrigin = NSPoint(
                        x: dragStartFrameOrigin.x + dx,
                        y: dragStartFrameOrigin.y + dy
                    )
                    var newFrame = self.panel.frame
                    newFrame.origin = newOrigin
                    self.panel.setFrame(newFrame, display: true)
                    self.borderLayer.frame = self.effectView.bounds
                case .leftMouseUp where isDragging:
                    isDragging = false
                    self.panel.ignoresMouseEvents = true  // Восстанавливаем пропуск кликов
                    self.saveCurrentPosition()            // Сохраняем перетащенную позицию
                default:
                    break
                }
            }
        }
    }

    /// Удаляет глобальный монитор перетаскивания.
    private func stopDragMonitor() {
        if let monitor = dragMonitor {
            NSEvent.removeMonitor(monitor)
            dragMonitor = nil
        }
        // Гарантируем восстановление пропуска кликов, если монитор остановлен во время перетаскивания.
        panel.ignoresMouseEvents = true
    }

    // MARK: - Вспомогательные методы

    /// Хвост накопленного превью, помещающийся в отведённую высоту.
    ///
    /// `preview_text` приходит КУМУЛЯТИВНЫМ (backend режет его до 900 знаков),
    /// а панель зафиксирована на `maxHeight`. Длинная диктовка целиком не влезает
    /// — и владелец смотрит на ЗАСТЫВШЕЕ НАЧАЛО, пока говорит дальше («показывает
    /// всё сообщение, которое от начала до конца», жалоба 02.09.2026). Меняются
    /// только последние слова — их и показываем.
    ///
    /// Измеритель передаётся снаружи: у живого оверлея это `boundingRect` с его
    /// реальным шрифтом и шириной, у теста — предсказуемая подделка, поэтому
    /// поведение проверяется без создания NSPanel.
    ///
    /// Режем по границе слова: строка, начинающаяся с обрубка, читается хуже
    /// обрезанной честно. Ведущее многоточие говорит, что начало за кадром.
    nonisolated static func tailFitting(
        _ text: String,
        available: CGFloat,
        measure: (String) -> CGFloat
    ) -> String {
        guard available > 0 else { return text }
        if measure(text) <= available { return text }

        let words = text.split(separator: " ", omittingEmptySubsequences: false)
        guard !words.isEmpty else { return text }

        // Двоичный поиск по числу последних слов: высота растёт монотонно, так
        // что «помещается» — монотонный предикат, и хватает log(n) измерений.
        var low = 1
        var high = words.count
        var best = ""
        while low <= high {
            let mid = (low + high) / 2
            let candidate = "…" + words.suffix(mid).joined(separator: " ")
            if measure(candidate) <= available {
                best = candidate
                low = mid + 1
            } else {
                high = mid - 1
            }
        }
        // Не влезает даже одно слово — это перекос вёрстки, а не повод показать
        // пустой оверлей: отдаём последнее слово и оставляем обрезку NSTextField.
        return best.isEmpty ? "…" + String(words[words.count - 1]) : best
    }

    /// Высота, доступная тексту при фиксированной высоте панели.
    private func availableTextHeight() -> CGFloat {
        let insets: CGFloat = KrabEarTheme.Metrics.comfortable * 2
        let topRowH: CGFloat = KrabEarTheme.Metrics.comfortable + 16
        let stageLabelH: CGFloat = stageBadge.isHidden ? 0 : (18 + KrabEarTheme.Metrics.standard)
        let padding: CGFloat = KrabEarTheme.Metrics.tight + KrabEarTheme.Metrics.comfortable
        _ = insets
        return maxHeight - (topRowH + stageLabelH + padding)
    }

    private func heightForString(_ string: String, font: NSFont, width: CGFloat) -> CGFloat {
        guard !string.isEmpty, width > 0 else { return 22 }
        let attrs: [NSAttributedString.Key: Any] = [.font: font]
        let boundingRect = (string as NSString).boundingRect(
            with: CGSize(width: width, height: CGFloat.greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: attrs
        )
        return ceil(boundingRect.height)
    }

    private func clamp(value: CGFloat, min minV: CGFloat, max maxV: CGFloat) -> CGFloat {
        Swift.max(minV, Swift.min(maxV, value))
    }
}
