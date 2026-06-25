/*
 StreamingPasteController.swift

 Потоковая вставка текста по мере диктовки (opt-in, streaming_paste_enabled).

 Архитектура:
 - Подписывается на SSE /v1/events (realtime.partial_transcript + realtime.final_transcript),
   используя тот же паттерн PartialSSEDelegate, что и RealtimeOverlayController+PartialSSE.swift.
 - Алгоритм stable-prefix commit:
     1. На каждый partial P вычисляем longestCommonPrefix(lastPartial, P), затем откатываем
        к последней границе слова (не вставляем полуслова).
     2. Вставляем только подстроку stable[committedCount...] (новый подтверждённый хвост).
     3. На final F вставляем F[committedCount...] (оставшийся хвост), сбрасываем сессию.
 - Каждый чанк вставляется через clipboard + Cmd+V (appendChunk в PasteService).
 - Если за текущую запись было вставлено ≥1 чанк, didStreamThisRecording = true.
   main+PasteHandling.swift читает это свойство и пропускает финальную полную вставку.

 Ограничения (известные артефакты):
 - Если partial ревизует уже вставленный диапазон (committedCount > len(stable)),
   un-paste НЕ происходит — принимаем редкий артефакт и продолжаем с нового стабильного
   префикса. Это фундаментальное ограничение clipboard+Cmd+V подхода.
 - Поведение (тайминг, курсор, ревизии) можно верифицировать только на реальном Mac с
   живой записью — unit-тесты не покрывают SSE latency и cursor position.
*/

import AppKit
import Foundation
import ObjectiveC

// MARK: - SSE delegate (reused pattern from PartialSSEDelegate in RealtimeOverlayController+PartialSSE)

private final class StreamingSSEDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
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
        // SSE connection closed — lifecycle managed by start/stop.
    }
}

// MARK: - StreamingPasteController

@MainActor
final class StreamingPasteController {

    // MARK: - Configuration

    /// Включён ли режим потоковой вставки (из AgentSettings.streamingPasteEnabled).
    var isEnabled: Bool = false

    // MARK: - Public state (read by main+PasteHandling.swift)

    /// true если в этой записи было вставлено ≥1 чанка. Читается в performAutoPaste.
    private(set) var didStreamThisRecording: Bool = false

    // MARK: - Private session state

    private var lastPartial: String = ""
    private var committedCount: Int = 0

    // MARK: - SSE connection state

    private var sseDelegate: StreamingSSEDelegate?
    private var sseSession: URLSession?
    private var sseTask: URLSessionDataTask?
    private var sseEventTypeBuf: String = ""

    // MARK: - Dependencies

    private let pasteService: PasteService
    private let logger = AgentLogger.shared

    // MARK: - Init

    init(pasteService: PasteService) {
        self.pasteService = pasteService
    }

    // MARK: - Recording lifecycle

    /// Вызывается при старте записи (миррор startRealtimeOverlayPolling).
    func recordingDidStart(restBaseURL: String = "http://127.0.0.1:5005") {
        guard isEnabled else { return }
        resetSessionState()
        startSSE(restBaseURL: restBaseURL)
        logger.info("[StreamingPaste] Сессия стартовала")
    }

    /// Вызывается при остановке записи (миррор stopRealtimeOverlayPolling).
    func recordingDidStop() {
        stopSSE()
        // didStreamThisRecording сохраняется до сброса вызовом resetAfterFinalPaste().
        logger.info("[StreamingPaste] Сессия остановлена. didStreamThisRecording=\(didStreamThisRecording)")
    }

    /// Сбрасывает флаг didStreamThisRecording после того как main+PasteHandling принял решение.
    /// Вызывается в performAutoPaste после чтения флага.
    func resetAfterFinalPaste() {
        didStreamThisRecording = false
    }

    // MARK: - SSE connection

    private func startSSE(restBaseURL: String) {
        stopSSE()
        let filter = "realtime.partial_transcript,realtime.final_transcript"
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=\(filter)") else { return }

        let delegate = StreamingSSEDelegate { [weak self] line in
            Task { @MainActor [weak self] in
                self?.handleSSELine(line)
            }
        }
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let task = session.dataTask(with: request)

        sseDelegate = delegate
        sseSession = session
        sseTask = task
        sseEventTypeBuf = ""
        task.resume()
    }

    private func stopSSE() {
        sseTask?.cancel()
        sseSession?.invalidateAndCancel()
        sseDelegate = nil
        sseSession = nil
        sseTask = nil
        sseEventTypeBuf = ""
    }

    // MARK: - SSE line parsing (same protocol as PartialSSEDelegate pattern)

    private func handleSSELine(_ line: String) {
        if line.hasPrefix("event: ") {
            sseEventTypeBuf = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        } else if line.hasPrefix("data: ") {
            let eventType = sseEventTypeBuf
            let jsonStr = String(line.dropFirst(6))
            handleSSEEvent(type: eventType, json: jsonStr)
            sseEventTypeBuf = ""
        } else if line.isEmpty {
            sseEventTypeBuf = ""
        }
    }

