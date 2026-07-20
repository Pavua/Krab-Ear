/*
 SelectionTranslator.swift — Cmd+Shift+T auto-translate selection (Phase 2A).

 Связи модуля:
 1) main.swift: создаётся в applicationDidFinishLaunching, принимает IPCClient.
 2) IPCClient: вызов translate_selection метода Python backend.
 3) NotificationService: HUD overlay «RU → ES: done» (2 сек).

 Пути перевода:
 A) Primary (AX API): читает kAXSelectedTextAttribute → translate → пишет обратно через AXUIElementSetAttributeValue.
 B) Fallback (Clipboard): сохраняет clipboard → Cmd+C → читает clipboard → translate → Cmd+V → восстанавливает clipboard.

 Если translate_selection IPC fails — показывает error HUD, текст/clipboard НЕ меняет.

 Hotkey по умолчанию: Cmd+Shift+T (конфигурируется через UserDefaults "KrabEar_SelectionHotkey").
 Target lang по умолчанию: "auto" (конфигурируется через UserDefaults "KrabEar_SelectionTargetLang").
*/

import AppKit
import ApplicationServices
import Foundation

// MARK: - SelectionTranslatorConfig

/// Конфигурация SelectionTranslator, сохраняемая в UserDefaults.
struct SelectionTranslatorConfig {
    /// Включён ли selection translate.
    var enabled: Bool
    /// Hotkey как строка: "cmd_shift_t" | "cmd_opt_t" | … (будущие варианты).
    var hotkey: String
    /// Target язык перевода: "auto" | "es" | "ru" | "en".
    var targetLang: String

    static let `default` = SelectionTranslatorConfig(
        enabled: false,
        hotkey: "cmd_shift_t",
        targetLang: "auto"
    )

    // MARK: UserDefaults keys
    static let enabledKey  = "KrabEar_SelectionTranslateEnabled"
    static let hotkeyKey   = "KrabEar_SelectionHotkey"
    static let targetKey   = "KrabEar_SelectionTargetLang"

    static func load(from defaults: UserDefaults = .standard) -> SelectionTranslatorConfig {
        return SelectionTranslatorConfig(
            enabled: defaults.object(forKey: enabledKey) != nil
                ? defaults.bool(forKey: enabledKey)
                : Self.default.enabled,
            hotkey: defaults.string(forKey: hotkeyKey) ?? Self.default.hotkey,
            targetLang: defaults.string(forKey: targetKey) ?? Self.default.targetLang
        )
    }

    func save(to defaults: UserDefaults = .standard) {
        defaults.set(enabled, forKey: Self.enabledKey)
        defaults.set(hotkey, forKey: Self.hotkeyKey)
        defaults.set(targetLang, forKey: Self.targetKey)
    }
}

// MARK: - Global event monitor

/// Контракт системного наблюдателя за глобальным нажатием клавиш.
///
/// Изоляция на MainActor сохраняет прежний жизненный цикл AppKit и позволяет
/// unit-тестам подставить счётчик, не регистрируя настоящий глобальный NSEvent monitor.
@MainActor
protocol SelectionEventMonitoring {
    func installKeyDownMonitor(
        handler: @escaping @Sendable (NSEvent) -> Void
    ) -> Any?

    func removeMonitor(_ monitor: Any)
}

/// Системная реализация для рабочего приложения, напрямую делегирующая NSEvent API.
@MainActor
struct SystemSelectionEventMonitor: SelectionEventMonitoring {
    /// Инициализатор без состояния безопасен как аргумент по умолчанию до входа в MainActor.
    nonisolated init() {}

    func installKeyDownMonitor(
        handler: @escaping @Sendable (NSEvent) -> Void
    ) -> Any? {
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown, handler: handler)
    }

    func removeMonitor(_ monitor: Any) {
        NSEvent.removeMonitor(monitor)
    }
}

// MARK: - SelectionTranslator

