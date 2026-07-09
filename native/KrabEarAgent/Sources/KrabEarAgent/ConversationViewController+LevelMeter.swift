/*
 ConversationViewController+LevelMeter.swift
 Живой визуализатор уровня микрофона для вкладки «Разговор с AI».

 Архитектура:
 - MicLevelMeterView — NSView с рядом вертикальных CALayer-баров.
   Слои создаются один раз в setupBars(); обновление через updateLevel() — только
   изменение bounds.size.height (дёшево, без layout-pass).
 - Ring buffer (lastLevels) — последние N нормализованных RMS-значений.
   Каждый бар отражает своё «историческое» значение (правый — самый свежий).
 - Все операции — @MainActor; слои строятся lazily при первом вызове.
 - Reduce Motion: плавный CATransaction (0.08s) отключается, значения
   устанавливаются немедленно.
 - Glyph-guard чист: бары — CALayer, не текст/символы.
 - AGENT-3: нет IPC-вызовов.
*/

import AppKit
import QuartzCore

// MARK: - MicLevelMeterView

/// Горизонтальный ряд вертикальных баров, реагирующих на RMS микрофона.
@MainActor
final class MicLevelMeterView: NSView {

    // MARK: - Config

    /// Количество баров.
    private let barCount: Int = 20

    /// Минимальная высота бара как доля от высоты view (idle-состояние).
    private let minBarFraction: CGFloat = 0.08

    /// Горизонтальный отступ между барами.
    private let barGap: CGFloat = 2.0

    // MARK: - State

    /// Ring-buffer последних normalised RMS-значений (0…1). Ёмкость = barCount.
    private var lastLevels: [CGFloat]

    /// Индекс для следующей записи в ring-buffer.
    private var ringHead: Int = 0

    /// Слои баров. Создаются один раз в setupBars().
    private var barLayers: [CALayer] = []

    /// Флаг: слои уже созданы.
    private var barsReady: Bool = false

    // MARK: - Init

    override init(frame: NSRect) {
        lastLevels = [CGFloat](repeating: 0, count: barCount)
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        lastLevels = [CGFloat](repeating: 0, count: barCount)
        super.init(coder: coder)
        wantsLayer = true
    }

    // MARK: - Layout

    override func layout() {
        super.layout()
        // Обновляем геометрию баров при каждом layout-pass.
        repositionBars()
    }

    // MARK: - Public API

    /// Обновить уровень. Вызывается из processAudioSamples (@MainActor).
    /// level: 0…1 (нормализованный RMS).
    func updateLevel(_ level: CGFloat) {
        // Запись в ring-buffer.
        lastLevels[ringHead] = level
        ringHead = (ringHead + 1) % barCount

        // Ленивое создание слоёв при первом обновлении (view уже в иерархии).
        if !barsReady {
            setupBars()
        }

        applyLevelsToLayers()
    }

    /// Сбросить уровни в idle (вызывается при остановке захвата).
    func resetToIdle() {
        for i in 0..<barCount { lastLevels[i] = 0 }
        ringHead = 0
        if barsReady {
            applyLevelsToLayers()
        }
    }

    // MARK: - Private — bar creation

    private func setupBars() {
        guard let rootLayer = layer else { return }
        barLayers.removeAll()

        for _ in 0..<barCount {
            let bar = CALayer()
            bar.cornerRadius = 2
            // Цвет задаётся в updateBarColors() (зависит от effectiveAppearance).
            bar.actions = ["bounds": NSNull(), "position": NSNull()]  // отключить implicit анимации
            rootLayer.addSublayer(bar)
            barLayers.append(bar)
        }

        updateBarColors()
        repositionBars()
        barsReady = true
    }

    /// Перерасчёт геометрии баров при layout.
    private func repositionBars() {
        guard !barLayers.isEmpty else { return }
        let totalWidth = bounds.width
        let totalHeight = bounds.height
        guard totalWidth > 0, totalHeight > 0 else { return }

        let barWidth = max(1, (totalWidth - CGFloat(barCount - 1) * barGap) / CGFloat(barCount))

        // Считываем текущий ring-buffer в порядке «слева-направо = старые→свежие»
        let orderedLevels = orderedLevelArray()

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for (i, bar) in barLayers.enumerated() {
            let x = CGFloat(i) * (barWidth + barGap)
            let level = orderedLevels[i]
            let frac = minBarFraction + level * (1.0 - minBarFraction)
            let barHeight = frac * totalHeight
            let y = (totalHeight - barHeight) / 2.0
            bar.frame = CGRect(x: x, y: y, width: barWidth, height: barHeight)
        }
        CATransaction.commit()
    }

