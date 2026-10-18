import AppKit
import ApplicationServices
import Foundation

enum PasteEffect: String, Sendable { case noEffects, completed, partial, unknown }
struct PasteAttemptResult {
    let ok: Bool
    let reason: String
    let effect: PasteEffect
    let backspacePairs: Int
    init(ok: Bool, reason: String, effect: PasteEffect? = nil, backspacePairs: Int = 0) {
        self.ok = ok; self.reason = reason
        self.effect = effect ?? (ok ? .completed : .noEffects)
        self.backspacePairs = backspacePairs
    }
}
struct PasteInputSnapshot { let pid: pid_t; let epoch: UInt64; let changeCount: Int; let isAlive: () -> Bool }
struct PasteKeyEventPair { let down: () -> Void; let up: () -> Void }

/// Один запрос владеет lease вплоть до key-up или подтверждённого cleanup child.
final class PasteService {
    let inputQueue: InputAutomationQueue
    private let pasteboard: NSPasteboard
    private let defaults: UserDefaults
    private let repastePerformer: ((String) -> PasteAttemptResult)?
    private let modifierKeysDown: () -> Bool
    private let accessibilityTrusted: () -> Bool
    private let keyEventFactory: (pid_t, CGKeyCode, CGEventFlags) -> PasteKeyEventPair?
    private let frontmostPID: () -> pid_t?
    private let captureTargetLiveness: (pid_t) -> (() -> Bool)
    private let focusRole: ((pid_t) -> String?)?
    private let focusEditable: ((pid_t) -> Bool?)?
    private let collapseSelection: ((pid_t) -> Void)?
    private let runner: any AudioDuckingCommandRunning
    private let operationBudget: Duration
    private let logger = AgentLogger.shared
    var smartFieldFormatEnabled = false
    var onSecureFieldSkipped: (() -> Void)?
    var onConcealedClipboardSkipped: (() -> Void)?
    private let lastPastedTextKey = "KrabEar_LastPastedText"
    let repasteCooldownSec: TimeInterval = 1
    private var lastPastedAt: Date?
    init(pasteboard: NSPasteboard = .general, defaults: UserDefaults = .standard,
         repastePerformer: ((String) -> PasteAttemptResult)? = nil,
         modifierKeysDown: (() -> Bool)? = nil,
         accessibilityTrusted: @escaping () -> Bool = { AXIsProcessTrusted() },
         keyEventFactory: ((pid_t, CGKeyCode, CGEventFlags) -> PasteKeyEventPair?)? = nil,
         inputQueue: InputAutomationQueue = .shared,
         frontmostPID: @escaping () -> pid_t? = { NSWorkspace.shared.frontmostApplication?.processIdentifier },
         targetIsAlive: ((pid_t) -> Bool)? = nil,
         focusRole: ((pid_t) -> String?)? = nil,
         focusEditable: ((pid_t) -> Bool?)? = nil,
         collapseSelection: ((pid_t) -> Void)? = nil,
         runner: any AudioDuckingCommandRunning = SystemAudioDuckingCommandRunner(),
         operationBudget: Duration = .seconds(5)) {
        self.pasteboard = pasteboard; self.defaults = defaults; self.repastePerformer = repastePerformer
        self.modifierKeysDown = modifierKeysDown ?? {
            let rOpt = CGEventSource.keyState(.combinedSessionState, key: Keycode.rightOption.rawValue)
            let lOpt = CGEventSource.keyState(.combinedSessionState, key: Keycode.leftOption.rawValue)
            let lCmd = CGEventSource.keyState(.combinedSessionState, key: Keycode.leftCommand.rawValue)
            let rCmd = CGEventSource.keyState(.combinedSessionState, key: Keycode.rightCommand.rawValue)
            return rOpt || lOpt || lCmd || rCmd
        }
        self.accessibilityTrusted = accessibilityTrusted
        self.keyEventFactory = keyEventFactory ?? Self.systemKeyEvents
        self.inputQueue = inputQueue; self.frontmostPID = frontmostPID
        self.captureTargetLiveness = { pid in
            if let targetIsAlive { return { targetIsAlive(pid) } }
            let application = NSRunningApplication(processIdentifier: pid)
            return { application?.isTerminated == false }
        }
        self.focusRole = focusRole; self.focusEditable = focusEditable; self.collapseSelection = collapseSelection
        self.runner = runner; self.operationBudget = operationBudget
    }
    var lastPastedText: String? {
        get { defaults.string(forKey: lastPastedTextKey) }
        set { defaults.set(newValue, forKey: lastPastedTextKey) }
    }
    func recordLastPaste(_ text: String) { lastPastedText = text; lastPastedAt = Date() }
    @MainActor func repastLast() async -> PasteAttemptResult {
        guard let text = lastPastedText, !text.isEmpty else { return refusal("no_last_paste") }
        if let at = lastPastedAt, Date().timeIntervalSince(at) < repasteCooldownSec { return refusal("repaste_too_soon") }
        if let repastePerformer { return repastePerformer(text) }
        return await pasteToFrontmostApp(text)
    }
    private func pasteboardHoldsConcealedContent(_ board: NSPasteboard) -> Bool {
        board.types?.contains(.init("org.nspasteboard.ConcealedType")) ?? false
    }
    @discardableResult func putToClipboard(_ text: String) -> Bool {
        inputQueue.invalidatePending()
        guard !pasteboardHoldsConcealedContent(pasteboard) else { onConcealedClipboardSkipped?(); return false }
        pasteboard.clearContents(); return pasteboard.setString(text, forType: .string)
    }
    func putToClipboardUserInitiated(_ text: String) {
        inputQueue.invalidatePending(); pasteboard.clearContents(); pasteboard.setString(text, forType: .string)
    }
    static func revisionBudget(count: Int) -> Duration? {
        guard count >= 0 else { return nil }
        let (millis, overflow) = Int64(count).multipliedReportingOverflow(by: 35)
        let (total, overflow2) = millis.addingReportingOverflow(4000)
        guard !overflow, !overflow2 else { return nil }
        return .milliseconds(total)
    }
    private func refusal(_ reason: String, pairs: Int = 0, effect: PasteEffect? = nil) -> PasteAttemptResult {
        PasteAttemptResult(ok: false, reason: reason, effect: effect ?? (pairs > 0 ? .partial : .noEffects), backspacePairs: pairs)
    }
    @MainActor func captureTargetPID() -> pid_t? { frontmostPID() }
    @MainActor func captureInput(pid: pid_t) -> PasteInputSnapshot {
        PasteInputSnapshot(pid: pid, epoch: inputQueue.generation, changeCount: pasteboard.changeCount, isAlive: captureTargetLiveness(pid))
    }
    func deadlineForPaste() -> ContinuousClock.Instant { .now + operationBudget }
    @MainActor func pasteToFrontmostApp(_ text: String, targetPID: pid_t? = nil,
                                      lease: InputAutomationLease? = nil, snapshot: PasteInputSnapshot? = nil) async -> PasteAttemptResult {
        await perform(text: text, deleteCount: 0, targetPID: targetPID, lease: lease, snapshot: snapshot)
    }
    @MainActor func deleteBackward(count: Int, targetPID: pid_t? = nil) async -> PasteAttemptResult {
        guard count > 0 else { return refusal("empty_count") }
        return await perform(text: nil, deleteCount: count, targetPID: targetPID, lease: nil)
    }
    /// Очередь могла ждать чужой input. Проверка не требует foreground до активации.
    @MainActor func validateQueuedSnapshot(_ snapshot: PasteInputSnapshot, lease: InputAutomationLease) -> String? {
        if !inputQueue.owns(lease) { return "input_quarantined" }
        if Task.isCancelled { return "input_cancelled" }
        if ContinuousClock.now >= lease.deadline { return "input_deadlineExceeded" }
        if inputQueue.generation != snapshot.epoch { return "clipboard_changed" }
        if !inputQueue.ownsClipboardChanges(board: pasteboard.name.rawValue, from: snapshot.changeCount, to: pasteboard.changeCount) {
            return "clipboard_changed"
        }
        return nil
    }
    @MainActor func revise(deleteCount: Int, text: String, targetPID: pid_t?) async -> PasteAttemptResult {
        guard deleteCount >= 0 else { return refusal("invalid_count") }
        return await perform(text: text, deleteCount: deleteCount, targetPID: targetPID, lease: nil)
    }
    @MainActor private func perform(text: String?, deleteCount: Int, targetPID: pid_t?, lease supplied: InputAutomationLease?, snapshot: PasteInputSnapshot? = nil) async -> PasteAttemptResult {
        let clean = text?.trimmingCharacters(in: .whitespacesAndNewlines)
        if deleteCount == 0 && (clean?.isEmpty ?? true) { return refusal("empty_text") }
        guard let budget: Duration = deleteCount > 0 ? Self.revisionBudget(count: deleteCount) : operationBudget else { return refusal("invalid_count") }
        let deadline = supplied?.deadline ?? (.now + budget)
        if pasteboardHoldsConcealedContent(pasteboard) { onConcealedClipboardSkipped?(); return refusal("concealed_clipboard_skipped") }
        // Все значения принадлежат этому запросу и не перечитываются из delegate после await.
        guard let pid = targetPID ?? frontmostPID(), pid != ProcessInfo.processInfo.processIdentifier else { return refusal("no_external_target") }
        if let snapshot, snapshot.pid != pid { return refusal("operation_target_changed") }
        let isAlive = snapshot?.isAlive ?? captureTargetLiveness(pid)
        let epoch = snapshot?.epoch ?? inputQueue.generation
        var clipboardCount = snapshot?.changeCount ?? pasteboard.changeCount
        let lease: InputAutomationLease
        if let supplied { lease = supplied }
        else {
            switch await inputQueue.acquire(deadline: deadline) {
            case .failure(let why): return refusal("input_" + why.rawValue)
            case .success(let acquired): lease = acquired
            }
        }
        defer { if supplied == nil { inputQueue.release(lease) } }
        func validate() -> String? {
            if !inputQueue.owns(lease) { return "input_quarantined" }
            if Task.isCancelled { return "input_cancelled" }
            if ContinuousClock.now >= deadline { return "input_deadlineExceeded" }
            if !isAlive() || frontmostPID() != pid { return "target_changed" }
            if inputQueue.generation != epoch { return "clipboard_changed" }
            if pasteboard.changeCount != clipboardCount {
                guard inputQueue.ownsClipboardChanges(board: pasteboard.name.rawValue, from: clipboardCount, to: pasteboard.changeCount) else { return "clipboard_changed" }
                clipboardCount = pasteboard.changeCount
            }
            if pasteboardHoldsConcealedContent(pasteboard) { onConcealedClipboardSkipped?(); return "concealed_clipboard_skipped" }
            if accessibilityTrusted(), readRole(pid) == "AXSecureTextField" { onSecureFieldSkipped?(); return "secure_field_skipped" }
            return nil
        }
        if let why = validate() { return refusal(why) }
        let modifierDeadline = min(deadline, .now + .milliseconds(2500))
        while modifierKeysDown() {
            if let why = validate() { return refusal(why) }
            if ContinuousClock.now >= modifierDeadline { return refusal("modifiers_stuck") }
            do { try await Task.sleep(for: .milliseconds(20)) } catch { return refusal("input_cancelled") }
        }
        do { try await Task.sleep(for: .milliseconds(120)) } catch { return refusal("input_cancelled") }
        if let why = validate() { return refusal(why) }
        let trusted = accessibilityTrusted()
        if trusted, readEditable(pid) == false { return refusal("no_editable_focus") }
        var insert = text
        if smartFieldFormatEnabled, trusted,
           ["AXSearchField", "AXComboBox"].contains(readRole(pid) ?? "") {
            insert = textForSearchField(clean ?? "")
        }
        if deleteCount > 0 {
            // Checked budget выше гарантирует, что count*30 представимо.
            if ContinuousClock.now.duration(to: deadline) < .milliseconds(Int64(deleteCount) * 30 + (insert == nil ? 0 : 150)) {
                return refusal("input_insufficient_budget")
            }
        }
        if !trusted {
            // Один bounded канал. После старта AppleEvent любой неуспех консервативно unknown.
            if let insert {
                let beforeWrite = pasteboard.changeCount
                pasteboard.clearContents()
                guard pasteboard.setString(insert, forType: .string) else { return refusal("clipboard_write_failed") }
                clipboardCount = pasteboard.changeCount
                inputQueue.recordClipboardWrite(board: pasteboard.name.rawValue, before: beforeWrite, after: clipboardCount)
            }
            if let why = validate() { return refusal(why) }
            let command = "tell application \"System Events\"\nset p to first application process whose unix id is \(pid)\nif frontmost of p is false then error \"target_changed\"\ntell p\n" +
                (deleteCount > 0 ? "repeat \(deleteCount) times\nkey code 51\nend repeat\n" : "") +
                (insert != nil ? "keystroke \"v\" using command down\n" : "") + "end tell\nend tell"
            let runner = self.runner
            let result = await Task.detached { runner.run(script: command, deadline: deadline) }.value
            switch result {
            case .success:
                // Script подтвердил отправку команд, но за время await мог смениться
                // target/clipboard. Это unknown результата, а не право на повтор ввода.
                if let why = validate() { return refusal(why, pairs: deleteCount, effect: .unknown) }
                if let insert { recordLastPaste(insert.trimmingCharacters(in: .whitespacesAndNewlines)) }
                return PasteAttemptResult(ok: true, reason: "ok_via_osascript", backspacePairs: deleteCount)
            case .failure(let why, let cleanup):
                if !cleanup { inputQueue.quarantine() }
                return refusal("automation_" + why.rawValue, effect: .unknown)
            }
        }
        var pairs = 0
        for _ in 0..<deleteCount {
            if let why = validate() { return refusal(why, pairs: pairs) }
            guard let events = keyEventFactory(pid, Keycode.delete.rawValue, []) else { return refusal("event_post_failed", pairs: pairs) }
            events.down()
            // Ошибка sleep/cancellation не может пропустить обязательный key-up.
            try? await Task.sleep(for: .milliseconds(15))
            events.up(); pairs += 1
            try? await Task.sleep(for: .milliseconds(15))
        }
        if let why = validate() { return refusal(why, pairs: pairs) }
        if let insert {
            guard let events = keyEventFactory(pid, Keycode.v.rawValue, .maskCommand) else { return refusal("event_post_failed", pairs: pairs) }
            let beforeWrite = pasteboard.changeCount
            pasteboard.clearContents()
            guard pasteboard.setString(insert, forType: .string) else { return refusal("clipboard_write_failed", pairs: pairs) }
            clipboardCount = pasteboard.changeCount
            inputQueue.recordClipboardWrite(board: pasteboard.name.rawValue, before: beforeWrite, after: clipboardCount)
            if let why = validate() { return refusal(why, pairs: pairs) }
            events.down()
            try? await Task.sleep(for: .milliseconds(30))
            events.up()
            // Key-up обязателен, но drift после down оставляет результат неизвестным.
            if let why = validate() { return refusal(why, pairs: pairs, effect: .unknown) }
            if readEditable(pid) == true {
                if let collapseSelection { collapseSelection(pid) } else { collapseSelectionIfNeeded(pid: pid) }
            }
            recordLastPaste(insert.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return PasteAttemptResult(ok: true, reason: "ok", backspacePairs: pairs)
    }
    static func systemKeyEvents(pid: pid_t, key: CGKeyCode, flags: CGEventFlags) -> PasteKeyEventPair? {
        guard let source = CGEventSource(stateID: .hidSystemState),
              let down = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: false) else { return nil }
        down.flags = flags; up.flags = flags
        return PasteKeyEventPair(down: { down.postToPid(pid) }, up: { up.postToPid(pid) })
    }
    private func readRole(_ pid: pid_t) -> String? {
        if let focusRole { return focusRole(pid) }
        return focusedElementRole(pid: pid)
    }
    private func readEditable(_ pid: pid_t) -> Bool? {
        if let focusEditable { return focusEditable(pid) }
        let state = inspectFocusedElementState(pid: pid)
        return state == .unknown ? nil : state != .nonEditable
    }
    private enum FocusState { case editable, nonEditable, unknown }
    private func focusedElementRole(pid: pid_t) -> String? {
        let appElement = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appElement, 0.1)
        var focusedRef: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(
            appElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        )
        guard status == .success, let focusedRef else { return nil }
        guard CFGetTypeID(focusedRef) == AXUIElementGetTypeID() else { return nil }
        let focusedElement = focusedRef as! AXUIElement

        var roleRef: CFTypeRef?
        let roleStatus = AXUIElementCopyAttributeValue(
            focusedElement,
            kAXRoleAttribute as CFString,
            &roleRef
        )
        guard roleStatus == .success, let role = roleRef as? String else { return nil }
        return role
    }