/// Глобальный hotkey Cmd+Shift+T → перевод выделенного текста in-place.
///
/// Порядок работы:
/// 1. Слушает .keyDown через глобальный NSEvent monitor.
/// 2. При срабатывании пробует AX path (readSelection / writeSelection).
/// 3. Если AX fails — clipboard fallback (save→Cmd+C→read→translate→Cmd+V→restore).
/// 4. IPC → translate_selection(text, target_lang) → translated_text.
/// 5. Показывает HUD «RU → ES: done (Xs)» 2 сек через NotificationService.
@MainActor
final class SelectionTranslator {

    // MARK: - Dependencies

    private let ipcClient: IPCClient
    private let notificationService: NotificationService
    private let eventMonitor: any SelectionEventMonitoring
    private let logger = AgentLogger.shared

    // MARK: - State

    var config: SelectionTranslatorConfig {
        didSet { applyConfig() }
    }

    private var globalMonitor: Any?
    private var isTranslating = false

    // MARK: - Init

    init(
        ipcClient: IPCClient,
        notificationService: NotificationService,
        defaults: UserDefaults = .standard,
        eventMonitor: any SelectionEventMonitoring = SystemSelectionEventMonitor()
    ) {
        self.ipcClient = ipcClient
        self.notificationService = notificationService
        self.eventMonitor = eventMonitor
        self.config = SelectionTranslatorConfig.load(from: defaults)
    }

    // MARK: - Lifecycle

    func start() {
        applyConfig()
        logger.info("SelectionTranslator запущен. enabled=\(config.enabled), hotkey=\(config.hotkey), target=\(config.targetLang)")
    }

    func stop() {
        removeMonitor()
        logger.info("SelectionTranslator остановлен.")
    }

    // MARK: - Config apply

    private func applyConfig() {
        removeMonitor()
        guard config.enabled else { return }
        installMonitor()
    }

    private func removeMonitor() {
        if let m = globalMonitor {
            eventMonitor.removeMonitor(m)
            globalMonitor = nil
        }
    }

