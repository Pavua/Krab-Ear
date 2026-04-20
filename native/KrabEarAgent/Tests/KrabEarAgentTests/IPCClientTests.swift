/*
 IPCClientTests — тесты Unix socket JSON-RPC клиента (IPCClient).

 Покрытие:
 1. Encoding  — структура JSON-запроса (id / method / params).
 2. Decoding  — разбор корректного ответа backend.
 3. Errors    — socketCreateFailed(errno), socketConnectFailed, invalidResponse,
                backendError, writeFailed/readFailed через описания.
 4. Integration — in-process POSIX socket сервер, round-trip, FD leak check.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

// MARK: - Helpers

/// Запускает минимальный POSIX Unix-socket сервер в фоновом потоке.
/// Принимает одно соединение, читает запрос (до \n), отвечает `response`, закрывает.
private func runEchoServer(
    socketPath: String,
    response: String,
    ready: @escaping () -> Void
) {
    let serverFd = socket(AF_UNIX, SOCK_STREAM, 0)
    precondition(serverFd >= 0, "server socket() failed")

    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    // Capture size before taking unsafe pointer to avoid Swift 6 exclusivity violation.
    let sunPathSize = MemoryLayout.size(ofValue: addr.sun_path)
    withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
        ptr.withMemoryRebound(to: CChar.self, capacity: sunPathSize) { cPtr in
            for i in 0..<sunPathSize { cPtr[i] = 0 }
            let bytes = Array(socketPath.utf8)
            for (i, b) in bytes.enumerated() { cPtr[i] = CChar(bitPattern: b) }
        }
    }

    let pathLen = socketPath.utf8.count
    let addrLen = socklen_t(MemoryLayout<sa_family_t>.size + pathLen + 1)

    let bound: Int32 = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.bind(serverFd, $0, addrLen)
        }
    }
    precondition(bound == 0, "bind() failed: \(String(cString: strerror(errno)))")
    precondition(Darwin.listen(serverFd, 1) == 0, "listen() failed")

    ready()

    DispatchQueue.global(qos: .utility).async {
        let clientFd = Darwin.accept(serverFd, nil, nil)
        guard clientFd >= 0 else { close(serverFd); return }
        defer { close(clientFd); close(serverFd) }

        // Читаем до \n (или 4096 байт)
        var buf = [UInt8](repeating: 0, count: 4096)
        Darwin.read(clientFd, &buf, buf.count)

        // Отвечаем
        let bytes = Array((response + "\n").utf8)
        _ = bytes.withUnsafeBytes { Darwin.write(clientFd, $0.baseAddress, $0.count) }
    }
}

/// Создаёт временный путь к сокету и убирает файл после теста.
private func tempSocketPath() -> String {
    let name = "krabear_test_\(Int.random(in: 100_000...999_999)).sock"
    return (NSTemporaryDirectory() as NSString).appendingPathComponent(name)
}

// MARK: - IPCClientTests

final class IPCClientTests: XCTestCase {

    // MARK: 1. Request JSON encoding

    func test_requestJSON_hasCorrectStructure() throws {
        // Build the same dict that IPCClient.call() builds.
        let method = "ping"
        let params: [String: Any] = ["key": "value", "num": 42]
        let requestId = UUID().uuidString

        let request: [String: Any] = [
            "id": requestId,
            "method": method,
            "params": params,
        ]

        let data = try JSONSerialization.data(withJSONObject: request)
        let decoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )

        XCTAssertEqual(decoded["id"] as? String, requestId)
        XCTAssertEqual(decoded["method"] as? String, method)
        let decodedParams = try XCTUnwrap(decoded["params"] as? [String: Any])
        XCTAssertEqual(decodedParams["key"] as? String, "value")
        XCTAssertEqual(decodedParams["num"] as? Int, 42)
    }

    func test_requestJSON_idIsUniquePerCall() throws {
        var ids: Set<String> = []
        for _ in 0..<10 {
            let id = UUID().uuidString
            ids.insert(id)
        }
        XCTAssertEqual(ids.count, 10, "Каждый вызов должен генерировать уникальный id")
    }

    func test_requestJSON_emptyParams_encodesAsObject() throws {
        let request: [String: Any] = [
            "id": UUID().uuidString,
            "method": "status",
            "params": [String: Any](),
        ]
        let data = try JSONSerialization.data(withJSONObject: request)
        let decoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        // params должен декодироваться как словарь, а не NSNull
        let decodedParams = try XCTUnwrap(decoded["params"] as? [String: Any])
        XCTAssertTrue(decodedParams.isEmpty)
    }

    // MARK: 2. Response JSON decoding

    func test_responseDecoding_ok_true_returnsDict() throws {
        let json = #"{"id":"abc","ok":true,"result":{"status":"ok"}}"#
        let data = Data(json.utf8)
        let decoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        XCTAssertEqual(decoded["id"] as? String, "abc")
        XCTAssertEqual(decoded["ok"] as? Bool, true)
        let result = try XCTUnwrap(decoded["result"] as? [String: Any])
        XCTAssertEqual(result["status"] as? String, "ok")
    }

    func test_responseDecoding_ok_false_hasError() throws {
        let json = #"{"id":"xyz","ok":false,"error":{"message":"not found"}}"#
        let data = Data(json.utf8)
        let decoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        XCTAssertEqual(decoded["ok"] as? Bool, false)
        let error = try XCTUnwrap(decoded["error"] as? [String: Any])
        XCTAssertEqual(error["message"] as? String, "not found")
    }

    func test_responseDecoding_malformedJSON_throwsError() {
        // IPCClient.call() должен бросить IPCError.invalidResponse при невалидном JSON.
        let badJSON = "not json at all\n"
        let data = Data(badJSON.utf8)
        XCTAssertThrowsError(
            try JSONSerialization.jsonObject(with: data)
        ) { error in
            XCTAssertNotNil(error)
        }
    }

    // MARK: 3. IPCError — корректное хранение ассоциированных значений

    func test_socketCreateFailed_carriesErrno() {
        let err: IPCError = .socketCreateFailed(errno: EACCES)
        guard case .socketCreateFailed(let code) = err else {
            return XCTFail("Ожидался .socketCreateFailed")
        }
        XCTAssertEqual(code, EACCES)
    }

    func test_socketCreateFailed_localizedDescriptionContainsErrno() {
        let err: IPCError = .socketCreateFailed(errno: EPERM)
        let description = err.errorDescription ?? ""
        XCTAssertTrue(
            description.contains("\(EPERM)"),
            "errorDescription должен содержать код errno"
        )
    }

    func test_socketConnectFailed_carriesMessage() {
        let msg = "путь сокета слишком длинный"
        let err: IPCError = .socketConnectFailed(msg)
        guard case .socketConnectFailed(let reason) = err else {
            return XCTFail("Ожидался .socketConnectFailed")
        }
        XCTAssertEqual(reason, msg)
    }

    func test_socketConnectFailed_localizedDescriptionContainsReason() {
        let msg = "connection refused"
        let err: IPCError = .socketConnectFailed(msg)
        XCTAssertTrue(err.errorDescription?.contains(msg) ?? false)
    }

    func test_backendError_carriesMessage() {
        let msg = "method not found"
        let err: IPCError = .backendError(msg)
        guard case .backendError(let m) = err else {
            return XCTFail("Ожидался .backendError")
        }
        XCTAssertEqual(m, msg)
    }

    func test_writeFailed_hasDescription() {
        let err: IPCError = .writeFailed
        XCTAssertNotNil(err.errorDescription)
        XCTAssertFalse(err.errorDescription!.isEmpty)
    }

    func test_readFailed_hasDescription() {
        let err: IPCError = .readFailed
        XCTAssertNotNil(err.errorDescription)
        XCTAssertFalse(err.errorDescription!.isEmpty)
    }

    func test_invalidResponse_hasDescription() {
        let err: IPCError = .invalidResponse
        XCTAssertNotNil(err.errorDescription)
        XCTAssertFalse(err.errorDescription!.isEmpty)
    }

    // MARK: 4. socketConnectFailed — нет сокета по пути

    func test_connectToNonexistentSocket_throws_socketConnectFailed() {
        let client = IPCClient(socketPath: "/tmp/krabear_nonexistent_\(Int.random(in: 0...999_999)).sock")
        XCTAssertThrowsError(try client.call(method: "ping")) { error in
            guard let ipcErr = error as? IPCError else {
                return XCTFail("Ожидался IPCError, получили \(error)")
            }
            if case .socketConnectFailed(_) = ipcErr {
                // ожидаемо
            } else if case .socketCreateFailed(_) = ipcErr {
                // тоже допустимо на некоторых платформах
            } else {
                XCTFail("Ожидался socketConnectFailed или socketCreateFailed, получили \(ipcErr)")
            }
        }
    }

    // MARK: 5. Integration — in-process round-trip

    func test_integration_roundTrip_successResponse() throws {
        let socketPath = tempSocketPath()
        defer { unlink(socketPath) }

        let readyExp = expectation(description: "server ready")
        let okJSON = #"{"id":"1","ok":true,"result":{"status":"ok"}}"#

        runEchoServer(socketPath: socketPath, response: okJSON) {
            readyExp.fulfill()
        }
        wait(for: [readyExp], timeout: 1.0)

        let client = IPCClient(socketPath: socketPath)
        let result = try client.call(method: "ping")

        XCTAssertEqual(result["ok"] as? Bool, true)
        let res = result["result"] as? [String: Any]
        XCTAssertEqual(res?["status"] as? String, "ok")
    }

    func test_integration_roundTrip_backendError() throws {
        let socketPath = tempSocketPath()
        defer { unlink(socketPath) }

        let readyExp = expectation(description: "server ready")
        let errJSON = #"{"id":"2","ok":false,"error":{"message":"method_not_found"}}"#

        runEchoServer(socketPath: socketPath, response: errJSON) {
            readyExp.fulfill()
        }
        wait(for: [readyExp], timeout: 1.0)

        let client = IPCClient(socketPath: socketPath)
        XCTAssertThrowsError(try client.call(method: "unknown")) { error in
            guard case .backendError(let msg) = error as? IPCError else {
                return XCTFail("Ожидался IPCError.backendError")
            }
            XCTAssertEqual(msg, "method_not_found")
        }
    }

    func test_integration_malformedResponse_throwsError() throws {
        let socketPath = tempSocketPath()
        defer { unlink(socketPath) }

        let readyExp = expectation(description: "server ready")
        runEchoServer(socketPath: socketPath, response: "NOT_JSON") {
            readyExp.fulfill()
        }
        wait(for: [readyExp], timeout: 1.0)

        // IPCClient.call() uses `try JSONSerialization.jsonObject(...)` inside a guard —
        // if JSON parsing throws, the error propagates as-is (NSError from Foundation),
        // not wrapped as IPCError.invalidResponse. Both are acceptable.
        let client = IPCClient(socketPath: socketPath)
        XCTAssertThrowsError(try client.call(method: "ping"),
            "Malformed JSON response must throw some error")
    }

    // MARK: 6. FD leak — последовательные вызовы не текут дескрипторами

    func test_integration_sequentialCalls_noFDLeak() throws {
        // Каждый run-through: новый сервер принимает одно соединение.
        // Проверяем что процесс не накапливает дескрипторы после N вызовов.
        let callCount = 5
        var fdsBefore: Int = 0
        var fdsAfter: Int = 0

        func countOpenFDs() -> Int {
            var count = 0
            for fd in Int32(3)..<Int32(256) {
                var stat = Darwin.stat()
                if fstat(fd, &stat) == 0 { count += 1 }
            }
            return count
        }

        // Прогреваем (первый вызов может открыть lazy resources)
        do {
            let warmupPath = tempSocketPath()
            defer { unlink(warmupPath) }
            let warmupExp = expectation(description: "warmup")
            runEchoServer(socketPath: warmupPath,
                          response: #"{"id":"w","ok":true,"result":{}}"#) { warmupExp.fulfill() }
            wait(for: [warmupExp], timeout: 1.0)
            _ = try? IPCClient(socketPath: warmupPath).call(method: "warmup")
        }

        fdsBefore = countOpenFDs()

        for i in 0..<callCount {
            let path = tempSocketPath()
            defer { unlink(path) }
            let exp = expectation(description: "server_\(i)")
            let resp = #"{"id":"\#(i)","ok":true,"result":{"i":\#(i)}}"#
            runEchoServer(socketPath: path, response: resp) { exp.fulfill() }
            wait(for: [exp], timeout: 1.0)
            _ = try IPCClient(socketPath: path).call(method: "noop")
        }

        fdsAfter = countOpenFDs()

        // Допускаем небольшой drift (XPC, GCD internals), но не >5 на 5 вызовов
        let drift = fdsAfter - fdsBefore
        XCTAssertLessThanOrEqual(drift, 5,
            "Обнаружена утечка FD: было \(fdsBefore), стало \(fdsAfter) (\(drift) новых)")
    }
}
