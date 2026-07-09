# Волна 3c: Conversation UX Polish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Довести UX «Разговора с AI» до предсказуемого: обработка `conv.interrupted` (клиент реально останавливает воспроизведение), локальная озвучка ошибок, плавающий статус-overlay, регрессионный тест wake-поллера.

**Architecture:** Всё — Swift-сторона (`native/KrabEarAgent`). Прерывание сходится в одном обработчике `handleInterrupted()` (серверное событие = источник истины, ручная кнопка ждёт подтверждения с 2с fallback). Озвучка ошибок — новый класс `ConversationErrorAnnouncer` (дебаунс+фразы, чистая логика) с инжекцией реального синтеза (IPC `synthesize_speech` off-main → `AVAudioPlayer`) из `+VoiceTab`. Overlay — новый `ConversationStatusOverlay` (NSPanel по образцу `LiveSubtitlesOverlay`), которым владеет `ConversationViewController`.

**Tech Stack:** Swift 6 (strict concurrency), AppKit, AVFoundation, XCTest. Спека: `docs/superpowers/specs/2026-07-09-conversation-ux-polish-design.md`.

**Контекст-якоря (проверены 2026-07-09):**
- `ConversationEvents.swift:111-113` — `conv.interrupted` сейчас → `.unknown`.
- `ConversationViewController.swift:156-160` — `interruptAI()` шлёт control и сразу ставит `.listening`.
- `ConversationViewController+Audio.swift:122-153` — `audioHolder.playerNode` (AVAudioPlayerNode, private holder в том же файле).
- `ConversationViewController+WebSocket.swift:105-113` — receive-failure ветка (единственный сигнал и «VG недоступен при старте», и «обрыв посреди»; различаются по `conversationState == .connecting`).
- `tts_service.py:421-470` — `synthesize_speech` возвращает `{wav_bytes_b64, language, engine, byte_count}` при успехе (БЕЗ ключа `ok`; `ok:false` только при ошибке; пустой `wav_bytes_b64` = синтез не удался).
- IPC off-main паттерн — `HistoryPanelController+QuickActions.swift:91-96` (`let ipcClient = self.ipcClient` → `DispatchQueue.global(qos: .userInitiated).async` → `nonisolated(unsafe) let response = try ipcClient.call(...)`).
- Source-contract паттерн — `MainErrorsWiringTests.swift:298-315` + `mainSwiftURL` walk-up helper.
- Wake-поллер проводка — `main.swift:527` (вызов `setupWakeWordConversationObservers()`) и `main.swift:551-563` (тело: pause/resume `.conversation`).
- 🔴 Глиф-гейт: НЕ вводить новые non-ASCII глифы. Все эмодзи overlay берёт из существующих `ConversationState.localizedLabel` строк.
- 🔴 Никаких `runModal()` — overlay это `NSPanel.orderFront`, как `LiveSubtitlesOverlay`.

**Все команды из каталога:** `<worktree>/native/KrabEarAgent`. Сборка: `swift build -c release`. Тесты: `swift test --filter <Класс>`.

---

## File map

| Файл | Действие | Ответственность |
|---|---|---|
| `Sources/KrabEarAgent/ConversationEvents.swift` | Modify | +case `.interrupted(reason:)` + decode |
| `Sources/KrabEarAgent/ConversationViewController.swift` | Modify | обработка `.interrupted`, rework `interruptAI()`, свойства announcer/overlay/timer, триггер `.serverError` |
| `Sources/KrabEarAgent/ConversationViewController+Audio.swift` | Modify | +`flushDownlinkPlayback()`, level-feed в overlay |
| `Sources/KrabEarAgent/ConversationViewController+WebSocket.swift` | Modify | классификация WS-failure → announcer |
| `Sources/KrabEarAgent/ConversationErrorAnnouncer.swift` | Create | дебаунс + фразы + playWav |
| `Sources/KrabEarAgent/ConversationStatusOverlay.swift` | Create | плавающий NSPanel статуса |
| `Sources/KrabEarAgent/HistoryPanelController+VoiceTab.swift` | Modify | инжекция реального speak (IPC) |
| `Tests/KrabEarAgentTests/ConversationInterruptTests.swift` | Create | Tasks 1-2 |
| `Tests/KrabEarAgentTests/ConversationErrorAnnouncerTests.swift` | Create | Tasks 3-4 |
| `Tests/KrabEarAgentTests/ConversationStatusOverlayTests.swift` | Create | Tasks 5-6 |
| `Tests/KrabEarAgentTests/WakeWordConversationWiringTests.swift` | Create | Task 7 |

---

### Task 1: Событие `.interrupted` в словаре ConversationEvent

**Files:**
- Modify: `Sources/KrabEarAgent/ConversationEvents.swift`
- Test: `Tests/KrabEarAgentTests/ConversationInterruptTests.swift` (create)

- [ ] **Step 1: Write the failing tests**

Создать `Tests/KrabEarAgentTests/ConversationInterruptTests.swift`:

```swift
/*
 ConversationInterruptTests — Волна 3c.

 Покрывает:
 1. Декодирование conv.interrupted → .interrupted(reason:) (было .unknown — событие молча логировалось).
 2. handleDownlinkEvent(.interrupted) — стоп плеера-хвоста, state → .listening, строка «— Прервано».
 3. interruptAI() ждёт серверного подтверждения; 2с fallback переводит локально.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Task 1: decode

final class ConversationInterruptedDecodeTests: XCTestCase {

    private func decode(_ json: String) -> ConversationEvent? {
        ConversationEvent.decode(from: Data(json.utf8))
    }

    func test_decode_interrupted_withReason() {
        let ev = decode(#"{"type":"conv.interrupted","data":{"reason":"user_started_speaking"}}"#)
        guard case .interrupted(let reason)? = ev else {
            return XCTFail("Ожидали .interrupted, получили \(String(describing: ev))")
        }
        XCTAssertEqual(reason, "user_started_speaking")
    }

    func test_decode_interrupted_withoutReason_emptyString() {
        let ev = decode(#"{"type":"conv.interrupted"}"#)
        guard case .interrupted(let reason)? = ev else {
            return XCTFail("Ожидали .interrupted, получили \(String(describing: ev))")
        }
        XCTAssertEqual(reason, "")
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `swift test --filter ConversationInterruptedDecodeTests`
Expected: FAIL — компиляция падает (`.interrupted` case не существует) ЛИБО «Ожидали .interrupted» (сейчас decode возвращает `.unknown`).

- [ ] **Step 3: Add the case + decode branch**

В `ConversationEvents.swift`, в `enum ConversationEvent` после `case closed`:

```swift
    /// AI прерван (barge-in голосом ИЛИ подтверждение ручного control-interrupt).
    /// conv.interrupted — с Волны 3c означает ПОДТВЕРЖДЁННУЮ осмысленную речь
    /// (VG фильтрует шум/кашель на своей стороне, см. бриф 2026-07-09-vg-barge-in-resume).
    case interrupted(reason: String)
