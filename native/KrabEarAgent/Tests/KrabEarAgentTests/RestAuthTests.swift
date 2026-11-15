/*
 RestAuthTests — Bearer-ключ локального REST (решение владельца 2026-11-15).

 1. Ключ из файла ставится в Authorization.
 2. Env KRAB_EAR_REST_API_KEY старше файла.
 3. Короткий/пустой ключ и отсутствующий файл заголовка не ставят.
 4. KRAB_EAR_REST_API_KEY_FILE переопределяет путь.
 5. Source contract: каждый Swift-файл, строящий запрос к /v1/events, вызывает
    RestAuth.apply, иначе подписка молча получит 401 после включения auth.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

final class RestAuthTests: XCTestCase {
    private let goodKey = String(repeating: "k", count: 43)

    private func tempFile(_ content: String) throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("restauth-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("rest_api_key")
        try content.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    private func request() -> URLRequest {
        URLRequest(url: URL(string: "http://127.0.0.1:5005/v1/events")!)
    }

    func test_keyFromFile_isAppliedAsBearer() throws {
        let file = try tempFile(goodKey + "\n")
        var req = request()

        RestAuth.apply(to: &req, environment: [:], defaultFile: file)

        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer \(goodKey)")
    }

    func test_envKey_winsOverFile() throws {
        let file = try tempFile(goodKey)
        let envKey = String(repeating: "e", count: 40)
        var req = request()

        RestAuth.apply(to: &req, environment: ["KRAB_EAR_REST_API_KEY": envKey], defaultFile: file)

        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer \(envKey)")
    }

    func test_shortKey_isIgnored() throws {
        let file = try tempFile("short")
        var req = request()

        RestAuth.apply(to: &req, environment: [:], defaultFile: file)

        XCTAssertNil(req.value(forHTTPHeaderField: "Authorization"))
    }

    func test_missingFile_leavesRequestWithoutHeader() {
        var req = request()
        let missing = FileManager.default.temporaryDirectory
            .appendingPathComponent("no-such-\(UUID().uuidString)")

        RestAuth.apply(to: &req, environment: [:], defaultFile: missing)

        XCTAssertNil(req.value(forHTTPHeaderField: "Authorization"))
    }

    func test_fileOverrideEnv_replacesDefaultPath() throws {
        let override = try tempFile(goodKey)
        let decoy = try tempFile(String(repeating: "d", count: 40))

        let token = RestAuth.bearerToken(
            environment: ["KRAB_EAR_REST_API_KEY_FILE": override.path],
            defaultFile: decoy
        )

        XCTAssertEqual(token, goodKey)
    }

    func test_everySourceThatBuildsEventsRequestAppliesRestAuth() throws {
        let sources = Self.sourcesDir
        let files = try FileManager.default.contentsOfDirectory(at: sources, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" && $0.lastPathComponent != "RestAuth.swift" }
        var offenders: [String] = []
        for file in files {
            let text = try String(contentsOf: file, encoding: .utf8)
            // Реальное построение URL запроса, а не упоминание в комментарии.
            let buildsRequest = text.contains("URLRequest(url: url")
                && text.contains("/v1/events?filter")
            if buildsRequest && !text.contains("RestAuth.apply(to: &request)") {
                offenders.append(file.lastPathComponent)
            }
        }
        XCTAssertEqual(offenders, [], "SSE /v1/events без RestAuth.apply получит 401: \(offenders)")
    }

    private static var sourcesDir: URL {
        URL(fileURLWithPath: #file)
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent", isDirectory: true)
    }
}
