/*
 Вставка текста в активное приложение macOS.

 Связи модуля:
 1) main.swift: использует сервис после получения транскрибации.
*/

import AppKit
import ApplicationServices
import Foundation

/// Результат попытки вставки текста в активное приложение.
struct PasteAttemptResult {
    let ok: Bool
    let reason: String
}

/// Нативная вставка текста в активное приложение через буфер обмена и Cmd+V.
final class PasteService {
    // macOS virtual key codes for modifier keys
    private let rightOptionKeyCode: CGKeyCode = Keycode.rightOption.rawValue
    private let leftOptionKeyCode: CGKeyCode = Keycode.leftOption.rawValue
    private let leftCommandKeyCode: CGKeyCode = Keycode.leftCommand.rawValue
    private let rightCommandKeyCode: CGKeyCode = Keycode.rightCommand.rawValue
    /// Max time to wait for modifier keys release before giving up on paste (ms)
    private let modifierReleaseTimeoutMs = 2_500
    /// Delay after modifier release before pasting, lets the OS finish key-up processing (µs)
    private let prePasteDelayUs: useconds_t = 120_000
    /// Delay between Cmd+V key-down and key-up events (µs)
    private let cmdVKeypressDelayUs: useconds_t = 30_000
    private let logger = AgentLogger.shared
    /// Throttle для Accessibility prompt'ов: показываем dialog не чаще чем раз
    /// в 10 минут per app launch. Без этого каждый paste при missing AX = новый
    /// блокирующий dialog (ad-hoc codesigned apps теряют TCC permission после
    /// каждого rebuild из-за смены cdhash). Текст остаётся в clipboard —
    /// пользователь может вставить вручную Cmd+V.
    private var lastAXPromptAt: Date?
    private let axPromptCooldownSec: TimeInterval = 600

    private enum FocusState {
        case editable
        case nonEditable
        case unknown
    }

    func putToClipboard(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }

    func pasteToFrontmostApp(_ text: String, targetPID: pid_t? = nil) -> PasteAttemptResult {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            return PasteAttemptResult(ok: false, reason: "empty_text")
        }

        putToClipboard(text)
        let axTrusted = isAccessibilityTrusted()

        guard waitForModifierRelease(timeoutMs: modifierReleaseTimeoutMs) else {
            logger.warn("Автовставка отменена: модификаторы не отпущены в таймаут")
            return PasteAttemptResult(ok: false, reason: "modifiers_stuck")
        }
        usleep(prePasteDelayUs)

        let resolvedPID: pid_t
        if let targetPID {
            resolvedPID = targetPID
        } else if let frontmostPID = NSWorkspace.shared.frontmostApplication?.processIdentifier,
                  frontmostPID != ProcessInfo.processInfo.processIdentifier {
            resolvedPID = frontmostPID
        } else {
            return PasteAttemptResult(ok: false, reason: "no_external_target")
        }

        let focusState = axTrusted ? inspectFocusedElementState(pid: resolvedPID) : .unknown
        if focusState == .nonEditable {
            logger.warn("Автовставка: фокус не на текстовом элементе (pid=\(resolvedPID))")
            return PasteAttemptResult(ok: false, reason: "no_editable_focus")
        }

        // Ключевой принцип: одна попытка вставки = один канал ввода.
        // Иначе часть приложений вставляет текст дубликатами (2-3 раза).
        let inserted: Bool
        let resultReason: String
        if axTrusted {
            inserted = postCommandVToPid(resolvedPID)
            resultReason = inserted ? "ok" : "event_post_failed"
        } else {
            inserted = runAppleScriptPaste() || runExternalOsaScriptPaste()
            if inserted {
                logger.info("Автовставка выполнена через osascript fallback (AX=false)")
                resultReason = "ok_via_osascript"
            } else {
                requestAccessibilityPromptIfNeeded()
                logger.warn("Автовставка недоступна: AX=false и fallback osascript не сработал")
                resultReason = "accessibility_not_granted"
            }
        }

        guard inserted else {
            return PasteAttemptResult(ok: false, reason: resultReason)
        }

        // Иногда после вставки UI оставляет выделение текста.
        // Пытаемся аккуратно свернуть выделение в каретку в конце выделенного блока.
        if axTrusted {
            collapseSelectionIfNeeded(pid: resolvedPID)
        }

        // Если фокус определить не удалось, считаем попытку условно успешной:
        // в этом случае UI покажет статус ok, но в логе останется подробная диагностика.
        if focusState == .unknown {
            logger.info("Автовставка выполнена без AX-подтверждения фокуса (pid=\(resolvedPID))")
            return PasteAttemptResult(ok: true, reason: resultReason)
        }

