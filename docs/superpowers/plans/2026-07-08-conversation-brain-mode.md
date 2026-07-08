# Волна 3b — brain_mode тоггл для «Разговора с AI» (Krab Ear/Swift) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в Conversation-таб Krab Ear сегмент-тоггл «Быстро / Краб / Авто», который
явно передаёт `brain_mode` в WS-запрос к Voice Gateway на каждый старт разговора, персистит
выбор между запусками, и даёт кнопку «Сделать дефолтом» для установки серверного дефолта VG.

**Architecture:** `ConversationConfig` (Models.swift) получает новое поле `brainMode` (default
`"auto"`) + `httpBaseURLString` (HTTP-база VG, отдельно от WS-URL). Query-параметр `brain_mode`
добавляется в WS-запрос ВСЕГДА (в отличие от `engine`/`brain`/`lang`, которые опускаются при
значении `"auto"`) — контракт из брифа для VG требует явной передачи. Персистентность —
`UserDefaults`. HTTP PUT для «сделать дефолтом» — отдельный маленький файл-расширение с
DEBUG-testable билдером запроса (по прецеденту `_buildWSRequest`).

**Tech Stack:** Swift 6, AppKit (NSSegmentedControl, NSTextField), URLSession async/await, XCTest.

**Referenced spec:** `docs/superpowers/specs/2026-07-08-conversation-brain-mode-design.md` §5
(раздел, покрываемый этим планом). VG/Main-Krab части спеки — вне этого плана (внешние сессии).

---

### Task 1: `ConversationConfig` — новые поля `brainMode` + `httpBaseURLString`

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/Models.swift:19-42`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/ModelsTests.swift`

- [ ] **Step 1: Write the failing test**

Добавить в `ModelsTests.swift` после существующего `test_conversationConfig_customInit()`
(после строки 39):

```swift
    func test_conversationConfig_defaultValues_includesBrainModeAndHttpBase() {
        let config = ConversationConfig.default
        XCTAssertEqual(config.brainMode, "auto")
        XCTAssertEqual(config.httpBaseURLString, "http://127.0.0.1:8090")
    }

    func test_conversationConfig_customInit_brainModeAndHttpBase() {
        var config = ConversationConfig.default
        config.brainMode = "krab"
        config.httpBaseURLString = "http://127.0.0.1:9090"
        XCTAssertEqual(config.brainMode, "krab")
        XCTAssertEqual(config.httpBaseURLString, "http://127.0.0.1:9090")
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd native/KrabEarAgent && swift test --filter ModelsTests`
Expected: FAIL — `value of type 'ConversationConfig' has no member 'brainMode'`

- [ ] **Step 3: Implement**

В `Models.swift` заменить строки 19-42 (весь `struct ConversationConfig` целиком):

```swift
struct ConversationConfig {
    /// WS endpoint Voice Gateway. Placeholder — реальный GW подключается в PR 1.1.
    var wsURLString: String

    /// API-ключ Voice Gateway (может быть пустой для локального режима).
    var apiKey: String

    /// Языковой хинт для STT: "auto" | "ru" | "en" | "es".
    var languageHint: String

    /// Движок AI: "auto" | "moshi" | "seamless".
    var engine: String

    /// LLM-мозг (конкретная модель): "auto" | "qwen3-4b" | "llama-3.2-3b".
    var brain: String

    /// Режим приоритета мозга (Волна 3b): "fast" | "krab" | "auto".
    /// В отличие от `brain`, всегда передаётся в WS query-param `brain_mode`
    /// (даже при значении "auto") — контракт с Voice Gateway требует явности.
    var brainMode: String = "auto"

    /// HTTP-база Voice Gateway (без ws-схемы), например "http://127.0.0.1:8090".
    /// Используется для не-WS запросов (напр. PUT /v1/settings/conversation).
    var httpBaseURLString: String = "http://127.0.0.1:8090"

    static let `default` = ConversationConfig(
        wsURLString:  "ws://127.0.0.1:8090/v1/conversation",
        apiKey:       "",
        languageHint: "auto",
        engine:       "auto",
        brain:        "auto"
    )
}
```

