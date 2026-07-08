/*
 StreamingPasteController.swift

 Потоковая вставка текста по мере диктовки (opt-in, streaming_paste_enabled).

 Архитектура:
 - Подписывается на SSE /v1/events (realtime.partial_transcript + realtime.final_transcript),
   используя тот же паттерн PartialSSEDelegate, что и RealtimeOverlayController+PartialSSE.swift.
 - Алгоритм stable-prefix commit с debounce-укрупнением чанков:
     1. На каждый partial P вычисляем longestCommonPrefix(lastPartial, P), затем откатываем
        к последней границе слова (не вставляем полуслова) → `stable`.
     2. Если `stable` НЕ начинается с уже вставленного `committedText` — backend "ревизовал"
        уже вставленный диапазон (переосмыслил сказанное). Выполняем performRevision:
        откатываем разошедшийся хвост через `pasteService.deleteBackward()` (симулированные
        Backspace) и вставляем исправленный хвост — полная замена диапазона, без silent skip.
     3. Иначе копим новый хвост (`stable` минус `committedText`) и вставляем его НЕ на каждое
        событие, а только когда: (a) с последней вставки прошло ≥debounceIntervalSec, ИЛИ
        (b) хвост оканчивается знаком завершения предложения (.!?…). Это резко сокращает
        число Cmd+V-вставок за одну фразу (с "много раз в секунду" до "раз на слово/фразу"),
        так что Cmd+Z пользователя убирает осмысленный кусок, а не 1-2 символа.
     4. На final F: если F начинается с committedText — вставляем остаток F[committedText.count...];
        иначе (final тоже ревизовал) — performRevision(F). Сбрасываем сессию.
 - Каждый чанк вставляется через clipboard + Cmd+V (appendChunk → StreamingPasteTarget.pasteToFrontmostApp).
 - Если за текущую запись было вставлено/откачено ≥1 чанк, didStreamThisRecording = true.
   main+PasteHandling.swift читает это свойство и пропускает финальную полную вставку.

 Ограничения (известные артефакты):
 - Настоящий атомарный "один Cmd+Z отменяет вообще всё" в ЛЮБОМ стороннем macOS-приложении
   недостижим через Accessibility/keystroke-симуляцию — цель здесь "предсказуемо", не
   "идеально атомарно" (см. debounce выше).
 - Поведение (тайминг, курсор, ревизии) можно верифицировать только на реальном Mac с
   живой записью — unit-тесты покрывают debounce/revision-логику через инжектируемый
   StreamingPasteTarget + clock, но не реальный SSE latency/cursor position.
*/

import AppKit
import Foundation
import ObjectiveC

// MARK: - StreamingPasteTarget (test-isolation seam)

/// Абстракция вставки/удаления текста в целевом приложении. `PasteServiceStreamingAdapter`
/// реализует её поверх реального `PasteService` (см. ниже). Тесты подставляют fake,
/// реализующий этот протокол, чтобы проверять debounce/revision-логику без реальных
/// keystroke side-effects (тот же паттерн, что `ToastPanelFactory` для ErrorToastView).
/// НЕ помечен `@MainActor`: методы `PasteService` сами по себе nonisolated (вызываются
/// off-main из существующих call site'ов, см. main+PasteHandling.swift) — протокол должен
/// сохранять ту же изоляцию (точнее, её отсутствие), иначе PasteService.repastLast() и
/// другие nonisolated вызовы `pasteToFrontmostApp` перестанут компилироваться.
protocol StreamingPasteTarget: AnyObject {
    func pasteToFrontmostApp(_ text: String) -> PasteAttemptResult
    func deleteBackward(count: Int) -> PasteAttemptResult
}

/// Тонкий адаптер PasteService → StreamingPasteTarget. Отдельный тип (а не extension
/// PasteService: StreamingPasteTarget напрямую) — чтобы НЕ вводить новый однопараметрический
/// overload `pasteToFrontmostApp(_:)`/`deleteBackward(count:)` на самом PasteService: такой
/// overload перехватывал бы существующие 1-арг вызовы (например `repastLast()` вызывает
/// `pasteToFrontmostApp(text)`), которые сейчас резолвятся в дефолт-параметрическую версию.
final class PasteServiceStreamingAdapter: StreamingPasteTarget {
    private let pasteService: PasteService

    init(pasteService: PasteService) {
        self.pasteService = pasteService
    }

    func pasteToFrontmostApp(_ text: String) -> PasteAttemptResult {
        pasteService.pasteToFrontmostApp(text, targetPID: nil)
    }

