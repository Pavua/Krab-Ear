import Foundation

/// Общий слой чтения/разбора IPC-ответа. Раньше логика дублировалась в `IPCClient.call` и
/// `IPCRealSocketProvider.sendSync`, и в обоих были три дыры:
///  1. `SO_RCVTIMEO` действует на каждый `read`, а не на вызов целиком: ответ «по байту раз в
///     timeout-1 секунд» держал вызов бесконечно — нужен общий дедлайн;
///  2. размер ответа не ограничивался;
///  3. ответ без булева `ok` или с чужим `id` принимался как успех (id `null`/отсутствует —
///     допустимо: backend не всегда может его отразить, см. `parseEnvelope`).
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
                if errno == EINTR { continue }  // сигнал прервал read: повторяем в пределах общего дедлайна
                if errno == EAGAIN || errno == EWOULDBLOCK { throw IPCError.timeout }
                throw IPCError.readFailed
            }
            // EOF до перевода строки: ответ оборван (найденный перевод строки завершает цикл ниже).
            if count == 0 { throw IPCError.invalidResponse }
            // Перевод строки ищем ТОЛЬКО в свежем чанке. Поиск по всему накопленному буферу после
            // каждого read давал квадратичное время (замер -O: 4 МБ — 18 с, 8 МБ — 45 с) и под
            // общим дедлайном превращал крупный легитимный ответ в timeout.
            if let end = chunk[0..<count].firstIndex(of: newline) {
                responseData.append(contentsOf: chunk[0...end])
                break
            }
            responseData.append(contentsOf: chunk[0..<count])
            // Лимит без перевода строки: читать дальше нет смысла — ответ заведомо невалиден.
            if responseData.count > maxBytes { throw IPCError.invalidResponse }
        }
        if responseData.count > maxBytes { throw IPCError.invalidResponse }
        return responseData
    }

    /// Только настоящие JSON `true`/`false`. `as? Bool` на NSNumber принимает и `1`/`0`.
    static func strictBool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    /// Строгий разбор конверта `{id, ok, result|error}`.
    /// - Parameter expectedID: id запроса; `nil` отключает проверку (тестовый seam с моками,
    ///   которые не знают случайный id).
    static func parseEnvelope(_ data: Data, expectedID: String?) throws -> [String: Any] {
        guard
            let text = String(data: data, encoding: .utf8),
            let line = text.split(separator: "\n").first,
            let json = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
            let ok = strictBool(json["ok"])
        else { throw IPCError.invalidResponse }

        // Backend отражает id через `safe_request_id`: при сбое санитайзера (текст запроса с
        // незакрытой кавычкой после `receipt:`/`capability:`, больше 20000 узлов JSON) в ответе
        // `"id": null`, хотя запрос ВЫПОЛНЕН. Соединение одноразовое, поэтому «id не отражён» —
        // не чужой ответ; отвергаем только явно другой id.
        if let expectedID, let echoed = json["id"], !(echoed is NSNull) {
            guard (echoed as? String) == expectedID else { throw IPCError.invalidResponse }
        }
        if !ok {
            let error = (json["error"] as? [String: Any]) ?? [:]
            throw IPCError.backendError((error["message"] as? String) ?? "unknown")
        }
        return json
    }
}