Поля `brainMode`/`httpBaseURLString` со значением по умолчанию НЕ ломают существующие
call sites (`ConversationConfig(...)` без этих двух именованных аргументов) — синтезированный
memberwise-инициализатор Swift берёт property-default как default параметра (SE-0242).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd native/KrabEarAgent && swift test --filter ModelsTests`
Expected: PASS, все существующие `ConversationConfig(...)` call sites компилируются без правок.

- [ ] **Step 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/Models.swift native/KrabEarAgent/Tests/KrabEarAgentTests/ModelsTests.swift
git commit -m "feat(conversation): add brainMode + httpBaseURLString to ConversationConfig"
```

---

### Task 2: WS-запрос всегда несёт `brain_mode` query-параметр

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+WebSocket.swift:54-66` (production) и `:192-201` (DEBUG hook)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/ConversationVCWebSocketTests.swift`

- [ ] **Step 1: Write the failing test**

Добавить в `ConversationVCURLBuildingTests` (после `test_buildWSRequest_allNonAuto_allParamsPresent`,
после строки 279):

```swift
    // MARK: brain_mode — ВСЕГДА присутствует (в отличие от engine/brain/lang)

    func test_buildWSRequest_brainModeAuto_stillIncludesParam() {
        var config = ConversationConfig.default
        config.brainMode = "auto"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let brainModeParam = items.first(where: { $0.name == "brain_mode" })
        XCTAssertEqual(brainModeParam?.value, "auto",
                       "brain_mode должен передаваться явно, даже если равен auto")
    }

    func test_buildWSRequest_brainModeKrab_includesParam() {
        var config = ConversationConfig.default
        config.brainMode = "krab"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let brainModeParam = items.first(where: { $0.name == "brain_mode" })
        XCTAssertEqual(brainModeParam?.value, "krab")
    }

    func test_buildWSRequest_brainModeFast_includesParam() {
        var config = ConversationConfig.default
        config.brainMode = "fast"
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)!
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let brainModeParam = items.first(where: { $0.name == "brain_mode" })
        XCTAssertEqual(brainModeParam?.value, "fast")
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd native/KrabEarAgent && swift test --filter ConversationVCURLBuildingTests`
Expected: FAIL — `brainModeParam` is nil (параметр ещё не добавляется).

- [ ] **Step 3: Implement**

В `ConversationViewController+WebSocket.swift`, метод `startWebSocketSession()` (строки 54-66),
заменить блок построения query-параметров:

```swift
        // Указываем движок и мозг через query params (spec-compatible).
        if var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            var items = components.queryItems ?? []
            if config.engine != "auto" { items.append(URLQueryItem(name: "engine", value: config.engine)) }
            if config.brain  != "auto" { items.append(URLQueryItem(name: "brain",  value: config.brain))  }
            if config.languageHint != "auto" { items.append(URLQueryItem(name: "lang", value: config.languageHint)) }
            // brain_mode (Волна 3b) — ВСЕГДА передаём явно, даже "auto":
            // Voice Gateway полагается на явный сигнал от клиента, не на умолчание сервера.
            items.append(URLQueryItem(name: "brain_mode", value: config.brainMode))
            if !items.isEmpty {
                components.queryItems = items
                if let newURL = components.url {
                    request.url = newURL
                }
            }
        }
```

И симметрично в DEBUG-хуке `_buildWSRequest` (строки 192-201):

```swift
        if var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            var items = components.queryItems ?? []
            if config.engine != "auto" { items.append(URLQueryItem(name: "engine", value: config.engine)) }
            if config.brain  != "auto" { items.append(URLQueryItem(name: "brain",  value: config.brain))  }
            if config.languageHint != "auto" { items.append(URLQueryItem(name: "lang", value: config.languageHint)) }
            items.append(URLQueryItem(name: "brain_mode", value: config.brainMode))
            if !items.isEmpty {
                components.queryItems = items
                if let newURL = components.url { request.url = newURL }
            }
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd native/KrabEarAgent && swift test --filter ConversationVCWebSocketTests`
Expected: PASS — все тесты файла зелёные, включая новые 3 + существующие
(`test_buildWSRequest_autoValues_noQueryParams` теперь имеет 1 параметр `brain_mode=auto`, но
сам тест проверяет `queryItems?.first` через `XCTAssertNil` — это СЛОМАЕТСЯ, см. Step 3.5 ниже).

