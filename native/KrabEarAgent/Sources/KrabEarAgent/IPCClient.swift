/*
 Unix socket IPC-клиент для вызова методов Python backend.

 Связи модуля:
 1) BackendSupervisor/main.swift/HistoryPanelController: единый канал команд.
*/

import Foundation

enum IPCError: Error, LocalizedError {
    case socketCreateFailed
    case socketConnectFailed(String)
    case writeFailed
    case readFailed
    case invalidResponse
    case backendError(String)

    var errorDescription: String? {
        switch self {
        case .socketCreateFailed:
            return "Не удалось создать IPC сокет"
        case .socketConnectFailed(let reason):
            return "Не удалось подключиться к backend: \(reason)"
        case .writeFailed:
            return "Ошибка отправки данных в backend"
        case .readFailed:
            return "Ошибка чтения ответа backend"
        case .invalidResponse:
            return "Backend вернул некорректный ответ"
        case .backendError(let message):
            return "Backend ошибка: \(message)"
        }
    }
}

/// Клиент JSON-команд к локальному Unix socket backend.
final class IPCClient {
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
            throw IPCError.socketCreateFailed
        }
        defer { close(fd) }

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
        let ipcReadBufferSize = 4096
        var chunk = [UInt8](repeating: 0, count: ipcReadBufferSize)

        while true {
            let count = Darwin.read(fd, &chunk, chunk.count)
            if count < 0 {
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
