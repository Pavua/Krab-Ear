/*
 Unix socket IPC-клиент для вызова методов Python backend.

 Связи модуля:
 1) BackendSupervisor/main.swift/HistoryPanelController: единый канал команд.
*/

import Foundation

/// Max IPC response chunk size in bytes. 4 KB covers all current payloads; larger responses stream in multiple reads.
private let ipcReadBufferSize: Int = 4096

// MARK: - IPCSocketProviding (testability injection point)

/// Abstraction over the low-level "send a JSON payload, get a JSON response" operation.
/// Production code uses `IPCRealSocketProvider` (POSIX Unix socket).
/// Tests inject `FlakyMockSocketProvider` or similar to exercise reconnect logic without
/// spawning a real Python backend.
///
/// Conformances must be `Sendable` because `IPCClient` schedules calls on background queues.
protocol IPCSocketProviding: Sendable {
    /// Sends `payload` over the underlying transport and returns the raw response `Data`.
    /// - Parameter payload: JSON-encoded request (already includes the trailing newline).
    /// - Parameter timeoutSec: Wall-clock timeout for the underlying I/O operations.
    /// - Throws: `IPCError` on any socket / transport failure.
    func send(payload: Data, timeoutSec: Int) async throws -> Data
}

// MARK: - Real provider (POSIX Unix socket)

/// Default `IPCSocketProviding` implementation that connects to a Unix-domain socket.
final class IPCRealSocketProvider: IPCSocketProviding, @unchecked Sendable {
    private let socketPath: String

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        struct Capture: @unchecked Sendable { let payload: Data; let timeoutSec: Int }
        let cap = Capture(payload: payload, timeoutSec: timeoutSec)
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async { [socketPath] in
                do {
                    let data = try IPCRealSocketProvider.sendSync(
                        payload: cap.payload,
                        timeoutSec: cap.timeoutSec,
                        socketPath: socketPath
                    )
                    cont.resume(returning: data)
                } catch {
                    cont.resume(throwing: error)
                }
            }
        }
    }

    private static func sendSync(payload: Data, timeoutSec: Int, socketPath: String) throws -> Data {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw IPCError.socketCreateFailed(errno: errno) }
        defer { close(fd) }

        var noSigpipe: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSigpipe, socklen_t(MemoryLayout<Int32>.size))

        var tv = timeval(tv_sec: timeoutSec, tv_usec: 0)
        let tvSize = socklen_t(MemoryLayout<timeval>.size)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, tvSize)
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, tvSize)

        try IPCRealSocketProvider.connectSocket(fd: fd, socketPath: socketPath)

        let bytes = Array(payload)
        let written = bytes.withUnsafeBytes { buf in
            Darwin.write(fd, buf.baseAddress, buf.count)
        }
        guard written == bytes.count else { throw IPCError.writeFailed }

        var responseData = Data()
        var chunk = [UInt8](repeating: 0, count: ipcReadBufferSize)
        while true {
            let count = Darwin.read(fd, &chunk, chunk.count)
            if count < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK { throw IPCError.timeout }
                throw IPCError.readFailed
            }
            if count == 0 {
                if responseData.isEmpty || !responseData.contains(UInt8(ascii: "\n")) {
                    throw IPCError.invalidResponse
                }
                break
            }
            responseData.append(contentsOf: chunk[0..<count])
            if chunk[0..<count].contains(UInt8(ascii: "\n")) { break }
        }
        return responseData
    }

    private static func connectSocket(fd: Int32, socketPath: String) throws {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)

        let maxLength = MemoryLayout.size(ofValue: addr.sun_path)
        let utf8Path = Array(socketPath.utf8)
        if utf8Path.count >= maxLength {
            throw IPCError.socketConnectFailed("путь сокета слишком длинный")
        }

        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: maxLength) { cPtr in
                for i in 0..<maxLength { cPtr[i] = 0 }
                for (idx, byte) in utf8Path.enumerated() {
                    cPtr[idx] = CChar(bitPattern: byte)
                }
            }
        }

        let pathLength = utf8Path.count
        let addrLen = socklen_t(MemoryLayout<sa_family_t>.size + pathLength + 1)

        let result = withUnsafePointer(to: &addr) { rawPtr -> Int32 in
            rawPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(fd, sockPtr, addrLen)
            }
        }

        if result != 0 {
            let errnoString = String(cString: strerror(errno))
            throw IPCError.socketConnectFailed(errnoString)
        }
    }
}