- [ ] **Step 3.5: Исправить существующий тест `test_buildWSRequest_autoValues_noQueryParams`**

Этот тест (строка 218) утверждал «при auto-значениях query params не должны добавляться» — это
больше не так из-за `brain_mode`. Заменить тело теста (строки 218-232):

```swift
    func test_buildWSRequest_autoValues_onlyBrainModeParam() {
        let config = ConversationConfig(
            wsURLString: "ws://localhost:8090/v1/conversation",
            apiKey: "",
            languageHint: "auto",
            engine: "auto",
            brain: "auto"
        )
        let vc = makeVC(config: config)
        let req = vc._buildWSRequest(for: config.wsURLString)
        XCTAssertNotNil(req)
        let items = URLComponents(url: req!.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        XCTAssertEqual(items.count, 1,
                       "При auto-значениях engine/brain/lang опускаются, но brain_mode остаётся")
        XCTAssertEqual(items.first?.name, "brain_mode")
        XCTAssertEqual(items.first?.value, "auto")
    }
```

- [ ] **Step 4 (повтор): Run full file test to verify it passes**

Run: `cd native/KrabEarAgent && swift test --filter ConversationVCWebSocketTests`
Expected: PASS — все тесты файла зелёные.

- [ ] **Step 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+WebSocket.swift native/KrabEarAgent/Tests/KrabEarAgentTests/ConversationVCWebSocketTests.swift
git commit -m "feat(conversation): always send brain_mode query param to Voice Gateway"
```

---

### Task 3: Персистентность `brainMode` (UserDefaults) — новый файл

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+BrainMode.swift`
- Test: Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/ConversationVCBrainModeTests.swift`

- [ ] **Step 1: Write the failing test**

Создать `ConversationVCBrainModeTests.swift`:

```swift
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd native/KrabEarAgent && swift test --filter ConversationVCBrainModeTests`
Expected: FAIL с ошибками компиляции — `ConversationViewController` не имеет `savedBrainMode`,
`saveBrainMode`, `brainModeControl`, `onBrainModeSegmentChanged`, `_buildSetDefaultRequest`.

- [ ] **Step 3: Implement**

Создать `ConversationViewController+BrainMode.swift`:

```swift
/*
 ConversationViewController+BrainMode — тоггл brain_mode (Волна 3b).

 Персистентность выбора между запусками (UserDefaults) + действие «Сделать дефолтом»
 (PUT /v1/settings/conversation на Voice Gateway). Сегментный контрол строится в
 +UI.swift; здесь — только поведение (обновление config, персистентность, сеть).

 Значения сегментов (индекс → brainMode): 0 = "fast", 1 = "krab", 2 = "auto".
 Контракт с Voice Gateway: docs/design-briefs/2026-07-08-vg-conversation-brain-mode.md
*/

import Foundation

private let kBrainModeUserDefaultsKey = "KrabEar_ConversationBrainMode"

extension ConversationViewController {

    /// Сегменты тоггла в порядке индекса: 0=fast, 1=krab, 2=auto. Единственный источник
    /// правды для маппинга индекс↔значение — используется и здесь, и в viewDidLoad()
    /// (ConversationViewController.swift) при восстановлении сохранённого выбора.
    static let brainModeSegmentValues = ["fast", "krab", "auto"]

    // MARK: - UserDefaults persistence (static — доступно до создания VC)

    /// Последний сохранённый выбор пользователя. "auto" если ключ не задан или
    /// содержит неизвестное значение.
    static var savedBrainMode: String {
        let raw = UserDefaults.standard.string(forKey: kBrainModeUserDefaultsKey) ?? "auto"
        return brainModeSegmentValues.contains(raw) ? raw : "auto"
    }

    /// Сохранить выбор пользователя.
    static func saveBrainMode(_ mode: String) {
        UserDefaults.standard.set(mode, forKey: kBrainModeUserDefaultsKey)
    }

