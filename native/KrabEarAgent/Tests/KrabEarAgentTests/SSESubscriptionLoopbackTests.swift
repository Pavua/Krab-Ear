import XCTest
import Foundation
import Darwin
@testable import KrabEarAgent

/// Реальные TCP/HTTP/URLSession/parser/owner, только loopback и synthetic SSE.
final class SSESubscriptionLoopbackTests: XCTestCase {
    @MainActor
    func testActualURLSessionReconnectsAfterServerClosesStream() async throws {
        let server = try SSELoopbackFixture()
        let serving = Task.detached { server.serveTwoStreams() }
        let received = expectation(description: "both real streams")
        received.expectedFulfillmentCount = 2
        var lines: [String] = []
        let owner = SSESubscriptionTransport()
        owner.start(request: URLRequest(url: URL(string: "http://127.0.0.1:\(server.port)/events")!),
                    onConnectionReset: {}, onLine: { line in
            if line.hasPrefix("data: ") { lines.append(line); received.fulfill() }
        })
        await fulfillment(of: [received], timeout: 8)
        owner.stop()
        let served = await serving.value
        XCTAssertEqual(served, 2)
        XCTAssertEqual(lines, ["data: first", "data: second"])
    }
}

final class SSELoopbackFixture: @unchecked Sendable {
    let port: UInt16
    private let listener: Int32
    private let onConnection: (@Sendable (Int) -> Void)?

    init(onConnection: (@Sendable (Int) -> Void)? = nil) throws {
        self.onConnection = onConnection
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw POSIXError(.ENFILE) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, listen(fd, 2) == 0 else {
            close(fd); throw POSIXError(.EADDRINUSE)
        }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &length) }
        }
        guard named == 0, fcntl(fd, F_SETFL, O_NONBLOCK) == 0 else {
            close(fd); throw POSIXError(.EIO)
        }
        listener = fd
        port = UInt16(bigEndian: address.sin_port)
    }

    /// Ограниченное время ожидания; закрываем исключительно собственные descriptors.
    func serveTwoStreams() -> Int {
        defer { close(listener) }
        let deadline = Date().addingTimeInterval(9)
        var count = 0
        while count < 2, Date() < deadline {
            let client = accept(listener, nil, nil)
            if client < 0 { usleep(10_000); continue }
            serve(client, body: count == 0 ? "data: first\n\n" : "data: second\n\n")
            count += 1
            onConnection?(count)
        }
        return count
    }

    private func serve(_ client: Int32, body: String) {
        defer { close(client) }
        var noSignal: Int32 = 1
        _ = setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        _ = setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var buffer = [UInt8](repeating: 0, count: 4096)
        var request = Data()
        while request.count < 16384 {
            let read = recv(client, &buffer, buffer.count, 0)
            guard read > 0 else { return }
            request.append(contentsOf: buffer.prefix(read))
            if request.range(of: Data("\r\n\r\n".utf8)) != nil { break }
        }
        let response = Data(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n" +
                             "Content-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n" + body).utf8)
        response.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let written = send(client, bytes.baseAddress!.advanced(by: offset), bytes.count - offset, 0)
                guard written > 0 else { return }
                offset += written
            }
        }
    }
}