enum IPCError: Error, LocalizedError {
    case socketCreateFailed(errno: Int32)
    case socketConnectFailed(String)
    case writeFailed
    case readFailed
    case timeout
    case invalidResponse
    case backendError(String)

    var errorDescription: String? {
        switch self {
        case .socketCreateFailed(let err):
            return "Не удалось создать IPC сокет (errno=\(err): \(String(cString: strerror(err))))"
        case .socketConnectFailed(let reason):
            return "Не удалось подключиться к backend: \(reason)"
        case .writeFailed:
            return "Ошибка отправки данных в backend"
        case .readFailed:
            return "Ошибка чтения ответа backend"
        case .timeout:
            return "Backend не ответил за установленный таймаут (timeout)"
        case .invalidResponse:
            return "Backend вернул некорректный ответ"
        case .backendError(let message):
            return "Backend ошибка: \(message)"
        }
    }

    /// Transient errors: socket missing or connection reset — safe to retry.
    var isTransient: Bool {
        switch self {
        case .socketConnectFailed, .writeFailed, .readFailed:
            return true
        case .socketCreateFailed, .timeout, .invalidResponse, .backendError:
            return false
        }
    }
}

/// Клиент JSON-команд к локальному Unix socket backend.
final class IPCClient: @unchecked Sendable {
    private let socketPath: String
    var endpoint: String { socketPath }

    /// Injectable socket provider — `nil` means use the real POSIX Unix socket.
    /// Set a custom `IPCSocketProviding` implementation in tests to exercise
    /// reconnect/backoff logic without a real backend process.
    let socketProvider: (any IPCSocketProviding)?

    /// Production initialiser — uses POSIX Unix socket.
    init(socketPath: String) {
        self.socketPath = socketPath
        self.socketProvider = nil
    }

    /// Testability initialiser — injects a custom socket provider.
    /// `socketPath` is unused when a provider is supplied (kept for `endpoint` consistency).
    init(socketPath: String = "/tmp/krabear_mock.sock", socketProvider: any IPCSocketProviding) {
        self.socketPath = socketPath
        self.socketProvider = socketProvider
    }

    /// Default socket timeout (seconds) для long-running operations
    /// (transcribe на max profile + LLM rewrite = 5-10 сек typically; up to 60s
    /// для длинных audio file imports). См. PR #316 для history rationale.
    public static let defaultTimeoutSec: Int = 60

    /// Quick timeout (seconds) для status / diagnostics calls которые должны
    /// ответить быстро. Используй через `call(method:params:timeoutSec:)`
    /// overload (например `getDiagnostics`, `getRecordingState`, `ping`).
    public static let quickTimeoutSec: Int = 5

    func call(method: String, params: [String: Any] = [:]) throws -> [String: Any] {
        return try call(method: method, params: params, timeoutSec: Self.defaultTimeoutSec)
    }

