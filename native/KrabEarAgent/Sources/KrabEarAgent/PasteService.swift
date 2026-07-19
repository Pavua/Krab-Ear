/*
 Вставка текста в активное приложение macOS.

 Связи модуля:
 1) main.swift: использует сервис после получения транскрибации.
 2) Quick replay (Cmd+Option+V): repastLast() / recordLastPaste() / lastPastedText.
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

    // MARK: - Smart field-aware paste

    /// Управляет умной вставкой по типу поля (AX role-based). Обновляется из настроек.
    var smartFieldFormatEnabled: Bool = false

    /// Уведомительный хук для secure-field skip (вызывается синхронно на вызывающем потоке).
    /// Позволяет внешнему коду (AgentAppDelegate) показать уведомление не создавая зависимость
    /// от NotificationService напрямую в PasteService.
    var onSecureFieldSkipped: (() -> Void)?

    /// Уведомительный хук для пропуска записи в буфер обмена из-за защищённого
    /// содержимого (S34, org.nspasteboard.ConcealedType). Тот же паттерн, что
    /// onSecureFieldSkipped — вызывается синхронно, вызывающий код сам решает,
    /// как уведомить пользователя.
    var onConcealedClipboardSkipped: (() -> Void)?

    /// AX-роль сфокусированного элемента для указанного PID.
    /// Зеркалит паттерн из `inspectFocusedElementState(pid:)` и `collapseSelectionIfNeeded(pid:)`.
    private func focusedElementRole(pid: pid_t) -> String? {
        let appElement = AXUIElementCreateApplication(pid)
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
    private let backspaceKeyCode: CGKeyCode = Keycode.delete.rawValue
    /// Delay between individual Backspace key-down/key-up pairs when deleting a run (µs)
    private let backspaceKeypressDelayUs: useconds_t = 15_000
    private let logger = AgentLogger.shared
    /// Throttle для Accessibility prompt'ов: показываем dialog не чаще чем раз
    /// в 10 минут per app launch. Без этого каждый paste при missing AX = новый
    /// блокирующий dialog (ad-hoc codesigned apps теряют TCC permission после
    /// каждого rebuild из-за смены cdhash). Текст остаётся в clipboard —
    /// пользователь может вставить вручную Cmd+V.
    private var lastAXPromptAt: Date?
    private let axPromptCooldownSec: TimeInterval = 600

    // MARK: - Quick replay (Cmd+Option+V)

    private let lastPastedTextKey = "KrabEar_LastPastedText"
    /// Кулдаун между повторными вставками, предотвращает случайный дубль.
    let repasteCooldownSec: TimeInterval = 1.0
    private var lastPastedAt: Date?

    /// Последний успешно вставленный текст. Читается из/записывается в UserDefaults.
    var lastPastedText: String? {
        get { UserDefaults.standard.string(forKey: lastPastedTextKey) }
        set { UserDefaults.standard.set(newValue, forKey: lastPastedTextKey) }
    }

    /// Запоминает успешно вставленный текст для возможного быстрого повтора.
    func recordLastPaste(_ text: String) {
        lastPastedText = text
        lastPastedAt = Date()
    }

    /// Повторяет последнюю вставку через `pasteToFrontmostApp`.
    /// Возвращает PasteAttemptResult с reason "no_last_paste" или "repaste_too_soon"
    /// при неудовлетворённых предусловиях.
    func repastLast() -> PasteAttemptResult {
        guard let text = lastPastedText, !text.isEmpty else {
            return PasteAttemptResult(ok: false, reason: "no_last_paste")
        }
        if let lastAt = lastPastedAt,
           Date().timeIntervalSince(lastAt) < repasteCooldownSec {
            return PasteAttemptResult(ok: false, reason: "repaste_too_soon")
        }
        return pasteToFrontmostApp(text)
    }

    private enum FocusState {
        case editable
        case nonEditable
        case unknown
    }

    func putToClipboard(_ text: String) {
        let pasteboard = NSPasteboard.general
        guard !pasteboardHoldsConcealedContent(pasteboard) else {
            logger.warn("[Clipboard] Overwrite skipped — pasteboard holds concealed content")
            onConcealedClipboardSkipped?()
            return
        }
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }

    /// S34: org.nspasteboard.ConcealedType — де-факто стандарт (nspasteboard.org),
    /// менеджеры паролей (1Password, Bitwarden, Keychain Access и др.) помечают
    /// этим типом чувствительный контент рядом с .string, чтобы clipboard-утилиты
    /// его не логировали/не затирали.
    private func pasteboardHoldsConcealedContent(_ pasteboard: NSPasteboard) -> Bool {
        pasteboard.types?.contains(NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")) ?? false
    }

    func pasteToFrontmostApp(_ text: String, targetPID: pid_t? = nil) -> PasteAttemptResult {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            return PasteAttemptResult(ok: false, reason: "empty_text")
        }

        // MARK: Smart field-aware paste gate
        // Выполняется ТОЛЬКО при smartFieldFormatEnabled == true и при наличии AX доступа.
        // Определяет роль сфокусированного поля и применяет соответствующий форматинг:
        //   AXSecureTextField → пропускаем вставку полностью (пароль)
        //   AXSearchField, AXComboBox → убираем хвостовую пунктуацию
        //   всё остальное → текст не меняем
        var textToInsert = text  // может быть переопределён для AXSearchField/AXComboBox
        if smartFieldFormatEnabled, isAccessibilityTrusted() {
            // Приоритет PID: явный targetPID → frontmost app.
            let axPID: pid_t?
            if let tpid = targetPID {
                axPID = tpid
            } else if let frontmostPID = NSWorkspace.shared.frontmostApplication?.processIdentifier,
                      frontmostPID != ProcessInfo.processInfo.processIdentifier {
                axPID = frontmostPID
            } else {
                axPID = nil
            }

            if let pid = axPID, let role = focusedElementRole(pid: pid) {
                switch role {
                case "AXSecureTextField":
                    // Никогда не вставляем в защищённое поле (пароль).
                    logger.warn("[SmartPaste] Secure field (pid=\(pid)) — paste skipped")
                    onSecureFieldSkipped?()
                    return PasteAttemptResult(ok: false, reason: "secure_field_skipped")
                case "AXSearchField", "AXComboBox":
                    // Поисковые/комбо поля: убираем хвостовую пунктуацию.
                    textToInsert = textForSearchField(cleanText)
                    logger.info("[SmartPaste] Search/combo field (pid=\(pid), role=\(role)) — stripped trailing punct")
                default:
                    break
                }
            }
        }

        putToClipboard(textToInsert)
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

        // Запоминаем вставленный текст для быстрого повтора (Cmd+Option+V).
        // Используем textToInsert (может отличаться от text после smartField trim).
        let effectiveText = textToInsert.trimmingCharacters(in: .whitespacesAndNewlines)
        recordLastPaste(effectiveText.isEmpty ? cleanText : effectiveText)

        // Если фокус определить не удалось, считаем попытку условно успешной:
        // в этом случае UI покажет статус ok, но в логе останется подробная диагностика.
        if focusState == .unknown {
            logger.info("Автовставка выполнена без AX-подтверждения фокуса (pid=\(resolvedPID))")
            return PasteAttemptResult(ok: true, reason: resultReason)
        }

        return PasteAttemptResult(ok: true, reason: resultReason)
    }

    // MARK: - Streaming paste revision (delete-back)

    /// Удаляет `count` символов ПЕРЕД курсором в целевом приложении через симулированные
    /// Backspace-нажатия. В отличие от `pasteToFrontmostApp` НЕ трогает буфер обмена.
    /// Используется `StreamingPasteController` для отката ранее вставленного текста,
    /// когда backend "ревизует" уже вставленный partial (переосмысление сказанного).
    func deleteBackward(count: Int, targetPID: pid_t? = nil) -> PasteAttemptResult {
        guard count > 0 else {
            return PasteAttemptResult(ok: false, reason: "empty_count")
        }

        let axTrusted = isAccessibilityTrusted()

        guard waitForModifierRelease(timeoutMs: modifierReleaseTimeoutMs) else {
            logger.warn("Откат вставки отменён: модификаторы не отпущены в таймаут")
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

        let removed: Bool
        let resultReason: String
        if axTrusted {
            removed = postBackspacesToPid(resolvedPID, count: count)
            resultReason = removed ? "ok" : "event_post_failed"
        } else {
            removed = runAppleScriptBackspace(count: count)
            if removed {
                logger.info("Откат вставки выполнен через osascript fallback (AX=false)")
                resultReason = "ok_via_osascript"
            } else {
                requestAccessibilityPromptIfNeeded()
                logger.warn("Откат вставки недоступен: AX=false и fallback osascript не сработал")
                resultReason = "accessibility_not_granted"
            }
        }

        guard removed else {
            return PasteAttemptResult(ok: false, reason: resultReason)
        }
        return PasteAttemptResult(ok: true, reason: resultReason)
    }

    private func postBackspacesToPid(_ targetPID: pid_t, count: Int) -> Bool {
        guard let source = CGEventSource(stateID: .hidSystemState) else { return false }
        for _ in 0..<count {
            guard
                let keyDown = CGEvent(keyboardEventSource: source, virtualKey: backspaceKeyCode, keyDown: true),
                let keyUp = CGEvent(keyboardEventSource: source, virtualKey: backspaceKeyCode, keyDown: false)
            else {
                return false
            }
            keyDown.postToPid(targetPID)
            usleep(backspaceKeypressDelayUs)
            keyUp.postToPid(targetPID)
            usleep(backspaceKeypressDelayUs)
        }
        return true
    }

    private func runAppleScriptBackspace(count: Int) -> Bool {
        let scriptSource = """
        tell application "System Events"
            repeat \(count) times
                key code 51
            end repeat
        end tell
        """
        guard let script = NSAppleScript(source: scriptSource) else {
            return false
        }

        var errorDict: NSDictionary?
        _ = script.executeAndReturnError(&errorDict)
        if let errorDict {
            logger.warn("AppleScript fallback отката вернул ошибку: \(errorDict)")
            return false
        }
        return true
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
