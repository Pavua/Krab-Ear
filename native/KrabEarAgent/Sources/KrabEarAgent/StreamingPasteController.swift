import AppKit
import Foundation

@MainActor protocol StreamingPasteTarget: AnyObject {
    func pasteToFrontmostApp(_ text: String) async -> PasteAttemptResult
    func deleteBackward(count: Int) async -> PasteAttemptResult
    func reserveInput(target: pid_t?, maximumDeleteCount: Int) -> PasteInputReservation?
    func paste(_ text: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult
    func revise(count: Int, text: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult
    func captureTarget() -> pid_t?
    func paste(_ text: String, target: pid_t?) async -> PasteAttemptResult
    func revise(count: Int, text: String, target: pid_t?) async -> PasteAttemptResult
}
extension StreamingPasteTarget {
    func reserveInput(target: pid_t?, maximumDeleteCount: Int) -> PasteInputReservation? { nil }
    func paste(_ text: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult { await paste(text, target: target) }
    func revise(count: Int, text: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult { await revise(count: count, text: text, target: target) }

    func captureTarget() -> pid_t? { nil }
    func paste(_ text: String, target: pid_t?) async -> PasteAttemptResult { await pasteToFrontmostApp(text) }
    func revise(count: Int, text: String, target: pid_t?) async -> PasteAttemptResult {
        let removed = count > 0 ? await deleteBackward(count: count) : PasteAttemptResult(ok: true, reason: "ok")
        guard removed.ok else { return removed }
        if text.isEmpty { return removed }
        let inserted = await pasteToFrontmostApp(text)
        return PasteAttemptResult(ok: inserted.ok, reason: inserted.reason,
            effect: !inserted.ok && count > 0 ? .partial : inserted.effect, backspacePairs: count)
    }
}
@MainActor final class PasteServiceStreamingAdapter: StreamingPasteTarget {
    private let pasteService: PasteService
    init(pasteService: PasteService) { self.pasteService = pasteService }
    func reserveInput(target: pid_t?, maximumDeleteCount: Int) -> PasteInputReservation? {
        pasteService.reserveInput(target: target, maximumDeleteCount: maximumDeleteCount)
    }
    func paste(_ text: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult {
        await revise(count: 0, text: text, target: target, reservation: reservation)
    }
    func revise(count: Int, text: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult {
        guard let reservation else { return await revise(count: count, text: text, target: target) }
        return await pasteService.performReserved(text, deleteCount: count, request: reservation)
    }
    func captureTarget() -> pid_t? { pasteService.captureTargetPID() }
    func pasteToFrontmostApp(_ text: String) async -> PasteAttemptResult { await pasteService.pasteToFrontmostApp(text) }
    func deleteBackward(count: Int) async -> PasteAttemptResult { await pasteService.deleteBackward(count: count) }
    func paste(_ text: String, target: pid_t?) async -> PasteAttemptResult {
        guard let target else { return PasteAttemptResult(ok: false, reason: "no_external_target") }
        return await pasteService.pasteToFrontmostApp(text, targetPID: target)
    }
    func revise(count: Int, text: String, target: pid_t?) async -> PasteAttemptResult {
        guard let target else { return PasteAttemptResult(ok: false, reason: "no_external_target") }
        return await pasteService.revise(deleteCount: count, text: text, targetPID: target)
    }
}
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
        // Жизненный цикл закрытого SSE соединения принадлежит start/stop.
    }
}


/// Сессия — отдельный reference owner: поздний G1 completion не меняет G2.
@MainActor final class StreamingPasteController {
    var isEnabled = false
    var debounceIntervalSec: TimeInterval = 0.3
    var now: () -> Date = Date.init
    private final class Session {
        let id = UUID()
        var lastPartial = "", committedText = "", latestStable = ""
        var lastFlushAt: Date?, lastFailureAt: Date?
        var committedTarget: pid_t?
        var multipleTargets = false
        var effect: PasteEffect = .noEffects
        var pairs = 0
        var maximumAcceptedLength = 0
        var lastReason = "stream_noEffects"
        var finalResult: Task<PasteAttemptResult, Never>?
        var finalizing = false
        var tail: Task<Void, Never>?
    }
    private var session = Session()
    private let pasteService: any StreamingPasteTarget
    private var sseDelegate: StreamingSSEDelegate?
    private var sseSession: URLSession?
    private var sseTask: URLSessionDataTask?
    private var eventType = ""
    private let logger = AgentLogger.shared
    private(set) var didStreamThisRecording = false
    init(pasteService: any StreamingPasteTarget) { self.pasteService = pasteService }
    func recordingDidStart(restBaseURL: String = "http://127.0.0.1:5005") {
        guard isEnabled else { return }
        beginSession()
        startSSE(restBaseURL: restBaseURL)
    }
    func beginSession() { session = Session(); didStreamThisRecording = false }
    func recordingDidStop() { stopSSE() }
    func resetAfterFinalPaste() { didStreamThisRecording = false }
    private func startSSE(restBaseURL: String) {
        stopSSE()
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=realtime.partial_transcript,realtime.final_transcript") else { return }
        let owner = session
        let delegate = StreamingSSEDelegate { [weak self, weak owner] line in
            Task { @MainActor [weak self, weak owner] in
                guard let self, let owner, self.session === owner else { return }
                await self.handleSSELine(line)
            }
        }
        let connection = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        sseDelegate = delegate; sseSession = connection
        sseTask = connection.dataTask(with: request); sseTask?.resume()
    }
    private func stopSSE() {
        sseTask?.cancel(); sseSession?.invalidateAndCancel()
        sseTask = nil; sseDelegate = nil; sseSession = nil; eventType = ""
    }
    private func handleSSELine(_ line: String) async {
        if line.hasPrefix("event: ") { eventType = String(line.dropFirst(7)); return }
        guard line.hasPrefix("data: ") else { return }
        let type = eventType; eventType = ""
        guard let data = String(line.dropFirst(6)).data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let payload = obj["data"] as? [String: Any] ?? obj
        guard let text = payload["text"] as? String, !text.isEmpty else { return }
        if type == "realtime.partial_transcript" { await handlePartial(text) }
        else if type == "realtime.final_transcript" { _ = await handleFinal(text) }
    }
    func handlePartial(_ text: String) async {
        let owner = session, target = pasteService.captureTarget()
        guard !owner.finalizing else { return }
        let reservation = pasteService.reserveInput(target: target, maximumDeleteCount: owner.maximumAcceptedLength)
        // Отказ admission не создаёт скрытую неограниченную цепь Task за owner.tail.
        if let failure = reservation?.failure { owner.lastReason = failure.reason; return }
        owner.maximumAcceptedLength = max(owner.maximumAcceptedLength, text.count)
        let previous = owner.tail
        let task = Task { @MainActor in
            defer { reservation?.cancel() }
            await previous?.value
            guard owner.effect != .partial && owner.effect != .unknown else { return }
            let stable = trimToWordBoundary(longestCommonPrefix(owner.lastPartial, text))
            owner.lastPartial = text; owner.latestStable = stable
            if let failure = owner.lastFailureAt, now().timeIntervalSince(failure) < debounceIntervalSec { return }
            let revision = !stable.hasPrefix(owner.committedText)
            let tail = String(stable.dropFirst(owner.committedText.count))
            let sentence = tail.last(where: { !$0.isWhitespace }).map { ".!?…".contains($0) } ?? false
            if revision || owner.lastFlushAt == nil || sentence || now().timeIntervalSince(owner.lastFlushAt!) >= debounceIntervalSec {
                _ = await update(owner: owner, desired: stable, target: target, reservation: reservation)
            }
        }
        owner.tail = task
        await withTaskCancellationHandler(operation: { await task.value }, onCancel: {
            task.cancel(); reservation?.cancelWaiting()
        })
    }
    @discardableResult func handleFinal(_ text: String) async -> PasteAttemptResult {
        let final = finalBarrier(text)
        return await withTaskCancellationHandler(operation: { await final.value }, onCancel: { final.cancel() })
    }
    func finalBarrier(_ text: String) -> Task<PasteAttemptResult, Never> {
        // Захватывается до первого await; начало новой записи не заменяет owner.
        let owner = session, target = pasteService.captureTarget()
        if let final = owner.finalResult { return final }
        owner.finalizing = true
        let reservation = pasteService.reserveInput(target: target, maximumDeleteCount: owner.maximumAcceptedLength)
        let previous = owner.tail
        let task = Task { @MainActor in
            defer { reservation?.cancel() }
            await previous?.value
            if let failure = reservation?.failure { owner.lastReason = failure.reason; return }
            if owner.effect != .partial && owner.effect != .unknown {
                _ = await update(owner: owner, desired: text, target: target, reservation: reservation)
            }
        }
        owner.tail = task
        let final = Task { @MainActor in
            await withTaskCancellationHandler(operation: { await task.value }, onCancel: {
                task.cancel(); reservation?.cancelWaiting()
            })
            let effect: PasteEffect = owner.committedText == text && owner.effect == .completed ? .completed
                : (owner.effect == .completed ? .partial : owner.effect)
            return PasteAttemptResult(ok: effect == .completed, reason: effect == .completed ? "stream_completed" : owner.lastReason,
                effect: effect, backspacePairs: owner.pairs, permitsFreshFallback: false)
        }
        owner.finalResult = final
        return final
    }
    private func update(owner: Session, desired: String, target: pid_t?, reservation: PasteInputReservation?) async -> PasteAttemptResult {
        let common = longestCommonPrefix(owner.committedText, desired)
        let remove = owner.committedText.count - common.count
        let suffix = String(desired.dropFirst(common.count))
        if remove == 0 && suffix.isEmpty { return PasteAttemptResult(ok: true, reason: "unchanged", effect: owner.effect) }
        if remove > 0 && (owner.multipleTargets || owner.committedTarget != target) {
            owner.lastReason = "revision_target_changed"
            owner.effect = owner.effect == .noEffects ? .noEffects : .partial
            return PasteAttemptResult(ok: false, reason: "revision_target_changed", effect: owner.effect)
        }
        let result = remove > 0
            ? await pasteService.revise(count: remove, text: suffix, target: target, reservation: reservation)
            : await pasteService.paste(suffix, target: target, reservation: reservation)
        owner.lastReason = result.reason
        owner.pairs += result.backspacePairs
        if result.ok {
            if !owner.committedText.isEmpty && owner.committedTarget != target { owner.multipleTargets = true }
            owner.committedTarget = target; owner.committedText = desired
            owner.lastFlushAt = now(); owner.lastFailureAt = nil; owner.effect = .completed
            if session === owner { didStreamThisRecording = true }
        } else {
            owner.lastFailureAt = now()
            if session === owner && result.effect != .noEffects { didStreamThisRecording = true }
            if result.effect == .unknown { owner.effect = .unknown }
            else if result.effect == .partial { owner.effect = .partial }
            // No-effect отказ допускает retry partial-событий; final truthful aggregate
            // вычисляется из committedText, а не из общего флага delegate.
        }
        return result
    }
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


}
