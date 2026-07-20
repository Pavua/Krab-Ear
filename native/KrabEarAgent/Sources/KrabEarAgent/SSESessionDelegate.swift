/*
 SSESessionDelegate — общий URLSessionDataDelegate для SSE long-poll стриминга.

 Используется LiveSubtitlesOverlay (Phase 2B HUD) и TranslationStreamView
 (Phase 2 PR 2.3 dual-pane). Раньше был закрыт внутри LiveSubtitlesOverlay;
 вынесен в общий файл, чтобы избежать дубликата.

 Обработка:
 - Накапливает незавершённую строку отдельно для каждой URLSessionTask, чтобы
   повторное использование делегата не смешивало разные SSE-потоки.
 - Разрезает данные по `\n` и передаёт владельцу только полные строки.
 - При EOF или ошибке очищает буфер завершившейся задачи и уведомляет владельца;
   решение о переподключении остаётся у конкретного экрана.
*/

import Foundation

final class SSESessionDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let onLine: (String) -> Void
    private let onComplete: ((Error?) -> Void)?
    private let bufferLock = NSLock()
    private var buffers: [Int: String] = [:]

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

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        complete(taskIdentifier: task.taskIdentifier, error: error)
    }

    /// Детерминированный вход для unit-тестов без создания URLSessionTask.
    func _testReceive(_ text: String, taskIdentifier: Int = 0) {
        receive(Data(text.utf8), taskIdentifier: taskIdentifier)
    }

    /// Детерминированное завершение потока для unit-тестов.
    func _testComplete(error: Error? = nil, taskIdentifier: Int = 0) {
        complete(taskIdentifier: taskIdentifier, error: error)
    }

    private func receive(_ data: Data, taskIdentifier: Int) {
        let completedLines: [String]

        bufferLock.lock()
        var buffer = buffers[taskIdentifier, default: ""]
        buffer += String(decoding: data, as: UTF8.self)
        let parts = buffer.components(separatedBy: "\n")
        buffers[taskIdentifier] = parts.last ?? ""
        completedLines = Array(parts.dropLast())
        bufferLock.unlock()

        // Обработчик может вызвать код владельца, поэтому выполняем его вне блокировки.
        completedLines.forEach(onLine)
    }

    private func complete(taskIdentifier: Int, error: Error?) {
        bufferLock.lock()
        buffers.removeValue(forKey: taskIdentifier)
        bufferLock.unlock()
        onComplete?(error)
    }
}
