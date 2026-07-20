/*
 SSESessionDelegate — общий URLSessionDataDelegate для SSE long-poll стриминга.

 Используется LiveSubtitlesOverlay (Phase 2B HUD) и TranslationStreamView
 (Phase 2 PR 2.3 dual-pane). Раньше был закрыт внутри LiveSubtitlesOverlay;
 вынесен в общий файл, чтобы избежать дубликата.

 Обработка:
 - Проверяет HTTP 2xx и MIME `text/event-stream` до приёма тела ответа.
 - Накапливает сырые байты незавершённой строки отдельно для каждой
   URLSessionTask, чтобы split UTF-8 и разные SSE-потоки не повреждали друг друга.
 - Разрезает данные по `\n` и передаёт владельцу только полные строки.
 - При EOF или ошибке очищает буфер завершившейся задачи и уведомляет владельца;
   решение о переподключении остаётся у конкретного экрана.
*/

import Foundation

final class SSESessionDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private struct TaskState {
        var buffer = Data()
        var responseAccepted = false
    }

    private let onLine: (String) -> Void
    private let onComplete: ((Error?) -> Void)?
    private let bufferLock = NSLock()
    private var taskStates: [Int: TaskState] = [:]

    init(
        onLine: @escaping (String) -> Void,
        onComplete: ((Error?) -> Void)? = nil
    ) {
        self.onLine = onLine
        self.onComplete = onComplete
        super.init()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        receive(data, taskIdentifier: dataTask.taskIdentifier)
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        let accepted = Self.acceptsEventStreamResponse(response)
        setResponseAccepted(accepted, taskIdentifier: dataTask.taskIdentifier)
        completionHandler(accepted ? .allow : .cancel)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        complete(taskIdentifier: task.taskIdentifier, error: error)
    }

    /// Детерминированный вход для unit-тестов без создания URLSessionTask.
    func _testReceive(_ text: String, taskIdentifier: Int = 0) {
        _testReceive(Data(text.utf8), taskIdentifier: taskIdentifier)
    }

    /// Бинарный тестовый вход позволяет разрезать многобайтовый UTF-8 scalar
    /// ровно между сетевыми чанками.
    func _testReceive(_ data: Data, taskIdentifier: Int = 0) {
        // Старые тесты делегата проверяют только буферизацию и моделируют тело
        // уже принятого SSE-ответа. Явно отклонённое состояние не перезаписываем.
        bufferLock.lock()
        if taskStates[taskIdentifier] == nil {
            taskStates[taskIdentifier] = TaskState(responseAccepted: true)
        }
        bufferLock.unlock()
        receive(data, taskIdentifier: taskIdentifier)
    }

    /// Детерминированная проверка HTTP/MIME-контракта без реального сокета.
    @discardableResult
    func _testReceiveResponse(
        statusCode: Int,
        contentType: String?,
        taskIdentifier: Int = 0
    ) -> Bool {
        let accepted = Self.acceptsEventStreamResponse(
            statusCode: statusCode,
            contentType: contentType
        )
        setResponseAccepted(accepted, taskIdentifier: taskIdentifier)
        return accepted
    }

    /// Детерминированное завершение потока для unit-тестов.
    func _testComplete(error: Error? = nil, taskIdentifier: Int = 0) {
        complete(taskIdentifier: taskIdentifier, error: error)
    }

    private func receive(_ data: Data, taskIdentifier: Int) {
        let completedLines: [String]

        bufferLock.lock()
        guard var state = taskStates[taskIdentifier], state.responseAccepted else {
            bufferLock.unlock()
            return
        }

        state.buffer.append(data)
        var lines: [String] = []
        while let newlineIndex = state.buffer.firstIndex(of: 0x0A) {
            let lineBytes = state.buffer[state.buffer.startIndex..<newlineIndex]
            lines.append(String(decoding: lineBytes, as: UTF8.self))
            let nextIndex = state.buffer.index(after: newlineIndex)
            state.buffer.removeSubrange(state.buffer.startIndex..<nextIndex)
        }
        taskStates[taskIdentifier] = state
        completedLines = lines
        bufferLock.unlock()

        // Обработчик может вызвать код владельца, поэтому выполняем его вне блокировки.
        completedLines.forEach(onLine)
    }

    private func complete(taskIdentifier: Int, error: Error?) {
        bufferLock.lock()
        taskStates.removeValue(forKey: taskIdentifier)
        bufferLock.unlock()
        onComplete?(error)
    }

    private func setResponseAccepted(_ accepted: Bool, taskIdentifier: Int) {
        bufferLock.lock()
        var state = taskStates[taskIdentifier, default: TaskState()]
        state.responseAccepted = accepted
        if !accepted {
            state.buffer.removeAll(keepingCapacity: false)
        }
        taskStates[taskIdentifier] = state
        bufferLock.unlock()
    }

    private static func acceptsEventStreamResponse(_ response: URLResponse) -> Bool {
        guard let httpResponse = response as? HTTPURLResponse else { return false }
        let contentType = httpResponse.value(forHTTPHeaderField: "Content-Type")
            ?? httpResponse.mimeType
        return acceptsEventStreamResponse(
            statusCode: httpResponse.statusCode,
            contentType: contentType
        )
    }

    private static func acceptsEventStreamResponse(
        statusCode: Int,
        contentType: String?
    ) -> Bool {
        guard (200..<300).contains(statusCode), let contentType else { return false }
        let mediaType = contentType
            .split(separator: ";", maxSplits: 1, omittingEmptySubsequences: true)
            .first?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return mediaType == "text/event-stream"
    }
}
