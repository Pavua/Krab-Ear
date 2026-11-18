import Foundation

/// Bearer-ключ локального REST (`http://127.0.0.1:5005`).
///
/// С 2026-11-15 REST по умолчанию требует `Authorization: Bearer <ключ>` на
/// защищённых маршрутах (в том числе SSE `/v1/events`). Backend выдаёт ключ в
/// `<data_dir>/rest_api_key` (0600); агент читает его оттуда. `/health` ключа не
/// требует.
///
/// Порядок поиска ключа: env `KRAB_EAR_REST_API_KEY`, затем файл из env
/// `KRAB_EAR_REST_API_KEY_FILE`, затем
/// `~/Library/Application Support/KrabEar/rest_api_key`. Ключ нигде не
/// логируется. Нет ключа — заголовок не ставится (старый открытый режим
/// `REST_API_ALLOW_ANONYMOUS` продолжает работать).
enum RestAuth {
    static let defaultKeyFileURL = URL(
        fileURLWithPath: NSString(
            string: "~/Library/Application Support/KrabEar/rest_api_key"
        ).expandingTildeInPath
    )

    /// Минимальная длина осмысленного ключа (зеркалит backend `_MIN_KEY_LEN`).
    static let minimumKeyLength = 32

    static func bearerToken(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaultFile: URL = RestAuth.defaultKeyFileURL
    ) -> String? {
        if let fromEnv = environment["KRAB_EAR_REST_API_KEY"]?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            fromEnv.count >= minimumKeyLength {
            return fromEnv
        }
        let fileURL: URL
        if let override = environment["KRAB_EAR_REST_API_KEY_FILE"], !override.isEmpty {
            fileURL = URL(fileURLWithPath: NSString(string: override).expandingTildeInPath)
        } else {
            fileURL = defaultFile
        }
        guard let raw = try? String(contentsOf: fileURL, encoding: .utf8) else { return nil }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return token.count >= minimumKeyLength ? token : nil
    }

    /// Ставит `Authorization: Bearer …`, если ключ найден. Ключ читается при
    /// каждом вызове: SSE переподключается редко, а ключ может появиться уже
    /// после старта агента (backend выдаёт его при своём запуске).
    static func apply(
        to request: inout URLRequest,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaultFile: URL = RestAuth.defaultKeyFileURL
    ) {
        // Ключ уходит только на loopback (ревью E4 N3, 2026-11-16): URL базы REST
        // может прийти из настроек, а чужому хосту ключ отдавать нельзя.
        guard isLoopback(request.url) else {
            // Запрос могли перенацелить после первого apply: ранее поставленный ключ снимаем.
            if request.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer ") == true {
                request.setValue(nil, forHTTPHeaderField: "Authorization")
            }
            return
        }
        guard let token = bearerToken(environment: environment, defaultFile: defaultFile) else { return }
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }

    /// Только точные loopback-имена: `127.0.0.1.evil.example.com` не проходит.
    static func isLoopback(_ url: URL?) -> Bool {
        guard let host = url?.host?.lowercased() else { return false }
        return host == "127.0.0.1" || host == "localhost" || host == "::1"
    }
}
