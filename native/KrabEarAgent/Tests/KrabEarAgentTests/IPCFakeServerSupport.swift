import Foundation

/// Общий помощник тестовых fake-серверов IPC.
///
/// Реальный backend всегда эхом возвращает `id` запроса, а клиент (E4 п.7) теперь отвергает
/// ответ с чужим/отсутствующим id. Фейки раньше отвечали фиксированным JSON с произвольным id,
/// поэтому эхо вынесено сюда: id берётся из реально полученного запроса.
enum IPCFakeServerSupport {
    /// Возвращает `response` с `id`, подставленным из JSON-запроса (`requestBytes`).
    /// Если запрос или ответ не разбираются как JSON-объект, ответ отдаётся как есть.
    static func echoingRequestID(request requestBytes: [UInt8], into response: String) -> String {
        let firstLine = Data(requestBytes.prefix(while: { $0 != UInt8(ascii: "\n") }))
        guard
            let requestObject = (try? JSONSerialization.jsonObject(with: firstLine)) as? [String: Any],
            let requestID = requestObject["id"],
            var responseObject = (try? JSONSerialization.jsonObject(with: Data(response.utf8))) as? [String: Any]
        else { return response }
        responseObject["id"] = requestID
        guard
            let data = try? JSONSerialization.data(withJSONObject: responseObject),
            let text = String(data: data, encoding: .utf8)
        else { return response }
        return text
    }
}
