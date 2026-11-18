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

    /// Чтение линейно по размеру ответа: 4 МБ обязаны прочитаться за доли секунды.
    /// Регрессия: поиск перевода строки по всему буферу после каждого read (4 МБ — 18 с).
    func test_readResponse_largeResponse_isLinear() throws {
        let (r, w) = try makePair()
        let size = 4 * 1024 * 1024
        let writerThread = Thread {
            var payload = [UInt8](repeating: 0x61, count: size)
            payload[size - 1] = 0x0A
            payload.withUnsafeBytes { raw in
                var offset = 0
                while offset < size {
                    let n = Darwin.write(w, raw.baseAddress! + offset, size - offset)
                    if n <= 0 { break }
                    offset += n
                }
            }
        }
        writerThread.start()

        let started = ContinuousClock.now
        let data = try IPCWire.readResponse(fd: r, timeoutSec: 30)
        let elapsed = ContinuousClock.now - started
        XCTAssertEqual(data.count, size)
        XCTAssertEqual(data.last, 0x0A)
        XCTAssertLessThan(elapsed, .seconds(3), "чтение 4 МБ заняло \(elapsed): цикл снова квадратичен")
    }

    /// Перевод строки в середине чанка: хвост чанка отбрасывается, а не дописывается.
    func test_readResponse_newlineInsideLaterChunk_dropsTail() throws {
        let (r, w) = try makePair()
        let head = [UInt8](repeating: 0x61, count: 5000)
        IPCWireTests.put(w, head + [0x0A] + Array("TAIL".utf8))
        let data = try IPCWire.readResponse(fd: r, timeoutSec: 2)
        XCTAssertEqual(data.count, head.count + 1)
        XCTAssertEqual(data.last, 0x0A)
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

    func test_parseEnvelope_rejectsForeignID() {
        // Чужой строковый id, id другого типа и id, искажённый редактором backend.
        for raw in ["\"other\"", "7", "\"[REDACTED]F9-x\""] {
            XCTAssertThrowsError(
                try IPCWire.parseEnvelope(Data("{\"id\":\(raw),\"ok\":true}\n".utf8), expectedID: "x"), raw
            ) { error in
                guard case IPCError.invalidResponse = error else { return XCTFail("\(error)") }
            }
        }
    }

    /// Backend отражает id через `safe_request_id`, и при сбое санитайзера отдаёт `null`
    /// для УЖЕ выполненного запроса (замер: текст `receipt: "незакрытая кавычка`).
    /// Такой ответ — успех, а не invalidResponse: иначе действие выполнено, а клиент видит отказ.
    func test_parseEnvelope_nullOrMissingID_isNotForeign() throws {
        let nullID = try IPCWire.parseEnvelope(Data("{\"id\":null,\"ok\":true,\"result\":{\"v\":1}}\n".utf8), expectedID: "x")
        XCTAssertEqual((nullID["result"] as? [String: Any])?["v"] as? Int, 1)
        _ = try IPCWire.parseEnvelope(Data("{\"ok\":true,\"result\":{}}\n".utf8), expectedID: "x")
        // invalid_json backend шлёт с id=null: сообщение об ошибке должно дойти.
        let raw = "{\"id\":null,\"ok\":false,\"error\":{\"code\":\"invalid_json\",\"message\":\"bad\"}}\n"
        XCTAssertThrowsError(try IPCWire.parseEnvelope(Data(raw.utf8), expectedID: "x")) { error in
            guard case IPCError.backendError(let message) = error else { return XCTFail("\(error)") }
            XCTAssertEqual(message, "bad")
        }
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

    /// Ревью ChatGPT (2026-11-16): `as? Bool` на NSNumber принимает 1/0 как true/false.
    func test_parseEnvelope_numericOK_isNotABoolean() {
        for raw in ["{\"id\":\"x\",\"ok\":1}\n", "{\"id\":\"x\",\"ok\":0}\n"] {
            XCTAssertThrowsError(try IPCWire.parseEnvelope(Data(raw.utf8), expectedID: "x"), raw) { error in
                guard case IPCError.invalidResponse = error else { return XCTFail("\(raw): \(error)") }
            }
        }
    }

    func test_strictBool_acceptsOnlyJSONBooleans() throws {
        let object = try JSONSerialization.jsonObject(
            with: Data("{\"t\":true,\"f\":false,\"one\":1,\"zero\":0,\"s\":\"true\"}".utf8)) as! [String: Any]
        XCTAssertEqual(IPCWire.strictBool(object["t"]), true)
        XCTAssertEqual(IPCWire.strictBool(object["f"]), false)
        XCTAssertNil(IPCWire.strictBool(object["one"]))
        XCTAssertNil(IPCWire.strictBool(object["zero"]))
        XCTAssertNil(IPCWire.strictBool(object["s"]))
        XCTAssertNil(IPCWire.strictBool(nil))
    }

    /// Ревью ChatGPT (M04/M05): продовый лимит и таймаут проверялись только с подставленными параметрами.
    func test_productionResponseCap_is32MiB() {
        XCTAssertEqual(IPCWire.maxResponseBytes, 32 * 1024 * 1024)
    }

    func test_readResponse_silentPeer_timesOutWithinDeadline() throws {
        let (r, _) = try makePair()
        let started = ContinuousClock.now
        XCTAssertThrowsError(try IPCWire.readResponse(fd: r, timeoutSec: 1)) { error in
            guard case IPCError.timeout = error else { return XCTFail("\(error)") }
        }
        XCTAssertLessThan(ContinuousClock.now - started, .seconds(3))
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