    func deleteBackward(count: Int) -> PasteAttemptResult {
        pasteService.deleteBackward(count: count, targetPID: nil)
    }
}

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

    /// Минимальный интервал между вставками чанков (debounce), секунды.
    /// Инжектируемый var (не let) — тесты подставляют маленькое значение вместо реального ожидания.
    var debounceIntervalSec: TimeInterval = 0.3

    /// Источник текущего времени. Инжектируемый — тесты подставляют fake clock
    /// для детерминированной проверки debounce-условий без реального sleep().
    var now: () -> Date = Date.init

    /// Знаки завершения предложения — при их появлении в новом хвосте вставка
    /// форсируется немедленно, даже если debounceIntervalSec ещё не истёк.
    private let sentenceEndingChars: Set<Character> = [".", "!", "?", "…"]

    // MARK: - Public state (read by main+PasteHandling.swift)

    /// true если в этой записи было вставлено/откачено ≥1 чанка. Читается в performAutoPaste.
    private(set) var didStreamThisRecording: Bool = false

    // MARK: - Private session state

    /// Последний RAW partial-текст от backend (используется для LCP-расчёта между событиями).
    private var lastPartial: String = ""

    /// Текст, который РЕАЛЬНО уже вставлен в целевое приложение в текущей сессии
    /// (правда о состоянии экрана — используется и для debounce-diff, и для revision-detection).
    private var committedText: String = ""

    /// Последний вычисленный stable-текст (обрезанный до границы слова). Может содержать
    /// хвост, ещё не вставленный (ожидает debounce-флаша).
    private var latestStable: String = ""

    /// Момент последней фактической вставки чанка. nil = ещё не вставляли в этой сессии
    /// (первый доступный chunk вставляется без ожидания debounce).
    private var lastFlushAt: Date?

    // MARK: - SSE connection state

    private var sseDelegate: StreamingSSEDelegate?
    private var sseSession: URLSession?
    private var sseTask: URLSessionDataTask?
    private var sseEventTypeBuf: String = ""

    // MARK: - Dependencies

    private let pasteService: any StreamingPasteTarget
    private let logger = AgentLogger.shared

    // MARK: - Init

    init(pasteService: any StreamingPasteTarget) {
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
    // handlePartial/handleFinal умышленно НЕ private (internal) — тестовый seam, тот же
    // паттерн, что dismissCurrentToast() в ErrorToastPresenter. Продовый вызов идёт через
    // handleSSEEvent, тесты вызывают их напрямую с fake StreamingPasteTarget + fake clock.

    /// Обрабатывает очередной partial (растущее лучшее предположение backend).
    func handlePartial(_ partial: String) {
        // 1. Longest common prefix с предыдущим partial.
        let rawStable = longestCommonPrefix(lastPartial, partial)

        // 2. Откатываем до последней границы слова (не вставляем полуслова).
        let stable = trimToWordBoundary(rawStable)
        lastPartial = partial

        // 3. Если stable больше НЕ начинается с уже вставленного committedText — backend
        //    ревизовал уже вставленный диапазон (переосмысление сказанного, короче/другое).
        //    Выполняем полную замену диапазона вместо silent skip.
        if !committedText.isEmpty && !stable.hasPrefix(committedText) {
            performRevision(correctedStable: stable)
            return
        }

        // 4. Иначе — копим новый хвост, но вставляем его не на каждое событие
        //    (см. maybeFlush: debounce interval ИЛИ конец предложения).
        latestStable = stable
        maybeFlush(force: false)
    }

    /// Обрабатывает финальный transcript — вставляет оставшийся хвост (форсированно, без debounce),
    /// либо, если final тоже ревизует уже вставленное, выполняет полную замену диапазона.
    func handleFinal(_ finalText: String) {
        if finalText.hasPrefix(committedText) {
            let tail = String(finalText.dropFirst(committedText.count))
            if !tail.isEmpty {
                appendChunk(tail)
                committedText = finalText
                didStreamThisRecording = true
                logger.info("[StreamingPaste] Final tail вставлен: len=\(tail.count)")
            }
        } else {
            // Final короче/другой относительно committedText — та же ревизия, что и для partial.
            performRevision(correctedStable: finalText)
        }

        // Сбрасываем сессию (запись завершена).
        resetSessionState()
        // didStreamThisRecording НЕ сбрасываем здесь — его читает main+PasteHandling.
    }

    // MARK: - Debounce flush

    /// Флашит накопленный (но ещё не вставленный) хвост `latestStable`, если выполнено
    /// одно из условий: (a) `force == true` (используется на final), (b) хвост оканчивается
    /// знаком завершения предложения, (c) с последней вставки прошло ≥debounceIntervalSec
    /// (или это первая вставка в сессии — не ждём).
    private func maybeFlush(force: Bool) {
        guard latestStable.count > committedText.count else { return }
        let tail = String(latestStable.dropFirst(committedText.count))
        guard !tail.isEmpty else { return }

        let elapsedEnough: Bool
        if let lastFlushAt {
            elapsedEnough = now().timeIntervalSince(lastFlushAt) >= debounceIntervalSec
        } else {
            elapsedEnough = true
        }

        guard force || elapsedEnough || endsWithSentenceBoundary(tail) else { return }

        appendChunk(tail)
        committedText = latestStable
        lastFlushAt = now()
        didStreamThisRecording = true
        logger.info("[StreamingPaste] Chunk вставлен (debounce): len=\(tail.count), total committed=\(committedText.count)")
    }

    /// true если (обрезанный по пробелам) хвост оканчивается знаком завершения предложения.
    private func endsWithSentenceBoundary(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard let last = trimmed.last else { return false }
        return sentenceEndingChars.contains(last)
    }

    // MARK: - Revision (full-range replace)

    /// Полная замена диапазона: откатывает разошедшийся хвост уже вставленного текста
    /// через `pasteService.deleteBackward()` (симулированные Backspace), затем вставляет
    /// исправленный хвост `correctedStable`. Вызывается когда backend прислал partial/final,
    /// который переосмыслил (укоротил/изменил) уже вставленный диапазон.
    private func performRevision(correctedStable: String) {
        let common = longestCommonPrefix(committedText, correctedStable)
        let removeCount = committedText.count - common.count
        let newSuffix = String(correctedStable.dropFirst(common.count))

        logger.warn("[StreamingPaste] Ревизия: откат \(removeCount) симв., вставка \(newSuffix.count) симв. взамен")

        if removeCount > 0 {
            let deleteResult = pasteService.deleteBackward(count: removeCount)
            if !deleteResult.ok {
                logger.warn("[StreamingPaste] Revision delete failed: \(deleteResult.reason)")
            }
        }
        if !newSuffix.isEmpty {
            appendChunk(newSuffix)
        }

        committedText = correctedStable
        latestStable = correctedStable
        lastFlushAt = now()
        didStreamThisRecording = true
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
        committedText = ""
        latestStable = ""
        lastFlushAt = nil
    }
}