    // MARK: - Segment action (target/action wiring — в +UI.swift buildUI())

    @objc func onBrainModeSegmentChanged() {
        let idx = brainModeControl.selectedSegment
        let values = ConversationViewController.brainModeSegmentValues
        let mode = (idx >= 0 && idx < values.count) ? values[idx] : "auto"
        config.brainMode = mode
        ConversationViewController.saveBrainMode(mode)
    }

    // MARK: - "Сделать дефолтом" (PUT /v1/settings/conversation)

    @objc func onSetBrainModeDefaultTapped() {
        guard let request = _buildSetDefaultRequest() else {
            showBrainModeHint("✗ Неверный адрес Voice Gateway")
            return
        }
        Task {
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let ok = (response as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
                await MainActor.run {
                    self.showBrainModeHint(ok ? "✓ Сохранено как дефолт" : "✗ Ошибка сохранения")
                }
            } catch {
                await MainActor.run {
                    self.showBrainModeHint("✗ \(error.localizedDescription)")
                }
            }
        }
    }

    private func showBrainModeHint(_ text: String) {
        brainModeHintLabel.stringValue = text
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
            guard let self, self.brainModeHintLabel.stringValue == text else { return }
            self.brainModeHintLabel.stringValue = ""
        }
    }

    // MARK: - DEBUG test hook

    /// Строит PUT-запрос к Voice Gateway settings API без выполнения (для тестов).
    func _buildSetDefaultRequest() -> URLRequest? {
        let base = config.httpBaseURLString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !base.isEmpty, let url = URL(string: base + "/v1/settings/conversation") else {
            return nil
        }
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["brain_mode": config.brainMode])
        return request
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd native/KrabEarAgent && swift test --filter ConversationVCBrainModeTests`
Expected: FAIL ещё раз на этом шаге из-за отсутствующих UI-свойств `brainModeControl`/
`brainModeHintLabel` — они добавляются в Task 4. Это ОЖИДАЕМО: перейти к Task 4 прежде чем
финально перепроверять этот файл (Step 4 этого таска будет перепройден в конце Task 4).

- [ ] **Step 5: Commit (после Task 4, см. ниже)**

Коммит этого файла делается ВМЕСТЕ с Task 4 (тесты не компилируются без UI-свойств оттуда) —
см. Step 5 в Task 4.

---

### Task 4: UI — сегмент-контрол + кнопка «Сделать дефолтом» + hint-лейбл

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController.swift:84-94` (UI elements)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+UI.swift:139-156` (controlsCard)

- [ ] **Step 1: Добавить свойства в ConversationViewController.swift**

После строки 93 (`let brainSelector = NSPopUpButton(...)`) и перед строкой 94
(`let settingsDisclosure = ...`), вставить:

```swift
    let brainModeControl    = NSSegmentedControl(labels: ["Быстро", "Краб", "Авто"], trackingMode: .selectOne, target: nil, action: nil)
    let setBrainModeDefault = ThemeSecondaryButton(title: "Сделать дефолтом", target: nil, action: nil)
    let brainModeHintLabel  = NSTextField(labelWithString: "")
```

- [ ] **Step 2: Инициализировать выбранный сегмент из сохранённого значения**

В `viewDidLoad()` (строка 119-123), после `buildUI()` и перед `applyState(.idle)`, вставить:

```swift
        let savedIdx = ConversationViewController.brainModeSegmentValues.firstIndex(of: config.brainMode) ?? 2
        brainModeControl.selectedSegment = savedIdx
```

(Итоговый `viewDidLoad()`:)
```swift
    override func viewDidLoad() {
        super.viewDidLoad()
        buildUI()
        let savedIdx = ConversationViewController.brainModeSegmentValues.firstIndex(of: config.brainMode) ?? 2
        brainModeControl.selectedSegment = savedIdx
        applyState(.idle)
    }