```

В `decode`, заменить ветку `case "conv.interrupted"` (строки 111-113):

```swift
        case "conv.interrupted":
            let reason = (payload["reason"] as? String) ?? ""
            return .interrupted(reason: reason)
```

Заодно поправить doc-комментарий в `ConversationViewController.swift:242` у `.unknown` — убрать `conv.interrupted` из перечня неизвестных: `// Неизвестный тип события (conv.vad_*, conv.audio_chunk) — логируем, не падаем.` (сделаем в Task 2, там этот switch и так правится — здесь только отметить).

- [ ] **Step 4: Run tests to verify they pass**

Run: `swift test --filter ConversationInterruptedDecodeTests`
Expected: 2 passed. ⚠️ Если `handleDownlinkEvent` перестал компилироваться (switch стал неисчерпывающим) — добавить ВРЕМЕННУЮ ветку `case .interrupted: break` в `ConversationViewController.swift` (Task 2 заменит её настоящей логикой).

- [ ] **Step 5: Commit**

```bash
git add Sources/KrabEarAgent/ConversationEvents.swift Tests/KrabEarAgentTests/ConversationInterruptTests.swift Sources/KrabEarAgent/ConversationViewController.swift
git commit -m "feat(conversation): conv.interrupted → typed event .interrupted(reason:)"
```

---

### Task 2: Обработка прерывания — flush плеера + единая точка + fallback

**Files:**
- Modify: `Sources/KrabEarAgent/ConversationViewController+Audio.swift`
- Modify: `Sources/KrabEarAgent/ConversationViewController.swift`
- Test: `Tests/KrabEarAgentTests/ConversationInterruptTests.swift` (append)

- [ ] **Step 1: Write the failing tests**

Дописать в `ConversationInterruptTests.swift`:

```swift
// MARK: - Task 2: handleInterrupted + interruptAI fallback

@MainActor
final class ConversationInterruptHandlingTests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
        vc.isSessionActive = true
    }

    override func tearDown() async throws {
        vc.interruptFallbackTimer?.invalidate()
        vc = nil
        try await super.tearDown()
    }

    func test_interruptedEvent_setsListening_andAppendsTranscriptLine() {
        vc.conversationState = .speaking
        vc.handleDownlinkEvent(.interrupted(reason: "user_started_speaking"))
        XCTAssertEqual(vc.conversationState, .listening)
        XCTAssertTrue(vc.transcriptBuffer.contains("— Прервано"),
                      "transcript должен получить служебную строку «— Прервано»")
    }

    func test_interruptedEvent_ignored_whenSessionInactive() {
        vc.isSessionActive = false
        vc.conversationState = .idle
        vc.handleDownlinkEvent(.interrupted(reason: "x"))
        XCTAssertEqual(vc.conversationState, .idle)
        XCTAssertFalse(vc.transcriptBuffer.contains("— Прервано"))
    }

    func test_interruptAI_doesNotSwitchStateImmediately() {
        vc.conversationState = .speaking
        vc.interruptAI()
        XCTAssertEqual(vc.conversationState, .speaking,
                       "interruptAI ждёт серверного conv.interrupted, не переключает сам")
        XCTAssertNotNil(vc.interruptFallbackTimer, "fallback-таймер должен быть взведён")
    }

    func test_interruptAI_fallbackFires_whenNoServerConfirmation() {
        vc.interruptFallbackInterval = 0.05
        vc.conversationState = .speaking
        vc.interruptAI()
        let exp = expectation(description: "fallback")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exp.fulfill() }
        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(vc.conversationState, .listening)
        XCTAssertTrue(vc.transcriptBuffer.contains("— Прервано"))
    }

    func test_serverConfirmation_cancelsFallback_noDoubleLine() {
        vc.interruptFallbackInterval = 0.05
        vc.conversationState = .speaking
        vc.interruptAI()
        vc.handleDownlinkEvent(.interrupted(reason: "confirmed"))  // подтверждение пришло раньше fallback
        let exp = expectation(description: "wait past fallback window")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exp.fulfill() }
        wait(for: [exp], timeout: 2.0)
        let occurrences = vc.transcriptBuffer.components(separatedBy: "— Прервано").count - 1
        XCTAssertEqual(occurrences, 1, "fallback не должен продублировать обработку")
    }

    func test_flushDownlinkPlayback_nilPlayer_noCrash() {
        // Аудио-движок не стартовал — playerNode nil; вызов не должен падать.
        vc.flushDownlinkPlayback()
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `swift test --filter ConversationInterruptHandlingTests`
Expected: FAIL компиляцией — нет `interruptFallbackTimer`/`interruptFallbackInterval`/`flushDownlinkPlayback`.

- [ ] **Step 3: Implement — Audio flush**

В `ConversationViewController+Audio.swift`, добавить в конец `extension ConversationViewController` (тот же файл — доступ к private `audioHolder`):

```swift
    // MARK: - Interrupt support (Волна 3c)

    /// Сбросить уже запланированные downlink-буферы (прерывание ответа).
    /// AVAudioPlayerNode.stop() снимает все scheduled buffers; play() возвращает
    /// узел в играющее состояние для следующих буферов. Engine и захват не трогаем —
    /// сессия продолжается. Безопасно при nil (аудио не стартовало) и при
    /// не-запущенном engine (play() на attached-node у остановленного engine
    /// не вызывается — guard по isRunning).
    func flushDownlinkPlayback() {
        guard let player = audioHolder.playerNode else { return }
        player.stop()
        if audioHolder.engine?.isRunning == true {
            player.play()
        }
    }
