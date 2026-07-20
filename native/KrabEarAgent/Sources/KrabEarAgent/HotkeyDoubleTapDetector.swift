/*
 HotkeyDoubleTapDetector — детектор двойного нажатия Right Option в окне 300 мс.

 Логика:
 - Слушает globalMonitor (.flagsChanged).
 - Первое нажатие (Right Option down) запускает таймер на 300 мс.
 - Второе нажатие в пределах окна → вызывает callback onDoubleTap.
 - Единственное нажатие (одиночный hold) не конфликтует с диктовкой:
   callback срабатывает только на ДВА press-события, не на hold.

 Связи:
 - HotkeyManager.swift: создаёт экземпляр и передаёт onDoubleTap-callback.
 - HistoryPanelController+VoiceTab.swift: получает вызов triggerConversationToggle().
*/

import AppKit
import Foundation

/// Детектор двойного нажатия Right Option в окне 300 мс.
///
/// Не конфликтует с одиночным Right Option single-hold (диктовка):
/// одиночный нажим не запускает callback, только двойной быстрый тап.
///
/// Всё состояние защищено main-thread dispatch (NSEvent monitors deliver on main).
@MainActor
final class HotkeyDoubleTapDetector {

    // MARK: - Configuration

    /// Максимальный интервал между двумя нажатиями для детекции double-tap (сек).
    let windowMs: TimeInterval

    // MARK: - State

    private var firstTapTime: TimeInterval?
    private var globalMonitor: Any?
    private var localMonitor: Any?
    private let onDoubleTap: @MainActor () -> Void

    // MARK: - Init

    /// - Parameters:
    ///   - windowMs: Ширина окна детекции в секундах (default 0.3 = 300 мс).
    ///   - onDoubleTap: Вызывается на главном потоке при детекции двойного нажатия.
    init(windowMs: TimeInterval = 0.3, onDoubleTap: @escaping @MainActor () -> Void) {
        self.windowMs = windowMs
        self.onDoubleTap = onDoubleTap
    }

    // MARK: - Start / Stop

    /// Запустить мониторинг hotkey (глобальный + локальный).
    nonisolated func start() {
        // NSEvent monitors must be installed from main thread
        DispatchQueue.main.async {
            Task { @MainActor in
                self.startOnMain()
            }
        }
    }

    @MainActor
    private func startOnMain() {
        stopOnMain()

        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handle(event: event)
            }
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handle(event: event)
            }
            return event
        }
    }

    /// Остановить мониторинг и сбросить состояние.
    nonisolated func stop() {
        DispatchQueue.main.async {
            Task { @MainActor in
                self.stopOnMain()
            }
        }
    }

    @MainActor
    private func stopOnMain() {
        if let m = globalMonitor { NSEvent.removeMonitor(m); globalMonitor = nil }
        if let m = localMonitor  { NSEvent.removeMonitor(m); localMonitor  = nil }
        firstTapTime = nil
    }

    // MARK: - Event handling

    @MainActor
    private func handle(event: NSEvent) {
        guard event.keyCode == Keycode.rightOption.rawValue else { return }

        // Реагируем только на press (флаг появился — key down)
        let isDown = event.modifierFlags.contains(.option)
        guard isDown else { return }

        injectTapAt(time: Date().timeIntervalSinceReferenceDate)
    }

    /// Тест-хук: ввести синтетический тап в логику детектора.
    /// Используется только из тестового таргета через testable subclass.
    @MainActor
    func injectTapAt(time: TimeInterval) {
        if let first = firstTapTime, (time - first) <= windowMs {
            // Второй тап в окне → double-tap детектирован
            firstTapTime = nil
            onDoubleTap()
        } else {
            // Первый тап — запускаем окно
            firstTapTime = time
            // Сброс через windowMs + небольшой буфер (если второго тапа не было)
            let deadline = windowMs + 0.05
            let saved = time
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: UInt64(deadline * 1_000_000_000))
                if let current = self.firstTapTime, abs(current - saved) < 0.001 {
                    self.firstTapTime = nil
                }
            }
        }
    }
}