    private func installMonitor() {
        globalMonitor = eventMonitor.installKeyDownMonitor { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handleKeyEvent(event)
            }
        }
    }

    // MARK: - Key event

    private func handleKeyEvent(_ event: NSEvent) {
        guard isHotkeyMatch(event) else { return }
        guard !isTranslating else {
            logger.info("SelectionTranslator: перевод уже выполняется, игнорируем повторный триггер")
            return
        }
        logger.info("SelectionTranslator: hotkey triggered")
        Task { @MainActor in
            await performTranslation()
        }
    }

    /// Проверяет соответствие event сконфигурированному hotkey.
    func isHotkeyMatch(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        switch config.hotkey {
        case "cmd_shift_t":
            // keyCode для T = 17 (kVK_ANSI_T)
            return flags == [.command, .shift] && event.keyCode == 17
        case "cmd_opt_t":
            return flags == [.command, .option] && event.keyCode == 17
        default:
            return flags == [.command, .shift] && event.keyCode == 17
        }
    }

    // MARK: - Translation pipeline

    @MainActor
    private func performTranslation() async {
        isTranslating = true
        defer { isTranslating = false }

        let startTime = Date()

        // 1. Попытаться AX path
        if let (text, element) = readSelectionViaAX() {
            logger.info("SelectionTranslator: AX path — выделено \(text.count) символов")
            guard let translated = await callTranslateIPC(text: text) else { return }
            let latency = Date().timeIntervalSince(startTime)

            // Пробуем записать обратно через AX
            if writeSelectionViaAX(element: element, text: translated) {
                logger.info("SelectionTranslator: AX path успешен (\(String(format: "%.2f", latency))s)")
                showSuccessHUD(original: text, translated: translated, latency: latency)
            } else {
                // AX write не удалась — fallback на clipboard paste
                logger.info("SelectionTranslator: AX write failed, fallback на clipboard paste")
                _savedClipboard = NSPasteboard.general.string(forType: .string)
                await clipboardPasteFallback(translatedText: translated)
                showSuccessHUD(original: text, translated: translated, latency: Date().timeIntervalSince(startTime))
            }
            return
        }

        // 2. Fallback — clipboard path
        logger.info("SelectionTranslator: AX read failed, clipboard fallback")
        guard let (originalText, clipboardChanged) = await readSelectionViaClipboard() else {
            showErrorHUD(reason: "Нет выделенного текста")
            return
        }
        guard clipboardChanged else {
            showErrorHUD(reason: "Нет выделенного текста")
            return
        }

        guard let translated = await callTranslateIPC(text: originalText) else { return }
        let latency = Date().timeIntervalSince(startTime)

        await clipboardPasteFallback(translatedText: translated)
        showSuccessHUD(original: originalText, translated: translated, latency: latency)
    }

    // MARK: - AX path

    /// Пытается прочитать kAXSelectedTextAttribute из focused element.
    /// Возвращает (selectedText, element) или nil если не удалось.
    func readSelectionViaAX() -> (String, AXUIElement)? {
        guard AXIsProcessTrusted() else { return nil }

        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else {
            return nil
        }
        let appElement = AXUIElementCreateApplication(pid)

        // Получаем focused element
        var focusedRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            appElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        ) == .success, let focusedRef else { return nil }

        guard CFGetTypeID(focusedRef) == AXUIElementGetTypeID() else { return nil }
        let focusedElement = focusedRef as! AXUIElement

        // Читаем selected text
        var selectedRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            focusedElement,
            kAXSelectedTextAttribute as CFString,
            &selectedRef
        ) == .success,
        let selectedRef,
        let selected = selectedRef as? String,
        !selected.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return nil }

        return (selected, focusedElement)
    }

    /// Записывает translated text в AX element через kAXSelectedTextAttribute.
    /// Returns true если успешно.
    func writeSelectionViaAX(element: AXUIElement, text: String) -> Bool {
        let status = AXUIElementSetAttributeValue(
            element,
            kAXSelectedTextAttribute as CFString,
            text as CFTypeRef
        )
        return status == .success
    }

    // MARK: - Clipboard fallback path

    /// Сохраняет clipboard, шлёт Cmd+C, ждёт 60ms, читает clipboard.
    /// Returns (selectedText, changed) или nil при ошибке.
    @MainActor
    private func readSelectionViaClipboard() async -> (String, Bool)? {
        let pasteboard = NSPasteboard.general
        let savedContents = pasteboard.string(forType: .string)
        let savedChangeCount = pasteboard.changeCount

        // Синтетический Cmd+C к frontmost app
        guard let targetPID = NSWorkspace.shared.frontmostApplication?.processIdentifier else {
            return nil
        }
        guard let source = CGEventSource(stateID: .hidSystemState) else { return nil }

        let cKeyCode: CGKeyCode = 8 // kVK_ANSI_C
        guard
            let keyDown = CGEvent(keyboardEventSource: source, virtualKey: cKeyCode, keyDown: true),
            let keyUp   = CGEvent(keyboardEventSource: source, virtualKey: cKeyCode, keyDown: false)
        else { return nil }

        keyDown.flags = .maskCommand
        keyUp.flags   = .maskCommand
        keyDown.postToPid(targetPID)
        let keyPressUs: useconds_t = 30_000
        usleep(keyPressUs)
        keyUp.postToPid(targetPID)

        // Ждём пока clipboard изменится (≤ 300ms, шаг 10ms)
        let waitMs = 300
        let stepUs: useconds_t = 10_000
        for _ in 0..<(waitMs / 10) {
            usleep(stepUs)
            if pasteboard.changeCount != savedChangeCount { break }
        }

        let newText = pasteboard.string(forType: .string) ?? ""
        let changed = !newText.isEmpty && newText != savedContents

        // Сохраняем исходный clipboard для восстановления (используется позже)
        _savedClipboard = savedContents
        return (newText, changed)
    }

    private var _savedClipboard: String?

    /// Вставляет текст через clipboard: ставит текст → Cmd+V → восстанавливает через 1s.
    @MainActor
    private func clipboardPasteFallback(translatedText: String) async {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(translatedText, forType: .string)

        guard let targetPID = NSWorkspace.shared.frontmostApplication?.processIdentifier else { return }
        guard let source = CGEventSource(stateID: .hidSystemState) else { return }

        let vKeyCode: CGKeyCode = Keycode.v.rawValue
        guard
            let keyDown = CGEvent(keyboardEventSource: source, virtualKey: vKeyCode, keyDown: true),
            let keyUp   = CGEvent(keyboardEventSource: source, virtualKey: vKeyCode, keyDown: false)
        else { return }

        keyDown.flags = .maskCommand
        keyUp.flags   = .maskCommand
        keyDown.postToPid(targetPID)
        usleep(30_000)
        keyUp.postToPid(targetPID)

        // Восстанавливаем старый clipboard через 1s (чтобы Cmd+V успел завершиться)
        let savedClipboard = _savedClipboard
        _savedClipboard = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            if let saved = savedClipboard {
                pasteboard.clearContents()
                pasteboard.setString(saved, forType: .string)
            } else {
                pasteboard.clearContents()
            }
        }
    }

    // MARK: - IPC

    /// Вызывает translate_selection IPC, возвращает переведённый текст или nil при ошибке.
    /// При ошибке показывает error HUD.
    @MainActor
    func callTranslateIPC(text: String) async -> String? {
        var params: [String: Any] = ["text": text]
        if config.targetLang != "auto" {
            params["target_lang"] = config.targetLang
        }

        // Capture locals for use inside Task.detached (avoids Sendable complaints on self)
        nonisolated(unsafe) let paramsCopy = params
        let client = ipcClient

        let response: [String: Any]?
        do {
            response = try await Task.detached(priority: .userInitiated) {
                try client.call(method: "translate_selection", params: paramsCopy)
            }.value
        } catch {
            logger.error("SelectionTranslator IPC error: \(error.localizedDescription)")
            await MainActor.run { self.showErrorHUD(reason: "Ошибка IPC: \(error.localizedDescription)") }
            return nil
        }

        guard let result = response?["result"] as? [String: Any] else {
            await MainActor.run { self.showErrorHUD(reason: "Backend вернул пустой ответ") }
            return nil
        }
        let translated = result["translated_text"] as? String
            ?? result["text"] as? String
            ?? ""
        if translated.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            showErrorHUD(reason: "Перевод пустой")
            return nil
        }
        return translated
    }

    // MARK: - HUD

    private func showSuccessHUD(original: String, translated: String, latency: TimeInterval) {
        let direction = inferDirection(original: original, translated: translated)
        let ms = Int(latency * 1000)
        let body = "\(direction): done (\(ms)ms)"
        notificationService.notify(title: "Перевод готов", body: body)
        logger.info("SelectionTranslator: \(body)")
    }

    func showErrorHUD(reason: String) {
        notificationService.notify(title: "Перевод не выполнен", body: reason)
        logger.warn("SelectionTranslator: ошибка — \(reason)")
    }

    /// Пытается угадать направление перевода для HUD.
    private func inferDirection(original: String, translated: String) -> String {
        // Упрощённый детект: кириллица → латиница = RU→ES и наоборот
        let hasCyrillic = original.unicodeScalars.contains { $0.value >= 0x0400 && $0.value <= 0x04FF }
        let translatedCyrillic = translated.unicodeScalars.contains { $0.value >= 0x0400 && $0.value <= 0x04FF }
        if hasCyrillic && !translatedCyrillic {
            return "RU → ES"
        } else if !hasCyrillic && translatedCyrillic {
            return "ES → RU"
        }
        return config.targetLang == "auto" ? "Auto" : "\(config.targetLang.uppercased())"
    }
}
