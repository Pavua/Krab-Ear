/*
 Unix socket IPC-клиент для вызова методов Python backend.

 Связи модуля:
 1) BackendSupervisor/main.swift/HistoryPanelController: единый канал команд.
*/

import Foundation

/// Max IPC response chunk size in bytes. 4 KB covers all current payloads; larger responses stream in multiple reads.
private let ipcReadBufferSize: Int = 4096

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
            return "Backend не ответил за 5 секунд (timeout)"
        case .invalidResponse:
            return "Backend вернул некорректный ответ"
        case .backendError(let message):
            return "Backend ошибка: \(message)"
        }
    }
}

/// Клиент JSON-команд к локальному Unix socket backend.
final class IPCClient: @unchecked Sendable {
    private let socketPath: String
    var endpoint: String { socketPath }

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    func call(method: String, params: [String: Any] = [:]) throws -> [String: Any] {
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

        // Socket timeout: 60s upper bound для long-running операций (transcribe на
        // max profile = 4-10s + LLM rewrite 0.6-1s; для длинных аудио до 30+s).
        // PR #288 поставил 5s timeout как mitigation для AGENT-3 hangs, но это
        // обрезало валидные транскрипты > 5s (см. backend.log: "STT готово: 4.47s").
        // AGENT-3 root cause (sync IPC на main thread) уже покрыт PRs #299/#300/#315
        // — 30+ функций async-wrapped через DispatchQueue.global. С async wrap'ом
        // 60s timeout не блокирует UI: long ops ждут на background, UI остаётся
        // responsive. Per-method timeout — отдельный future PR.
        var tv = timeval(tv_sec: 60, tv_usec: 0)
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