    /// Преобразует текст для вставки в поле поиска/комбобокс: снимает хвостовой пробел
    /// и хвостовой пунктуационный символ (`.`, `!`, `?`) из TRAILING run.
    private func textForSearchField(_ text: String) -> String {
        var result = text
        // Сначала убираем хвостовой пробел.
        while result.last?.isWhitespace == true {
            result.removeLast()
        }
        // Затем убираем хвостовой пунктуационный символ (только один).
        if let last = result.last, ".!?".contains(last) {
            result.removeLast()
        }
        return result
    }
    private func inspectFocusedElementState(pid: pid_t) -> FocusState {
        let appElement = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appElement, 0.1)
        var focusedRef: CFTypeRef?
        let focusedStatus = AXUIElementCopyAttributeValue(
            appElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        )
        guard focusedStatus == .success, let focusedRef else {
            return .unknown
        }
        guard CFGetTypeID(focusedRef) == AXUIElementGetTypeID() else {
            logger.warn("[PasteService] inspectFocusedElementState: focusedRef не является AXUIElement (typeID=\(CFGetTypeID(focusedRef))), pid=\(pid)")
            return .unknown
        }
        let focusedElement = focusedRef as! AXUIElement

        var editableRef: CFTypeRef?
        let editableStatus = AXUIElementCopyAttributeValue(
            focusedElement,
            "AXEditable" as CFString,
            &editableRef
        )
        if editableStatus == .success,
           let editable = editableRef as? Bool,
           editable {
            return .editable
        }

