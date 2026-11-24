/*
 VoiceGatewayConnectionTests — карточка «Подключение к Voice Gateway» (24.11.2026).

 Покрытие:
   - VoiceGatewayCheck: «ключ принят» только по ответу маршрута с авторизацией, не по /health;
   - карточка: пустое поле ключ не стирает, Enter/потеря фокуса сохраняют, поле очищается,
     secure-поле, статус ключа словами, раскладка на узкой ширине без сжатия;
   - контроллер: сохранённый ключ/адрес СРАЗУ применяются к конфигу разговора (регресс:
     раньше только после перезапуска агента);
   - сетевая проверка (URLProtocol-заглушка): Bearer уходит только на auth-маршрут,
     ключ не попадает в текст «Подробностей».
 В тестах только заведомо тестовые значения ключа.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

/// Заглушка сети: /health → 200, /v1/settings/conversation → 200 c верным ключом, иначе 401.
final class VGStubProtocol: URLProtocol {
    /// Журнал запросов: путь → заголовки Authorization по порядку.
    nonisolated(unsafe) static var seenAuthHeaders: [String: [String?]] = [:]
    nonisolated(unsafe) static var acceptedKey = "test-key-ok"
    nonisolated(unsafe) static var healthStatus = 200

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url?.path ?? ""
        let auth = request.value(forHTTPHeaderField: "Authorization")
        Self.seenAuthHeaders[path, default: []].append(auth)
        let code: Int
        if path == "/health" { code = Self.healthStatus }
        else if path == "/v1/settings/conversation" { code = (auth == "Bearer \(Self.acceptedKey)") ? 200 : 401 }
        else { code = 404 }
        let resp = HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"status":"ok"}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@MainActor
final class VoiceGatewayConnectionTests: XCTestCase {

    // MARK: - Логика итога

    func test_check_keyAcceptedOnlyAfterAuthRoute() {
        typealias C = VoiceGatewayCheck
        // /health ок, но маршрут с авторизацией не опрошен → ключ НЕ «принят».
        XCTAssertEqual(C.evaluate(healthStatus: 200, healthError: nil, authStatus: nil, keyProvided: true).key, .unknown)
        XCTAssertEqual(C.evaluate(healthStatus: 200, healthError: nil, authStatus: 200, keyProvided: true).key, .accepted)
        XCTAssertEqual(C.evaluate(healthStatus: 200, healthError: nil, authStatus: 401, keyProvided: true).key, .rejected)
        XCTAssertEqual(C.evaluate(healthStatus: 200, healthError: nil, authStatus: 401, keyProvided: false).key, .required)
        XCTAssertEqual(C.evaluate(healthStatus: 200, healthError: nil, authStatus: 200, keyProvided: false).key, .notRequired)
        let down = C.evaluate(healthStatus: nil, healthError: "отказано в соединении", authStatus: nil, keyProvided: true)
        XCTAssertEqual(down.address, .unreachable("отказано в соединении"))
        XCTAssertFalse(down.isReady)
        XCTAssertTrue(C.evaluate(healthStatus: 200, healthError: nil, authStatus: 200, keyProvided: true).isReady)
        XCTAssertFalse(C.evaluate(healthStatus: 200, healthError: nil, authStatus: 401, keyProvided: false).isReady)
    }

    func test_check_authOffGateway_isNotVerified_notAccepted() {
        // Гейт Opus: шлюз с выключенной авторизацией отвечает 200 и с ключом — это не «принят».
        let c = VoiceGatewayCheck.evaluate(healthStatus: 200, healthError: nil, authStatus: 200,
                                           keyProvided: true, authStatusWithoutKey: 200)
        XCTAssertEqual(c.key, .notVerified)
        XCTAssertTrue(c.isReady)
        XCTAssertEqual(VoiceGatewayCheck.evaluate(healthStatus: 200, healthError: nil, authStatus: 200,
                                                  keyProvided: true, authStatusWithoutKey: 401).key, .accepted)
    }

    func test_card_deleteKeyButton_onlyWhenSaved_andCallsHandler() {
        let view = VoiceGatewayConnectionView()
        var deleted = 0
        view.onDeleteKey = { deleted += 1 }
        view.apply(keyStatus: .missing)
        XCTAssertTrue(view.deleteKeyButton.isHidden)
        view.apply(keyStatus: .saved)
        XCTAssertFalse(view.deleteKeyButton.isHidden)
        view.deleteKeyButton.performClick(nil)
        XCTAssertEqual(deleted, 1)
    }

    func test_controller_deleteKey_clearsConversationKey() {
        let controller = makeController()
        guard let vc = controller.conversationVC else { return XCTFail("нет разговора") }
        controller.commitVoiceGatewayKey("test-key-ok")
        controller.commitVoiceGatewayKey("")
        XCTAssertEqual(vc.config.apiKey, "", "явное удаление очищает ключ разговора")
        XCTAssertEqual(controller.voiceGatewayConnectionView.keyStatus, .missing)
    }

    func test_check_linesAreWords_notJSON() {
        let c = VoiceGatewayCheck.evaluate(healthStatus: 200, healthError: nil, authStatus: 401, keyProvided: false)
        XCTAssertEqual(c.addressLine, "Адрес: шлюз доступен")
        XCTAssertEqual(c.keyLine, "Ключ: нужен — шлюз отвечает 401 без ключа")
        XCTAssertEqual(c.summaryLine, "Итог: разговор с AI работать не будет")
    }

    // MARK: - Карточка

    private func commitViaEndEditing(_ view: VoiceGatewayConnectionView, field: NSTextField) {
        view.controlTextDidEndEditing(Notification(name: NSControl.textDidEndEditingNotification, object: field))
    }

    func test_card_emptyKeyDoesNotErase_nonEmptyCommitsAndClears() {
        let view = VoiceGatewayConnectionView()
        var committed: [String] = []
        view.onCommitKey = { committed.append($0) }
        view.keyField.stringValue = "   "
        commitViaEndEditing(view, field: view.keyField)
        XCTAssertTrue(committed.isEmpty, "пустое поле = «не менять», ключ не стирается")
        view.keyField.stringValue = "  test-key-ok  "
        commitViaEndEditing(view, field: view.keyField)
        XCTAssertEqual(committed, ["test-key-ok"], "сохранение по потере фокуса, без пробелов")
        XCTAssertEqual(view.keyField.stringValue, "", "введённый ключ не остаётся на экране")
        XCTAssertTrue(view.keyField is NSSecureTextField)
    }

    func test_card_urlCommitsOncePerChange() {
        let view = VoiceGatewayConnectionView()
        var urls: [String] = []
        view.onCommitURL = { urls.append($0) }
        view.setURL("http://127.0.0.1:8090")
        commitViaEndEditing(view, field: view.urlField)
        XCTAssertTrue(urls.isEmpty, "без изменений — без сохранения")
        view.urlField.stringValue = "http://10.0.0.2:8090"
        commitViaEndEditing(view, field: view.urlField)
        commitViaEndEditing(view, field: view.urlField)
        XCTAssertEqual(urls, ["http://10.0.0.2:8090"])
    }

    func test_card_keyStatusInWords() {
        let view = VoiceGatewayConnectionView()
        view.apply(keyStatus: .saved)
        XCTAssertTrue(view.keyStatusText.hasPrefix("Ключ сохранён"))
        view.apply(keyStatus: .missing)
        XCTAssertTrue(view.keyStatusText.hasPrefix("Ключ не задан"))
    }

    func test_card_layoutNarrow_fieldsFullWidth_noCrushedLabels() {
        let view = VoiceGatewayConnectionView()
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 520, height: 600))
        root.addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: root.topAnchor),
            view.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            view.widthAnchor.constraint(equalToConstant: 520),
        ])
        root.layoutSubtreeIfNeeded()
        let url = view.urlField.convert(view.urlField.bounds, to: root)
        XCTAssertGreaterThan(url.width, 450, "поле адреса во всю ширину: \(url.width)")
        let key = view.keyField.convert(view.keyField.bounds, to: root)
        XCTAssertGreaterThanOrEqual(key.width, 160)
        let paste = view.pasteButton.convert(view.pasteButton.bounds, to: root)
        XCTAssertLessThanOrEqual(paste.maxX, 520)
        XCTAssertGreaterThanOrEqual(paste.width, view.pasteButton.intrinsicContentSize.width - 1, "кнопка не сжата")
        XCTAssertFalse(root.hasAmbiguousLayout)
    }

    // MARK: - Контроллер: ключ сразу применяется к разговору

    private func makeController() -> HistoryPanelController {
        let ipc = IPCClient(socketPath: "/nonexistent-krab-ear-test.sock")
        return HistoryPanelController(
            ipcClient: ipc,
            plaintextExportCoordinator: PlaintextExportCoordinator(ipc: ipc),
            settingsProvider: { .default },
            settingsUpdater: { _ in .default },
            onToggleRecording: {}, onRestartAgent: {}, onStopAgent: {},
            onPasteHistoryItem: { _ in }, onSwapRuEsDirection: {}
        )
    }

    func test_controller_keyAndURLApplyToConversationImmediately() {
        let controller = makeController()
        guard let vc = controller.conversationVC else { return XCTFail("нет разговора") }
        controller.commitVoiceGatewayKey("test-key-ok")
        XCTAssertEqual(vc.config.apiKey, "test-key-ok", "ключ обязан попасть в разговор без перезапуска")
        XCTAssertEqual(controller.voiceGatewayConnectionView.keyStatus, .saved)
        controller.commitVoiceGatewayURL("http://10.0.0.2:8090/")
        XCTAssertTrue(vc.config.wsURLString.hasPrefix("ws://10.0.0.2:8090/v1/sessions/vs_"), vc.config.wsURLString)
        XCTAssertEqual(vc.config.httpBaseURLString, "http://10.0.0.2:8090")
    }

    /// Регресс: старый путь (Enter в поле ключа) тоже обязан обновлять разговор.
    func test_controller_legacyEnterPath_appliesKeyToConversation() {
        let controller = makeController()
        guard let vc = controller.conversationVC else { return XCTFail("нет разговора") }
        controller.voiceGatewayAPIKeyField.stringValue = "test-key-legacy"
        controller.onVoiceGatewayAPIKeyChanged()
        XCTAssertEqual(vc.config.apiKey, "test-key-legacy")
    }

    func test_renderPNG_whenRequested() throws {
        guard let dir = ProcessInfo.processInfo.environment["KRAB_RENDER_DIR"] else { throw XCTSkip("KRAB_RENDER_DIR не задан") }
        for (name, ap) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            let view = VoiceGatewayConnectionView()
            view.setURL("http://127.0.0.1:8090")
            view.apply(keyStatus: .missing)
            view.apply(check: VoiceGatewayCheck.evaluate(healthStatus: 200, healthError: nil, authStatus: 401, keyProvided: false), hasDetails: true)
            let root = NSView(frame: NSRect(x: 0, y: 0, width: 683, height: 420))
            root.appearance = NSAppearance(named: ap)
            root.wantsLayer = true
            root.addSubview(view)
            NSLayoutConstraint.activate([
                view.topAnchor.constraint(equalTo: root.topAnchor, constant: 16),
                view.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
                view.widthAnchor.constraint(equalToConstant: 651),
            ])
            root.appearance?.performAsCurrentDrawingAppearance { root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor }
            root.layoutSubtreeIfNeeded()
            guard let rep = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { continue }
            root.cacheDisplay(in: root.bounds, to: rep)
            try rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: dir).appendingPathComponent("vg_card_\(name).png"))
        }
    }

    func test_controller_cardIsInConversationTab_andOldRowHidden() {
        let controller = makeController()
        XCTAssertTrue(controller.voiceGatewayRow.isHidden, "старая строка на «Live перевод» скрыта")
        let item = controller.mainTabView.tabViewItems.first {
            ($0.identifier as? String) == HistoryPanelController.PanelTab.conversation.rawValue
        }
        XCTAssertTrue(controller.voiceGatewayConnectionView.isDescendant(of: item!.view!))
    }

    // MARK: - Сетевая проверка

    private func stubConfig() -> URLSessionConfiguration {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [VGStubProtocol.self]
        return c
    }

    func test_probe_bearerOnlyToAuthRoute_andKeyNotInDetails() async {
        VGStubProtocol.seenAuthHeaders = [:]
        VGStubProtocol.healthStatus = 200
        let ok = await HistoryPanelController.probeVoiceGateway(base: "http://vg.test", apiKey: "test-key-ok", configuration: stubConfig())
        XCTAssertEqual(ok.check.key, .accepted)
        XCTAssertEqual(VGStubProtocol.seenAuthHeaders["/health"] ?? [], [nil], "/health — без ключа")
        // Первый — с ключом; второй контрольный (после 200) — без ключа.
        XCTAssertEqual(VGStubProtocol.seenAuthHeaders["/v1/settings/conversation"] ?? [], ["Bearer test-key-ok", nil])
        XCTAssertFalse(ok.details.contains("test-key-ok"), "ключ не должен попадать в «Подробности»")

        let bad = await HistoryPanelController.probeVoiceGateway(base: "http://vg.test", apiKey: "test-key-wrong", configuration: stubConfig())
        XCTAssertEqual(bad.check.key, .rejected)
        let none = await HistoryPanelController.probeVoiceGateway(base: "http://vg.test", apiKey: "", configuration: stubConfig())
        XCTAssertEqual(none.check.key, .required)
    }

    func test_probe_healthDown_meansKeyUnknown() async {
        VGStubProtocol.healthStatus = 503
        defer { VGStubProtocol.healthStatus = 200 }
        let r = await HistoryPanelController.probeVoiceGateway(base: "http://vg.test", apiKey: "test-key-ok", configuration: stubConfig())
        XCTAssertEqual(r.check.address, .badResponse(503))
        XCTAssertEqual(r.check.key, .unknown)
    }
}
