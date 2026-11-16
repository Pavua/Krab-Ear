/*
 IPCWireTests — E4 п.7: чтение ответа backend ограничено общим дедлайном и размером,
 а конверт разбирается строго.

 Раньше:
 - SO_RCVTIMEO действует на КАЖДЫЙ read, поэтому ответ «по байту раз в timeout-1 секунд»
   держал вызов бесконечно (общего дедлайна не было);
 - размер ответа не ограничивался (в память читалось всё до перевода строки/EOF);
 - ответ без булева `ok` или с чужим `id` принимался как успех.
*/

import XCTest
@testable import KrabEarAgent

final class IPCWireTests: XCTestCase {

    // MARK: - helpers

    private func makePair() throws -> (reader: Int32, writer: Int32) {
        var fds: [Int32] = [0, 0]
        XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &fds), 0)
        let (reader, writer) = (fds[0], fds[1])
        addTeardownBlock { close(reader); close(writer) }
        return (reader, writer)
    }

    private static func put(_ fd: Int32, _ bytes: [UInt8]) {
        _ = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
    }

    // MARK: - readResponse

    func test_readResponse_returnsOnlyFirstLine() throws {
        let (r, w) = try makePair()
        IPCWireTests.put(w, Array("{\"a\":1}\nTRAILING".utf8))
        let data = try IPCWire.readResponse(fd: r, timeoutSec: 2)
        XCTAssertEqual(String(decoding: data, as: UTF8.self), "{\"a\":1}\n")
    }

    func test_readResponse_eofWithoutNewline_isInvalid() throws {
        let (r, w) = try makePair()
        IPCWireTests.put(w, Array("{\"a\":1}".utf8))
        close(w)
        XCTAssertThrowsError(try IPCWire.readResponse(fd: r, timeoutSec: 2)) { error in
            guard case IPCError.invalidResponse = error else { return XCTFail("\(error)") }
        }
    }

    func test_readResponse_oversize_isRejected() throws {
        let (r, w) = try makePair()
        IPCWireTests.put(w, [UInt8](repeating: 0x41, count: 4096)) // без перевода строки
        XCTAssertThrowsError(try IPCWire.readResponse(fd: r, timeoutSec: 2, maxBytes: 1024)) { error in
            guard case IPCError.invalidResponse = error else { return XCTFail("\(error)") }
        }
    }

    /// Дедлайн общий: по байту раз в 0.3с при таймауте 1с обязан оборваться ~за 1с.
    func test_readResponse_trickle_hitsTotalDeadline() throws {
        let (r, w) = try makePair()
        let stop = ManagedAtomicFlag()
        let writerThread = Thread {
            while !stop.isSet {
                IPCWireTests.put(w, [0x41])
                Thread.sleep(forTimeInterval: 0.3)
            }
        }
        writerThread.start()
        addTeardownBlock { stop.set() }

        let started = ContinuousClock.now
        let done = expectation(description: "readResponse завершился")
        nonisolated(unsafe) var thrown: Error?
        DispatchQueue.global().async {
            do { _ = try IPCWire.readResponse(fd: r, timeoutSec: 1) } catch { thrown = error }
            done.fulfill()
        }
        wait(for: [done], timeout: 5)
        stop.set()
        let elapsed = ContinuousClock.now - started
        guard case IPCError.timeout? = thrown else { return XCTFail("ожидался .timeout, получено \(String(describing: thrown))") }
        XCTAssertLessThan(elapsed, .seconds(3), "общий дедлайн ~1с, не «1с на каждый read»")
    }

    // MARK: - parseEnvelope

    func test_parseEnvelope_requiresBoolOK() {
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data("{\"id\":\"x\",\"result\":{}}\n".utf8), expectedID: "x"))
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data("{\"id\":\"x\",\"ok\":\"yes\"}\n".utf8), expectedID: "x"))
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data("[1,2]\n".utf8), expectedID: "x"))
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data("not json\n".utf8), expectedID: "x"))
    }

    func test_parseEnvelope_rejectsForeignOrMissingID() {
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data("{\"id\":\"other\",\"ok\":true}\n".utf8), expectedID: "x")) { error in
            guard case IPCError.invalidResponse = error else { return XCTFail("\(error)") }
        }
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data("{\"ok\":true}\n".utf8), expectedID: "x"))
    }

    func test_parseEnvelope_okTrue_returnsEnvelope() throws {
        let json = try IPCWire.parseEnvelope(Data("{\"id\":\"x\",\"ok\":true,\"result\":{\"v\":1}}\n".utf8), expectedID: "x")
        XCTAssertEqual((json["result"] as? [String: Any])?["v"] as? Int, 1)
    }

    func test_parseEnvelope_okFalse_throwsBackendErrorWithMessage() {
        let raw = "{\"id\":\"x\",\"ok\":false,\"error\":{\"code\":\"c\",\"message\":\"boom\"}}\n"
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data(raw.utf8), expectedID: "x")) { error in
            guard case IPCError.backendError(let message) = error else { return XCTFail("\(error)") }
            XCTAssertEqual(message, "boom")
        }
    }

    func test_parseEnvelope_nilExpectedID_skipsIDCheck() throws {
        // Тестовый seam socketProvider: моки не знают случайный id запроса.
        _ = try IPCWire.parseEnvelope(Data("{\"ok\":true}\n".utf8), expectedID: nil)
    }
}

/// Минимальный потокобезопасный флаг для остановки writer-потока теста.
private final class ManagedAtomicFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
    func set() { lock.lock(); value = true; lock.unlock() }
}
