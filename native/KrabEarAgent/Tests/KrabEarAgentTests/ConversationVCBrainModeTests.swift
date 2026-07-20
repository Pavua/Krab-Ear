/*
 ConversationVCBrainModeTests — тесты brain_mode тоггла (Волна 3b).

 Покрывает:
 1. UserDefaults round-trip (save/load), дефолт "auto" когда ключ не задан.
 2. onBrainModeSegmentChanged — обновляет config.brainMode + персистит.
 3. _buildSetDefaultRequest — PUT-запрос к VG settings API (DEBUG hook).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - UserDefaults persistence

final class ConversationBrainModePersistenceTests: XCTestCase {

    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "ConversationBrainModePersistenceTests")

    override func tearDown() {
        defaultsDomain.removePersistentDomain()
        super.tearDown()
    }

    func test_savedBrainMode_defaultsToAuto_whenUnset() {
        XCTAssertEqual(
            ConversationViewController.savedBrainMode(in: defaultsDomain.defaults),
            "auto"
        )
    }

    func test_savedBrainMode_roundTrip() {
        ConversationViewController.saveBrainMode("krab", in: defaultsDomain.defaults)
        XCTAssertEqual(
            ConversationViewController.savedBrainMode(in: defaultsDomain.defaults),
            "krab"
        )

        ConversationViewController.saveBrainMode("fast", in: defaultsDomain.defaults)
        XCTAssertEqual(
            ConversationViewController.savedBrainMode(in: defaultsDomain.defaults),
            "fast"
        )
    }
}

// MARK: - Segment action

@MainActor
final class ConversationBrainModeSegmentActionTests: XCTestCase {

    private var vc: ConversationViewController!
    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "ConversationBrainModeSegmentActionTests")

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default, userDefaults: defaultsDomain.defaults)
        vc.loadView()
        vc.viewDidLoad()
    }

    override func tearDown() async throws {
        vc = nil
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    func test_onBrainModeSegmentChanged_fast_updatesConfigAndPersists() {
        vc.brainModeControl.selectedSegment = 0
        vc.onBrainModeSegmentChanged()
        XCTAssertEqual(vc.config.brainMode, "fast")
        XCTAssertEqual(
            ConversationViewController.savedBrainMode(in: defaultsDomain.defaults),
            "fast"
        )
    }

    func test_onBrainModeSegmentChanged_krab_updatesConfigAndPersists() {
        vc.brainModeControl.selectedSegment = 1
        vc.onBrainModeSegmentChanged()
        XCTAssertEqual(vc.config.brainMode, "krab")
        XCTAssertEqual(
            ConversationViewController.savedBrainMode(in: defaultsDomain.defaults),
            "krab"
        )
    }

    func test_onBrainModeSegmentChanged_auto_updatesConfigAndPersists() {
        vc.brainModeControl.selectedSegment = 2
        vc.onBrainModeSegmentChanged()
        XCTAssertEqual(vc.config.brainMode, "auto")
        XCTAssertEqual(
            ConversationViewController.savedBrainMode(in: defaultsDomain.defaults),
            "auto"
        )
    }
}

// MARK: - Set-default PUT request builder (DEBUG hook)

@MainActor
final class ConversationBrainModeSetDefaultRequestTests: XCTestCase {

    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "ConversationBrainModeSetDefaultRequestTests")

    override func tearDown() async throws {
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    private func makeViewController(config: ConversationConfig = .default) -> ConversationViewController {
        ConversationViewController(config: config, userDefaults: defaultsDomain.defaults)
    }

    func test_buildSetDefaultRequest_methodAndURL() {
        var config = ConversationConfig.default
        config.httpBaseURLString = "http://127.0.0.1:8090"
        config.brainMode = "krab"
        let vc = makeViewController(config: config)

        let req = vc._buildSetDefaultRequest()
        XCTAssertNotNil(req)
        XCTAssertEqual(req?.httpMethod, "PUT")
        XCTAssertEqual(req?.url?.absoluteString, "http://127.0.0.1:8090/v1/settings/conversation")
    }

    func test_buildSetDefaultRequest_bodyContainsBrainMode() {
        var config = ConversationConfig.default
        config.brainMode = "auto"
        let vc = makeViewController(config: config)

        let req = vc._buildSetDefaultRequest()
        guard let body = req?.httpBody,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
        else {
            return XCTFail("httpBody должен быть валидным JSON")
        }
        XCTAssertEqual(json["brain_mode"] as? String, "auto")
    }

    func test_buildSetDefaultRequest_setsContentTypeHeader() {
        let vc = makeViewController()
        let req = vc._buildSetDefaultRequest()
        XCTAssertEqual(req?.value(forHTTPHeaderField: "Content-Type"), "application/json")
    }

    func test_buildSetDefaultRequest_invalidBaseURL_returnsNil() {
        var config = ConversationConfig.default
        config.httpBaseURLString = ""
        let vc = makeViewController(config: config)
        XCTAssertNil(vc._buildSetDefaultRequest())
    }

    // MARK: Authorization header (живой e2e 2026-07-08 обнаружил: VG требует Bearer-токен
    // на /v1/settings/conversation так же, как на WS-эндпоинте conversation; без этого
    // заголовка запрос падает с 401 missing_auth_token на любом VG с настроенным api_key).

    func test_buildSetDefaultRequest_withApiKey_setsAuthorizationHeader() {
        var config = ConversationConfig.default
        config.apiKey = "tok-secret"
        let vc = makeViewController(config: config)
        let req = vc._buildSetDefaultRequest()
        XCTAssertEqual(req?.value(forHTTPHeaderField: "Authorization"), "Bearer tok-secret")
    }

    func test_buildSetDefaultRequest_emptyApiKey_noAuthorizationHeader() {
        var config = ConversationConfig.default
        config.apiKey = ""
        let vc = makeViewController(config: config)
        let req = vc._buildSetDefaultRequest()
        XCTAssertNil(req?.value(forHTTPHeaderField: "Authorization"),
                     "Пустой apiKey не должен добавлять заголовок Authorization")
    }
}