```

- [ ] **Step 4: Implement — свойства + единая точка + rework interruptAI**

В `ConversationViewController.swift`:

(а) К stored-свойствам класса (после `var isSessionActive = false`):

```swift
    /// Fallback-таймер ручного прерывания: если сервер не подтвердил conv.interrupted
    /// за interruptFallbackInterval — применяем прерывание локально.
    var interruptFallbackTimer: Timer?
    /// Интервал fallback (инжектируется в тестах; прод — 2с).
    var interruptFallbackInterval: TimeInterval = 2.0
```

(б) Заменить `interruptAI()` (строки 155-160):

```swift
    /// Прервать текущее TTS-воспроизведение AI. Вызывается из кнопки «Прервать AI»
    /// (окно и overlay). Состояние переключает НЕ сам — ждёт серверного
    /// conv.interrupted (единая точка handleInterrupted); fallback через
    /// interruptFallbackInterval, если подтверждение не пришло.
    func interruptAI() {
        guard isSessionActive else { return }
        sendControlMessage(.interrupt)
        interruptFallbackTimer?.invalidate()
        interruptFallbackTimer = Timer.scheduledTimer(
            withTimeInterval: interruptFallbackInterval, repeats: false
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.isSessionActive else { return }
                AgentLogger.shared.info("[ConversationVC] Interrupt: сервер не подтвердил за \(self.interruptFallbackInterval)s — локальный fallback")
                self.handleInterrupted(reason: "local_fallback")
            }
        }
    }

    /// Единая точка обработки прерывания — из серверного conv.interrupted
    /// (голосовой barge-in ИЛИ подтверждение кнопки) и из локального fallback.
    func handleInterrupted(reason: String) {
        guard isSessionActive else { return }
        interruptFallbackTimer?.invalidate()
        interruptFallbackTimer = nil
        flushDownlinkPlayback()
        appendTranscriptLine("— Прервано")
        conversationState = .listening
    }
```

(в) В `handleDownlinkEvent` заменить временную ветку из Task 1 (либо добавить перед `case .unknown`):

```swift
        case .interrupted(let reason):
            AgentLogger.shared.info("[ConversationVC] conv.interrupted (\(reason))")
            handleInterrupted(reason: reason)
```

(г) Обновить doc-комментарий `.unknown` (строка 242): `// Неизвестный тип события (conv.vad_*, conv.audio_chunk) — логируем, не падаем.`

(д) В `stopConversation()` добавить сброс таймера (после `isSessionActive = false`):

```swift
        interruptFallbackTimer?.invalidate()
        interruptFallbackTimer = nil
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `swift test --filter "ConversationInterruptedDecodeTests|ConversationInterruptHandlingTests"`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add Sources/KrabEarAgent/ConversationViewController.swift Sources/KrabEarAgent/ConversationViewController+Audio.swift Tests/KrabEarAgentTests/ConversationInterruptTests.swift
git commit -m "feat(conversation): единая обработка прерывания — flush плеера, серверное подтверждение, 2с fallback"
```

---

### Task 3: ConversationErrorAnnouncer — дебаунс + фразы

**Files:**
- Create: `Sources/KrabEarAgent/ConversationErrorAnnouncer.swift`
- Test: `Tests/KrabEarAgentTests/ConversationErrorAnnouncerTests.swift` (create)

- [ ] **Step 1: Write the failing tests**

Создать `Tests/KrabEarAgentTests/ConversationErrorAnnouncerTests.swift`:

```swift
/*
 ConversationErrorAnnouncerTests — Волна 3c, секция «локальная озвучка ошибок».

 Дебаунс 30с на класс ошибки; фразы фиксированы спекой и НЕ содержат слово
 «Краб» (анти-триггер wake word); отсутствие speak-клоужера — тихая деградация.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class ConversationErrorAnnouncerTests: XCTestCase {

    private var announcer: ConversationErrorAnnouncer!
    private var spoken: [String] = []
    private var fakeNow: Date = Date(timeIntervalSince1970: 1_000_000)

    override func setUp() async throws {
        try await super.setUp()
        spoken = []
        fakeNow = Date(timeIntervalSince1970: 1_000_000)
        announcer = ConversationErrorAnnouncer()
        announcer.now = { [weak self] in self?.fakeNow ?? Date() }
        announcer.speak = { [weak self] phrase in self?.spoken.append(phrase) }
    }

    func test_firstAnnounce_speaksPhrase() {
        XCTAssertTrue(announcer.announce(.gatewayUnreachable))
        XCTAssertEqual(spoken, ["Голосовой шлюз недоступен."])
    }

    func test_debounce_blocksRepeatWithin30s() {
        _ = announcer.announce(.connectionLost)
        fakeNow = fakeNow.addingTimeInterval(29)
        XCTAssertFalse(announcer.announce(.connectionLost))
        XCTAssertEqual(spoken.count, 1)
    }

    func test_debounce_allowsAfter30s() {
        _ = announcer.announce(.connectionLost)
        fakeNow = fakeNow.addingTimeInterval(31)
        XCTAssertTrue(announcer.announce(.connectionLost))
        XCTAssertEqual(spoken.count, 2)
    }

    func test_debounce_isPerClass_independentClasses() {
        _ = announcer.announce(.gatewayUnreachable)
        XCTAssertTrue(announcer.announce(.serverError),
                      "дебаунс per-class: другой класс не блокируется")
        XCTAssertEqual(spoken, ["Голосовой шлюз недоступен.", "Произошла ошибка. Попробуй ещё раз."])
    }

    func test_noSpeakClosure_returnsFalse_noCrash() {
        announcer.speak = nil
        XCTAssertFalse(announcer.announce(.serverError))
    }

    func test_phrases_doNotContainWakeWord() {
        for phrase in ConversationErrorAnnouncer.phrases.values {
            XCTAssertFalse(phrase.lowercased().contains("краб"),
                           "фраза «\(phrase)» не должна содержать wake word")
        }
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `swift test --filter ConversationErrorAnnouncerTests`
Expected: FAIL компиляцией — класс не существует.

- [ ] **Step 3: Implement the class**

Создать `Sources/KrabEarAgent/ConversationErrorAnnouncer.swift`:

```swift
/*
 ConversationErrorAnnouncer — локальная озвучка ошибок «Разговора с AI» (Волна 3c).

 Принцип (спека §4): голосовой интерфейс должен голосом сообщать о сбое —
 молчание выглядит как зависание. Работает НЕЗАВИСИМО от VG-соединения:
 синтез — локальный IPC synthesize_speech (инжектится из +VoiceTab как
 speak-клоужер), воспроизведение — отдельный AVAudioPlayer (НЕ conversation-плеер,
 должен жить и после stopConversation()).

 Деградация: privacy mode / backend недоступен / пустой синтез → speak-клоужер
 молча ничего не проигрывает — остаётся текущий текст в transcript. Без ретраев.

 Дебаунс: не чаще 1 озвучки на класс ошибки за 30с (реконнект-циклы не спамят).
 Фразы НЕ содержат слово «Краб» — чтобы не триггерить wake word (поллер к этому
 моменту уже может быть возобновлён).
*/