    /// Performs the backend handshake on first connect.
    ///
    /// Sends `swift_agent_version` + `capabilities` to backend and logs any
    /// version mismatch warnings. Gracefully degrades — never throws on
    /// unexpected `backend_version` or missing `phase_b_capable`.
    ///
    /// Call once after the socket path becomes reachable (e.g. in
    /// BackendSupervisor after backend reports ready).
    func performHandshake(
        swiftAgentVersion: String = "1.0.0",
        capabilities: [String] = ["error_bus_consumer", "live_subs", "selection_translator"]
    ) async {
        do {
            let result = try await callAsync(
                method: "handshake",
                params: [
                    "swift_agent_version": swiftAgentVersion,
                    "capabilities": capabilities,
                ],
                timeoutSec: IPCClient.quickTimeoutSec
            )
            let backendVersion = result["result"] as? [String: Any]
            let bv = backendVersion?["backend_version"] as? String ?? "unknown"
            let phaseB = (backendVersion?["phase_b_capable"] as? Bool) ?? false
            let phaseC = (backendVersion?["phase_c_capable"] as? Bool) ?? false
            if !phaseB {
                NSLog("[IPCClient] WARNING: backend does not report phase_b_capable")
            }
            NSLog(
                "[IPCClient] handshake OK: backend_version=%@ phase_b=%d phase_c=%d",
                bv, phaseB ? 1 : 0, phaseC ? 1 : 0
            )
        } catch {
            // Handshake failure is non-fatal — older backends don't have the method.
            NSLog("[IPCClient] handshake skipped (backend error: %@)", error.localizedDescription)
        }
    }

