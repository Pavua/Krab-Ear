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

    override func tearDown() {
        UserDefaults.standard.removeObject(forKey: "KrabEar_ConversationBrainMode")
        super.tearDown()
    }

    func test_savedBrainMode_defaultsToAuto_whenUnset() {
        UserDefaults.standard.removeObject(forKey: "KrabEar_ConversationBrainMode")
        XCTAssertEqual(ConversationViewController.savedBrainMode, "auto")
    }

    func test_savedBrainMode_roundTrip() {
        ConversationViewController.saveBrainMode("krab")
        XCTAssertEqual(ConversationViewController.savedBrainMode, "krab")

        ConversationViewController.saveBrainMode("fast")
        XCTAssertEqual(ConversationViewController.savedBrainMode, "fast")
    }
}

// MARK: - Segment action

@MainActor
final class ConversationBrainModeSegmentActionTests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
    }

    override func tearDown() async throws {
        UserDefaults.standard.removeObject(forKey: "KrabEar_ConversationBrainMode")
        vc = nil
        try await super.tearDown()
    }

    func test_onBrainModeSegmentChanged_fast_updatesConfigAndPersists() {
        vc.brainModeControl.selectedSegment = 0
        vc.onBrainModeSegmentChanged()
        XCTAssertEqual(vc.config.brainMode, "fast")
        XCTAssertEqual(ConversationViewController.savedBrainMode, "fast")
    }

    func test_onBrainModeSegmentChanged_krab_updatesConfigAndPersists() {
        vc.brainModeControl.selectedSegment = 1
        vc.onBrainModeSegmentChanged()
        XCTAssertEqual(vc.config.brainMode, "krab")
        XCTAssertEqual(ConversationViewController.savedBrainMode, "krab")
    }

    func test_onBrainModeSegmentChanged_auto_updatesConfigAndPersists() {
        vc.brainModeControl.selectedSegment = 2
        vc.onBrainModeSegmentChanged()
        XCTAssertEqual(vc.config.brainMode, "auto")
        XCTAssertEqual(ConversationViewController.savedBrainMode, "auto")
    }
}

// MARK: - Set-default PUT request builder (DEBUG hook)

@MainActor
final class ConversationBrainModeSetDefaultRequestTests: XCTestCase {

    func test_buildSetDefaultRequest_methodAndURL() {
        var config = ConversationConfig.default
        config.httpBaseURLString = "http://127.0.0.1:8090"
        config.brainMode = "krab"
        let vc = ConversationViewController(config: config)

        let req = vc._buildSetDefaultRequest()
        XCTAssertNotNil(req)
        XCTAssertEqual(req?.httpMethod, "PUT")
        XCTAssertEqual(req?.url?.absoluteString, "http://127.0.0.1:8090/v1/settings/conversation")
    }

    func test_buildSetDefaultRequest_bodyContainsBrainMode() {
        var config = ConversationConfig.default
        config.brainMode = "auto"
        let vc = ConversationViewController(config: config)

        let req = vc._buildSetDefaultRequest()
        guard let body = req?.httpBody,
              let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any]
        else {
            return XCTFail("httpBody должен быть валидным JSON")
        }
        XCTAssertEqual(json["brain_mode"] as? String, "auto")
    }

    func test_buildSetDefaultRequest_setsContentTypeHeader() {
        let vc = ConversationViewController(config: .default)
        let req = vc._buildSetDefaultRequest()
        XCTAssertEqual(req?.value(forHTTPHeaderField: "Content-Type"), "application/json")
    }

    func test_buildSetDefaultRequest_invalidBaseURL_returnsNil() {
        var config = ConversationConfig.default
        config.httpBaseURLString = ""
        let vc = ConversationViewController(config: config)
        XCTAssertNil(vc._buildSetDefaultRequest())
    }
}