        return PasteAttemptResult(ok: true, reason: resultReason)
    }

    private func postCommandVToPid(_ targetPID: pid_t) -> Bool {
        guard let source = CGEventSource(stateID: .hidSystemState) else { return false }
        guard let (keyDown, keyUp) = makeCommandVEvents(source: source) else { return false }
        keyDown.postToPid(targetPID)
        usleep(cmdVKeypressDelayUs)
        keyUp.postToPid(targetPID)
        return true
    }

    private func postCommandVToHID() -> Bool {
        guard let source = CGEventSource(stateID: .hidSystemState) else { return false }
        guard let (keyDown, keyUp) = makeCommandVEvents(source: source) else { return false }
        keyDown.post(tap: .cghidEventTap)
        usleep(cmdVKeypressDelayUs)
        keyUp.post(tap: .cghidEventTap)
        return true
    }

    private func makeCommandVEvents(source: CGEventSource) -> (CGEvent, CGEvent)? {
        let vKeyCode = Keycode.v.rawValue
        guard
            let keyDown = CGEvent(
                keyboardEventSource: source,
                virtualKey: vKeyCode,
                keyDown: true
            ),
            let keyUp = CGEvent(
                keyboardEventSource: source,
                virtualKey: vKeyCode,
                keyDown: false
            )
        else {
            return nil
        }
        keyDown.flags = .maskCommand
        keyUp.flags = .maskCommand
        return (keyDown, keyUp)
    }

    private func runAppleScriptPaste() -> Bool {
        let scriptSource = "tell application \"System Events\" to keystroke \"v\" using command down"
        guard let script = NSAppleScript(source: scriptSource) else {
            return false
        }

        var errorDict: NSDictionary?
        _ = script.executeAndReturnError(&errorDict)
        if let errorDict {
            logger.warn("AppleScript fallback вставки вернул ошибку: \(errorDict)")
            return false
        }
        return true
    }

    private func runExternalOsaScriptPaste() -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", "tell application \"System Events\" to keystroke \"v\" using command down"]

        let stderrPipe = Pipe()
        process.standardError = stderrPipe

        do {
            try process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 {
                return true
            }
            let errData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
            if let errText = String(data: errData, encoding: .utf8), !errText.isEmpty {
                logger.warn("osascript fallback завершился с ошибкой: \(errText.trimmingCharacters(in: .whitespacesAndNewlines))")
            }
            return false
        } catch {
            logger.warn("Не удалось запустить osascript fallback: \(error.localizedDescription)")
            return false
        }
    }

    private func inspectFocusedElementState(pid: pid_t) -> FocusState {
        let appElement = AXUIElementCreateApplication(pid)
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

    private func isAccessibilityTrusted() -> Bool {
        AXIsProcessTrusted()
    }

    private func collapseSelectionIfNeeded(pid: pid_t) {
        let appElement = AXUIElementCreateApplication(pid)
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

    private func requestAccessibilityPromptIfNeeded() {
        // Throttle: не чаще раз в 10 минут per app session.
        // Иначе каждый paste при missing AX = новый blocking dialog,
        // что критично для ad-hoc signed apps (cdhash меняется каждый rebuild).
        if let lastPrompt = lastAXPromptAt,
           Date().timeIntervalSince(lastPrompt) < axPromptCooldownSec {
            logger.info("Accessibility prompt пропущен (cooldown \(Int(axPromptCooldownSec))s)")
            return
        }
        let promptKey = "AXTrustedCheckOptionPrompt"
        let options = [promptKey: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        lastAXPromptAt = Date()
    }

    private func waitForModifierRelease(timeoutMs: Int) -> Bool {
        let stepUs: useconds_t = 20_000
        var waitedMs = 0
        while waitedMs < timeoutMs {
            let rightOptionDown = CGEventSource.keyState(.combinedSessionState, key: rightOptionKeyCode)
            let leftOptionDown = CGEventSource.keyState(.combinedSessionState, key: leftOptionKeyCode)
            let leftCommandDown = CGEventSource.keyState(.combinedSessionState, key: leftCommandKeyCode)
            let rightCommandDown = CGEventSource.keyState(.combinedSessionState, key: rightCommandKeyCode)
            if !rightOptionDown && !leftOptionDown && !leftCommandDown && !rightCommandDown {
                return true
            }
            usleep(stepUs)
            waitedMs += 20
        }
        return false
    }
}
