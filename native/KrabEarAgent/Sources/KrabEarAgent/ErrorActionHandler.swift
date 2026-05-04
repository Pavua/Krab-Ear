/*
 ErrorActionHandler — декодирует krab_error SSE-события, вызывает ToastPresenting,
 диспатчит actionable tap в backend через IPC.

 Связи модуля:
 1) main+Errors.swift: создаёт экземпляр и хранит через associated object.
 2) Task 11 ErrorToastPresenter: реализует ToastPresenting → показывает UI.
 3) Task 13: SSE подписка wire.
 4) IPCClient: handle_error_action + side_effect dispatch.
*/

import Foundation
import AppKit
import os

// MARK: - KrabErrorPayload

/// Wire format matching backend KrabError.model_dump(mode="json").
/// Field names match Python Pydantic model field names exactly.
struct KrabErrorPayload: Codable, Sendable {
    let severity: String
    let component: String
    let code: String
    let message_user: String
    let message_debug: String
    let timestamp: String
    let context: [String: AnyCodable]
    let actionable: Bool
    let action_id: String?

    init(
        severity: String,
        component: String,
        code: String,
        message_user: String,
        message_debug: String,
        timestamp: String,
        context: [String: AnyCodable] = [:],
        actionable: Bool = false,
        action_id: String? = nil
    ) {
        self.severity = severity
        self.component = component
        self.code = code
        self.message_user = message_user
        self.message_debug = message_debug
        self.timestamp = timestamp
        self.context = context
        self.actionable = actionable
        self.action_id = action_id
    }
}

// MARK: - AnyCodable

/// Decodes arbitrary JSON values from KrabError.context dict.
/// Supports Bool, Int, Double, String, Array, Dict, and null.
struct AnyCodable: Codable, Sendable {
    let value: any Sendable

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { value = NSNull(); return }
        if let v = try? c.decode(Bool.self) { value = v; return }
        if let v = try? c.decode(Int.self) { value = v; return }
        if let v = try? c.decode(Double.self) { value = v; return }
        if let v = try? c.decode(String.self) { value = v; return }
        if let v = try? c.decode([AnyCodable].self) {
            value = v.map { $0.value } as [any Sendable]
            return
        }
        if let v = try? c.decode([String: AnyCodable].self) {
            value = v.mapValues { $0.value } as [String: any Sendable]
            return
        }
        value = NSNull()
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch value {
        case let v as Bool: try c.encode(v)
        case let v as Int: try c.encode(v)
        case let v as Double: try c.encode(v)
        case let v as String: try c.encode(v)
        case is NSNull: try c.encodeNil()
        default: try c.encodeNil()
        }
    }

    init(value: some Sendable) {
        self.value = value
    }
}

// MARK: - ToastPresenting

/// Protocol для презентации ошибок в UI. Реализуется в Task 11 (ErrorToastPresenter).
@MainActor
protocol ToastPresenting: AnyObject {
    func present(error: KrabErrorPayload)
}

// MARK: - NotificationCenter extensions

extension Notification.Name {
    /// Fired when backend returns side_effect = "swift_focus_hf_token".
    /// Слушатели (Settings panel) должны сфокусировать поле HF token.
    static let focusHFTokenSetting = Notification.Name("KrabEar.focusHFTokenSetting")

    /// Fired when backend returns side_effect = "swift_focus_hotkey_tab".
    /// Слушатели (Settings panel) должны открыть Hotkey tab.
    static let focusHotkeyTab = Notification.Name("KrabEar.focusHotkeyTab")
}

// MARK: - ErrorActionHandler

/// Получает krab_error SSE-события, делегирует показ toast-у и диспатчит
/// action taps обратно в backend.
///
/// - Thread model: `@MainActor`; IPC вызовы уходят на background через `callAsync`.
/// - Создаётся в `main+Errors.swift`, хранится через associated object.
@MainActor
final class ErrorActionHandler {
    private let logger = Logger(subsystem: "com.antigravity.krab-ear", category: "ErrorActionHandler")
    private let ipcClient: IPCClient
    private let toastPresenter: any ToastPresenting

    init(ipcClient: IPCClient, toastPresenter: any ToastPresenting) {
        self.ipcClient = ipcClient
        self.toastPresenter = toastPresenter
    }

    // MARK: - Event handling

    /// Вызывается при получении krab_error SSE-события.
    /// Логирует и делегирует показ toast presenter'у.
    func handleErrorEvent(_ payload: KrabErrorPayload) async {
        logger.info(
            "krab_error received: code=\(payload.code, privacy: .public) severity=\(payload.severity, privacy: .public)"
        )
        toastPresenter.present(error: payload)
    }

    // MARK: - Action tap dispatch

    /// Вызывается когда пользователь нажал action button в toast.
    /// Отправляет `handle_error_action` в backend и обрабатывает side_effect.
    func handleActionTap(actionId: String) async {
        do {
            let response = try await ipcClient.callAsync(
                method: "handle_error_action",
                params: ["action_id": actionId],
                timeoutSec: IPCClient.quickTimeoutSec
            )
            logger.info("action \(actionId, privacy: .public) response received")
            handleSideEffect(from: response, actionId: actionId)
        } catch {
            logger.error(
                "action \(actionId, privacy: .public) failed: \(error.localizedDescription, privacy: .public)"
            )
        }
    }

    // MARK: - Side effect dispatch

    /// Обрабатывает `side_effect` строку из IPC response.
    /// Постит соответствующий NotificationCenter event для Settings panel.
    private func handleSideEffect(from response: [String: Any], actionId: String) {
        guard
            let result = response["result"] as? [String: Any],
            let sideEffect = result["side_effect"] as? String
        else { return }

        switch sideEffect {
        case "swift_focus_hf_token":
            logger.info("side_effect: swift_focus_hf_token → posting notification")
            NotificationCenter.default.post(name: .focusHFTokenSetting, object: nil)
        case "swift_focus_hotkey_tab":
            logger.info("side_effect: swift_focus_hotkey_tab → posting notification")
            NotificationCenter.default.post(name: .focusHotkeyTab, object: nil)
        default:
            logger.debug("side_effect '\(sideEffect, privacy: .public)' — no Swift handler")
        }
    }
}
