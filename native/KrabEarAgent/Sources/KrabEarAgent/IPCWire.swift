import Foundation

/// Общий слой чтения/разбора IPC-ответа. Раньше логика дублировалась в `IPCClient.call` и
/// `IPCRealSocketProvider.sendSync`, и в обоих были три дыры:
///  1. `SO_RCVTIMEO` действует на каждый `read`, а не на вызов целиком: ответ «по байту раз в
///     timeout-1 секунд» держал вызов бесконечно — нужен общий дедлайн;
///  2. размер ответа не ограничивался;
///  3. ответ без булева `ok` или с чужим `id` принимался как успех.
enum IPCWire {
    /// Верхняя граница одного ответа. Крупнейшие легитимные ответы (история/экспорт) — единицы МБ.
    static let maxResponseBytes = 32 * 1024 * 1024

    private static let readBufferSize = 4096

    /// Читает ответ до первого перевода строки под ОБЩИМ дедлайном `timeoutSec`.
    /// Возвращает данные до перевода строки включительно (хвост после него отбрасывается).
    static func readResponse(fd: Int32, timeoutSec: Int, maxBytes: Int = maxResponseBytes) throws -> Data {
        let deadline = ContinuousClock.now + .seconds(timeoutSec)
        var responseData = Data()
        var chunk = [UInt8](repeating: 0, count: readBufferSize)
        let newline = UInt8(ascii: "\n")

        while true {
            // Остаток общего бюджета становится таймаутом ЭТОГО read.
            let remaining = deadline - ContinuousClock.now
            guard remaining > .zero else { throw IPCError.timeout }
            let parts = remaining.components
            var tv = timeval(
                tv_sec: Int(parts.seconds),
                tv_usec: __darwin_suseconds_t(max(1, parts.attoseconds / 1_000_000_000_000))
            )
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

            let count = Darwin.read(fd, &chunk, chunk.count)
            if count < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK { throw IPCError.timeout }
                throw IPCError.readFailed
            }
            if count == 0 {
                guard responseData.contains(newline) else { throw IPCError.invalidResponse }
                break
            }
            responseData.append(contentsOf: chunk[0..<count])
            if let end = responseData.firstIndex(of: newline) {
                responseData = responseData.prefix(through: end)
                break
            }
            // Лимит без перевода строки: читать дальше нет смысла — ответ заведомо невалиден.
            if responseData.count > maxBytes { throw IPCError.invalidResponse }
        }
        if responseData.count > maxBytes { throw IPCError.invalidResponse }
        return responseData
    }

    /// Строгий разбор конверта `{id, ok, result|error}`.
    /// - Parameter expectedID: id запроса; `nil` отключает проверку (тестовый seam с моками,
    ///   которые не знают случайный id).
    static func parseEnvelope(_ data: Data, expectedID: String?) throws -> [String: Any] {
        guard
            let text = String(data: data, encoding: .utf8),
            let line = text.split(separator: "\n").first,
            let json = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
            let ok = json["ok"] as? Bool
        else { throw IPCError.invalidResponse }

        if let expectedID {
            guard (json["id"] as? String) == expectedID else { throw IPCError.invalidResponse }
        }
        if !ok {
            let error = (json["error"] as? [String: Any]) ?? [:]
            throw IPCError.backendError((error["message"] as? String) ?? "unknown")
        }
        return json
    }
}