    /// Async-friendly wrapper: выполняет sync `call(...)` на background queue,
    /// возвращая результат через Swift Concurrency. Использовать для всех
    /// IPC вызовов из @MainActor контекста — main thread не блокируется,
    /// AppHang детектор не срабатывает (Sentry KRAB-EAR-AGENT-4/8/A/B/C/D).
    ///
    /// When `socketProvider` is injected (test mode), routes through the provider
    /// instead of the POSIX socket stack so reconnect/backoff tests run without
    /// a real backend process.
    func callAsync(
        method: String,
        params: [String: Any] = [:],
        timeoutSec: Int = IPCClient.defaultTimeoutSec
    ) async throws -> [String: Any] {
        // Route through injected provider if present (test mode).
        if let provider = socketProvider {
            return try await callViaProvider(
                method: method, params: params, timeoutSec: timeoutSec, provider: provider
            )
        }

        // [String: Any] не Sendable — оборачиваем в @unchecked Sendable box
        // чтобы передать через @Sendable closure без data race warning.
        // IPC params/response — immutable JSON dict, гонок нет.
        struct SendableCapture: @unchecked Sendable {
            let method: String
            let params: [String: Any]
            let timeoutSec: Int
        }
        let captured = SendableCapture(method: method, params: params, timeoutSec: timeoutSec)
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else {
                    continuation.resume(throwing: IPCError.invalidResponse)
                    return
                }
                do {
                    let result = try self.call(
                        method: captured.method,
                        params: captured.params,
                        timeoutSec: captured.timeoutSec
                    )
                    continuation.resume(returning: result)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    /// Sends a request through the injected `IPCSocketProviding` and decodes the JSON response.
    private func callViaProvider(
        method: String,
        params: [String: Any],
        timeoutSec: Int,
        provider: any IPCSocketProviding
    ) async throws -> [String: Any] {
        let request: [String: Any] = [
            "id": UUID().uuidString,
            "method": method,
            "params": params,
        ]
        let payload = try JSONSerialization.data(withJSONObject: request)
        // Append newline delimiter (same framing as the real socket path).
        let delimited = payload + Data("\n".utf8)

        let responseData = try await provider.send(payload: delimited, timeoutSec: timeoutSec)

        guard
            let text = String(data: responseData, encoding: .utf8),
            let line = text.split(separator: "\n").first,
            let json = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
        else {
            throw IPCError.invalidResponse
        }

        if let ok = json["ok"] as? Bool, ok == false {
            let errDict = (json["error"] as? [String: Any]) ?? [:]
            let message = (errDict["message"] as? String) ?? "unknown"
            throw IPCError.backendError(message)
        }

        return json
    }

    /// Variant с explicit timeout. Используй `IPCClient.quickTimeoutSec` (5s) для
    /// status / diagnostics и `IPCClient.defaultTimeoutSec` (60s) для transcribe /
    /// summarize / extract. Custom Int OK для special cases.
    func call(method: String, params: [String: Any] = [:], timeoutSec: Int) throws -> [String: Any] {
        let request: [String: Any] = [
            "id": UUID().uuidString,
            "method": method,
            "params": params,
        ]

        let payload = try JSONSerialization.data(withJSONObject: request)
        guard let payloadString = String(data: payload, encoding: .utf8) else {
            throw IPCError.invalidResponse
        }

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw IPCError.socketCreateFailed(errno: errno)
        }
        defer { close(fd) }

        // SO_NOSIGPIPE: write() в закрытый сокет вернёт EPIPE вместо SIGPIPE-kill.
        // Дублирует global signal(SIGPIPE, SIG_IGN) из main.swift (KRAB-EAR-AGENT-F).
        var noSigpipe: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSigpipe, socklen_t(MemoryLayout<Int32>.size))

        // Per-method timeout: status calls могут использовать quickTimeoutSec (5s);
        // transcribe / LLM ops — defaultTimeoutSec (60s, см. PR #316 rationale).
        // AGENT-3 root cause (sync IPC на main thread) покрыт PRs #299/#300/#315.
        var tv = timeval(tv_sec: timeoutSec, tv_usec: 0)
        let tvSize = socklen_t(MemoryLayout<timeval>.size)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, tvSize)
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, tvSize)

        try connectSocket(fd: fd)

        let requestBytes = Array((payloadString + "\n").utf8)
        let written = requestBytes.withUnsafeBytes { buffer in
            Darwin.write(fd, buffer.baseAddress, buffer.count)
        }
        guard written == requestBytes.count else {
            throw IPCError.writeFailed
        }

        var responseData = Data()
        // IPC reads in a loop until EOF, so this is just the per-read buffer size
        var chunk = [UInt8](repeating: 0, count: ipcReadBufferSize)

        while true {
            let count = Darwin.read(fd, &chunk, chunk.count)
            if count < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK {
                    throw IPCError.timeout
                }
                throw IPCError.readFailed
            }
            if count == 0 {
                if responseData.isEmpty || !responseData.contains(UInt8(ascii: "\n")) {
                    throw IPCError.invalidResponse
                }
                break
            }
            responseData.append(contentsOf: chunk[0..<count])
            if chunk[0..<count].contains(UInt8(ascii: "\n")) {
                break
            }
        }

        guard
            let text = String(data: responseData, encoding: .utf8),
            let line = text.split(separator: "\n").first,
            let json = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
        else {
            throw IPCError.invalidResponse
        }

        if let ok = json["ok"] as? Bool, ok == false {
            let error = (json["error"] as? [String: Any]) ?? [:]
            let message = (error["message"] as? String) ?? "unknown"
            throw IPCError.backendError(message)
        }

        return json
    }

    private func connectSocket(fd: Int32) throws {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)

        let maxLength = MemoryLayout.size(ofValue: addr.sun_path)
        let utf8Path = Array(socketPath.utf8)
        if utf8Path.count >= maxLength {
            throw IPCError.socketConnectFailed("путь сокета слишком длинный")
        }

        // Заполняем C-буфер нулями и копируем путь сокета.
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: maxLength) { cPtr in
                for i in 0..<maxLength { cPtr[i] = 0 }
                for (idx, byte) in utf8Path.enumerated() {
                    cPtr[idx] = CChar(bitPattern: byte)
                }
            }
        }

        let pathLength = utf8Path.count
        let addrLen = socklen_t(MemoryLayout<sa_family_t>.size + pathLength + 1)

        let result = withUnsafePointer(to: &addr) { rawPtr -> Int32 in
            rawPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(fd, sockPtr, addrLen)
            }
        }

        if result != 0 {
            let errnoString = String(cString: strerror(errno))
            throw IPCError.socketConnectFailed(errnoString)
        }
    }
}