import AVFoundation
import Foundation

@MainActor
final class ConversationErrorAnnouncer {

    /// Класс ошибки — свой дебаунс-слот на каждый.
    enum ErrorClass: String, CaseIterable {
        /// VG недоступен при старте сессии (WS connect fail в состоянии .connecting).
        case gatewayUnreachable = "gateway_unreachable"
        /// Обрыв WS посреди активной сессии.
        case connectionLost = "connection_lost"
        /// conv.error / conv.fatal от сервера.
        case serverError = "server_error"
    }

    static let debounceInterval: TimeInterval = 30

    /// Фразы фиксированы спекой (§4.1). Без слова «Краб».
    static let phrases: [ErrorClass: String] = [
        .gatewayUnreachable: "Голосовой шлюз недоступен.",
        .connectionLost:     "Связь с голосовым шлюзом потеряна.",
        .serverError:        "Произошла ошибка. Попробуй ещё раз.",
    ]

    /// Реальная озвучка фразы (синтез + воспроизведение). Инжектится из
    /// HistoryPanelController+VoiceTab; в тестах — спай. Вызывается уже ПОСЛЕ
    /// дебаунс-гейта, на main actor. nil → тихая деградация (текст-only).
    var speak: ((String) -> Void)?

    /// Источник времени (инжектируется в тестах для детерминированного дебаунса).
    var now: () -> Date = { Date() }

    private var lastAnnounced: [ErrorClass: Date] = [:]

    /// Озвучить ошибку класса cls с дебаунсом. true = фраза ушла в speak.
    @discardableResult
    func announce(_ cls: ErrorClass) -> Bool {
        if let last = lastAnnounced[cls],
           now().timeIntervalSince(last) < Self.debounceInterval {
            return false
        }
        lastAnnounced[cls] = now()
        guard let phrase = Self.phrases[cls], let speak else { return false }
        speak(phrase)
        return true
    }

    // MARK: - WAV playback (используется реальным speak-клоужером из +VoiceTab)

    /// Держим плеер живым до конца воспроизведения (AVAudioPlayer останавливается
    /// при деаллокации). Одна ошибка перекрывает предыдущую — это ок для коротких фраз.
    static var activePlayer: AVAudioPlayer?

