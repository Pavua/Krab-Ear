/*
 RealtimeOverlayController+PartialSSE.swift
 SSE subscription for realtime.partial_transcript and realtime.final_transcript events.

 Architecture:
 - Subscribes to REST /v1/events?filter=realtime.partial_transcript,realtime.final_transcript
 - partial_transcript  → showPartialText() — italic font, alpha 0.7
 - final_transcript    → showFinalText()   — normal font, alpha 1.0
 - Uses URLSession + PartialSSEDelegate (same pattern as LiveSubtitlesOverlay)
 - Associated object storage keeps delegate/task alive without stored properties
   (which would require modifying the main file and potentially reverting).
*/

import AppKit
import Foundation
import ObjectiveC

// MARK: - PartialSSEDelegate

/// URLSession data delegate for SSE line-by-line streaming.
private final class PartialSSEDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let onLine: (String) -> Void
    private var buffer = ""

    init(onLine: @escaping (String) -> Void) {
        self.onLine = onLine
        super.init()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer += String(decoding: data, as: UTF8.self)
        let lines = buffer.components(separatedBy: "\n")
        buffer = lines.last ?? ""
        for line in lines.dropLast() {
            onLine(line)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        // SSE connection closed — not restarted (caller manages lifecycle via show/hide).
    }
}

// MARK: - Associated object keys
// UInt8 variables as key addresses — safe for &key UnsafeRawPointer use
// (no string-representation exposure). Each unique address = unique key.
private nonisolated(unsafe) var partialSSEDelegateKey: UInt8 = 0
private nonisolated(unsafe) var partialSSESessionKey:  UInt8 = 0
private nonisolated(unsafe) var partialSSETaskKey:     UInt8 = 0
private nonisolated(unsafe) var partialSSEEventBufKey: UInt8 = 0

// MARK: - RealtimeOverlayController + PartialSSE extension

extension RealtimeOverlayController {

    // MARK: - Public API

    /// Start SSE subscription for partial/final transcript events.
    func startPartialSSE(restBaseURL: String = "http://127.0.0.1:5005") {
        stopPartialSSE()
        let filter = "realtime.partial_transcript,realtime.final_transcript"
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=\(filter)") else { return }

        let delegate = PartialSSEDelegate { [weak self] line in
            Task { @MainActor [weak self] in
                self?.handlePartialSSELine(line)
            }
        }
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let task = session.dataTask(with: request)

        objc_setAssociatedObject(self, &partialSSEDelegateKey, delegate, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &partialSSESessionKey, session, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &partialSSETaskKey, task, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &partialSSEEventBufKey, NSMutableString(), .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        task.resume()
    }

    /// Stop SSE subscription and release resources.
    func stopPartialSSE() {
        if let task = objc_getAssociatedObject(self, &partialSSETaskKey) as? URLSessionDataTask {
            task.cancel()
        }
        if let session = objc_getAssociatedObject(self, &partialSSESessionKey) as? URLSession {
            session.invalidateAndCancel()
        }
        objc_setAssociatedObject(self, &partialSSEDelegateKey, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &partialSSESessionKey, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &partialSSETaskKey, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
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
        guard !text.isEmpty else { return }

        switch type {
        case "realtime.partial_transcript":
            showPartialText(text)
        case "realtime.final_transcript":
            showFinalText(text)
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
}
