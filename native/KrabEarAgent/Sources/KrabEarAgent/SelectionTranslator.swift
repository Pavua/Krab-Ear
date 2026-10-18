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

// MARK: - Accessibility access

/// Непрозрачный токен выделенного AX-элемента.
/// Модульные тесты используют пустой токен, поэтому им не нужно активное приложение.
@MainActor
final class SelectionAXElement {
    fileprivate let systemElement: AXUIElement?

    /// Безопасный токен для подменных адаптеров и модульных тестов.
    init() {
        systemElement = nil
    }

    fileprivate init(systemElement: AXUIElement) {
        self.systemElement = systemElement
    }
}

/// Прочитанный текст вместе с токеном элемента, куда затем возвращается перевод.
@MainActor
struct SelectionAXSelection {
    let text: String
    let element: SelectionAXElement
}

/// Контракт чтения и записи выделения через Accessibility API.
/// Вынесен отдельно, чтобы обычные unit-тесты не трогали реальное активное приложение.
@MainActor
protocol SelectionAccessibilityAccess: AnyObject {
    func readSelection() -> SelectionAXSelection?
    func writeSelection(element: SelectionAXElement, text: String) -> Bool
    func acceptsCurrentFocus(element: SelectionAXElement?) -> Bool
}
extension SelectionAccessibilityAccess {
    func acceptsCurrentFocus(element: SelectionAXElement?) -> Bool { true }
}

/// Рабочая реализация пути AX. Все прежние системные вызовы остаются здесь
/// и подключаются к SelectionTranslator как зависимость по умолчанию.
@MainActor
final class SystemSelectionAccessibilityAccess: SelectionAccessibilityAccess {
    nonisolated init() {}

    func readSelection() -> SelectionAXSelection? {
        guard AXIsProcessTrusted() else { return nil }
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else {
            return nil
        }
        let appElement = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appElement, 0.1)