    /// Проиграть WAV-данные (ответ synthesize_speech). Никогда не бросает.
    static func playWav(_ data: Data) {
        guard let player = try? AVAudioPlayer(data: data) else {
            AgentLogger.shared.info("[ErrorAnnouncer] AVAudioPlayer не открыл WAV (\(data.count) bytes)")
            return
        }
        activePlayer = player
        player.play()
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `swift test --filter ConversationErrorAnnouncerTests`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add Sources/KrabEarAgent/ConversationErrorAnnouncer.swift Tests/KrabEarAgentTests/ConversationErrorAnnouncerTests.swift
git commit -m "feat(conversation): ConversationErrorAnnouncer — дебаунс-озвучка ошибок (логика)"
```

---

### Task 4: Проводка announcer — WS-failure классификация, conv.error, реальный IPC-синтез

**Files:**
- Modify: `Sources/KrabEarAgent/ConversationViewController.swift`
- Modify: `Sources/KrabEarAgent/ConversationViewController+WebSocket.swift`
- Modify: `Sources/KrabEarAgent/HistoryPanelController+VoiceTab.swift`
- Test: `Tests/KrabEarAgentTests/ConversationErrorAnnouncerTests.swift` (append)

- [ ] **Step 1: Write the failing tests**

Дописать в `ConversationErrorAnnouncerTests.swift`:

```swift
// MARK: - Task 4: проводка триггеров в ConversationViewController

@MainActor
final class ConversationErrorAnnouncerWiringTests: XCTestCase {

    private var vc: ConversationViewController!
    private var spoken: [String] = []

    override func setUp() async throws {
        try await super.setUp()
        spoken = []
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
        vc.errorAnnouncer.speak = { [weak self] phrase in self?.spoken.append(phrase) }
        vc.isSessionActive = true
    }

    override func tearDown() async throws {
        vc.interruptFallbackTimer?.invalidate()
        vc = nil
        try await super.tearDown()
    }

    func test_wsFailure_whileConnecting_announcesGatewayUnreachable() {
        vc.conversationState = .connecting
        vc.classifyAndAnnounceWSFailure()
        XCTAssertEqual(spoken, ["Голосовой шлюз недоступен."])
    }

    func test_wsFailure_midSession_announcesConnectionLost() {
        vc.conversationState = .listening
        vc.classifyAndAnnounceWSFailure()
        XCTAssertEqual(spoken, ["Связь с голосовым шлюзом потеряна."])
    }

    func test_convError_announcesServerError() {
        vc.handleDownlinkEvent(.error(code: "conv.error", message: "brain exploded"))
        XCTAssertEqual(spoken, ["Произошла ошибка. Попробуй ещё раз."])
    }

    func test_userStop_neverAnnounces() {
        vc.conversationState = .listening
        vc.stopConversation()
        XCTAssertTrue(spoken.isEmpty, "штатная остановка пользователем не озвучивается")
    }

    func test_sourceContract_receiveLoopFailureBranch_callsClassifier() throws {
        // Receive-failure ветка в +WebSocket.swift обязана вызывать классификатор —
        // иначе озвучка «шлюз недоступен/связь потеряна» мертва в проде
        // (класс test-validates-the-hole: setupErrorBus/setupHealthMonitor).
        let src = try String(contentsOf: Self.wsSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains("classifyAndAnnounceWSFailure()"),
                      "startReceiveLoop failure-ветка должна вызывать classifyAndAnnounceWSFailure()")
    }

    private static var wsSwiftURL: URL {
        var url = URL(fileURLWithPath: #filePath)  // .../Tests/KrabEarAgentTests/<этот файл>
        url.deleteLastPathComponent()              // Tests/KrabEarAgentTests
        url.deleteLastPathComponent()              // Tests
        url.deleteLastPathComponent()              // native/KrabEarAgent
        return url
            .appendingPathComponent("Sources/KrabEarAgent/ConversationViewController+WebSocket.swift")
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `swift test --filter ConversationErrorAnnouncerWiringTests`
Expected: FAIL компиляцией — нет `errorAnnouncer`/`classifyAndAnnounceWSFailure`.

- [ ] **Step 3: Implement — свойство + классификатор + conv.error триггер**

В `ConversationViewController.swift`:

(а) stored-свойство (рядом с interrupt-таймером):

```swift
    /// Локальная озвучка ошибок (Волна 3c). Реальный speak инжектится из +VoiceTab;
    /// без инжекции — тихая text-only деградация.
    let errorAnnouncer = ConversationErrorAnnouncer()
```

(б) метод классификации (рядом с `handleInterrupted`):

```swift
    /// Классифицировать провал WS по текущему состоянию и озвучить.
    /// .connecting = не смогли подключиться; иначе — обрыв посреди сессии.
    func classifyAndAnnounceWSFailure() {
        let cls: ConversationErrorAnnouncer.ErrorClass =
            (conversationState == .connecting) ? .gatewayUnreachable : .connectionLost
        errorAnnouncer.announce(cls)
    }
```

(в) в `handleDownlinkEvent`, ветка `.error` — добавить озвучку ПЕРЕД stopConversation:

```swift
        case .error(let code, let message):
            appendTranscriptLine("— Ошибка [\(code)]: \(message)")
            errorAnnouncer.announce(.serverError)
            conversationState = .error(message)
            stopConversation()
```

- [ ] **Step 4: Implement — вызов из receive-failure ветки**

В `ConversationViewController+WebSocket.swift`, failure-ветка `startReceiveLoop` (строки 105-113) — добавить вызов до `stopConversation()`:

```swift
            case .failure(let error):
                Task { @MainActor [weak self] in
                    guard let self, self.isSessionActive else { return }
                    let desc = (error as NSError).localizedDescription
                    AgentLogger.shared.info("[WS] Receive error: \(desc)")
                    self.classifyAndAnnounceWSFailure()
                    self.conversationState = .error(desc)
                    self.stopConversation()
                }
```

- [ ] **Step 5: Implement — реальный speak в +VoiceTab**

В `HistoryPanelController+VoiceTab.swift::setupConversationTab`, после `let vc = ConversationViewController(config: config)`:

```swift
        // Волна 3c: локальная озвучка ошибок — синтез через IPC synthesize_speech
        // (строго off-main, AGENT-3), воспроизведение через AVAudioPlayer.
        // Пустой wav_bytes_b64 (privacy mode / TTS недоступен) → тихая text-only деградация.
        let ipcClient = self.ipcClient
        vc.errorAnnouncer.speak = { phrase in
            DispatchQueue.global(qos: .userInitiated).async {
                nonisolated(unsafe) let response = try? ipcClient.call(
                    method: "synthesize_speech",
                    params: ["text": phrase, "language": "ru"]
                )
                guard let result = response?["result"] as? [String: Any],
                      let b64 = result["wav_bytes_b64"] as? String, !b64.isEmpty,
                      let wav = Data(base64Encoded: b64)
                else { return }
                Task { @MainActor in
                    ConversationErrorAnnouncer.playWav(wav)
                }
            }
        }
```

⚠️ Проверить точное имя IPC-свойства/паттерна по соседям в том же файле или `HistoryPanelController+QuickActions.swift:91-96` — паттерн `let ipcClient = self.ipcClient` + `nonisolated(unsafe) let response = try ipcClient.call(...)` уже устоявшийся, скопировать 1-в-1.

- [ ] **Step 6: Run tests + build**

Run: `swift test --filter "ConversationErrorAnnouncerTests|ConversationErrorAnnouncerWiringTests"`
Expected: 11 passed.
Run: `swift build -c release`
Expected: Build complete (pre-existing warnings в BackendSupervisor.swift — не наши).

- [ ] **Step 7: Commit**

```bash
git add Sources/KrabEarAgent/ConversationViewController.swift Sources/KrabEarAgent/ConversationViewController+WebSocket.swift Sources/KrabEarAgent/HistoryPanelController+VoiceTab.swift Tests/KrabEarAgentTests/ConversationErrorAnnouncerTests.swift
git commit -m "feat(conversation): озвучка ошибок — WS-классификация, conv.error, IPC synthesize_speech"
```

---

### Task 5: ConversationStatusOverlay — плавающий статус

**Files:**
- Create: `Sources/KrabEarAgent/ConversationStatusOverlay.swift`
- Test: `Tests/KrabEarAgentTests/ConversationStatusOverlayTests.swift` (create)

- [ ] **Step 1: Write the failing tests**

Создать `Tests/KrabEarAgentTests/ConversationStatusOverlayTests.swift`:

```swift
/*
 ConversationStatusOverlayTests — Волна 3c, плавающий статус-HUD разговора.
 Панель headless (orderFront в тестах не вызываем на show? вызываем — NSPanel
 без NSApp.run безопасен, прецедент LiveSubtitlesOverlay-тесты).
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class ConversationStatusOverlayTests: XCTestCase {

    private var overlay: ConversationStatusOverlay!

    override func setUp() async throws {
        try await super.setUp()
        overlay = ConversationStatusOverlay()
    }

    override func tearDown() async throws {
        overlay.hide()
        overlay = nil
        UserDefaults.standard.removeObject(forKey: "KrabEar_ConversationStatusHUDPosition")
        try await super.tearDown()
    }

    func test_panel_isFloating_andDraggable() {
        XCTAssertEqual(overlay._testPanelLevel, .floating)
        XCTAssertTrue(overlay._testPanelIsDraggable)
    }

    func test_update_setsStatusText() {
        overlay.update(state: .thinking)
        XCTAssertEqual(overlay._testStatusText, "🟡 Думает")
    }

    func test_update_interruptButton_visibleOnlyWhenSpeaking() {
        overlay.update(state: .speaking)
        XCTAssertFalse(overlay.interruptButton.isHidden)
        overlay.update(state: .listening)
        XCTAssertTrue(overlay.interruptButton.isHidden)
    }

    func test_showHide_togglesIsVisible() {
        overlay.show()
        XCTAssertTrue(overlay.isVisible)
        overlay.hide()
        XCTAssertFalse(overlay.isVisible)
    }

    func test_interruptButton_firesCallback() {
        var fired = 0
        overlay.onInterrupt = { fired += 1 }
        overlay.interruptButton.performClick(nil)
        XCTAssertEqual(fired, 1)
    }

    func test_pushLevel_updatesMeter_noCrash() {
        overlay.show()
        overlay.pushLevel(0.6)  // smoke: не падает, meter принимает значение
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `swift test --filter ConversationStatusOverlayTests`
Expected: FAIL компиляцией — класс не существует.

- [ ] **Step 3: Implement the overlay**

Создать `Sources/KrabEarAgent/ConversationStatusOverlay.swift`:

```swift
/*
 ConversationStatusOverlay — плавающий HUD статуса «Разговора с AI» (Волна 3c).

 Показывается, когда сессия активна, а главное окно не в фокусе/скрыто —
 пользователь видит «Слушает/Думает/Говорит» поверх любых приложений и может
 прервать ответ кнопкой, не возвращаясь в окно.

 Паттерн: NSPanel floating/non-activating/draggable, позиция в UserDefaults —
 1-в-1 как LiveSubtitlesOverlay (без SSE: данные пушит ConversationViewController
 напрямую через update(state:)/pushLevel(_:)).

 Глиф-гейт: статусные эмодзи берутся ИЗ ConversationState.localizedLabel
 (уже в кодовой базе) — новых non-ASCII глифов файл не вводит.
*/

import AppKit

@MainActor
final class ConversationStatusOverlay: NSObject {

    // MARK: - UI

    private let panel: NSPanel
    private let statusLabel = NSTextField(labelWithString: "⚪ Готов")
    private let levelMeter = MicLevelMeterView(frame: NSRect(x: 0, y: 0, width: 120, height: 18))
    let interruptButton = ThemeSecondaryButton(title: "Прервать", target: nil, action: nil)

    /// Колбэк кнопки «Прервать» — ConversationViewController подвязывает interruptAI().
    var onInterrupt: (() -> Void)?

    private(set) var isVisible = false
    private let positionKey = "KrabEar_ConversationStatusHUDPosition"

    // MARK: - Test hooks

    var _testPanelLevel: NSWindow.Level { panel.level }
    var _testPanelIsDraggable: Bool { panel.isMovableByWindowBackground }
    var _testStatusText: String { statusLabel.stringValue }

    // MARK: - Init

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 280, height: 64),
            styleMask: [.nonactivatingPanel, .hudWindow, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        super.init()
        setupPanel()
        restorePosition()
    }

    // MARK: - Public API

    func show() {
        panel.orderFront(nil)
        isVisible = true
    }

    func hide() {
        panel.orderOut(nil)
        isVisible = false
    }

    /// Обновить статус (вызывается из applyState VC при каждом изменении состояния).
    func update(state: ConversationState) {
        statusLabel.stringValue = state.localizedLabel
        interruptButton.isHidden = (state != .speaking)
    }

    /// Прокинуть нормализованный mic-уровень (из computeAndPushLevel VC).
    func pushLevel(_ normalized: CGFloat) {
        levelMeter.updateLevel(normalized)
    }

    // MARK: - Setup

    private func setupPanel() {
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isOpaque = false
        panel.alphaValue = 0.95

        statusLabel.font = KrabEarTheme.Typography.body
        statusLabel.textColor = KrabEarTheme.Colors.textPrimary
        statusLabel.isBordered = false
        statusLabel.drawsBackground = false

        interruptButton.target = self
        interruptButton.action = #selector(onInterruptTapped)
        interruptButton.isHidden = true

        levelMeter.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            levelMeter.widthAnchor.constraint(equalToConstant: 120),
            levelMeter.heightAnchor.constraint(equalToConstant: 18),
        ])

        let row = NSStackView(views: [statusLabel, levelMeter, interruptButton])
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.comfortable
        row.alignment = .centerY
        row.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.comfortable,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.comfortable,
            right: KrabEarTheme.Metrics.spacious
        )
        row.translatesAutoresizingMaskIntoConstraints = false

        let backdrop = NSVisualEffectView()
        backdrop.material = .popover
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.wantsLayer = true
        backdrop.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        backdrop.layer?.cornerCurve = .continuous
        backdrop.layer?.masksToBounds = true

        backdrop.addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: backdrop.topAnchor),
            row.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            row.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            row.bottomAnchor.constraint(equalTo: backdrop.bottomAnchor),
        ])

        backdrop.frame = panel.contentView!.bounds
        backdrop.autoresizingMask = [.width, .height]
        panel.contentView = backdrop

        let drag = NSPanGestureRecognizer(target: self, action: #selector(handleDrag(_:)))
        backdrop.addGestureRecognizer(drag)
    }

    @objc private func onInterruptTapped() {
        onInterrupt?()
    }

    // MARK: - Position persistence (паттерн LiveSubtitlesOverlay)

    private func placeTopRight() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        let x = vf.maxX - size.width - 24
        let y = vf.maxY - size.height - 24
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }

    private func restorePosition() {
        if let saved = UserDefaults.standard.string(forKey: positionKey),
           let data = saved.data(using: .utf8),
           let dict = try? JSONSerialization.jsonObject(with: data) as? [String: CGFloat],
           let x = dict["x"], let y = dict["y"] {
            let size = panel.frame.size
            panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: false)
        } else {
            placeTopRight()
        }
    }

    private func savePosition() {
        let origin = panel.frame.origin
        let dict: [String: CGFloat] = ["x": origin.x, "y": origin.y]
        if let data = try? JSONSerialization.data(withJSONObject: dict),
           let str = String(data: data, encoding: .utf8) {
            UserDefaults.standard.set(str, forKey: positionKey)
        }
    }

    @objc private func handleDrag(_ gr: NSPanGestureRecognizer) {
        if gr.state == .ended || gr.state == .changed {
            savePosition()
        }
    }
}
```

⚠️ `KrabEarTheme.Typography.body` взят по прецеденту `LiveSubtitlesOverlay.swift:348`; если компиляция не найдёт — grep реальные имена в `KrabEarTheme.swift`. Если `MicLevelMeterView` не публичен/не инстанцируется с frame — прочитать его init в `ConversationViewController+LevelMeter.swift:25` и подстроиться. Если `ThemeSecondaryButton(title:target:action:)` сигнатура иная — скопировать вызов из `ConversationViewController.swift:89`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `swift test --filter ConversationStatusOverlayTests`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add Sources/KrabEarAgent/ConversationStatusOverlay.swift Tests/KrabEarAgentTests/ConversationStatusOverlayTests.swift
git commit -m "feat(conversation): ConversationStatusOverlay — плавающий статус-HUD"
```