```

(`brainModeSegmentValues` определён в `ConversationViewController+BrainMode.swift`, Task 3 —
компилируется только после того, как тот файл создан; порядок задач в этом плане это уже
учитывает.)

- [ ] **Step 3: Построить UI-ряд в +UI.swift**

В `buildUI()`, после блока `controlsCard`/`controlsRow` (после строки 156
`root.addArrangedSubview(controlsCard)`) и перед блоком `settingsCard` (строка 158), вставить
новую карточку:

```swift
        // --- Brain mode row (Волна 3b) ---
        let brainModeCard = makeCard(title: "Мозг разговора")
        let brainModeRow  = hStack()

        brainModeControl.target = self
        brainModeControl.action = #selector(onBrainModeSegmentChanged)
        brainModeRow.addArrangedSubview(brainModeControl)

        setBrainModeDefault.target = self
        setBrainModeDefault.action = #selector(onSetBrainModeDefaultTapped)
        setBrainModeDefault.heightAnchor.constraint(equalToConstant: 28).isActive = true
        brainModeRow.addArrangedSubview(setBrainModeDefault)
        brainModeRow.addArrangedSubview(NSView()) // spacer

        brainModeCard.contentStackView.addArrangedSubview(brainModeRow)

        styleLabel(brainModeHintLabel, font: KrabEarTheme.Typography.caption)
        brainModeHintLabel.textColor = KrabEarTheme.Colors.textSecondary
        brainModeCard.contentStackView.addArrangedSubview(brainModeHintLabel)

        root.addArrangedSubview(brainModeCard)
```

- [ ] **Step 4: Run tests to verify everything passes**

Run: `cd native/KrabEarAgent && swift test --filter ConversationVCBrainModeTests`
Expected: PASS — все тесты из Task 3 теперь компилируются и проходят.

Run: `cd native/KrabEarAgent && swift build -c release`
Expected: сборка без ошибок.

- [ ] **Step 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController.swift \
        native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+UI.swift \
        native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+BrainMode.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/ConversationVCBrainModeTests.swift
git commit -m "feat(conversation): brain_mode segmented control + set-as-default action"
```

---

### Task 5: Регрессионный тест — статус «Думает» уже существует

Живой аудит + чтение кода обнаружили, что `ConversationState.thinking` (лейбл «🟡 Думает»)
УЖЕ устанавливается в `handleDownlinkEvent` при получении финального STT
(`case .sttPartial(_, _, isFinal: true): conversationState = .thinking`,
`ConversationViewController.swift:199-202`) — новый код для этого не нужен (спека §5 закрыта
существующим поведением). Но точного теста на этот ПЕРЕХОД СОСТОЯНИЯ не было — существующий
`test_user_message_appended` проверяет только текст в буфере, не state. Добавляем недостающий
regression-тест, закрывающий требование спеки.

**Files:**
- Modify: `native/KrabEarAgent/Tests/KrabEarAgentTests/ConversationViewControllerTests.swift`

- [ ] **Step 1: Написать тест (характеризационный — код уже существует, тест должен сразу пройти)**

После `test_user_message_appended` (после строки 182), вставить:

```swift
    // MARK: - 6b. test_final_transcript_sets_thinking_state

    /// sttPartial с isFinal=true должен перевести state в .thinking (не только добавить текст) —
    /// это и есть визуальный сигнал «Думаю…» на время ожидания ответа мозга (Волна 3b §5).
    func test_final_transcript_sets_thinking_state() {
        vc.transcriptBuffer = ""
        vc.isSessionActive = true
        vc.conversationState = .listening

        vc.handleDownlinkEvent(.sttPartial(text: "Который час?", lang: "ru", isFinal: true))

        XCTAssertEqual(vc.conversationState, .thinking,
                       "Финальный STT должен переводить state в .thinking (статус «Думает» на время ответа мозга)")
    }
```

- [ ] **Step 2: Run test to verify it passes immediately (характеризационный тест, не TDD red→green)**

Run: `cd native/KrabEarAgent && swift test --filter ConversationViewControllerTests`
Expected: PASS сразу — поведение уже реализовано, этот тест документирует и защищает его от
случайной регрессии.

- [ ] **Step 3: Commit**

```bash
git add native/KrabEarAgent/Tests/KrabEarAgentTests/ConversationViewControllerTests.swift
git commit -m "test(conversation): lock existing .thinking state transition on final transcript"
```