    private func handleSSEEvent(type: String, json: String) {
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        let eventData = obj["data"] as? [String: Any] ?? obj
        let text = (eventData["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        switch type {
        case "realtime.partial_transcript":
            guard !text.isEmpty else { return }
            handlePartial(text)
        case "realtime.final_transcript":
            guard !text.isEmpty else { return }
            handleFinal(text)
        default:
            break
        }
    }

    // MARK: - Stable-prefix commit algorithm

    /// Обрабатывает очередной partial (растущее лучшее предположение backend).
    private func handlePartial(_ partial: String) {
        // 1. Longest common prefix с предыдущим partial.
        let rawStable = longestCommonPrefix(lastPartial, partial)

        // 2. Откатываем до последней границы слова (не вставляем полуслова).
        let stable = trimToWordBoundary(rawStable)

        // 3. Если stable короче уже зафиксированного — partial ревизовал прошлое.
        //    Un-paste невозможен (clipboard+Cmd+V). Логируем и ждём нового stable.
        if stable.count < committedCount {
            logger.warn("[StreamingPaste] Ревизия partial до committedCount=\(committedCount), stable.count=\(stable.count) — пропускаем")
            lastPartial = partial
            return
        }

        // 4. Вставляем новый подтверждённый кусок.
        let startIndex = stable.index(stable.startIndex, offsetBy: committedCount)
        let newText = String(stable[startIndex...])

        if !newText.isEmpty {
            appendChunk(newText)
            committedCount = stable.count
            didStreamThisRecording = true
            logger.info("[StreamingPaste] Partial chunk вставлен: len=\(newText.count), total committed=\(committedCount)")
        }

        lastPartial = partial
    }

    /// Обрабатывает финальный transcript — вставляет оставшийся хвост.
    private func handleFinal(_ finalText: String) {
        // Вставляем всё что не было вставлено как partial.
        if finalText.count > committedCount {
            let startIndex = finalText.index(finalText.startIndex, offsetBy: committedCount)
            let tail = String(finalText[startIndex...])
            if !tail.isEmpty {
                appendChunk(tail)
                didStreamThisRecording = true
                logger.info("[StreamingPaste] Final tail вставлен: len=\(tail.count)")
            }
        } else if finalText.count < committedCount {
            // Финальный текст короче вставленного — редкая ситуация (LLM rewrite + сокращение).
            logger.warn("[StreamingPaste] Final text короче committedCount (\(finalText.count) < \(committedCount)) — артефакт")
        }

        // Сбрасываем сессию (запись завершена).
        resetSessionState()
        // didStreamThisRecording НЕ сбрасываем здесь — его читает main+PasteHandling.
    }

    // MARK: - Chunk paste

    /// Вставляет чанк текста через clipboard + Cmd+V (добавление в текущую позицию курсора).
    private func appendChunk(_ text: String) {
        // pasteToFrontmostApp кладёт текст в clipboard и шлёт Cmd+V в frontmost app.
        // Это тот же механизм, что и для полной вставки — cursor position зависит от
        // target app (обычно в конце последнего paste). Дополнительная обёртка не нужна.
        let result = pasteService.pasteToFrontmostApp(text)
        if !result.ok {
            logger.warn("[StreamingPaste] Chunk paste failed: \(result.reason)")
        }
    }

    // MARK: - String helpers

    /// Длиннейший общий префикс двух строк (Character-level, Unicode-safe).
    private func longestCommonPrefix(_ a: String, _ b: String) -> String {
        var result = ""
        var ia = a.startIndex
        var ib = b.startIndex
        while ia < a.endIndex && ib < b.endIndex {
            if a[ia] == b[ib] {
                result.append(a[ia])
                a.formIndex(after: &ia)
                b.formIndex(after: &ib)
            } else {
                break
            }
        }
        return result
    }

    /// Откатывает строку к последней границе слова (whitespace).
    /// Если строка заканчивается пробелом — оставляем как есть (пробел — граница).
    /// Если пробела нет совсем — возвращаем пустую строку (нечего фиксировать).
    private func trimToWordBoundary(_ s: String) -> String {
        guard !s.isEmpty else { return s }
        // Если последний символ — пробел, граница уже чистая.
        if s.last?.isWhitespace == true { return s }
        // Ищем последний пробел.
        if let lastSpace = s.lastIndex(where: { $0.isWhitespace }) {
            // Включаем пробел как часть вставки (разделитель слов).
            return String(s[...lastSpace])
        }
        // Нет пробела → первое слово ещё не завершено, не вставляем ничего.
        return ""
    }

    // MARK: - State reset

    private func resetSessionState() {
        lastPartial = ""
        committedCount = 0
    }
}