---

### Task 6: Проводка overlay в ConversationViewController

**Files:**
- Modify: `Sources/KrabEarAgent/ConversationViewController.swift`
- Modify: `Sources/KrabEarAgent/ConversationViewController+Audio.swift` (level feed)
- Test: `Tests/KrabEarAgentTests/ConversationStatusOverlayTests.swift` (append)

- [ ] **Step 1: Write the failing tests**

Дописать в `ConversationStatusOverlayTests.swift`:

```swift
// MARK: - Task 6: проводка в ConversationViewController

@MainActor
final class ConversationOverlayWiringTests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
    }

    override func tearDown() async throws {
        vc.statusOverlay?.hide()
        vc.interruptFallbackTimer?.invalidate()
        vc = nil
        try await super.tearDown()
    }

    func test_shouldShowOverlay_matrix() {
        XCTAssertTrue(ConversationViewController.shouldShowOverlay(sessionActive: true,  windowIsKey: false))
        XCTAssertFalse(ConversationViewController.shouldShowOverlay(sessionActive: true,  windowIsKey: true))
        XCTAssertFalse(ConversationViewController.shouldShowOverlay(sessionActive: false, windowIsKey: false))
        XCTAssertFalse(ConversationViewController.shouldShowOverlay(sessionActive: false, windowIsKey: true))
    }

    func test_applyState_updatesOverlayText() {
        let overlay = ConversationStatusOverlay()
        vc.statusOverlay = overlay
        vc.conversationState = .speaking
        XCTAssertEqual(overlay._testStatusText, "🔴 Говорит")
    }

    func test_overlayInterrupt_wiredTo_interruptAI() {
        vc.isSessionActive = true
        vc.conversationState = .speaking
        vc.ensureStatusOverlay()
        vc.statusOverlay?.onInterrupt?()
        // interruptAI НЕ переключает состояние сам (Task 2) — но взводит fallback-таймер.
        XCTAssertNotNil(vc.interruptFallbackTimer,
                        "onInterrupt overlay должен вести в interruptAI()")
    }

    func test_computeAndPushLevel_feedsOverlay_noCrash() {
        let overlay = ConversationStatusOverlay()
        vc.statusOverlay = overlay
        vc.computeAndPushLevel([0.4, 0.5, 0.6])  // smoke: пуш в meter overlay не падает
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `swift test --filter ConversationOverlayWiringTests`
Expected: FAIL компиляцией — нет `statusOverlay`/`shouldShowOverlay`/`ensureStatusOverlay`.

- [ ] **Step 3: Implement — свойство, чистая функция видимости, lifecycle-хуки**

В `ConversationViewController.swift`:

(а) stored-свойство:

```swift
    /// Плавающий статус-HUD (Волна 3c). Создаётся лениво при старте сессии.
    var statusOverlay: ConversationStatusOverlay?
    /// Токены наблюдателей фокуса окна (живут до конца жизни VC — таб постоянный).
    private var windowFocusObservers: [NSObjectProtocol] = []