        var roleRef: CFTypeRef?
        let roleStatus = AXUIElementCopyAttributeValue(
            focusedElement,
            kAXRoleAttribute as CFString,
            &roleRef
        )
        guard roleStatus == .success, let role = roleRef as? String else {
            return .unknown
        }

        let editableRoles: Set<String> = [
            "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox",
            "AXWebArea", "AXTextView", "AXDocument",
        ]
        return editableRoles.contains(role) ? .editable : .nonEditable
    }

    private func collapseSelectionIfNeeded(pid: pid_t) {
        let appElement = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appElement, 0.1)
        var focusedRef: CFTypeRef?
        let focusedStatus = AXUIElementCopyAttributeValue(
            appElement,
            kAXFocusedUIElementAttribute as CFString,
            &focusedRef
        )
        guard focusedStatus == .success, let focusedRef else { return }
        guard CFGetTypeID(focusedRef) == AXUIElementGetTypeID() else {
            logger.warn("[PasteService] collapseSelectionIfNeeded: focusedRef не является AXUIElement (typeID=\(CFGetTypeID(focusedRef))), pid=\(pid)")
            return
        }
        let focusedElement = focusedRef as! AXUIElement

        var selectedRangeRef: CFTypeRef?
        let selectedStatus = AXUIElementCopyAttributeValue(
            focusedElement,
            kAXSelectedTextRangeAttribute as CFString,
            &selectedRangeRef
        )
        guard selectedStatus == .success, let selectedRangeRef else { return }
        guard CFGetTypeID(selectedRangeRef) == AXValueGetTypeID() else {
            logger.warn("[PasteService] collapseSelectionIfNeeded: selectedRangeRef не является AXValue (typeID=\(CFGetTypeID(selectedRangeRef))), pid=\(pid)")
            return
        }
        let selectedRangeValue = selectedRangeRef as! AXValue
        guard AXValueGetType(selectedRangeValue) == .cfRange else { return }

        var selectedRange = CFRange()
        guard AXValueGetValue(selectedRangeValue, .cfRange, &selectedRange) else { return }
        guard selectedRange.length > 0 else { return }

        var caretRange = CFRange(location: selectedRange.location + selectedRange.length, length: 0)
        guard let caretValue = AXValueCreate(.cfRange, &caretRange) else { return }
        let setStatus = AXUIElementSetAttributeValue(
            focusedElement,
            kAXSelectedTextRangeAttribute as CFString,
            caretValue
        )
        if setStatus == .success {
            logger.info("Выделение после вставки снято через AX (pid=\(pid))")
        }
    }


}