---

### Task 6: Подключить сохранённый `brainMode` и HTTP-базу в продакшен-конфиг

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+VoiceTab.swift:27-35`

Тестового покрытия на `setupConversationTab()` в проекте нет (тяжёлый `HistoryPanelController`
не имеет тестовой обвязки для этого метода — согласовано с существующей практикой: это
2-строчная glue-правка, проверяется сборкой + живым смоком в Task 7, отдельный unit-тест здесь
был бы непропорционален).

- [ ] **Step 1: Implement**

В `setupConversationTab(contentView:)` (строки 27-35) заменить конструктор `ConversationConfig`:

```swift
    func setupConversationTab(contentView voiceContentView: NSView) {
        let settings = settingsProvider()
        let httpBase = settings.voiceGatewayURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let config = ConversationConfig(
            wsURLString:  buildConversationWSURL(from: settings),
            apiKey:       settings.voiceGatewayAPIKey,
            languageHint: "auto",
            engine:       "auto",
            brain:        "auto",
            brainMode:    ConversationViewController.savedBrainMode,
            httpBaseURLString: httpBase
        )
        let vc = ConversationViewController(config: config)
        conversationVC = vc
```

(Остальное тело метода — без изменений.)

- [ ] **Step 2: Build to verify it compiles**

Run: `cd native/KrabEarAgent && swift build -c release`
Expected: сборка без ошибок.

- [ ] **Step 3: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+VoiceTab.swift
git commit -m "feat(conversation): wire saved brainMode + VG http base into production config"
```

---

### Task 7: Полный прогон тестов + сборка + живой смок

- [ ] **Step 1: Полный тестовый прогон**

Run: `cd native/KrabEarAgent && swift test 2>&1 | tail -60`
Expected: все тесты пакета зелёные (включая все существующие ConversationVC*Tests, ModelsTests).

- [ ] **Step 2: Полная сборка + сборка бандла**

```bash
cd native/KrabEarAgent && swift build -c release
cp -f .build/release/KrabEarAgent ../runtime/KrabEarAgent
cp -f .build/release/KrabEarAgent "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
codesign -s "Krab Ear Dev Local" -f ../runtime/KrabEarAgent
codesign -s "Krab Ear Dev Local" -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
```

(Или единой командой: `scripts/build_and_deploy.command` из корня репозитория — предпочтительно,
делает то же самое + dSYM + Sentry upload.)

- [ ] **Step 3: Живой смок (ручной, т.к. требует реального VG-эндпоинта)**

ПРЕДПОСЫЛКА: VG-сессия должна была реализовать `?brain_mode=` query-param поддержку (хотя бы
частично — сервер может её игнорировать, если ещё не готова, WS всё равно подключится). Если
`/v1/settings/conversation` ещё не существует — кнопка «Сделать дефолтом» покажет
«✗ Ошибка сохранения», это ОЖИДАЕМО и не блокирует остальной функционал (см. спека §6).

1. Запустить агент (`open "Krab Ear.app"` или `./native/runtime/KrabEarAgent`).
2. Открыть таб «Разговор с AI».
3. Убедиться, что карточка «Мозг разговора» видна с 3 сегментами (Быстро/Краб/Авто) и кнопкой
   «Сделать дефолтом».
4. Переключить на «Краб», нажать «Начать разговор» — сказать фразу, дождаться ответа. Замерить
   реальную латентность (для калибровки таймаутов в VG-брифе — сообщить владельцу).
5. Переключить на «Быстро» — убедиться, что разговор по-прежнему работает (текущее поведение
   не сломано).
6. Перезапустить агент — убедиться, что выбранный режим («Быстро» с прошлого шага) сохранился
   после рестарта (персистентность UserDefaults).
7. Нажать «Сделать дефолтом» — проверить hint-лейбл (✓ или ✗ в зависимости от готовности VG).

- [ ] **Step 4: Отчитаться владельцу о результатах живого смока**

Особенно — наблюдаемая латентность режима «Краб» (для калибровки `va_conversation_brain_timeout_krab_sec`/`_auto_sec` в VG-брифе) и любые найденные проблемы стыковки с VG API.