    /// Применить текущий ring-buffer к слоям (анимированно или нет).
    private func applyLevelsToLayers() {
        guard barsReady else { return }
        let totalHeight = bounds.height
        let totalWidth = bounds.width
        guard totalHeight > 0, totalWidth > 0 else { return }

        let barWidth = max(1, (totalWidth - CGFloat(barCount - 1) * barGap) / CGFloat(barCount))
        let orderedLevels = orderedLevelArray()
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion

        CATransaction.begin()
        if reduceMotion {
            CATransaction.setDisableActions(true)
        } else {
            CATransaction.setAnimationDuration(0.08)
            CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeOut))
        }

        for (i, bar) in barLayers.enumerated() {
            let x = CGFloat(i) * (barWidth + barGap)
            let level = orderedLevels[i]
            let frac = minBarFraction + level * (1.0 - minBarFraction)
            let barHeight = frac * totalHeight
            let y = (totalHeight - barHeight) / 2.0
            bar.frame = CGRect(x: x, y: y, width: barWidth, height: barHeight)
        }

        CATransaction.commit()
        updateBarColors()
    }

    /// Задать цвета баров (accent-градиент по уровню).
    private func updateBarColors() {
        let orderedLevels = orderedLevelArray()
        for (i, bar) in barLayers.enumerated() {
            let level = orderedLevels[i]
            // Цвет: от accent (низкий) до success (высокий).
            // Нельзя использовать CGColor напрямую из NSColor без resolving appearance.
            let resolvedColor: NSColor
            if level > 0.75 {
                // Высокий уровень — accent ярче
                resolvedColor = KrabEarTheme.Colors.accent
            } else {
                resolvedColor = KrabEarTheme.Colors.accent.withAlphaComponent(0.5 + level * 0.5)
            }
            // Resolve via effectiveAppearance to get correct CGColor in dark/light mode.
            var resolved: NSColor = .controlAccentColor
            effectiveAppearance.performAsCurrentDrawingAppearance {
                resolved = resolvedColor
            }
            bar.backgroundColor = resolved.cgColor
        }
    }

    /// Упорядоченный массив уровней: индекс 0 = самый старый, индекс N-1 = самый свежий.
    private func orderedLevelArray() -> [CGFloat] {
        var result = [CGFloat](repeating: 0, count: barCount)
        for i in 0..<barCount {
            let bufIdx = (ringHead + i) % barCount
            result[i] = lastLevels[bufIdx]
        }
        return result
    }

    // MARK: - Appearance change

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        if barsReady {
            updateBarColors()
        }
    }
}

// MARK: - ConversationViewController level-meter integration

extension ConversationViewController {

    // MARK: - Meter access (via associated object)

    private static var meterKey: UInt8 = 0

    /// Ленивый доступ к MicLevelMeterView, вставленному в waveformPlaceholder.
    var micLevelMeter: MicLevelMeterView? {
        return objc_getAssociatedObject(self, &ConversationViewController.meterKey) as? MicLevelMeterView
    }

    // MARK: - Setup (вызывается из buildUI после добавления waveformPlaceholder в иерархию)

    /// Встроить MicLevelMeterView внутрь waveformPlaceholder.
    /// Вызывается один раз из buildUI (ConversationViewController+UI.swift).
    func setupMicLevelMeter() {
        let meter = MicLevelMeterView()
        meter.translatesAutoresizingMaskIntoConstraints = false
        waveformPlaceholder.addSubview(meter)
        NSLayoutConstraint.activate([
            meter.topAnchor.constraint(equalTo: waveformPlaceholder.topAnchor, constant: 6),
            meter.leadingAnchor.constraint(equalTo: waveformPlaceholder.leadingAnchor, constant: 8),
            meter.trailingAnchor.constraint(equalTo: waveformPlaceholder.trailingAnchor, constant: -8),
            meter.bottomAnchor.constraint(equalTo: waveformPlaceholder.bottomAnchor, constant: -6),
        ])
        objc_setAssociatedObject(self, &ConversationViewController.meterKey, meter, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
    }

    // MARK: - RMS computation (called from processAudioSamples)

    /// Вычислить нормализованный RMS из массива PCM-сэмплов.
    /// Вызывается на @MainActor из processAudioSamples.
    func computeAndPushLevel(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        let sumSq = samples.reduce(Float(0)) { $0 + $1 * $1 }
        let rms = sqrtf(sumSq / Float(samples.count))
        // Нормализация: RMS mic обычно очень мал; ×8 даёт приятную чувствительность.
        let normalized = CGFloat(min(1.0, rms * 8.0))
        micLevelMeter?.updateLevel(normalized)
        statusOverlay?.pushLevel(normalized)
    }

    // MARK: - Reset on stop

    /// Плавно сбросить meter в idle. Вызывается из stopAudioCapture.
    func resetMicLevelMeter() {
        micLevelMeter?.resetToIdle()
    }
}
