/*
 RealtimeOverlayController+PartialSSE.swift
 SSE subscription for realtime.partial_transcript, realtime.final_transcript,
 and realtime.partial_disabled events.

 Architecture:
 - Subscribes to REST /v1/events?filter=realtime.partial_transcript,realtime.final_transcript,realtime.partial_disabled
 - partial_transcript  → showPartialText() — italic font, alpha 0.7
 - final_transcript    → showFinalText()   — normal font, alpha 1.0
 - partial_disabled    → showDisabledState() — stops dot pulse, shows user message
 - Общий SSESubscriptionTransport восстанавливает закрытый stream.
 - Associated object хранит владельца подписки; show/hide управляют start/stop.
*/

import AppKit
import Foundation
import ObjectiveC

// MARK: - Associated object keys
private nonisolated(unsafe) var partialSSETransportKey: UInt8 = 0
private nonisolated(unsafe) var partialSSEEventBufKey: UInt8 = 0

// MARK: - RealtimeOverlayController + PartialSSE extension

extension RealtimeOverlayController {

    // MARK: - Public API

    /// Start SSE subscription for partial/final transcript events.
    func startPartialSSE(restBaseURL: String = "http://127.0.0.1:5005",
                         transport: SSESubscriptionTransport? = nil) {
        stopPartialSSE()
        let filter = "realtime.partial_transcript,realtime.final_transcript,realtime.partial_disabled"
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=\(filter)") else { return }

        let owner = transport ?? SSESubscriptionTransport()
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        objc_setAssociatedObject(self, &partialSSETransportKey, owner, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        owner.start(request: request, onConnectionReset: { [weak self] in
            guard let self else { return }
            // Нельзя склеивать event из старого соединения с data нового.
            objc_setAssociatedObject(self, &partialSSEEventBufKey, NSMutableString(), .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        }, onLine: { [weak self] line in
            self?.handlePartialSSELine(line)
        })
    }

    /// Stop SSE subscription and release resources.
    func stopPartialSSE() {
        let owner = objc_getAssociatedObject(self, &partialSSETransportKey) as? SSESubscriptionTransport
        objc_setAssociatedObject(self, &partialSSETransportKey, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        owner?.stop()
        objc_setAssociatedObject(self, &partialSSEEventBufKey, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
    }

    // MARK: - SSE Line Parsing

    private func handlePartialSSELine(_ line: String) {
        // SSE protocol: "event: <type>" then "data: <json>"
        // We track the current event type using the associated buffer.
        let buf = objc_getAssociatedObject(self, &partialSSEEventBufKey) as? NSMutableString
            ?? NSMutableString()

        if line.hasPrefix("event: ") {
            buf.setString(String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces))
            objc_setAssociatedObject(self, &partialSSEEventBufKey, buf, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        } else if line.hasPrefix("data: ") {
            let eventType = buf as String
            let jsonStr = String(line.dropFirst(6))
            handlePartialSSEEvent(type: eventType, json: jsonStr)
            buf.setString("")
        } else if line.isEmpty {
            buf.setString("")
        }
    }

    private func handlePartialSSEEvent(type: String, json: String) {
        guard let data = json.data(using: .utf8),
              let obj  = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        let eventData = obj["data"] as? [String: Any] ?? obj
        let text = (eventData["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        switch type {
        case "realtime.partial_transcript":
            guard !text.isEmpty else { return }
            showPartialText(text)
        case "realtime.final_transcript":
            guard !text.isEmpty else { return }
            showFinalText(text)
        case "realtime.partial_disabled":
            showDisabledState()
        default:
            break
        }
    }

    // MARK: - Text display helpers

    /// Display partial (in-progress) transcription — italic, slightly dimmed.
    func showPartialText(_ text: String) {
        _isShowingPartial = true
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let italicFont = NSFont.systemFont(ofSize: 14, weight: .regular)
        let font: NSFont = NSFontManager.shared.font(
            withFamily: italicFont.familyName ?? "System",
            traits: .italicFontMask,
            weight: 5,
            size: 14
        ) ?? italicFont

        let attrs: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: NSColor.labelColor.withAlphaComponent(reduceMotion ? 1.0 : 0.7),
        ]
        primaryLabel.attributedStringValue = NSAttributedString(string: text, attributes: attrs)
        adjustHeight()
    }

    /// Display final (confirmed) transcription — normal weight, full opacity.
    func showFinalText(_ text: String) {
        _isShowingPartial = false
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 14, weight: .medium),
            .foregroundColor: NSColor.labelColor,
        ]
        primaryLabel.attributedStringValue = NSAttributedString(string: text, attributes: attrs)
        adjustHeight()
    }

    /// Backend emitted realtime.partial_disabled (10 consecutive STT failures).
    /// Freeze partial state and show an informational message so the overlay
    /// does not silently stall. Dot animation winds down naturally when the
    /// recording session ends (hide() → stopAllPulse()).
    private func showDisabledState() {
        _isShowingPartial = false
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 14, weight: .regular),
            .foregroundColor: KrabEarTheme.Colors.warning,
        ]
        primaryLabel.attributedStringValue = NSAttributedString(
            string: "Превью остановлено — перезапустите запись",
            attributes: attrs
        )
        adjustHeight()
    }
}