```

(б) чистая функция видимости + ensure + пересчёт:

```swift
    /// Правило видимости HUD: сессия активна И окно не в фокусе.
    static func shouldShowOverlay(sessionActive: Bool, windowIsKey: Bool) -> Bool {
        sessionActive && !windowIsKey
    }

    /// Создать overlay при первом обращении и подвязать кнопку «Прервать».
    func ensureStatusOverlay() {
        guard statusOverlay == nil else { return }
        let overlay = ConversationStatusOverlay()
        overlay.onInterrupt = { [weak self] in self?.interruptAI() }
        statusOverlay = overlay
    }

    /// Пересчитать видимость HUD (вызывается из start/stop, applyState и фокус-наблюдателей).
    func updateOverlayVisibility() {
        guard let overlay = statusOverlay else { return }
        let windowIsKey = view.window?.isKeyWindow ?? false
        if ConversationViewController.shouldShowOverlay(sessionActive: isSessionActive, windowIsKey: windowIsKey) {
            if !overlay.isVisible { overlay.show() }
        } else {
            if overlay.isVisible { overlay.hide() }
        }
    }
```

(в) в `viewDidLoad()` — наблюдатели фокуса (один раз; VC живёт всю жизнь приложения, teardown не нужен — задокументировать):

```swift
        // Волна 3c: HUD показывается, когда окно теряет фокус во время сессии.
        // VC живёт всю жизнь приложения (постоянный таб) — наблюдатели не снимаем.
        let nc = NotificationCenter.default
        for name in [NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification] {
            windowFocusObservers.append(
                nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                    Task { @MainActor in self?.updateOverlayVisibility() }
                }
            )
        }
```

(г) в `startConversation()` после `isSessionActive = true`:

```swift
        ensureStatusOverlay()
        updateOverlayVisibility()
```

(д) в `stopConversation()` после `conversationState = .idle`:

```swift
        updateOverlayVisibility()
```

(е) в `applyState(_:)` первой строкой после `statusLabel.stringValue = ...`:

```swift
        statusOverlay?.update(state: state)
```

- [ ] **Step 4: Implement — level feed**

В `ConversationViewController+LevelMeter.swift`, в `computeAndPushLevel` после `micLevelMeter?.updateLevel(normalized)`:

```swift
        statusOverlay?.pushLevel(normalized)
