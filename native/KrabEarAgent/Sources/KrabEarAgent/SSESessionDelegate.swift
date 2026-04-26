/*
 SSESessionDelegate — общий URLSessionDataDelegate для SSE long-poll стриминга.

 Используется LiveSubtitlesOverlay (Phase 2B HUD) и TranslationStreamView
 (Phase 2 PR 2.3 dual-pane). Раньше был file-private inside LiveSubtitlesOverlay;
 вынесен в shared file чтобы избежать дубликата.

 Обработка:
 - Накапливает данные в buffer; разрезает по `\n`; отправляет каждую полную
   строку в onLine callback. Последний (incomplete) кусок сохраняется в buffer
   до следующего didReceive.
 - При completion (с error или без) callbacks больше не вызываются — owner
   решает рестартовать ли task.
*/

import Foundation

final class SSESessionDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let onLine: (String) -> Void
    private var buffer = ""

    init(onLine: @escaping (String) -> Void) {
        self.onLine = onLine
        super.init()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer += String(decoding: data, as: UTF8.self)
        // Разбиваем по \n, отправляем полные строки.
        let lines = buffer.components(separatedBy: "\n")
        buffer = lines.last ?? ""
        for line in lines.dropLast() {
            onLine(line)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        // SSE соединение закрылось — не перезапускаем (stop() уже вызван).
    }
}