        var focusedRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            appElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        ) == .success, let focusedRef else { return nil }
        guard CFGetTypeID(focusedRef) == AXUIElementGetTypeID() else { return nil }
        let focusedElement = focusedRef as! AXUIElement

        var roleRef: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(focusedElement, kAXRoleAttribute as CFString, &roleRef)
        if (roleRef as? String) == "AXSecureTextField" { return nil }
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

        return SelectionAXSelection(
            text: selected,
            element: SelectionAXElement(systemElement: focusedElement)
        )
    }

    func acceptsCurrentFocus(element: SelectionAXElement?) -> Bool {
        guard AXIsProcessTrusted() else { return element == nil }
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else { return false }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.1)
        var focused: CFTypeRef?
        guard AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute as CFString, &focused) == .success,
              let focused, CFGetTypeID(focused) == AXUIElementGetTypeID() else { return element == nil }
        let current = focused as! AXUIElement
        var role: CFTypeRef?
        _ = AXUIElementCopyAttributeValue(current, kAXRoleAttribute as CFString, &role)
        guard role as? String != "AXSecureTextField" else { return false }
        if let element { return element.systemElement.map { CFEqual(current, $0) } ?? false }
        return true
    }

    func writeSelection(element: SelectionAXElement, text: String) -> Bool {
        guard let systemElement = element.systemElement else { return false }
        return AXUIElementSetAttributeValue(
            systemElement,
            kAXSelectedTextAttribute as CFString,
            text as CFTypeRef
        ) == .success
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

    private let pasteboard: NSPasteboard
    private let inputQueue: InputAutomationQueue
    private let targetPID: () -> pid_t?
    private let captureTargetLiveness: (pid_t) -> (() -> Bool)
    private let keyEvents: (pid_t, CGKeyCode, CGEventFlags) -> PasteKeyEventPair?
    private let translate: ((String) async -> String?)?
    private let ipcClient: IPCClient
    private let notificationService: NotificationService
    private let eventMonitor: any SelectionEventMonitoring
    private let accessibilityAccess: any SelectionAccessibilityAccess
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
        eventMonitor: any SelectionEventMonitoring = SystemSelectionEventMonitor(),
        accessibilityAccess: any SelectionAccessibilityAccess = SystemSelectionAccessibilityAccess(),
        pasteboard: NSPasteboard = .general, inputQueue: InputAutomationQueue = .shared,
        targetPID: @escaping () -> pid_t? = { NSWorkspace.shared.frontmostApplication?.processIdentifier },
        targetIsAlive: ((pid_t) -> Bool)? = nil,
        keyEvents: @escaping (pid_t, CGKeyCode, CGEventFlags) -> PasteKeyEventPair? = PasteService.systemKeyEvents,
        translate: ((String) async -> String?)? = nil
    ) {
        self.pasteboard = pasteboard; self.inputQueue = inputQueue; self.targetPID = targetPID
        self.keyEvents = keyEvents; self.translate = translate; self.captureTargetLiveness = { pid in
            if let targetIsAlive { return { targetIsAlive(pid) } }
            let application = NSRunningApplication(processIdentifier: pid)
            return { application?.isTerminated == false }
        }
        self.ipcClient = ipcClient
        self.notificationService = notificationService
        self.eventMonitor = eventMonitor
        self.accessibilityAccess = accessibilityAccess
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
    func performTranslation() async {
        guard !isTranslating, let pid = targetPID() else { return }
        let isAlive = captureTargetLiveness(pid)
        guard isAlive() else { return }
        isTranslating = true; defer { isTranslating = false }
        let epoch = inputQueue.generation
        let started = Date()
        guard case .success(let readLease) = await inputQueue.acquire(deadline: .now + .seconds(5)) else { return }
        guard !Task.isCancelled, targetPID() == pid, isAlive(), inputQueue.generation == epoch else {
            inputQueue.release(readLease); return
        }
        guard !hasConcealedClipboard(), accessibilityAccess.acceptsCurrentFocus(element: nil) else {
            inputQueue.release(readLease); return
        }
        let selection = readSelectionViaAX()
        let saved = pasteboard.string(forType: .string)
        let original: String
        var expectedCount = pasteboard.changeCount
        if let selection { original = selection.0 }
        else {
            guard !hasConcealedClipboard(), let events = keyEvents(pid, 8, .maskCommand) else {
                inputQueue.release(readLease); return
            }
            let before = pasteboard.changeCount
            events.down(); try? await Task.sleep(for: .milliseconds(30)); events.up()
            let copyDeadline = min(readLease.deadline, .now + .milliseconds(300))
            while pasteboard.changeCount == before && ContinuousClock.now < copyDeadline && !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(10))
            }
            guard !Task.isCancelled, ContinuousClock.now < readLease.deadline,
                  isAlive(), targetPID() == pid, inputQueue.generation == epoch,
                  !hasConcealedClipboard(), accessibilityAccess.acceptsCurrentFocus(element: nil) else {
                inputQueue.release(readLease); return
            }
            expectedCount = pasteboard.changeCount
            original = expectedCount != before ? (pasteboard.string(forType: .string) ?? "") : ""
        }
        inputQueue.release(readLease)
        guard !original.isEmpty, !Task.isCancelled else { return }
        // Долгий IPC никогда не удерживает input lease.
        guard let translated = await callTranslateIPC(text: original) else {
            if selection == nil { await restoreClipboard(saved, expectedCount: expectedCount) }
            return
        }
        guard case .success(let writeLease) = await inputQueue.acquire(deadline: .now + .seconds(5)) else { return }
        defer { inputQueue.release(writeLease) }
        guard !Task.isCancelled, targetPID() == pid, isAlive(),
              inputQueue.generation == epoch, pasteboard.changeCount == expectedCount,
              !hasConcealedClipboard(), accessibilityAccess.acceptsCurrentFocus(element: selection?.1) else { return }
        if let selection {
            // AX write — самостоятельный канал. False не разрешает повторный Cmd+V.
            guard writeSelectionViaAX(element: selection.1, text: translated) else {
                showErrorHUD(reason: "Не удалось подтвердить замену выделения"); return
            }
        } else {
            guard !hasConcealedClipboard(), let events = keyEvents(pid, Keycode.v.rawValue, .maskCommand) else { return }
            let beforeWrite = pasteboard.changeCount
            pasteboard.clearContents()
            guard pasteboard.setString(translated, forType: .string) else { return }
            let ownCount = pasteboard.changeCount
            inputQueue.recordClipboardWrite(board: pasteboard.name.rawValue, before: beforeWrite, after: ownCount)
            events.down(); try? await Task.sleep(for: .milliseconds(30)); events.up()
            // CAS: даже собственная новая запись пользователя не затирается restore.
            // Lease удерживается на время чтения target clipboard из Cmd+V.
            try? await Task.sleep(for: .seconds(1))
            if pasteboard.changeCount == ownCount {
                pasteboard.clearContents()
                if let saved { pasteboard.setString(saved, forType: .string) }
                inputQueue.recordClipboardWrite(board: pasteboard.name.rawValue, before: ownCount, after: pasteboard.changeCount)
            }
        }
        showSuccessHUD(original: original, translated: translated, latency: Date().timeIntervalSince(started))
    }
    private func hasConcealedClipboard() -> Bool {
        pasteboard.types?.contains(.init("org.nspasteboard.ConcealedType")) ?? false
    }
    private func restoreClipboard(_ saved: String?, expectedCount: Int) async {
        guard case .success(let lease) = await inputQueue.acquire(deadline: .now + .seconds(5)) else { return }
        defer { inputQueue.release(lease) }
        guard pasteboard.changeCount == expectedCount else { return }
        pasteboard.clearContents(); if let saved { pasteboard.setString(saved, forType: .string) }
    }

    // MARK: - AX path

    /// Пытается прочитать kAXSelectedTextAttribute из focused element.
    /// Возвращает (selectedText, element) или nil если не удалось.
    func readSelectionViaAX() -> (String, SelectionAXElement)? {
        guard let selection = accessibilityAccess.readSelection() else { return nil }
        return (selection.text, selection.element)
    }

    /// Записывает translated text в AX element через kAXSelectedTextAttribute.
    /// Returns true если успешно.
    func writeSelectionViaAX(element: SelectionAXElement, text: String) -> Bool {
        accessibilityAccess.writeSelection(element: element, text: text)
    }

    // MARK: - IPC

    /// Вызывает translate_selection IPC, возвращает переведённый текст или nil при ошибке.
    /// При ошибке показывает error HUD.
    @MainActor
    func callTranslateIPC(text: String) async -> String? {
        if let translate { return await translate(text) }
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