```

(Файл feed'а — `+LevelMeter.swift`, НЕ `+Audio.swift`: `computeAndPushLevel` живёт там; поправить File map при коммите не нужно, это уточнение.)

- [ ] **Step 5: Run tests + build**

Run: `swift test --filter "ConversationStatusOverlayTests|ConversationOverlayWiringTests"`
Expected: 10 passed.
Run: `swift build -c release`
Expected: Build complete.

- [ ] **Step 6: Commit**

```bash
git add Sources/KrabEarAgent/ConversationViewController.swift Sources/KrabEarAgent/ConversationViewController+LevelMeter.swift Tests/KrabEarAgentTests/ConversationStatusOverlayTests.swift
git commit -m "feat(conversation): overlay-проводка — lifecycle, фокус-наблюдатели, level-feed, interrupt"
```

---

### Task 7: Wake-поллер — регрессионный source-contract тест

**Files:**
- Create: `Tests/KrabEarAgentTests/WakeWordConversationWiringTests.swift`

Поведение НЕ меняется (разведка подтвердила корректность) — только страховка от класса «test-validates-the-hole».

- [ ] **Step 1: Write the tests (они должны сразу ПРОЙТИ — это пин существующего поведения)**

Создать `Tests/KrabEarAgentTests/WakeWordConversationWiringTests.swift`:

```swift
/*
 WakeWordConversationWiringTests — Волна 3c, секция 6 спеки.

 Пин существующей (корректной) проводки паузы wake-поллера вокруг
 conversation-lifecycle. Класс «test-validates-the-hole» дважды кусал проект
 (setupErrorBus, setupHealthMonitor — оба были определены, но не вызваны).
 Эти тесты грепают РЕАЛЬНЫЙ source, чтобы рефакторинг, молча выронивший вызов
 или одну из двух подписок, упал в CI.
*/

import XCTest
@testable import KrabEarAgent

final class WakeWordConversationWiringTests: XCTestCase {

    // MARK: 1. setupWakeWordConversationObservers реально ВЫЗЫВАЕТСЯ (не только определён)

    func test_setupWakeWordConversationObservers_is_actually_called() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        let callSites = src.components(separatedBy: "setupWakeWordConversationObservers()").count - 1
        // ≥2 вхождения: определение (func ...) даёт 1, вызов — ещё ≥1.
        XCTAssertGreaterThanOrEqual(callSites, 2,
            "setupWakeWordConversationObservers() должен быть и определён, и вызван в main.swift")
    }

    // MARK: 2. Обе подписки живы и дергают правильные методы

    func test_conversationStarted_pausesPoller() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains(".krabConversationStarted"),
                      "подписка на .krabConversationStarted обязана существовать")
        XCTAssertTrue(src.contains("pause(.conversation)"),
                      "обработчик started обязан вызывать pause(.conversation)")
    }

    func test_conversationStopped_resumesPoller() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains(".krabConversationStopped"),
                      "подписка на .krabConversationStopped обязана существовать")
        XCTAssertTrue(src.contains("resume(.conversation)"),
                      "обработчик stopped обязан вызывать resume(.conversation)")
    }

    // MARK: 3. Обе нотификации реально постятся из единой воронки start/stop

    func test_conversationVC_posts_bothNotifications() throws {
        let src = try String(contentsOf: Self.vcSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains("post(name: .krabConversationStarted"),
                      "startConversation обязан постить .krabConversationStarted")
        XCTAssertTrue(src.contains("post(name: .krabConversationStopped"),
                      "stopConversation обязан постить .krabConversationStopped")
    }

    // MARK: - Source URLs (#filePath walk-up, паттерн MainErrorsWiringTests)

    private static var sourcesDir: URL {
        var url = URL(fileURLWithPath: #filePath)  // .../Tests/KrabEarAgentTests/<файл>
        url.deleteLastPathComponent()              // Tests/KrabEarAgentTests
        url.deleteLastPathComponent()              // Tests
        url.deleteLastPathComponent()              // native/KrabEarAgent
        return url.appendingPathComponent("Sources/KrabEarAgent")
    }

    private static var mainSwiftURL: URL { sourcesDir.appendingPathComponent("main.swift") }
    private static var vcSwiftURL: URL { sourcesDir.appendingPathComponent("ConversationViewController.swift") }
}
```

- [ ] **Step 2: Run tests — verify they PASS immediately**

Run: `swift test --filter WakeWordConversationWiringTests`
Expected: 4 passed (пин существующего поведения). Если какой-то упал — СТОП: либо проводка реально сломана (эскалировать координатору, НЕ чинить молча), либо грep-строка не совпала с реальным кодом (открыть `main.swift:551-563` и подогнать строку под реальность).

- [ ] **Step 3: Commit**

```bash
git add Tests/KrabEarAgentTests/WakeWordConversationWiringTests.swift
git commit -m "test(wake-word): source-contract пин паузы поллера вокруг conversation lifecycle"
```

---

### Task 8: Полный прогон + release-сборка

**Files:** нет новых — финальная верификация.

- [ ] **Step 1: Full test suite**

Run: `swift test`
Expected: 0 failures (база была 1122 passed / 6 skipped; станет ~1150+). Известный флейк: `LaunchAgentManager` (реальные launchctl I/O) — при единичном падении перепрогнать, зелёный повтор = ок.

- [ ] **Step 2: Release build**

Run: `swift build -c release`
Expected: Build complete. Допустимы ТОЛЬКО pre-existing warnings (`BackendSupervisor.swift` NSLock) — новых warnings от наших файлов быть не должно.

- [ ] **Step 3: Глиф-гейт самопроверка**

Run: `git diff codex/krab-ear-v2...HEAD -- 'Sources/**' | grep '^+' | grep -oP '[^\x00-\x7F«»—…]' | sort -u`
Expected: только символы, уже встречающиеся в кодовой базе (⚪🟡🟢🔴 из localizedLabel). Новых непроверенных глифов нет.

- [ ] **Step 4: Commit (если были правки) и финальный отчёт**

Отчитаться координатору: число тестов, статус сборки, SHA последнего коммита.

---

## Вне плана (координатор делает сам после мержа)

- PR + CI + merge + `build_and_deploy.command` + `launchctl kickstart` + parity-коммит бинарей.
- VG-сторона (resume-on-false-positive) — параллельная сессия по брифу `docs/design-briefs/2026-07-09-vg-barge-in-resume.md`.
- Живой кросс-репо e2e (реальный кашель/речь поверх ответа, kill VG-процесса → озвучка ошибки) + голосовой смок с владельцем.
- Обновление `docs/ROADMAP-2026H2.md` + релиз v2.8.0.
