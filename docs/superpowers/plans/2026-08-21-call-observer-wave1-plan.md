# Call Observer Wave 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** macOS observer client for Voice Gateway agent calls: auto-appearing HUD → expandable panel, live two-sided transcript with translation, live audio listen, hangup button.

**Architecture:** Swift-only client of VG's verified WS/REST contract (spec `docs/superpowers/specs/2026-08-21-call-observer-wave1-design.md` — READ IT FIRST, it is the source of truth for every behavior below). Python diff is exactly 2 settings keys; the VG credential comes from the EXISTING `get_voice_gateway_credential` IPC (W1892), cached in memory. Call-end lifecycle is a ONE-SHOT automaton keyed by per-session observation generations (spec §4.1).

**Tech Stack:** Swift 6 / AppKit / URLSessionWebSocketTask / AVAudioEngine / GCD; Python 3 (token writer); flask + flask-sock (fake VG for e2e).

## Global Constraints

- Base branch: `feat/call-observer-w1` (already exists, spec committed). Every task commits there.
- VG base URL + api key ONLY via the dedicated IPC `get_voice_gateway_credential` (W1892, `settings_service.py:393`; general `get_settings` redacts the key), cached in memory; `tokenProvider` reads the cache.
- All network and IPC strictly off-main; UI mutations on main (AGENT-3 rule).
- No `runModal` — only `presentAlertSheet`/`presentPanelSheet` from `AlertHelpers.swift`.
- No emoji/Unicode glyph buttons — SF Symbols `speaker.wave.2` / `phone.down.fill` (CoreText hang class AGENT-J/M). Any NEW non-ASCII glyph in Swift string literals must already exist in `native/` (glyph gate).
- Event-type dispatch: EXACT string match, never prefix.
- Swift build gate: `cd native/KrabEarAgent && swift build -c release` must stay green after every task; Swift tests: `swift test --filter <TestClass>`.
- Python gate: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/<file> -v -p no:cacheprovider`; ubuntu-parity for changed test files: `scripts/pre_merge_py312_check.sh <files>`; `make audit-all` after backend changes.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `KrabEar/core/config.py` (modify ~:879) | +2 bool keys in `DEFAULT_SETTINGS` |
| `native/.../VGCallEvent.swift` (new) | typed decoder of VG stream events (exact-match dispatch) |
| `native/.../MuLawDecoder.swift` (new) | G.711 μ-law → PCM16/Float LUT |
| `native/.../VGWebSocketConnection.swift` (new) | shared WS transport: Bearer, backoff, ping-timer lifecycle, generation stamp |
| `native/.../VGSessionWatcher.swift` (new) | poll FSM: predicate, streaks, vgLost, resurrection, auth-reject |
| `native/.../VGCallStreamClient.swift` (new) | events WS on top of the transport; call.closed → permanent stop |
| `native/.../CallAudioPlayer.swift` (new) | monitor WS + AVAudioEngine playback; single-flight listen state |
| `native/.../CallObserverCoordinator.swift` (new) | §4.1 automaton, per-session generations, selection, privacy, cost poll |
| `native/.../CallObserverHUD.swift` (new) | floating NSPanel |
| `native/.../CallObserverPanelController.swift` (new) | full transcript window |
| `native/.../main+CallObserver.swift` (new) | AgentAppDelegate wiring + status-menu item |
| `native/.../HistoryPanelController+LiveSubsSettings.swift` (modify) | 2 checkboxes |
| `scripts/fake_vg_server.py` (new) | scripted fake VG (sessions/stream/monitor/diagnostics/hangup) |
| `scripts/e2e_call_observer_smoke.command` (new) | fake server + integration XCTest run |

Swift sources dir: `native/KrabEarAgent/Sources/KrabEarAgent/`; tests dir: `native/KrabEarAgent/Tests/KrabEarAgentTests/` (target `KrabEarAgentTests`, auto-globbed by SPM — no Package.swift edit needed).

---

### Task 1: Python — the two settings keys

**Files:**
- Modify: `KrabEar/core/config.py` (after the line `"voice_gateway_api_key": "",` ~:879)
- Test: `KrabEar/tests/test_call_observer_settings_w1.py`

**Interfaces:**
- Produces: `DEFAULT_SETTINGS["call_observer_hud_enabled"] = True`, `DEFAULT_SETTINGS["call_observer_autoplay_audio"] = False` — read by Swift via `get_settings` (booleans are not sensitive). The VG credential channel is NOT built here: it already exists (`get_voice_gateway_credential`, W1892, `settings_service.py:393`, dispatch `service.py:2730`, live consumer `HistoryPanelController+VoiceTab.swift`) and is consumed by Swift Task 8. An earlier draft invented a `vg_client_token` file — rejected by plan review as a rebuild of W1892.

- [ ] **Step 1: Write the failing test**

```python
"""Call Observer w1: два ключа настроек наблюдателя звонков."""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import DEFAULT_SETTINGS


class CallObserverSettingsTest(unittest.TestCase):
    def test_default_settings_keys(self):
        self.assertIs(DEFAULT_SETTINGS["call_observer_hud_enabled"], True)
        self.assertIs(DEFAULT_SETTINGS["call_observer_autoplay_audio"], False)

    def test_keys_are_not_sensitive(self):
        """Булы обязаны доходить до Swift через get_settings нередактированными."""
        from backend.settings_backup import SENSITIVE_FIELDS
        self.assertNotIn("call_observer_hud_enabled", SENSITIVE_FIELDS)
        self.assertNotIn("call_observer_autoplay_audio", SENSITIVE_FIELDS)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — verify FAIL**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_call_observer_settings_w1.py -v -p no:cacheprovider`
Expected: FAIL — `KeyError: 'call_observer_hud_enabled'`

- [ ] **Step 3: Add the two keys**

In `KrabEar/core/config.py`, directly after the line `"voice_gateway_api_key": "",`:

```python
    "call_observer_hud_enabled": True,
    "call_observer_autoplay_audio": False,
```

- [ ] **Step 4: Run — verify PASS** (same command → 2 passed)

- [ ] **Step 5: Guards + commit**

Run: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_call_observer_settings_w1.py` → PASS.
Run: `make audit-all` → green.

```bash
git add KrabEar/core/config.py KrabEar/tests/test_call_observer_settings_w1.py
git commit -m "feat(call-observer): 2 ключа настроек наблюдателя звонков (w1 T1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```


---

### Task 2: Swift — VGCallEvent decoder

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/VGCallEvent.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/VGCallEventTests.swift`

**Interfaces:**
- Produces: `enum VGCallEvent: Equatable` with cases `sttFinal(text:language:confidence:)`, `translationFinal(text:sourceText:srcLang:tgtLang:)`, `agentResponse(text:textRu:utteranceTs:action:)`, `agentAutoSpoken(text:textRu:action:digits:)`, `agentInterrupted(utteranceTs:spokenFraction:spokenText:)`, `callState(status:muted:held:)`, `callRinging`, `callAnswered`, `callEnded(reason:)`, `callClosed`, `diagnosticError(message:)`, `screeningStarted`, `costAlert(level:currentUsd:message:)`, `ignored(type:)`; `static func decode(_ data: Data) -> VGCallEvent?` (nil = not an event envelope, e.g. `pong` without `type`... a `pong` HAS `type:"pong"` → `.ignored`; nil only for non-JSON/malformed).

- [ ] **Step 1: Write the failing tests** — fixtures VERBATIM from VG publish sites (spec §2.2):

```swift
import XCTest
@testable import KrabEarAgent

final class VGCallEventTests: XCTestCase {
    private func decode(_ json: String) -> VGCallEvent? {
        VGCallEvent.decode(json.data(using: .utf8)!)
    }

    func test_sttFinal_full_shape() {
        let e = decode(#"{"type":"stt.final","ts":"2026-08-21T10:00:00Z","data":{"text":"hola","engine":"gigaam","confidence":0.91,"duration_ms":900,"language":"es"}}"#)
        XCTAssertEqual(e, .sttFinal(text: "hola", language: "es", confidence: 0.91))
    }

    func test_sttFinal_takeover_shape_text_language_only() {
        let e = decode(#"{"type":"stt.final","ts":"t","data":{"text":"si","language":"es"}}"#)
        XCTAssertEqual(e, .sttFinal(text: "si", language: "es", confidence: nil))
    }

    func test_sttFinal_realtime_shape_text_only() {
        let e = decode(#"{"type":"stt.final","ts":"t","data":{"text":"ok"}}"#)
        XCTAssertEqual(e, .sttFinal(text: "ok", language: nil, confidence: nil))
    }

    func test_translationFinal_with_provider() {
        let e = decode(#"{"type":"translation.final","ts":"t","data":{"text":"привет","source_text":"hola","src_lang":"es","tgt_lang":"ru","provider":"argos"}}"#)
        XCTAssertEqual(e, .translationFinal(text: "привет", sourceText: "hola", srcLang: "es", tgtLang: "ru"))
    }

    func test_agentResponse_full_and_minimal_realtime() {
        let full = decode(#"{"type":"agent.response","ts":"t","data":{"text":"Claro","text_ru":"Конечно","action":"continue","goal_reached":false,"summary":"","role":"assistant","lang":"es","utterance_ts":"u1"}}"#)
        XCTAssertEqual(full, .agentResponse(text: "Claro", textRu: "Конечно", utteranceTs: "u1", action: "continue"))
        let minimal = decode(#"{"type":"agent.response","ts":"t","data":{"text":"Si","utterance_ts":"u2","role":"assistant","lang":"es"}}"#)
        XCTAssertEqual(minimal, .agentResponse(text: "Si", textRu: nil, utteranceTs: "u2", action: nil))
    }

    func test_agentAutoSpoken() {
        let e = decode(#"{"type":"agent.suggestion.auto_spoken","ts":"t","data":{"text":"Uno","text_ru":"Один","action":"dtmf","digits":"1","goal_reached":false,"summary":"","result":""}}"#)
        XCTAssertEqual(e, .agentAutoSpoken(text: "Uno", textRu: "Один", action: "dtmf", digits: "1"))
    }

    func test_agentInterrupted_spoken_prefix() {
        let e = decode(#"{"type":"agent.interrupted","ts":"t","data":{"utterance_ts":"u1","spoken_fraction":0.42,"spoken_text":"Claro, ahora"}}"#)
        XCTAssertEqual(e, .agentInterrupted(utteranceTs: "u1", spokenFraction: 0.42, spokenText: "Claro, ahora"))
    }

    func test_callState_with_and_without_mute_hold() {
        XCTAssertEqual(decode(#"{"type":"call.state","ts":"t","data":{"session_id":"s","status":"running"}}"#),
                       .callState(status: "running", muted: nil, held: nil))
        XCTAssertEqual(decode(#"{"type":"call.state","ts":"t","data":{"status":"paused","muted":false,"held":true}}"#),
                       .callState(status: "paused", muted: false, held: true))
    }

    func test_callRinging_has_no_status_field() {
        XCTAssertEqual(decode(#"{"type":"call.ringing","ts":"t","data":{"call_sid":"CA1","twilio_status":"ringing","provider":"twilio"}}"#), .callRinging)
    }

    func test_callEnded_webhook_optional_fields() {
        XCTAssertEqual(decode(#"{"type":"call.ended","ts":"t","data":{"reason":"hangup","provider":"twilio","call_sid":"CA1","duration_seconds":63,"twilio_status":"completed"}}"#),
                       .callEnded(reason: "hangup"))
    }

    func test_callClosed() {
        XCTAssertEqual(decode(#"{"type":"call.closed","ts":"t","data":{"session_id":"s"}}"#), .callClosed)
    }

    func test_costAlert() {
        XCTAssertEqual(decode(#"{"type":"cost.alert","ts":"t","data":{"level":"session","threshold_usd":1.0,"current_usd":1.05,"message":"m"}}"#),
                       .costAlert(level: "session", currentUsd: 1.05, message: "m"))
    }

    func test_exact_match_prefix_trap() {
        // Игнорируемый agent.suggestion НЕ должен матчиться как auto_spoken.
        XCTAssertEqual(decode(#"{"type":"agent.suggestion","ts":"t","data":{"text":"x"}}"#), .ignored(type: "agent.suggestion"))
    }

    func test_unknown_type_ignored_and_pong_without_ts() {
        XCTAssertEqual(decode(#"{"type":"diagnostic.status","ts":"t","data":{}}"#), .ignored(type: "diagnostic.status"))
        XCTAssertEqual(decode(#"{"type":"pong"}"#), .ignored(type: "pong"))
    }

    func test_malformed_returns_nil() {
        XCTAssertNil(decode("not json"))
        XCTAssertNil(decode(#"{"no_type":1}"#))
    }
}
```

- [ ] **Step 2: Run — verify FAIL**

Run: `cd native/KrabEarAgent && swift test --filter VGCallEventTests`
Expected: compile error — `VGCallEvent` not found.

- [ ] **Step 3: Implement `VGCallEvent.swift`**

```swift
import Foundation

/// Событие realtime-канала VG `/v1/sessions/{id}/stream` (spec §2.2).
/// Диспетчеризация — ТОЧНОЕ совпадение type (prefix-матч съел бы
/// auto_spoken игнорируемым agent.suggestion). Все поля кроме text
/// опциональны: у VG до 4 publish-сайтов на событие с разными наборами.
enum VGCallEvent: Equatable {
    case sttFinal(text: String, language: String?, confidence: Double?)
    case translationFinal(text: String, sourceText: String?, srcLang: String?, tgtLang: String?)
    case agentResponse(text: String, textRu: String?, utteranceTs: String?, action: String?)
    case agentAutoSpoken(text: String, textRu: String?, action: String?, digits: String?)
    case agentInterrupted(utteranceTs: String?, spokenFraction: Double?, spokenText: String?)
    case callState(status: String, muted: Bool?, held: Bool?)
    case callRinging
    case callAnswered
    case callEnded(reason: String?)
    case callClosed
    case diagnosticError(message: String?)
    case screeningStarted
    case costAlert(level: String?, currentUsd: Double?, message: String?)
    case ignored(type: String)

    static func decode(_ data: Data) -> VGCallEvent? {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let type = obj["type"] as? String else { return nil }
        let d = obj["data"] as? [String: Any] ?? [:]
        func s(_ k: String) -> String? { d[k] as? String }
        func dbl(_ k: String) -> Double? {
            if let v = d[k] as? Double { return v }
            if let v = d[k] as? Int { return Double(v) }
            return nil
        }
        switch type {
        case "stt.final":
            guard let text = s("text") else { return .ignored(type: type) }
            return .sttFinal(text: text, language: s("language"), confidence: dbl("confidence"))
        case "translation.final":
            guard let text = s("text") else { return .ignored(type: type) }
            return .translationFinal(text: text, sourceText: s("source_text"),
                                     srcLang: s("src_lang"), tgtLang: s("tgt_lang"))
        case "agent.response":
            guard let text = s("text") else { return .ignored(type: type) }
            return .agentResponse(text: text, textRu: s("text_ru"),
                                  utteranceTs: s("utterance_ts"), action: s("action"))
        case "agent.suggestion.auto_spoken":
            guard let text = s("text") else { return .ignored(type: type) }
            return .agentAutoSpoken(text: text, textRu: s("text_ru"),
                                    action: s("action"), digits: s("digits"))
        case "agent.interrupted":
            return .agentInterrupted(utteranceTs: s("utterance_ts"),
                                     spokenFraction: dbl("spoken_fraction"),
                                     spokenText: s("spoken_text"))
        case "call.state":
            return .callState(status: s("status") ?? "",
                              muted: d["muted"] as? Bool, held: d["held"] as? Bool)
        case "call.ringing": return .callRinging
        case "call.answered": return .callAnswered
        case "call.ended": return .callEnded(reason: s("reason"))
        case "call.closed": return .callClosed
        case "diagnostic.error": return .diagnosticError(message: s("message") ?? s("detail"))
        case "screening.started": return .screeningStarted
        case "cost.alert":
            return .costAlert(level: s("level"), currentUsd: dbl("current_usd"), message: s("message"))
        default:
            return .ignored(type: type)
        }
    }
}
```

- [ ] **Step 4: Run — verify PASS**

Run: `cd native/KrabEarAgent && swift test --filter VGCallEventTests`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/VGCallEvent.swift native/KrabEarAgent/Tests/KrabEarAgentTests/VGCallEventTests.swift
git commit -m "feat(call-observer): типизированный декодер событий VG, exact-match dispatch (w1 T2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Swift — MuLawDecoder

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/MuLawDecoder.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/MuLawDecoderTests.swift`

**Interfaces:**
- Produces: `enum MuLawDecoder` with `static let table: [Int16]` (256 entries), `static func decode(_ data: Data) -> [Int16]`, `static func decodeToFloat(_ data: Data) -> [Float]`.

- [ ] **Step 1: Write the failing tests** (golden vectors of G.711 μ-law):

```swift
import XCTest
@testable import KrabEarAgent

final class MuLawDecoderTests: XCTestCase {
    func test_golden_vectors() {
        XCTAssertEqual(MuLawDecoder.table[0x00], -32124)
        XCTAssertEqual(MuLawDecoder.table[0x80], 32124)
        XCTAssertEqual(MuLawDecoder.table[0xFF], 0)
        XCTAssertEqual(MuLawDecoder.table[0x7F], 0)
        XCTAssertEqual(MuLawDecoder.table[0xE0], 372)
        XCTAssertEqual(MuLawDecoder.table[0x60], -372)
    }

    func test_table_is_antisymmetric() {
        for b in 0...127 {
            XCTAssertEqual(MuLawDecoder.table[b], -MuLawDecoder.table[b | 0x80],
                           "byte \(b) vs \(b | 0x80)")
        }
    }

    func test_decode_frame_and_float_range() {
        let frame = Data([0x00, 0xFF, 0x80])
        XCTAssertEqual(MuLawDecoder.decode(frame), [-32124, 0, 32124])
        let floats = MuLawDecoder.decodeToFloat(frame)
        XCTAssertEqual(floats.count, 3)
        XCTAssertEqual(floats[1], 0.0)
        XCTAssertTrue(floats.allSatisfy { $0 >= -1.0 && $0 <= 1.0 })
    }
}
```

- [ ] **Step 2: Run — verify FAIL** (`swift test --filter MuLawDecoderTests` → compile error)

- [ ] **Step 3: Implement `MuLawDecoder.swift`**

```swift
import Foundation

/// G.711 μ-law → PCM16 (монитор-аудио VG: mulaw_8k, кадры 100мс = 800 байт).
/// Таблица считается один раз при первом обращении.
enum MuLawDecoder {
    static let table: [Int16] = (0...255).map { byte in
        let u = ~UInt8(byte)
        let isNegative = (u & 0x80) != 0
        let exponent = Int((u >> 4) & 0x07)
        let mantissa = Int(u & 0x0F)
        let magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
        return Int16(clamping: isNegative ? -magnitude : magnitude)
    }

    static func decode(_ data: Data) -> [Int16] {
        data.map { table[Int($0)] }
    }

    static func decodeToFloat(_ data: Data) -> [Float] {
        data.map { Float(table[Int($0)]) / 32768.0 }
    }
}
```

- [ ] **Step 4: Run — verify PASS** (`swift test --filter MuLawDecoderTests`)

- [ ] **Step 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/MuLawDecoder.swift native/KrabEarAgent/Tests/KrabEarAgentTests/MuLawDecoderTests.swift
git commit -m "feat(call-observer): G.711 μ-law LUT-декодер с golden-векторами (w1 T3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Swift — VGWebSocketConnection (shared WS transport)

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/VGWebSocketConnection.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/VGWebSocketConnectionTests.swift`

**Interfaces:**
- Produces: `protocol VGWebSocketConnecting: AnyObject { func connect(); func permanentStop() }`; `final class VGWebSocketConnection: NSObject, VGWebSocketConnecting` with `init(url: URL, generation: UInt64, autoReconnect: Bool, tokenProvider: @escaping () -> String, onMessage: @escaping (VGWebSocketConnection.Message, UInt64) -> Void, onStateChange: ((Bool, UInt64) -> Void)?, onClose: ((Int, UInt64) -> Void)?)`; `enum Message { case text(String); case binary(Data) }`; pure helpers `static func backoffBounds(attempt: Int) -> (min: Double, max: Double)` and `static func makeRequest(url: URL, token: String) -> URLRequest`; `static func wsURL(httpBase: URL, path: String) -> URL?`.
- Callbacks fire on the connection's own serial queue — consumers re-dispatch to main themselves.

- [ ] **Step 1: Write the failing tests** (pure helpers — the socket itself is exercised by the T10 e2e):

```swift
import XCTest
@testable import KrabEarAgent

final class VGWebSocketConnectionTests: XCTestCase {
    func test_backoff_bounds_exponential_capped_with_jitter_band() {
        let b0 = VGWebSocketConnection.backoffBounds(attempt: 0)
        XCTAssertEqual(b0.min, 0.75, accuracy: 0.001)   // 1s −25%
        XCTAssertEqual(b0.max, 1.25, accuracy: 0.001)   // 1s +25%
        let b5 = VGWebSocketConnection.backoffBounds(attempt: 5)
        XCTAssertEqual(b5.min, 22.5, accuracy: 0.001)   // 30s cap −25%
        XCTAssertEqual(b5.max, 37.5, accuracy: 0.001)
        let b99 = VGWebSocketConnection.backoffBounds(attempt: 99)
        XCTAssertEqual(b99.max, 37.5, accuracy: 0.001)  // cap держится
    }

    func test_request_carries_bearer_only_when_token_nonempty() {
        let url = URL(string: "ws://127.0.0.1:8090/v1/sessions/s1/stream")!
        let with = VGWebSocketConnection.makeRequest(url: url, token: "sek")
        XCTAssertEqual(with.value(forHTTPHeaderField: "Authorization"), "Bearer sek")
        let without = VGWebSocketConnection.makeRequest(url: url, token: "")
        XCTAssertNil(without.value(forHTTPHeaderField: "Authorization"))
    }

    func test_wsURL_scheme_swap() {
        let base = URL(string: "http://127.0.0.1:8090")!
        XCTAssertEqual(VGWebSocketConnection.wsURL(httpBase: base, path: "/v1/sessions/a b/stream")?.absoluteString,
                       "ws://127.0.0.1:8090/v1/sessions/a%20b/stream")
        let https = URL(string: "https://vg.local")!
        XCTAssertEqual(VGWebSocketConnection.wsURL(httpBase: https, path: "/x")?.scheme, "wss")
    }

    func test_permanentStop_prevents_reconnect_flag() {
        let conn = VGWebSocketConnection(
            url: URL(string: "ws://127.0.0.1:1/dead")!, generation: 7,
            autoReconnect: true, tokenProvider: { "" },
            onMessage: { _, _ in }, onStateChange: nil, onClose: nil)
        conn.permanentStop()
        let exp = expectation(description: "queue drained")
        conn.testHook_onQueue { XCTAssertTrue(conn.testHook_isStopped); exp.fulfill() }
        wait(for: [exp], timeout: 2)
    }
}
```

- [ ] **Step 2: Run — verify FAIL** (`swift test --filter VGWebSocketConnectionTests` → compile error)

- [ ] **Step 3: Implement `VGWebSocketConnection.swift`**

```swift
import Foundation

protocol VGWebSocketConnecting: AnyObject {
    func connect()
    func permanentStop()
}

/// Общий WS-транспорт двух клиентов VG (events-stream + audio-monitor).
/// Bearer-заголовок из tokenProvider (кэш креденшела W1892, читается на
/// каждом коннекте), exp backoff 1→30с ±25% джиттера, ping каждые 25с.
/// Каждое сообщение доставляется со generation-штампом; отмена ВСЕГДА
/// инвалидирует ping-таймер (§4 спеки: таймеры не копятся между реконнектами).
final class VGWebSocketConnection: NSObject, VGWebSocketConnecting {
    enum Message { case text(String); case binary(Data) }

    private let url: URL
    let generation: UInt64
    private let autoReconnect: Bool
    private let tokenProvider: () -> String
    private let onMessage: (Message, UInt64) -> Void
    private let onStateChange: ((Bool, UInt64) -> Void)?
    private let onClose: ((Int, UInt64) -> Void)?

    private let queue = DispatchQueue(label: "krab.vg.ws")
    private lazy var session = URLSession(configuration: .ephemeral)
    private var task: URLSessionWebSocketTask?
    private var pingTimer: DispatchSourceTimer?
    private var reconnectAttempt = 0
    private var stopped = false

    init(url: URL, generation: UInt64, autoReconnect: Bool,
         tokenProvider: @escaping () -> String,
         onMessage: @escaping (Message, UInt64) -> Void,
         onStateChange: ((Bool, UInt64) -> Void)?,
         onClose: ((Int, UInt64) -> Void)?) {
        self.url = url
        self.generation = generation
        self.autoReconnect = autoReconnect
        self.tokenProvider = tokenProvider
        self.onMessage = onMessage
        self.onStateChange = onStateChange
        self.onClose = onClose
        super.init()
    }

    static func backoffBounds(attempt: Int) -> (min: Double, max: Double) {
        let base = Swift.min(30.0, pow(2.0, Double(Swift.min(attempt, 30))))
        return (base * 0.75, base * 1.25)
    }

    static func makeRequest(url: URL, token: String) -> URLRequest {
        var req = URLRequest(url: url)
        if !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return req
    }

    static func wsURL(httpBase: URL, path: String) -> URL? {
        guard var comps = URLComponents(url: httpBase, resolvingAgainstBaseURL: false) else { return nil }
        comps.scheme = (comps.scheme == "https") ? "wss" : "ws"
        comps.path = path
        return comps.url
    }

    func connect() { queue.async { self.openLocked() } }

    /// Терминал поколения / call.closed: больше НИКОГДА не реконнектится.
    func permanentStop() {
        queue.async {
            self.stopped = true
            self.teardownLocked()
        }
    }

    private func openLocked() {
        guard !stopped else { return }
        teardownLocked()
        let req = Self.makeRequest(url: url, token: tokenProvider())
        let t = session.webSocketTask(with: req)
        task = t
        t.resume()
        startPingLocked(for: t)
        onStateChange?(true, generation)
        receiveLoop(t)
    }

    private func receiveLoop(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard t === self.task, !self.stopped else { return }
                switch result {
                case .success(let msg):
                    self.reconnectAttempt = 0
                    switch msg {
                    case .string(let s): self.onMessage(.text(s), self.generation)
                    case .data(let d): self.onMessage(.binary(d), self.generation)
                    @unknown default: break
                    }
                    self.receiveLoop(t)
                case .failure:
                    let code = t.closeCode.rawValue
                    self.onClose?(code, self.generation)
                    self.onStateChange?(false, self.generation)
                    self.teardownLocked()
                    guard self.autoReconnect, !self.stopped else { return }
                    let bounds = Self.backoffBounds(attempt: self.reconnectAttempt)
                    self.reconnectAttempt += 1
                    let delay = Double.random(in: bounds.min...bounds.max)
                    self.queue.asyncAfter(deadline: .now() + delay) { [weak self] in
                        self?.openLocked()
                    }
                }
            }
        }
    }

    private func startPingLocked(for t: URLSessionWebSocketTask) {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 25, repeating: 25)
        timer.setEventHandler { [weak self, weak t] in
            guard let self, let t, t === self.task, !self.stopped else { return }
            t.sendPing { _ in }
        }
        timer.resume()
        pingTimer = timer
    }

    private func teardownLocked() {
        pingTimer?.cancel()
        pingTimer = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    // MARK: - Test hooks (только для unit-тестов)
    func testHook_onQueue(_ block: @escaping () -> Void) { queue.async(execute: block) }
    var testHook_isStopped: Bool { stopped }
}
```

- [ ] **Step 4: Run — verify PASS** (`swift test --filter VGWebSocketConnectionTests`)

- [ ] **Step 5: Full build + commit**

```bash
cd native/KrabEarAgent && swift build -c release && cd ../..
git add native/KrabEarAgent/Sources/KrabEarAgent/VGWebSocketConnection.swift native/KrabEarAgent/Tests/KrabEarAgentTests/VGWebSocketConnectionTests.swift
git commit -m "feat(call-observer): общий WS-транспорт (backoff, ping-lifecycle, generation) (w1 T4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Swift — VGSessionWatcher (poll FSM)

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/VGSessionWatcher.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/VGSessionWatcherTests.swift`

**Interfaces:**
- Consumes: nothing from other tasks (fetcher is a protocol).
- Produces:
  - `struct VGSessionInfo: Equatable { let id, status, phone, callDirection, createdAt, updatedAt, srcLang, tgtLang, callBrief: String }`
  - `protocol VGSessionFetching { func fetchSessions(completion: @escaping (Result<(statusCode: Int, body: Data), Error>) -> Void) }`
  - `protocol VGSessionWatcherDelegate: AnyObject` with `watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool)`, `watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64)`, `watcherCallGone(sessionId: String, generation: UInt64)`, `watcherVGLost(sessionId: String, generation: UInt64)`, `watcherAuthRejected()` — ALL delivered on main queue.
  - `final class VGSessionWatcher` with `init(fetcher: VGSessionFetching, now: @escaping () -> Date = Date.init, monotonic: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime })`, `weak var delegate`, `func start()`, `func stop()`, and test-visible `func pollOnce(completion: (() -> Void)? = nil)`.

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import KrabEarAgent

private final class ScriptedFetcher: VGSessionFetching {
    var script: [Result<(statusCode: Int, body: Data), Error>] = []
    private(set) var calls = 0
    func fetchSessions(completion: @escaping (Result<(statusCode: Int, body: Data), Error>) -> Void) {
        calls += 1
        completion(script.isEmpty ? .failure(URLError(.cannotConnectToHost)) : script.removeFirst())
    }
}

private final class SpyDelegate: VGSessionWatcherDelegate {
    var appeared: [(String, UInt64, Bool)] = []
    var updated: [(String, UInt64)] = []
    var gone: [(String, UInt64)] = []
    var lost: [(String, UInt64)] = []
    var authRejects = 0
    func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool) { appeared.append((s.id, generation, resurrected)) }
    func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64) { updated.append((s.id, generation)) }
    func watcherCallGone(sessionId: String, generation: UInt64) { gone.append((sessionId, generation)) }
    func watcherVGLost(sessionId: String, generation: UInt64) { lost.append((sessionId, generation)) }
    func watcherAuthRejected() { authRejects += 1 }
}

final class VGSessionWatcherTests: XCTestCase {
    private var fetcher = ScriptedFetcher()
    private var spy = SpyDelegate()
    private var fakeUptime: TimeInterval = 1000
    private var fakeNow = Date(timeIntervalSince1970: 1_755_800_000)

    private func makeWatcher() -> VGSessionWatcher {
        let w = VGSessionWatcher(fetcher: fetcher,
                                 now: { self.fakeNow },
                                 monotonic: { self.fakeUptime })
        w.delegate = spy
        return w
    }

    private func body(_ sessions: [[String: Any]]) -> Data {
        try! JSONSerialization.data(withJSONObject: ["ok": true, "count": sessions.count, "items": sessions])
    }

    private func session(_ id: String, status: String = "running", phone: String = "+341",
                         direction: String = "outbound", updatedSecondsAgo: Double = 60) -> [String: Any] {
        let iso = ISO8601DateFormatter()
        return ["id": id, "status": status, "phone": phone, "call_direction": direction,
                "created_at": iso.string(from: fakeNow.addingTimeInterval(-300)),
                "updated_at": iso.string(from: fakeNow.addingTimeInterval(-updatedSecondsAgo)),
                "src_lang": "es", "tgt_lang": "ru", "source": "twilio_pstn_outbound", "call_brief": ""]
    }

    private func poll(_ w: VGSessionWatcher) {
        let exp = expectation(description: "poll")
        w.pollOnce { exp.fulfill() }
        wait(for: [exp], timeout: 2)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))  // дренаж main-доставки
    }

    func test_appear_immediate_and_updated_on_next_poll() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")]))), .success((200, body([session("s1")])))]
        poll(w); poll(w)
        XCTAssertEqual(spy.appeared.map(\.0), ["s1"])
        XCTAssertEqual(spy.updated.map(\.0), ["s1"])
    }

    func test_predicate_rejects_no_phone_no_direction_and_stale() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([
            session("tg", phone: "", direction: ""),                // telegram-чат и т.п.
            session("old", updatedSecondsAgo: 7 * 3600),            // stale > 6h
            session("term", status: "stopped"),                     // терминальная
        ])))]
        poll(w)
        XCTAssertTrue(spy.appeared.isEmpty)
    }

    func test_unparseable_updated_at_fails_open_to_visible() {
        var s = session("s1"); s["updated_at"] = "garbage"
        let w = makeWatcher()
        fetcher.script = [.success((200, body([s])))]
        poll(w)
        XCTAssertEqual(spy.appeared.map(\.0), ["s1"])
    }

    func test_gone_requires_streak_2_and_only_on_success() {
        let w = makeWatcher()
        fetcher.script = [
            .success((200, body([session("s1")]))),
            .failure(URLError(.timedOut)),                       // fail ≠ gone
            .success((200, body([session("s1", status: "stopped")]))),  // предикат упал: streak 1
            .success((200, body([]))),                           // streak 2 → gone
        ]
        poll(w); poll(w); poll(w)
        XCTAssertTrue(spy.gone.isEmpty)
        poll(w)
        XCTAssertEqual(spy.gone.map(\.0), ["s1"])
    }

    func test_vgLost_needs_3_fails_AND_30s() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")]))),
                          .failure(URLError(.timedOut)), .failure(URLError(.timedOut)), .failure(URLError(.timedOut))]
        poll(w)
        poll(w); fakeUptime += 5
        poll(w); fakeUptime += 5      // 3 фейла, но лишь 10с — рано
        poll(w)
        XCTAssertTrue(spy.lost.isEmpty)
        fetcher.script = [.failure(URLError(.timedOut))]
        fakeUptime += 25              // теперь ≥30с с последнего успеха
        poll(w)
        XCTAssertEqual(spy.lost.map(\.0), ["s1"])
        // one-shot: ещё фейлы не дублируют
        fetcher.script = [.failure(URLError(.timedOut))]
        poll(w)
        XCTAssertEqual(spy.lost.count, 1)
    }

    func test_resurrection_same_id_new_generation() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")])))]
        poll(w)
        let firstGen = spy.appeared[0].1
        // vgLost
        fetcher.script = Array(repeating: .failure(URLError(.timedOut)), count: 3)
        poll(w); fakeUptime += 15; poll(w); fakeUptime += 20; poll(w)
        XCTAssertEqual(spy.lost.count, 1)
        // VG вернулся, звонок жив
        fetcher.script = [.success((200, body([session("s1")])))]
        poll(w)
        XCTAssertEqual(spy.appeared.count, 2)
        XCTAssertTrue(spy.appeared[1].2, "resurrected flag")
        XCTAssertGreaterThan(spy.appeared[1].1, firstGen)
    }

    func test_auth_reject_fires_once_and_counts_as_failure() {
        let w = makeWatcher()
        fetcher.script = [.success((403, Data())), .success((401, Data()))]
        poll(w); poll(w)
        XCTAssertEqual(spy.authRejects, 1)
    }

    func test_terminal_status_no_resurrection() {
        let w = makeWatcher()
        fetcher.script = [
            .success((200, body([session("s1")]))),
            .success((200, body([session("s1", status: "failed")]))),
            .success((200, body([session("s1", status: "failed")]))),  // streak 2 → gone
            .success((200, body([session("s1", status: "failed")]))),  // терминальная не воскресает
        ]
        poll(w); poll(w); poll(w); poll(w)
        XCTAssertEqual(spy.gone.count, 1)
        XCTAssertEqual(spy.appeared.count, 1)
    }
}
```

- [ ] **Step 2: Run — verify FAIL** (`swift test --filter VGSessionWatcherTests` → compile error)

- [ ] **Step 3: Implement `VGSessionWatcher.swift`**

```swift
import Foundation

struct VGSessionInfo: Equatable {
    let id: String
    let status: String
    let phone: String
    let callDirection: String
    let createdAt: String
    let updatedAt: String
    let srcLang: String
    let tgtLang: String
    let callBrief: String
}

protocol VGSessionFetching {
    /// GET {voice_gateway_url}/v1/sessions?limit=20 — completion на любой очереди.
    func fetchSessions(completion: @escaping (Result<(statusCode: Int, body: Data), Error>) -> Void)
}

protocol VGSessionWatcherDelegate: AnyObject {
    func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool)
    func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64)
    func watcherCallGone(sessionId: String, generation: UInt64)
    func watcherVGLost(sessionId: String, generation: UInt64)
    func watcherAuthRejected()
}

/// Дискавери живых звонков VG поллингом GET /v1/sessions (spec §3.1).
/// 🔴 Сессии VG НЕ исчезают из списка (терминальные строки остаются в SQLite,
/// рестарт VG патчит их в failed) → callGone ПРЕДИКАТНЫЙ, не по отсутствию.
/// fail ≠ absent; callGone только по успешному поллу, streak 2.
/// vgLost = 3 подряд неудачи И ≥30с с последнего успеха.
final class VGSessionWatcher {
    private struct Tracked {
        var generation: UInt64
        var goneStreak: Int = 0
        var terminal: Bool = false
        var lastStatus: String = ""
    }

    weak var delegate: VGSessionWatcherDelegate?

    private let fetcher: VGSessionFetching
    private let now: () -> Date
    private let monotonic: () -> TimeInterval
    private let queue = DispatchQueue(label: "krab.vg.watcher")
    private var tracked: [String: Tracked] = [:]
    private var failedStreak = 0
    private var lastSuccessUptime: TimeInterval?
    private var authHintFired = false
    private var running = false
    private static var generationCounter: UInt64 = 0
    private static let genLock = NSLock()

    private static let liveStatuses: Set<String> = ["created", "running", "paused"]
    private static let staleCutoff: TimeInterval = 6 * 3600  // = VG stale_running_session_max_age_hours
    private static let goneStreakThreshold = 2
    private static let vgLostFailures = 3
    private static let vgLostMinSilence: TimeInterval = 30

    init(fetcher: VGSessionFetching,
         now: @escaping () -> Date = Date.init,
         monotonic: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.fetcher = fetcher
        self.now = now
        self.monotonic = monotonic
    }

    func start() {
        queue.async {
            guard !self.running else { return }
            self.running = true
            self.scheduleNextLocked(after: 0.1)
        }
    }

    func stop() { queue.async { self.running = false } }

    /// Тестовый вход: один полл без таймера.
    func pollOnce(completion: (() -> Void)? = nil) {
        fetcher.fetchSessions { [weak self] result in
            guard let self else { completion?(); return }
            self.queue.async {
                self.handleLocked(result)
                completion?()
            }
        }
    }

    private static func nextGeneration() -> UInt64 {
        genLock.lock(); defer { genLock.unlock() }
        generationCounter += 1
        return generationCounter
    }

    private func scheduleNextLocked(after delay: TimeInterval) {
        guard running else { return }
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.running else { return }
            self.pollOnce { [weak self] in
                guard let self else { return }
                self.queue.async { self.scheduleNextLocked(after: self.currentCadenceLocked()) }
            }
        }
    }

    private func currentCadenceLocked() -> TimeInterval {
        if failedStreak > 0 { return min(60, 15 * Double(failedStreak)) }
        let hasLive = tracked.values.contains { !$0.terminal }
        return hasLive ? 2 : 3
    }

    private func handleLocked(_ result: Result<(statusCode: Int, body: Data), Error>) {
        switch result {
        case .failure:
            registerFailureLocked(authRejected: false)
        case .success(let resp) where resp.statusCode == 401 || resp.statusCode == 403:
            registerFailureLocked(authRejected: true)
        case .success(let resp) where resp.statusCode == 200:
            guard let obj = (try? JSONSerialization.jsonObject(with: resp.body)) as? [String: Any],
                  let items = obj["items"] as? [[String: Any]] else {
                registerFailureLocked(authRejected: false)
                return
            }
            handleSuccessLocked(items: items)
        case .success:
            registerFailureLocked(authRejected: false)
        }
    }

    private func registerFailureLocked(authRejected: Bool) {
        failedStreak += 1
        if authRejected && !authHintFired {
            authHintFired = true
            notify { $0.watcherAuthRejected() }
        }
        let silence = monotonic() - (lastSuccessUptime ?? monotonic())
        guard failedStreak >= Self.vgLostFailures,
              lastSuccessUptime != nil, silence >= Self.vgLostMinSilence else { return }
        for (id, entry) in tracked where !entry.terminal {
            tracked[id]?.terminal = true
            notify { $0.watcherVGLost(sessionId: id, generation: entry.generation) }
        }
    }

    private func handleSuccessLocked(items: [[String: Any]]) {
        failedStreak = 0
        authHintFired = false
        lastSuccessUptime = monotonic()

        var liveById: [String: VGSessionInfo] = [:]
        var seenIds = Set<String>()
        for raw in items {
            guard let info = Self.parse(raw) else { continue }
            seenIds.insert(info.id)
            if isLiveLocked(info) { liveById[info.id] = info }
        }

        for (id, info) in liveById {
            if var entry = tracked[id] {
                if entry.terminal {
                    // Resurrection (post-vgLost) — терминальный СТАТУС сюда не попадает,
                    // liveById уже отфильтрован предикатом.
                    let gen = Self.nextGeneration()
                    entry = Tracked(generation: gen, lastStatus: info.status)
                    tracked[id] = entry
                    notify { $0.watcherCallAppeared(info, generation: gen, resurrected: true) }
                } else {
                    entry.goneStreak = 0
                    entry.lastStatus = info.status
                    tracked[id] = entry
                    notify { $0.watcherCallUpdated(info, generation: entry.generation) }
                }
            } else {
                let gen = Self.nextGeneration()
                tracked[id] = Tracked(generation: gen, lastStatus: info.status)
                notify { $0.watcherCallAppeared(info, generation: gen, resurrected: false) }
            }
        }

        for (id, var entry) in tracked where !entry.terminal && liveById[id] == nil {
            entry.goneStreak += 1
            if entry.goneStreak >= Self.goneStreakThreshold {
                entry.terminal = true
                notify { $0.watcherCallGone(sessionId: id, generation: entry.generation) }
            }
            tracked[id] = entry
        }

        // Cleanup: терминальные записи, чьих id уже нет в ответе вовсе.
        for (id, entry) in tracked where entry.terminal && !seenIds.contains(id) {
            tracked.removeValue(forKey: id)
        }
    }

    private func isLiveLocked(_ s: VGSessionInfo) -> Bool {
        guard Self.liveStatuses.contains(s.status) else { return false }
        guard !s.phone.isEmpty || !s.callDirection.isEmpty else { return false }
        // stale-гард; непарсибельная дата — fail-open в сторону показа звонка.
        if let updated = Self.parseISO(s.updatedAt) {
            if now().timeIntervalSince(updated) > Self.staleCutoff { return false }
        }
        return true
    }

    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// VG отдаёт ISO и с долями секунды, и без — принимаем оба.
    static func parseISO(_ s: String) -> Date? {
        isoFractional.date(from: s) ?? isoPlain.date(from: s)
    }

    private static func parse(_ raw: [String: Any]) -> VGSessionInfo? {
        guard let id = raw["id"] as? String, let status = raw["status"] as? String else { return nil }
        func s(_ k: String) -> String { raw[k] as? String ?? "" }
        return VGSessionInfo(id: id, status: status, phone: s("phone"),
                             callDirection: s("call_direction"), createdAt: s("created_at"),
                             updatedAt: s("updated_at"), srcLang: s("src_lang"),
                             tgtLang: s("tgt_lang"), callBrief: s("call_brief"))
    }

    private func notify(_ block: @escaping (VGSessionWatcherDelegate) -> Void) {
        DispatchQueue.main.async { [weak self] in
            guard let d = self?.delegate else { return }
            block(d)
        }
    }
}
```

Add one more test to the T5 test file (both ISO variants must parse — VG
emits with and without fractional seconds):

```swift
    func test_iso_both_variants_parse() {
        XCTAssertNotNil(VGSessionWatcher.parseISO("2026-08-21T10:00:00Z"))
        XCTAssertNotNil(VGSessionWatcher.parseISO("2026-08-21T10:00:00.123Z"))
    }
```

- [ ] **Step 4: Run — verify PASS** (`swift test --filter VGSessionWatcherTests`)

- [ ] **Step 5: Full build + commit**

```bash
cd native/KrabEarAgent && swift build -c release && cd ../..
git add native/KrabEarAgent/Sources/KrabEarAgent/VGSessionWatcher.swift native/KrabEarAgent/Tests/KrabEarAgentTests/VGSessionWatcherTests.swift
git commit -m "feat(call-observer): poll-FSM ватчер (предикатный callGone, vgLost, resurrection) (w1 T5)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Swift — VGCallStreamClient (events WS)

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/VGCallStreamClient.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/VGCallStreamClientTests.swift`

**Interfaces:**
- Consumes: `VGCallEvent.decode` (T2); `VGWebSocketConnecting`, `VGWebSocketConnection` (T4).
- Produces: `final class VGCallStreamClient` with `var onEvent: ((VGCallEvent, UInt64) -> Void)?` (main queue, generation-stamped), `func connect(baseURL: URL, sessionId: String, generation: UInt64, tokenProvider: @escaping () -> String)`, `func disconnect()`, and test-only injectable `var connectionFactoryForTests: ((URL, UInt64, @escaping (VGWebSocketConnection.Message, UInt64) -> Void) -> VGWebSocketConnecting)?` (nil → real `VGWebSocketConnection` with `autoReconnect: true`); `var onConnectionState: ((Bool, UInt64) -> Void)?` for the reconnecting badge.

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import KrabEarAgent

private final class FakeConnection: VGWebSocketConnecting {
    var connected = false
    var permanentlyStopped = false
    func connect() { connected = true }
    func permanentStop() { permanentlyStopped = true }
}

final class VGCallStreamClientTests: XCTestCase {
    private func makeClient() -> (VGCallStreamClient, FakeConnection, capture: () -> ((VGWebSocketConnection.Message, UInt64) -> Void)?) {
        let client = VGCallStreamClient()
        let fake = FakeConnection()
        var handler: ((VGWebSocketConnection.Message, UInt64) -> Void)?
        client.connectionFactoryForTests = { _, _, onMessage in
            handler = onMessage
            return fake
        }
        return (client, fake, { handler })
    }

    private func drainMain() { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }

    func test_decodes_and_delivers_on_main_with_generation() {
        let (client, fake, capture) = makeClient()
        var got: [(VGCallEvent, UInt64)] = []
        client.onEvent = { got.append(($0, $1)) }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 5, tokenProvider: { "" })
        XCTAssertTrue(fake.connected)
        capture()?(.text(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#), 5)
        drainMain()
        XCTAssertEqual(got.count, 1)
        XCTAssertEqual(got[0].1, 5)
        XCTAssertEqual(got[0].0, .sttFinal(text: "hola", language: nil, confidence: nil))
    }

    func test_stale_generation_dropped_before_render() {
        let (client, _, capture) = makeClient()
        var got = 0
        client.onEvent = { _, _ in got += 1 }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 5, tokenProvider: { "" })
        let oldHandler = capture()!
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s2", generation: 6, tokenProvider: { "" })
        oldHandler(.text(#"{"type":"stt.final","ts":"t","data":{"text":"stale"}}"#), 5)  // событие A в полёте
        drainMain()
        XCTAssertEqual(got, 0, "stt.final чужого поколения не должен дойти до UI")
    }

    func test_callClosed_permanently_stops_connection_and_still_delivers() {
        let (client, fake, capture) = makeClient()
        var got: [VGCallEvent] = []
        client.onEvent = { e, _ in got.append(e) }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 7, tokenProvider: { "" })
        capture()?(.text(#"{"type":"call.closed","ts":"t","data":{"session_id":"s1"}}"#), 7)
        drainMain()
        XCTAssertTrue(fake.permanentlyStopped)
        XCTAssertEqual(got, [.callClosed])
    }

    func test_binary_and_malformed_ignored() {
        let (client, _, capture) = makeClient()
        var got = 0
        client.onEvent = { _, _ in got += 1 }
        client.connect(baseURL: URL(string: "http://127.0.0.1:8090")!, sessionId: "s1", generation: 8, tokenProvider: { "" })
        capture()?(.binary(Data([1, 2, 3])), 8)
        capture()?(.text("not json"), 8)
        drainMain()
        XCTAssertEqual(got, 0)
    }
}
```

- [ ] **Step 2: Run — verify FAIL**

- [ ] **Step 3: Implement `VGCallStreamClient.swift`**

```swift
import Foundation

/// Клиент событий VG WS `/v1/sessions/{id}/stream` (spec §3 комп. 2).
/// Подключается на callAppeared выбранной сессии; авто-реконнект внутри
/// транспорта; call.closed → permanentStop (терминал ЛЮБОГО звонка).
/// Каждое событие — на main со generation-штампом; чужое поколение
/// отбрасывается ДО UI. onConnectionState → бейдж «переподключение…».
final class VGCallStreamClient {
    var onEvent: ((VGCallEvent, UInt64) -> Void)?
    var onConnectionState: ((Bool, UInt64) -> Void)?

    /// Только для тестов; nil → реальный транспорт с autoReconnect.
    var connectionFactoryForTests: ((URL, UInt64,
        @escaping (VGWebSocketConnection.Message, UInt64) -> Void) -> VGWebSocketConnecting)?

    private var connection: VGWebSocketConnecting?
    private(set) var generation: UInt64 = 0

    func connect(baseURL: URL, sessionId: String, generation: UInt64,
                 tokenProvider: @escaping () -> String) {
        disconnect()
        self.generation = generation
        guard let url = VGWebSocketConnection.wsURL(httpBase: baseURL,
                                                    path: "/v1/sessions/\(sessionId)/stream") else { return }
        let handler: (VGWebSocketConnection.Message, UInt64) -> Void = { [weak self] msg, gen in
            guard case .text(let s) = msg, let data = s.data(using: .utf8),
                  let event = VGCallEvent.decode(data) else { return }
            DispatchQueue.main.async {
                guard let self, gen == self.generation else { return }
                if case .callClosed = event { self.connection?.permanentStop() }
                if case .ignored = event { return }
                self.onEvent?(event, gen)
            }
        }
        let conn: VGWebSocketConnecting
        if let factory = connectionFactoryForTests {
            conn = factory(url, generation, handler)
        } else {
            conn = VGWebSocketConnection(
                url: url, generation: generation, autoReconnect: true,
                tokenProvider: tokenProvider, onMessage: handler,
                onStateChange: { [weak self] connected, gen in
                    DispatchQueue.main.async {
                        guard let self, gen == self.generation else { return }
                        self.onConnectionState?(connected, gen)
                    }
                },
                onClose: nil)
        }
        connection = conn
        conn.connect()
    }

    func disconnect() {
        connection?.permanentStop()
        connection = nil
    }
}
```

- [ ] **Step 4: Run — verify PASS** (`swift test --filter VGCallStreamClientTests`)

- [ ] **Step 5: Full build + commit**

```bash
cd native/KrabEarAgent && swift build -c release && cd ../..
git add native/KrabEarAgent/Sources/KrabEarAgent/VGCallStreamClient.swift native/KrabEarAgent/Tests/KrabEarAgentTests/VGCallStreamClientTests.swift
git commit -m "feat(call-observer): events-WS клиент (generation-фильтр, call.closed→permanent stop) (w1 T6)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Swift — CallAudioPlayer (monitor WS + playback)

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/CallAudioPlayer.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/CallAudioPlayerTests.swift`

**Interfaces:**
- Consumes: `MuLawDecoder` (T3); `VGWebSocketConnecting`, `VGWebSocketConnection` (T4).
- Produces:
  - `protocol CallAudioEngineProtocol: AnyObject { func start() throws; func stop(); func schedule(_ samples: [Float]) }`
  - `final class CallAudioEngine: CallAudioEngineProtocol` (real AVAudioEngine impl, config-change aware)
  - `final class CallAudioPlayer` with `enum ListenState: Equatable { case idle, connecting, listening, subscriberLimit, failed }`, `var onStateChange: ((ListenState, UInt64) -> Void)?` (main queue — buttons render THIS, never the requested state), `func startListening(baseURL: URL, sessionId: String, generation: UInt64, tokenProvider: @escaping () -> String)`, `func stopListening()`, test hooks `var engineFactory: () -> CallAudioEngineProtocol`, `var connectionFactoryForTests: ((URL, UInt64, @escaping (VGWebSocketConnection.Message, UInt64) -> Void, @escaping (Int, UInt64) -> Void) -> VGWebSocketConnecting)?`.

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import KrabEarAgent

private final class SpyEngine: CallAudioEngineProtocol {
    var started = 0, stoppedCount = 0
    var scheduled: [[Float]] = []
    func start() throws { started += 1 }
    func stop() { stoppedCount += 1 }
    func schedule(_ samples: [Float]) { scheduled.append(samples) }
}

private final class FakeConn: VGWebSocketConnecting {
    var connects = 0, stops = 0
    func connect() { connects += 1 }
    func permanentStop() { stops += 1 }
}

final class CallAudioPlayerTests: XCTestCase {
    private var engine = SpyEngine()
    private var conn = FakeConn()
    private var onMessage: ((VGWebSocketConnection.Message, UInt64) -> Void)?
    private var onClose: ((Int, UInt64) -> Void)?
    private var states: [(CallAudioPlayer.ListenState, UInt64)] = []

    private func makePlayer() -> CallAudioPlayer {
        let p = CallAudioPlayer()
        p.engineFactory = { self.engine }
        p.connectionFactoryForTests = { _, _, msg, close in
            self.onMessage = msg; self.onClose = close
            return self.conn
        }
        p.onStateChange = { self.states.append(($0, $1)) }
        return p
    }

    private func start(_ p: CallAudioPlayer, gen: UInt64 = 1) {
        p.startListening(baseURL: URL(string: "http://127.0.0.1:8090")!,
                         sessionId: "s1", generation: gen, tokenProvider: { "" })
        drain()
    }

    private func drain() { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }

    func test_metadata_then_frames_reach_engine() {
        let p = makePlayer()
        start(p)
        XCTAssertEqual(conn.connects, 1)
        onMessage?(.text(#"{"format":"mulaw_8k","frame_ms":100}"#), 1); drain()
        XCTAssertEqual(states.last?.0, .listening)
        onMessage?(.binary(Data(repeating: 0xFF, count: 800)), 1); drain()
        XCTAssertEqual(engine.scheduled.count, 1)
        XCTAssertEqual(engine.scheduled[0].count, 800)
        XCTAssertEqual(engine.scheduled[0][0], 0.0)  // 0xFF → 0
    }

    func test_wrong_metadata_fails_closed() {
        let p = makePlayer()
        start(p)
        onMessage?(.text(#"{"format":"opus_48k","frame_ms":20}"#), 1); drain()
        XCTAssertEqual(states.last?.0, .failed)
        XCTAssertEqual(conn.stops, 1)
    }

    func test_single_flight_double_start_one_connection() {
        let p = makePlayer()
        start(p); start(p)
        XCTAssertEqual(conn.connects, 1, "двойной клик 🔊 не смеет съесть оба subscriber-слота")
    }

    func test_new_generation_tears_old_connection_first() {
        let p = makePlayer()
        start(p, gen: 1)
        start(p, gen: 2)
        XCTAssertEqual(conn.stops, 1)
        XCTAssertEqual(conn.connects, 2)
    }

    func test_close_1013_is_subscriberLimit_no_retry() {
        let p = makePlayer()
        start(p)
        onClose?(1013, 1); drain()
        XCTAssertEqual(states.last?.0, .subscriberLimit)
        XCTAssertEqual(conn.connects, 1, "retry только по явному повторному клику")
    }

    func test_close_1000_returns_idle_and_stops_engine() {
        let p = makePlayer()
        start(p)
        onMessage?(.text(#"{"format":"mulaw_8k","frame_ms":100}"#), 1); drain()
        onClose?(1000, 1); drain()
        XCTAssertEqual(states.last?.0, .idle)
        XCTAssertEqual(engine.stoppedCount, 1)
    }

    func test_stale_generation_frames_dropped() {
        let p = makePlayer()
        start(p, gen: 1)
        onMessage?(.text(#"{"format":"mulaw_8k","frame_ms":100}"#), 1); drain()
        let oldMessage = onMessage
        start(p, gen: 2)
        oldMessage?(.binary(Data(repeating: 0x00, count: 800)), 1); drain()
        XCTAssertTrue(engine.scheduled.isEmpty, "кадр поколения 1 после старта поколения 2")
    }

    func test_stopListening_idempotent() {
        let p = makePlayer()
        start(p)
        p.stopListening(); p.stopListening(); drain()
        XCTAssertEqual(conn.stops, 1)
        XCTAssertEqual(states.last?.0, .idle)
    }
}
```

- [ ] **Step 2: Run — verify FAIL**

- [ ] **Step 3: Implement `CallAudioPlayer.swift`**

```swift
import AppKit
import AVFoundation
import Foundation

protocol CallAudioEngineProtocol: AnyObject {
    func start() throws
    func stop()
    func schedule(_ samples: [Float])
}

/// Реальный движок: AVAudioEngine + AVAudioPlayerNode, 8кГц моно Float32;
/// mainMixer ресемплит в hardware rate. Переключение аудио-устройства/сон
/// (AVAudioEngineConfigurationChange) → перезапуск движка на месте.
final class CallAudioEngine: CallAudioEngineProtocol {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let format = AVAudioFormat(standardFormatWithSampleRate: 8000, channels: 1)!
    private var observer: NSObjectProtocol?

    func start() throws {
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        try engine.start()
        player.play()
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            // Наушники/AirPods/сон: движок остановился — рестарт, иначе кнопка
            // выглядит включённой при мёртвом звуке (spec §3 комп. 3).
            try? self.engine.start()
            self.player.play()
        }
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        player.stop()
        engine.stop()
        engine.detach(player)
    }

    func schedule(_ samples: [Float]) {
        guard let buf = AVAudioPCMBuffer(pcmFormat: format,
                                         frameCapacity: AVAudioFrameCount(samples.count)) else { return }
        buf.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { src in
            buf.floatChannelData![0].update(from: src.baseAddress!, count: samples.count)
        }
        player.scheduleBuffer(buf, completionHandler: nil)
    }
}

/// Прослушка звонка: WS /monitor/audio → μ-law декод → движок.
/// ОДИН владелец listen-состояния (spec §3 комп. 3): HUD и панель — два
/// рендера onStateChange. Single-flight + generation: двойной клик или
/// HUD+панель одновременно не открывают второй сокет (лимит VG = 2).
/// autoReconnect=false: 1013 (лимит) ретраится только явным повторным кликом.
final class CallAudioPlayer {
    enum ListenState: Equatable { case idle, connecting, listening, subscriberLimit, failed }

    var onStateChange: ((ListenState, UInt64) -> Void)?
    var engineFactory: () -> CallAudioEngineProtocol = { CallAudioEngine() }
    var connectionFactoryForTests: ((URL, UInt64,
                                     @escaping (VGWebSocketConnection.Message, UInt64) -> Void,
                                     @escaping (Int, UInt64) -> Void) -> VGWebSocketConnecting)?

    private var connection: VGWebSocketConnecting?
    private var engine: CallAudioEngineProtocol?
    private(set) var generation: UInt64 = 0
    private var state: ListenState = .idle
    private var metadataValidated = false
    private var lastConnect: (baseURL: URL, sessionId: String, tokenProvider: () -> String)?
    private var wakeObserver: NSObjectProtocol?

    init() {
        // Сон/пробуждение: WS умирает при «живом» на вид состоянии — переподключаемся
        // (spec §3 комп. 3: кнопка не смеет врать про играющий звук).
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.state == .listening || self.state == .connecting,
                  let p = self.lastConnect else { return }
            let gen = self.generation
            self.teardownLocked(newState: .idle)
            self.startListening(baseURL: p.baseURL, sessionId: p.sessionId,
                                generation: gen, tokenProvider: p.tokenProvider)
        }
    }

    func startListening(baseURL: URL, sessionId: String, generation: UInt64,
                        tokenProvider: @escaping () -> String) {
        DispatchQueue.main.async { [self] in
            if generation == self.generation, state == .connecting || state == .listening {
                return  // single-flight
            }
            teardownLocked(newState: nil)
            self.generation = generation
            metadataValidated = false
            lastConnect = (baseURL, sessionId, tokenProvider)
            guard let url = VGWebSocketConnection.wsURL(httpBase: baseURL,
                                                        path: "/v1/sessions/\(sessionId)/monitor/audio") else {
                setState(.failed); return
            }
            let onMessage: (VGWebSocketConnection.Message, UInt64) -> Void = { [weak self] msg, gen in
                DispatchQueue.main.async { self?.handleMessage(msg, gen) }
            }
            let onClose: (Int, UInt64) -> Void = { [weak self] code, gen in
                DispatchQueue.main.async { self?.handleClose(code, gen) }
            }
            let conn: VGWebSocketConnecting
            if let factory = connectionFactoryForTests {
                conn = factory(url, generation, onMessage, onClose)
            } else {
                conn = VGWebSocketConnection(url: url, generation: generation, autoReconnect: false,
                                             tokenProvider: tokenProvider,
                                             onMessage: onMessage, onStateChange: nil,
                                             onClose: onClose)
            }
            connection = conn
            setState(.connecting)
            conn.connect()
        }
    }

    func stopListening() {
        DispatchQueue.main.async { [self] in
            guard connection != nil || state != .idle else { return }
            teardownLocked(newState: .idle)
        }
    }

    private func handleMessage(_ msg: VGWebSocketConnection.Message, _ gen: UInt64) {
        guard gen == generation, connection != nil else { return }
        switch msg {
        case .text(let s):
            guard !metadataValidated else { return }
            guard let data = s.data(using: .utf8),
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  obj["format"] as? String == "mulaw_8k" else {
                teardownLocked(newState: .failed)
                return
            }
            metadataValidated = true
            let engine = engineFactory()
            do { try engine.start() } catch { teardownLocked(newState: .failed); return }
            self.engine = engine
            setState(.listening)
        case .binary(let frame):
            guard metadataValidated, let engine else { return }
            engine.schedule(MuLawDecoder.decodeToFloat(frame))
        }
    }

    private func handleClose(_ code: Int, _ gen: UInt64) {
        guard gen == generation else { return }
        teardownLocked(newState: code == 1013 ? .subscriberLimit : .idle)
    }

    private func teardownLocked(newState: ListenState?) {
        connection?.permanentStop()
        connection = nil
        engine?.stop()
        engine = nil
        metadataValidated = false
        if let newState { setState(newState) }
    }

    private func setState(_ s: ListenState) {
        state = s
        onStateChange?(s, generation)
    }
}
```

- [ ] **Step 4: Run — verify PASS** (`swift test --filter CallAudioPlayerTests`)

- [ ] **Step 5: Full build + commit**

```bash
cd native/KrabEarAgent && swift build -c release && cd ../..
git add native/KrabEarAgent/Sources/KrabEarAgent/CallAudioPlayer.swift native/KrabEarAgent/Tests/KrabEarAgentTests/CallAudioPlayerTests.swift
git commit -m "feat(call-observer): прослушка звонка (single-flight, 1013 без ретрая, config-change) (w1 T7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Swift — CallObserverCoordinator (§4.1 automaton) + wiring

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/CallObserverCoordinator.swift`
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/main+CallObserver.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/CallObserverCoordinatorTests.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/MainCallObserverWiringTests.swift`

**Interfaces:**
- Consumes: T5 watcher (delegate), T6 stream client, T7 audio player.
- Produces (consumed by T9 UI):
  - `struct TranscriptEntry: Equatable { enum Kind: Equatable { case remote(text: String, translation: String?); case agent(text: String, textRu: String?, utteranceTs: String?, interrupted: Bool, spokenText: String?, spokenFraction: Double?); case system(String) }; var kind: Kind }`
  - `protocol CallObserverHUDPresenting: AnyObject { func showHUD(session: VGSessionInfo); func updateHUD(status: String, lastEntries: [TranscriptEntry], listenState: CallAudioPlayer.ListenState); func showLinger(message: String); func hideHUD(); var isHUDVisible: Bool { get } }`
  - `protocol CallObserverPanelPresenting: AnyObject { func showPanel(session: VGSessionInfo); func updateTranscript(_ entries: [TranscriptEntry]); func updateStatus(status: String, muted: Bool?, held: Bool?, badge: String?); func updateCost(_ text: String); func setTerminal(message: String); func setLive(); func closeHangupSheetIfOpen(); var isPanelVisible: Bool { get } }`
  - `protocol VGCommandPosting { func hangup(baseURL: URL, sessionId: String, completion: @escaping (Result<Int, Error>) -> Void); func fetchCostUsd(baseURL: URL, sessionId: String, completion: @escaping (Double?) -> Void) }`
  - `protocol CallObserverSettingsProviding { func refresh(completion: @escaping (_ hudEnabled: Bool, _ autoplay: Bool, _ privacyMode: Bool, _ baseURL: URL) -> Void) }`
  - `final class CallObserverCoordinator: VGSessionWatcherDelegate` — public API used by UI/wiring: `func userExpandedHUD()`, `func userClosedHUD()`, `func userClosedPanel()`, `func userToggledListen()`, `func userRequestedHangupConfirmed()`, `func userSelectedSession(_ id: String)`, `func openPanelFromMenu()`, `var hasLiveCall: Bool`.

- [ ] **Step 1: Write the failing tests** (the §6 automaton list — this is the heart of the wave):

```swift
import XCTest
@testable import KrabEarAgent

private final class SpyHUD: CallObserverHUDPresenting {
    var shown: [String] = []; var lingers: [String] = []; var hides = 0
    var updates: [(String, Int, CallAudioPlayer.ListenState)] = []
    var isHUDVisible = false
    func showHUD(session: VGSessionInfo) { shown.append(session.id); isHUDVisible = true }
    func updateHUD(status: String, lastEntries: [TranscriptEntry], listenState: CallAudioPlayer.ListenState) {
        updates.append((status, lastEntries.count, listenState))
    }
    func showLinger(message: String) { lingers.append(message) }
    func hideHUD() { hides += 1; isHUDVisible = false }
}

private final class SpyPanel: CallObserverPanelPresenting {
    var shown: [String] = []; var transcripts: [[TranscriptEntry]] = []
    var terminals: [String] = []; var lives = 0; var sheetCloses = 0
    var costs: [String] = []; var badges: [String?] = []; var hangupPrompts = 0
    var isPanelVisible = false
    func showPanel(session: VGSessionInfo) { shown.append(session.id); isPanelVisible = true }
    func updateTranscript(_ entries: [TranscriptEntry]) { transcripts.append(entries) }
    func updateStatus(status: String, muted: Bool?, held: Bool?, badge: String?) { badges.append(badge) }
    func presentHangupConfirm() { hangupPrompts += 1 }
    func updateCost(_ text: String) { costs.append(text) }
    func setTerminal(message: String) { terminals.append(message) }
    func setLive() { lives += 1 }
    func closeHangupSheetIfOpen() { sheetCloses += 1 }
}

private final class SpyPoster: VGCommandPosting {
    var hangups: [String] = []
    var hangupResult: Result<Int, Error> = .success(200)
    var costValue: Double? = 0.42
    func hangup(baseURL: URL, sessionId: String, completion: @escaping (Result<Int, Error>) -> Void) {
        hangups.append(sessionId); completion(hangupResult)
    }
    func fetchCostUsd(baseURL: URL, sessionId: String, completion: @escaping (Double?) -> Void) {
        completion(costValue)
    }
}

private final class FixedSettings: CallObserverSettingsProviding {
    var hudEnabled = true; var autoplay = false; var privacy = false
    func refresh(completion: @escaping (Bool, Bool, Bool, URL) -> Void) {
        completion(hudEnabled, autoplay, privacy, URL(string: "http://127.0.0.1:8090")!)
    }
}

final class CallObserverCoordinatorTests: XCTestCase {
    private var hud = SpyHUD()
    private var panel = SpyPanel()
    private var poster = SpyPoster()
    private var settings = FixedSettings()
    private var stream = VGCallStreamClient()
    private var player = CallAudioPlayer()
    private var streamHandler: ((VGWebSocketConnection.Message, UInt64) -> Void)?

    private func resetFixtures() {
        hud = SpyHUD(); panel = SpyPanel(); poster = SpyPoster()
        settings = FixedSettings(); stream = VGCallStreamClient(); player = CallAudioPlayer()
        streamHandler = nil
    }

    private func makeCoordinator() -> CallObserverCoordinator {
        stream.connectionFactoryForTests = { _, _, onMessage in
            self.streamHandler = onMessage
            final class NoopConn: VGWebSocketConnecting { func connect() {}; func permanentStop() {} }
            return NoopConn()
        }
        player.connectionFactoryForTests = { _, _, _, _ in
            final class NoopConn: VGWebSocketConnecting { func connect() {}; func permanentStop() {} }
            return NoopConn()
        }
        let c = CallObserverCoordinator(hud: hud, panel: panel, poster: poster,
                                        settings: settings, stream: stream, player: player,
                                        tokenProvider: { "" },
                                        lingerSeconds: 0.1,
                                        costPollInterval: 0.05,
                                        uiCoalesceInterval: 0)
        return c
    }

    private func session(_ id: String) -> VGSessionInfo {
        VGSessionInfo(id: id, status: "running", phone: "+341", callDirection: "outbound",
                      createdAt: "2026-08-21T10:00:00Z", updatedAt: "2026-08-21T10:00:00Z",
                      srcLang: "es", tgtLang: "ru", callBrief: "")
    }

    private func drain(_ t: TimeInterval = 0.05) { RunLoop.main.run(until: Date().addingTimeInterval(t)) }

    private func emit(_ json: String, gen: UInt64) {
        streamHandler?(.text(json), gen); drain()
    }

    func test_happy_path_appear_transcript_end_once() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        XCTAssertEqual(hud.shown, ["s1"])
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#, gen: 1)
        emit(#"{"type":"translation.final","ts":"t","data":{"text":"привет","source_text":"hola","src_lang":"es","tgt_lang":"ru"}}"#, gen: 1)
        emit(#"{"type":"agent.response","ts":"t","data":{"text":"Claro","text_ru":"Конечно","utterance_ts":"u1"}}"#, gen: 1)
        emit(#"{"type":"call.ended","ts":"t","data":{"reason":"hangup"}}"#, gen: 1)
        // дублирующие терминалы — no-op
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        emit(#"{"type":"call.closed","ts":"t","data":{"session_id":"s1"}}"#, gen: 1)
        XCTAssertEqual(hud.lingers.count, 1, "терминал ровно один раз")
        XCTAssertEqual(panel.terminals.count, panel.isPanelVisible ? 1 : 0)
    }

    func test_all_five_signals_each_terminal_once() {
        let signals: [(String, (CallObserverCoordinator) -> Void)] = [
            ("call.ended", { c in self.emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1) }),
            ("call.closed", { c in self.emit(#"{"type":"call.closed","ts":"t","data":{}}"#, gen: 1) }),
            ("callGone", { c in c.watcherCallGone(sessionId: "s1", generation: 1); self.drain() }),
            ("vgLost", { c in c.watcherVGLost(sessionId: "s1", generation: 1); self.drain() }),
            ("hangupTerminal", { c in
                self.poster.hangupResult = .success(409)
                c.userRequestedHangupConfirmed(); self.drain()
            }),
        ]
        for (name, fire) in signals {
            resetFixtures()  // свежие спаи (инлайн-инициализация не сбрасывается setUp-ом)
            let c = makeCoordinator()
            c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
            fire(c)
            fire(c)
            XCTAssertEqual(hud.lingers.count, 1, "сигнал \(name): терминал дважды")
        }
    }

    func test_linger_hides_hud_after_delay_and_new_call_cancels_stale_linger() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        // B появился в linger-окне A
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        drain(0.2)  // linger A истёк
        XCTAssertTrue(hud.isHUDVisible, "linger A спрятал HUD живого B")
    }

    func test_vgLost_message_differs() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherVGLost(sessionId: "s1", generation: 1); drain()
        XCTAssertTrue(hud.lingers[0].contains("VG") || hud.lingers[0].contains("связь"),
                      "vgLost должен отличаться от обычного конца")
    }

    func test_resurrection_preserves_transcript() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#, gen: 1)
        c.watcherVGLost(sessionId: "s1", generation: 1); drain()
        c.watcherCallAppeared(session("s1"), generation: 3, resurrected: true); drain()
        XCTAssertGreaterThanOrEqual(panel.lives, 1, "панель вышла из terminal в live")
        XCTAssertEqual(panel.terminals.count, 1)
        XCTAssertFalse(panel.transcripts.isEmpty)
        XCTAssertEqual(panel.transcripts.last?.count, 1, "транскрипт сохранён при resurrection")
    }

    func test_agent_interrupted_matches_by_utterance_ts_not_last() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"agent.response","ts":"t","data":{"text":"Первая","utterance_ts":"u1"}}"#, gen: 1)
        emit(#"{"type":"agent.response","ts":"t","data":{"text":"Вторая","utterance_ts":"u2"}}"#, gen: 1)
        emit(#"{"type":"agent.interrupted","ts":"t","data":{"utterance_ts":"u1","spoken_fraction":0.5,"spoken_text":"Пер"}}"#, gen: 1)
        guard case .agent(_, _, _, let interrupted1, let spoken1, _)? =
                panel.transcripts.last?.first?.kind else { return XCTFail() }
        XCTAssertTrue(interrupted1); XCTAssertEqual(spoken1, "Пер")
        guard case .agent(_, _, _, let interrupted2, _, _)? =
                panel.transcripts.last?.last?.kind else { return XCTFail() }
        XCTAssertFalse(interrupted2, "прервана ПЕРВАЯ, не последняя")
    }

    func test_auto_spoken_renders_as_agent_line() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"agent.suggestion.auto_spoken","ts":"t","data":{"text":"Uno","text_ru":"Один"}}"#, gen: 1)
        guard case .agent(let text, _, _, _, _, _)? = panel.transcripts.last?.last?.kind else { return XCTFail() }
        XCTAssertEqual(text, "Uno")
    }

    func test_privacy_on_suppresses_auto_show_manual_stays() {
        settings.privacy = true
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        XCTAssertTrue(hud.shown.isEmpty, "privacy: авто-показ подавлен")
        c.openPanelFromMenu(); drain()
        XCTAssertEqual(panel.shown, ["s1"], "ручной вход разрешён")
    }

    func test_privacy_flip_midcall_hides_auto_hud_keeps_manual_panel() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        settings.privacy = true
        c.watcherCallUpdated(session("s1"), generation: 1); drain()  // полл-тик перечитывает
        XCTAssertEqual(hud.hides, 1, "авто-показанный HUD скрыт")
        XCTAssertTrue(panel.isPanelVisible, "вручную открытая панель остаётся")
    }

    func test_hangup_single_flight_and_409_terminal_silent() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        poster.hangupResult = .success(409)
        c.userRequestedHangupConfirmed()
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertEqual(poster.hangups.count, 1, "single-flight")
        XCTAssertEqual(hud.lingers.count, 1, "409 → терминал, без error-тоста")
    }

    func test_terminal_closes_open_hangup_sheet() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        XCTAssertGreaterThanOrEqual(panel.sheetCloses, 1)
    }

    func test_terminal_of_selected_with_second_live_no_linger_over_live() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        XCTAssertTrue(hud.lingers.isEmpty, "linger при живом втором звонке запрещён")
        XCTAssertTrue(hud.isHUDVisible)
        // панель осталась на терминальной A до ручного свитча;
        // setLive: №1 — openPanelFromMenu (живой A), №2 — свитч на живой B.
        // 🔴 НЕ «чинить» реализацию под ==1, убирая setLive из openPanelFromMenu:
        // ручное открытие живого звонка навсегда потеряло бы бейдж «в эфире».
        c.userSelectedSession("s2"); drain()
        XCTAssertEqual(panel.lives, 2)
    }

    func test_manually_closed_hud_not_resurrected_by_linger() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.userClosedHUD(); drain()
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        XCTAssertTrue(hud.lingers.isEmpty, "linger не воскрешает вручную закрытый HUD")
    }

    func test_stale_generation_events_dropped() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        let before = panel.transcripts.count
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"stale"}}"#, gen: 99)
        XCTAssertEqual(panel.transcripts.count, before)
    }

    func test_transcript_capped_at_500() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        for i in 0..<510 {
            streamHandler?(.text(#"{"type":"stt.final","ts":"t","data":{"text":"m\#(i)"}}"#), 1)
        }
        drain()
        XCTAssertLessThanOrEqual(panel.transcripts.last?.count ?? 0, 500)
    }

    func test_cost_polling_updates_and_stops_at_terminal() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain(0.2)
        XCTAssertEqual(panel.costs.last, "$0.42")
        poster.costValue = nil
        drain(0.15)
        XCTAssertEqual(panel.costs.last, "—")
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        let frozen = panel.costs.count
        drain(0.2)
        XCTAssertEqual(panel.costs.count, frozen, "терминал остановил cost-поллинг")
    }

    func test_hangup_502_badge_no_terminal() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        poster.hangupResult = .success(502)
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertTrue(hud.lingers.isEmpty, "502 — не терминал")
        XCTAssertTrue(panel.badges.compactMap { $0 }.contains { $0.contains("Не удалось") })
    }

    func test_hangup_404_after_end_terminal_silent() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        poster.hangupResult = .success(404)
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertEqual(hud.lingers.count, 1, "404 → терминал без error-тоста")
        XCTAssertFalse(panel.badges.compactMap { $0 }.contains { $0.contains("Не удалось") })
    }

    func test_hangup_from_hud_opens_panel_with_confirm() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.userRequestedHangupFromHUD(); drain()
        XCTAssertTrue(panel.isPanelVisible)
        XCTAssertEqual(panel.hangupPrompts, 1)
    }
}
```

- [ ] **Step 2: Run — verify FAIL**

- [ ] **Step 3: Implement `CallObserverCoordinator.swift`**

```swift
import Foundation

struct TranscriptEntry: Equatable {
    enum Kind: Equatable {
        case remote(text: String, translation: String?)
        case agent(text: String, textRu: String?, utteranceTs: String?,
                   interrupted: Bool, spokenText: String?, spokenFraction: Double?)
        case system(String)
    }
    var kind: Kind
}

protocol CallObserverHUDPresenting: AnyObject {
    func showHUD(session: VGSessionInfo)
    func updateHUD(status: String, lastEntries: [TranscriptEntry], listenState: CallAudioPlayer.ListenState)
    func showLinger(message: String)
    func hideHUD()
    var isHUDVisible: Bool { get }
}

protocol CallObserverPanelPresenting: AnyObject {
    func showPanel(session: VGSessionInfo)
    func updateTranscript(_ entries: [TranscriptEntry])
    func updateStatus(status: String, muted: Bool?, held: Bool?, badge: String?)
    func updateCost(_ text: String)
    func setTerminal(message: String)
    func setLive()
    func closeHangupSheetIfOpen()
    func presentHangupConfirm()
    var isPanelVisible: Bool { get }
}

protocol VGCommandPosting {
    func hangup(baseURL: URL, sessionId: String, completion: @escaping (Result<Int, Error>) -> Void)
    func fetchCostUsd(baseURL: URL, sessionId: String, completion: @escaping (Double?) -> Void)
}

protocol CallObserverSettingsProviding {
    func refresh(completion: @escaping (_ hudEnabled: Bool, _ autoplay: Bool,
                                        _ privacyMode: Bool, _ baseURL: URL) -> Void)
}

/// Дирижёр Call Observer (spec §3 комп. 6 + §4.1). Весь код — на main queue
/// (все входы уже доставляются на main: watcher/stream/player так спроектированы).
/// §4.1: ПЯТЬ терминальных сигналов → one-shot per observation-generation;
/// generations ПО-СЕССИОННЫЕ (два звонка = два поколения).
final class CallObserverCoordinator: NSObject, VGSessionWatcherDelegate {
    private struct ObservedCall {
        var session: VGSessionInfo
        var generation: UInt64
        var terminalDelivered = false
        var transcript: [TranscriptEntry] = []
    }

    private let hud: CallObserverHUDPresenting
    private let panel: CallObserverPanelPresenting
    private let poster: VGCommandPosting
    private let settings: CallObserverSettingsProviding
    private let stream: VGCallStreamClient
    private let player: CallAudioPlayer
    private let tokenProvider: () -> String
    private let lingerSeconds: TimeInterval
    private let costPollInterval: TimeInterval
    private let uiCoalesceInterval: TimeInterval

    private var observed: [String: ObservedCall] = [:]
    private var selectedId: String?
    private var hudManuallyClosed = false
    private var hudAutoShown = false
    private var panelOpenedManually = false
    private var listenStartedManually = false
    private var hangupInFlight = false
    private var lingerWork: DispatchWorkItem?
    private var pushWork: DispatchWorkItem?
    private var costTimer: DispatchSourceTimer?
    private var listenState: CallAudioPlayer.ListenState = .idle

    private var hudEnabled = true
    private var autoplay = false
    private var privacyMode = false
    private var baseURL = URL(string: "http://127.0.0.1:8090")!

    private static let transcriptCap = 500

    init(hud: CallObserverHUDPresenting, panel: CallObserverPanelPresenting,
         poster: VGCommandPosting, settings: CallObserverSettingsProviding,
         stream: VGCallStreamClient, player: CallAudioPlayer,
         tokenProvider: @escaping () -> String,
         lingerSeconds: TimeInterval = 3.0,
         costPollInterval: TimeInterval = 3.0,
         uiCoalesceInterval: TimeInterval = 0.1) {
        self.hud = hud
        self.panel = panel
        self.poster = poster
        self.settings = settings
        self.stream = stream
        self.player = player
        self.tokenProvider = tokenProvider
        self.lingerSeconds = lingerSeconds
        self.costPollInterval = costPollInterval
        self.uiCoalesceInterval = uiCoalesceInterval
        super.init()
        stream.onEvent = { [weak self] event, gen in self?.handleStreamEvent(event, gen) }
        player.onStateChange = { [weak self] state, _ in
            self?.listenState = state
            self?.refreshHUD()
        }
        stream.onConnectionState = { [weak self] connected, _ in
            guard let self, let id = self.selectedId,
                  let call = self.observed[id], !call.terminalDelivered else { return }
            self.panel.updateStatus(status: call.session.status, muted: nil, held: nil,
                                    badge: connected ? nil : "переподключение…")
        }
        refreshSettings()
    }

    var hasLiveCall: Bool { observed.values.contains { !$0.terminalDelivered } }

    // MARK: - Watcher delegate (main queue)

    func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool) {
        refreshSettings()
        lingerWork?.cancel()  // B в linger-окне A: чужой таймер не смеет спрятать живой HUD
        lingerWork = nil
        var call = ObservedCall(session: s, generation: generation)
        if resurrected, let old = observed[s.id] {
            call.transcript = old.transcript  // VG не реплеит историю — не стирать
        }
        observed[s.id] = call
        // HUD следит за НОВЕЙШИМ живым: пока панель закрыта, selection следует
        // за новым звонком; открытая панель = ручная супервизия (§4.1).
        if selectedId == nil || observed[selectedId!] == nil || !panel.isPanelVisible {
            selectedId = s.id
        }
        if s.id == selectedId {
            connectStreams(for: call)
            if resurrected, panel.isPanelVisible { panel.setLive() }
            pushTranscript()
        }
        if resurrected { hudManuallyClosed = false }
        maybeAutoShowHUD(for: s)
        if autoplay && !privacyMode && !resurrected && s.id == selectedId {
            player.startListening(baseURL: baseURL, sessionId: s.id,
                                  generation: generation, tokenProvider: tokenProvider)
        }
        refreshHUD()
    }

    func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64) {
        guard var call = observed[s.id], call.generation == generation else { return }
        call.session = s
        observed[s.id] = call
        refreshSettings()
        applyPrivacySuppressionIfNeeded()
        refreshHUD()
    }

    func watcherCallGone(sessionId: String, generation: UInt64) {
        deliverTerminal(sessionId: sessionId, generation: generation,
                        message: "Звонок завершён")
    }

    func watcherVGLost(sessionId: String, generation: UInt64) {
        deliverTerminal(sessionId: sessionId, generation: generation,
                        message: "Связь с VG потеряна")
    }

    func watcherAuthRejected() {
        NSLog("CallObserver: VG отверг токен — форс-обновление креденшела W1892")
        refreshSettings()  // провайдер перечитает get_voice_gateway_credential
        if panel.isPanelVisible {
            panel.updateStatus(status: "", muted: nil, held: nil,
                               badge: "VG отверг токен — проверьте voice_gateway_api_key")
        }
    }

    // MARK: - Stream events (main queue, generation уже отфильтрован клиентом,
    // но дублируем проверку против selected: сессий может быть >1)

    private func handleStreamEvent(_ event: VGCallEvent, _ gen: UInt64) {
        guard let id = selectedId, var call = observed[id], call.generation == gen else { return }
        switch event {
        case .sttFinal(let text, _, _):
            append(&call, .init(kind: .remote(text: text, translation: nil)))
        case .translationFinal(let text, _, _, _):
            // Перевод приклеивается к последней remote-строке без перевода.
            if let idx = call.transcript.lastIndex(where: {
                if case .remote(_, nil) = $0.kind { return true } else { return false }
            }), case .remote(let orig, _) = call.transcript[idx].kind {
                call.transcript[idx] = .init(kind: .remote(text: orig, translation: text))
            } else {
                append(&call, .init(kind: .remote(text: "", translation: text)))
            }
        case .agentResponse(let text, let textRu, let uts, _):
            append(&call, .init(kind: .agent(text: text, textRu: textRu, utteranceTs: uts,
                                             interrupted: false, spokenText: nil, spokenFraction: nil)))
        case .agentAutoSpoken(let text, let textRu, _, _):
            append(&call, .init(kind: .agent(text: text, textRu: textRu, utteranceTs: nil,
                                             interrupted: false, spokenText: nil, spokenFraction: nil)))
        case .agentInterrupted(let uts, let fraction, let spoken):
            if let uts, let idx = call.transcript.firstIndex(where: {
                if case .agent(_, _, uts, false, _, _) = $0.kind { return true } else { return false }
            }), case .agent(let t, let ru, _, _, _, _) = call.transcript[idx].kind {
                call.transcript[idx] = .init(kind: .agent(text: t, textRu: ru, utteranceTs: uts,
                                                          interrupted: true, spokenText: spoken,
                                                          spokenFraction: fraction))
            }
        case .callState(let status, let muted, let held):
            call.session = VGSessionInfo(id: call.session.id, status: status,
                                         phone: call.session.phone,
                                         callDirection: call.session.callDirection,
                                         createdAt: call.session.createdAt,
                                         updatedAt: call.session.updatedAt,
                                         srcLang: call.session.srcLang,
                                         tgtLang: call.session.tgtLang,
                                         callBrief: call.session.callBrief)
            observed[id] = call
            panel.updateStatus(status: status, muted: muted, held: held, badge: nil)
            refreshHUD()
            return
        case .callEnded, .callClosed:
            observed[id] = call
            deliverTerminal(sessionId: id, generation: gen, message: "Звонок завершён")
            return
        case .diagnosticError:
            append(&call, .init(kind: .system("Реплика не переведена")))
        case .screeningStarted:
            append(&call, .init(kind: .system("Скрининг входящего")))
        case .costAlert(_, let usd, _):
            panel.updateCost(usd.map { String(format: "⚠ $%.2f", $0) } ?? "⚠")
            return
        case .callRinging, .callAnswered, .ignored:
            return
        }
        observed[id] = call
        pushTranscript()
        refreshHUD()
    }

    private func append(_ call: inout ObservedCall, _ entry: TranscriptEntry) {
        call.transcript.append(entry)
        if call.transcript.count > Self.transcriptCap {
            call.transcript.removeFirst(call.transcript.count - Self.transcriptCap)
        }
    }

    // MARK: - §4.1 one-shot terminal

    private func deliverTerminal(sessionId: String, generation: UInt64, message: String) {
        guard var call = observed[sessionId], call.generation == generation,
              !call.terminalDelivered else { return }
        call.terminalDelivered = true
        observed[sessionId] = call

        if sessionId == selectedId {
            stream.disconnect()
            player.stopListening()
            stopCostTimer()
            panel.closeHangupSheetIfOpen()
            if panel.isPanelVisible { panel.setTerminal(message: message) }
        }
        hangupInFlight = false

        // Linger: только если НЕТ другого живого звонка и HUD не закрыт вручную.
        let anotherLive = observed.contains { $0.key != sessionId && !$0.value.terminalDelivered }
        if !anotherLive && !hudManuallyClosed && (hud.isHUDVisible || hudAutoShown) {
            hud.showLinger(message: message)
            lingerWork?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self, !self.hasLiveCall else { return }  // появился живой — не прятать
                self.hud.hideHUD()
                self.hudAutoShown = false
            }
            lingerWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + lingerSeconds, execute: work)
        }
        refreshHUD()
    }

    // MARK: - User actions

    func userExpandedHUD() {
        guard let id = selectedId, let call = observed[id] else { return }
        panelOpenedManually = true
        hud.hideHUD()
        panel.showPanel(session: call.session)
        if call.terminalDelivered {
            panel.setTerminal(message: "Звонок завершён")
        } else {
            panel.setLive()
            startCostTimer()
        }
        pushTranscript()
    }

    func userClosedHUD() {
        hudManuallyClosed = true
        hudAutoShown = false
        hud.hideHUD()
        // Окна ≠ соединения: стримы/прослушка живут дальше (§4).
    }

    func userClosedPanel() {
        panelOpenedManually = false
        stopCostTimer()
        // Соединения не рвём (§4): транскрипт копится, аудио играет дальше.
    }

    func userToggledListen() {
        guard let id = selectedId, let call = observed[id], !call.terminalDelivered else { return }
        if listenState == .idle || listenState == .subscriberLimit || listenState == .failed {
            listenStartedManually = true
            player.startListening(baseURL: baseURL, sessionId: id,
                                  generation: call.generation, tokenProvider: tokenProvider)
        } else {
            listenStartedManually = false
            player.stopListening()
        }
    }

    func userRequestedHangupConfirmed() {
        guard let id = selectedId, let call = observed[id],
              !call.terminalDelivered, !hangupInFlight else { return }
        hangupInFlight = true
        let gen = call.generation
        poster.hangup(baseURL: baseURL, sessionId: id) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.hangupInFlight = false
                switch result {
                case .success(let code) where code == 200 || code == 404 || code == 409:
                    // 200 → терминал приедет по WS/поллу, но не ждём: сигнал №4.
                    // 404/409 после конца — тихо, звонок и так мёртв (§2.4).
                    self.deliverTerminal(sessionId: id, generation: gen,
                                         message: "Звонок завершён")
                case .success, .failure:
                    self.panel.updateStatus(status: self.observed[id]?.session.status ?? "",
                                            muted: nil, held: nil,
                                            badge: "Не удалось положить трубку")
                }
            }
        }
    }

    /// Трубка из HUD: панель откроется и поднимет confirm-sheet (HUD без окна для sheet).
    func userRequestedHangupFromHUD() {
        userExpandedHUD()
        panel.presentHangupConfirm()
    }

    func userSelectedSession(_ id: String) {
        guard let call = observed[id], id != selectedId else { return }
        selectedId = id
        stream.disconnect()
        player.stopListening()
        panel.showPanel(session: call.session)  // showPanel НЕ трогает state-бейдж
        if call.terminalDelivered {
            panel.setTerminal(message: "Звонок завершён")
        } else {
            panel.setLive()
            connectStreams(for: call)
            startCostTimer()
        }
        pushTranscript()
    }

    func openPanelFromMenu() {
        guard let id = selectedId, let call = observed[id] else { return }
        panelOpenedManually = true
        panel.showPanel(session: call.session)
        if call.terminalDelivered {
            panel.setTerminal(message: "Звонок завершён")
        } else {
            panel.setLive()
            startCostTimer()
        }
        pushTranscript()
    }

    /// Для пикера сессий в панели (>1 одновременных звонков — редкость).
    func observedSessions() -> [(id: String, label: String)] {
        observed
            .map { ($0.key, $0.value.session.phone.isEmpty ? $0.key : $0.value.session.phone) }
            .sorted { $0.0 < $1.0 }
    }

    // MARK: - Internals

    private func connectStreams(for call: ObservedCall) {
        stream.connect(baseURL: baseURL, sessionId: call.session.id,
                       generation: call.generation, tokenProvider: tokenProvider)
    }

    private func maybeAutoShowHUD(for s: VGSessionInfo) {
        guard hudEnabled, !privacyMode, !hudManuallyClosed, !panel.isPanelVisible else { return }
        // HUD следит за НОВЕЙШИМ живым звонком.
        hudAutoShown = true
        hud.showHUD(session: s)
    }

    private func refreshHUD() {
        guard hud.isHUDVisible, let id = selectedId, let call = observed[id] else { return }
        hud.updateHUD(status: call.session.status,
                      lastEntries: Array(call.transcript.suffix(2)),
                      listenState: listenState)
    }

    private func pushTranscript() {
        guard panel.isPanelVisible, let id = selectedId, let call = observed[id] else { return }
        if uiCoalesceInterval <= 0 {
            panel.updateTranscript(call.transcript)
            return
        }
        // Коалесируем шторм событий (§8): не чаще одного рендера в uiCoalesceInterval.
        if pushWork != nil { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pushWork = nil
            guard self.panel.isPanelVisible, let id = self.selectedId,
                  let call = self.observed[id] else { return }
            self.panel.updateTranscript(call.transcript)
        }
        pushWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + uiCoalesceInterval, execute: work)
    }

    private func refreshSettings() {
        settings.refresh { [weak self] hudEnabled, autoplay, privacy, base in
            let apply = {
                guard let self else { return }
                self.hudEnabled = hudEnabled
                self.autoplay = autoplay
                self.privacyMode = privacy
                self.baseURL = base
            }
            // Синхронный провайдер на main (тесты, кэш) обязан примениться ДО
            // maybeAutoShowHUD — иначе privacy-гейт первого звонка читает стейл.
            if Thread.isMainThread { apply() } else { DispatchQueue.main.async(execute: apply) }
        }
    }

    /// Чекбоксы настроек зовут это напрямую (generic-нотификации в агенте НЕТ).
    func settingsDidChange() {
        refreshSettings()
        applyPrivacySuppressionIfNeeded()
    }

    /// privacy включили мид-колл: авто-показанный HUD прячем, autoplay-аудио глушим;
    /// вручную открытое/включённое остаётся (явные действия владельца).
    private func applyPrivacySuppressionIfNeeded() {
        guard privacyMode else { return }
        if hudAutoShown && hud.isHUDVisible { hud.hideHUD(); hudAutoShown = false }
        if !listenStartedManually && listenState != .idle { player.stopListening() }
    }

    private func startCostTimer() {
        stopCostTimer()
        guard let id = selectedId else { return }
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + costPollInterval, repeating: costPollInterval)
        t.setEventHandler { [weak self] in
            guard let self, let call = self.observed[id], !call.terminalDelivered else { return }
            self.poster.fetchCostUsd(baseURL: self.baseURL, sessionId: id) { usd in
                DispatchQueue.main.async {
                    self.panel.updateCost(usd.map { String(format: "$%.2f", $0) } ?? "—")
                }
            }
        }
        t.resume()
        costTimer = t
    }

    private func stopCostTimer() {
        costTimer?.cancel()
        costTimer = nil
    }
}
```

⚠️ Two known simplifications the implementer must carry over EXACTLY (they are spec decisions, not accidents): (1) `settings.refresh` is async — the first `callAppeared` may run with defaults for one tick; acceptable, self-heals on the next poll tick. (2) `handleStreamEvent` `.callState` branch keeps the parsed status only in `session.status` copy — it deliberately does not rebuild all fields.

- [ ] **Step 4: Write `main+CallObserver.swift`** — real wiring:

First, VERIFY the anchors (exact commands):
- `grep -n "completeStartupAfterBackendReady\|applicationWillTerminate" native/KrabEarAgent/Sources/KrabEarAgent/main.swift | head -5` → the startup/shutdown methods to hook.
- `grep -n "get_voice_gateway_credential" native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+VoiceTab.swift | head -3` → the EXISTING call-site pattern for the credential IPC (W1892) — mirror its off-main invocation style.
- `grep -n "func \|menuWillOpen" native/KrabEarAgent/Sources/KrabEarAgent/main+BrainLease.swift | head -8` → the status-menu-item pattern to mirror.
- `grep -rn "callAsyncWithRecovery\|callWithRecovery" native/KrabEarAgent/Sources/KrabEarAgent/main+IPCRecovery.swift | head -3` → the project's ONE IPC-recovery helper (28 call sites; do not write a second).

```swift
import AppKit
import Foundation

/// Проводка Call Observer (spec §3 комп. 6): единственный владелец —
/// AgentAppDelegate.callObserverCoordinator. Креденшел VG — через
/// существующий IPC get_voice_gateway_credential (W1892), кэш в памяти;
/// backend, умерший мид-сессии, оставляет кэш живым.
extension AgentAppDelegate {
    func setupCallObserver() {
        let settings = IPCCallObserverSettings(ipcCall: { [weak self] method, params, cb in
            // СУЩЕСТВУЮЩИЙ off-main IPC-хелпер проекта (см. verification grep) —
            // тот же путь, каким VoiceTab зовёт get_voice_gateway_credential.
            self?.callObserverIPC(method: method, params: params, completion: cb)
        })
        let tokenProvider: () -> String = { settings.lastApiKey }
        let hudController = CallObserverHUD()
        let panelController = CallObserverPanelController()
        let coordinator = CallObserverCoordinator(
            hud: hudController, panel: panelController,
            poster: URLSessionVGCommandPoster(tokenProvider: tokenProvider),
            settings: settings,
            stream: VGCallStreamClient(), player: CallAudioPlayer(),
            tokenProvider: tokenProvider)
        hudController.coordinator = coordinator
        panelController.coordinator = coordinator
        self.callObserverCoordinator = coordinator
        let watcher = VGSessionWatcher(fetcher: URLSessionVGSessionFetcher(
            baseURLProvider: { settings.lastBaseURL }, tokenProvider: tokenProvider))
        watcher.delegate = coordinator
        self.callObserverWatcher = watcher
        watcher.start()
    }

    func tearDownCallObserver() {
        callObserverWatcher?.stop()
    }
}

/// GET /v1/sessions?limit=20 (off-main, ephemeral session, timeout 5с).
final class URLSessionVGSessionFetcher: VGSessionFetching {
    private let baseURLProvider: () -> URL
    private let tokenProvider: () -> String
    private let session: URLSession

    init(baseURLProvider: @escaping () -> URL, tokenProvider: @escaping () -> String) {
        self.baseURLProvider = baseURLProvider
        self.tokenProvider = tokenProvider
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 5
        session = URLSession(configuration: cfg)
    }

    func fetchSessions(completion: @escaping (Result<(statusCode: Int, body: Data), Error>) -> Void) {
        var url = baseURLProvider()
        url.append(path: "/v1/sessions")
        url.append(queryItems: [URLQueryItem(name: "limit", value: "20")])
        let req = VGWebSocketConnection.makeRequest(url: url, token: tokenProvider())
        session.dataTask(with: req) { data, resp, error in
            if let error { completion(.failure(error)); return }
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            completion(.success((code, data ?? Data())))
        }.resume()
    }
}

/// POST hangup + GET diagnostics (cost) — off-main.
final class URLSessionVGCommandPoster: VGCommandPosting {
    private let tokenProvider: () -> String
    private let session: URLSession

    init(tokenProvider: @escaping () -> String) {
        self.tokenProvider = tokenProvider
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 10
        session = URLSession(configuration: cfg)
    }

    func hangup(baseURL: URL, sessionId: String, completion: @escaping (Result<Int, Error>) -> Void) {
        var url = baseURL
        url.append(path: "/v1/telephony/calls/\(sessionId)/hangup")
        var req = VGWebSocketConnection.makeRequest(url: url, token: tokenProvider())
        req.httpMethod = "POST"
        session.dataTask(with: req) { _, resp, error in
            if let error { completion(.failure(error)); return }
            completion(.success((resp as? HTTPURLResponse)?.statusCode ?? 0))
        }.resume()
    }

    func fetchCostUsd(baseURL: URL, sessionId: String, completion: @escaping (Double?) -> Void) {
        var url = baseURL
        url.append(path: "/v1/sessions/\(sessionId)/diagnostics")
        let req = VGWebSocketConnection.makeRequest(url: url, token: tokenProvider())
        session.dataTask(with: req) { data, _, _ in
            // Реальный VG кладёт costs НА ВЕРХНИЙ уровень ({**diag, status, ...}).
            guard let data,
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let costs = obj["costs"] as? [String: Any],
                  let total = costs["total_usd"] as? Double else { completion(nil); return }
            completion(total)
        }.resume()
    }
}

/// Настройки + креденшел через IPC, кэш в памяти (backend может умереть мид-сессии).
final class IPCCallObserverSettings: CallObserverSettingsProviding {
    private let ipcCall: (String, [String: Any], @escaping ([String: Any]?) -> Void) -> Void
    private(set) var lastBaseURL = URL(string: "http://127.0.0.1:8090")!
    private(set) var lastApiKey = ""

    init(ipcCall: @escaping (String, [String: Any], @escaping ([String: Any]?) -> Void) -> Void) {
        self.ipcCall = ipcCall
    }

    func refresh(completion: @escaping (Bool, Bool, Bool, URL) -> Void) {
        // Два вызова: булы+privacy из get_settings; url+key из узкого W1892-хендлера
        // (get_settings редактирует ключ — wave-35 CRIT).
        ipcCall("get_voice_gateway_credential", [:]) { [weak self] cred in
            if let cred, cred["ok"] as? Bool == true {
                if let url = (cred["voice_gateway_url"] as? String).flatMap(URL.init(string:)) {
                    self?.lastBaseURL = url
                }
                if let key = cred["voice_gateway_api_key"] as? String {
                    self?.lastApiKey = key  // IPC-провал → живёт последний успешный кэш
                }
            }
            self?.ipcCall("get_settings", [:]) { result in
                let s = (result?["settings"] as? [String: Any]) ?? result ?? [:]
                let hud = s["call_observer_hud_enabled"] as? Bool ?? true
                let autoplay = s["call_observer_autoplay_audio"] as? Bool ?? false
                let privacy = s["privacy_mode_enabled"] as? Bool ?? false
                completion(hud, autoplay, privacy,
                           self?.lastBaseURL ?? URL(string: "http://127.0.0.1:8090")!)
            }
        }
    }
}
```

Wiring verification steps the implementer MUST do (exact identifiers differ from this sketch):
1. `AgentAppDelegate` stored properties `callObserverCoordinator` / `callObserverWatcher` — add where sibling controllers are stored in `main.swift` (grep `quickCapturePanelController`).
2. Call `setupCallObserver()` from the SAME place `setupHealthMonitor()` is called at startup, and `tearDownCallObserver()` from `applicationWillTerminate` (decorative-wiring bug class: defined-but-never-called setup).
3. `callObserverIPC` = thin wrapper over the project's existing off-main IPC helper (verification grep above); `get_settings` response envelope — mirror an existing call site (`grep -n "get_settings" native/KrabEarAgent/Sources/KrabEarAgent/*.swift | head -5`). IPC strictly off-main.
4. Status-menu item «Звонок агента…» — mirror `main+BrainLease.swift` (insert item, enable only when `coordinator.hasLiveCall`, action → `openPanelFromMenu()`).

- [ ] **Step 5: Write `MainCallObserverWiringTests.swift`** — source-contract (the decorative-wiring guard):

```swift
import XCTest

/// Source-контракт: setupCallObserver реально вызывается из старта агента
/// (класс бага setupErrorBus/setupHealthMonitor — определено, но не вызвано).
final class MainCallObserverWiringTests: XCTestCase {
    private func sourceText(_ file: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        return try String(contentsOf: root.appendingPathComponent(file), encoding: .utf8)
    }

    func test_setupCallObserver_is_actually_called_from_startup() throws {
        let main = try sourceText("main.swift")
        XCTAssertTrue(main.contains("setupCallObserver()"),
                      "setupCallObserver определён, но не вызван из main.swift — декоративная проводка")
    }

    func test_tearDownCallObserver_is_actually_called_from_shutdown() throws {
        let main = try sourceText("main.swift")
        XCTAssertTrue(main.contains("tearDownCallObserver()"))
    }
}
```

- [ ] **Step 6: Run all Task-8 tests — verify PASS**

Run: `cd native/KrabEarAgent && swift test --filter CallObserverCoordinatorTests && swift test --filter MainCallObserverWiringTests`

- [ ] **Step 7: Full build + commit**

```bash
cd native/KrabEarAgent && swift build -c release && cd ../..
git add native/KrabEarAgent/Sources/KrabEarAgent/CallObserverCoordinator.swift native/KrabEarAgent/Sources/KrabEarAgent/main+CallObserver.swift native/KrabEarAgent/Sources/KrabEarAgent/main.swift native/KrabEarAgent/Tests/KrabEarAgentTests/CallObserverCoordinatorTests.swift native/KrabEarAgent/Tests/KrabEarAgentTests/MainCallObserverWiringTests.swift
git commit -m "feat(call-observer): координатор §4.1 (one-shot автомат, per-session generations) + проводка (w1 T8)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Swift — HUD + Panel (functional UI) + settings checkboxes

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/CallObserverHUD.swift`
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/CallObserverPanelController.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+LiveSubsSettings.swift` (2 checkboxes)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/CallObserverUITests.swift`

**Interfaces:**
- Consumes: `CallObserverHUDPresenting`, `CallObserverPanelPresenting`, `TranscriptEntry`, `CallAudioPlayer.ListenState` (T8); `KrabEarTheme` tokens; `presentAlertSheet` from `AlertHelpers.swift`.
- Produces: `final class CallObserverHUD: NSObject, CallObserverHUDPresenting` with `weak var coordinator: CallObserverCoordinator?`; `final class CallObserverPanelController: NSWindowController, CallObserverPanelPresenting` with `weak var coordinator: CallObserverCoordinator?`.

Functional-but-unstyled is the deliverable — visual polish is a SEPARATE agy/Gemini brief AFTER merge (spec §7; do not gold-plate).

- [ ] **Step 1: Write the failing tests** (behavioral, not pixel):

```swift
import AppKit
import XCTest
@testable import KrabEarAgent

final class CallObserverUITests: XCTestCase {
    private func session(_ id: String = "s1") -> VGSessionInfo {
        VGSessionInfo(id: id, status: "running", phone: "+34 600 000 000",
                      callDirection: "outbound", createdAt: "2026-08-21T10:00:00Z",
                      updatedAt: "2026-08-21T10:00:00Z", srcLang: "es", tgtLang: "ru", callBrief: "")
    }

    func test_hud_show_hide_visibility() {
        let hud = CallObserverHUD()
        XCTAssertFalse(hud.isHUDVisible)
        hud.showHUD(session: session())
        XCTAssertTrue(hud.isHUDVisible)
        hud.hideHUD()
        XCTAssertFalse(hud.isHUDVisible)
    }

    func test_hud_buttons_are_sf_symbols_not_text_glyphs() {
        let hud = CallObserverHUD()
        hud.showHUD(session: session())
        XCTAssertNotNil(hud.testHook_listenButton.image, "кнопка прослушки обязана быть SF Symbol")
        XCTAssertNotNil(hud.testHook_hangupButton.image)
        XCTAssertTrue(hud.testHook_listenButton.title.isEmpty, "никаких эмодзи-тайтлов (AGENT-J/M)")
        hud.hideHUD()
    }

    func test_hud_click_vs_drag_threshold() {
        XCTAssertTrue(CallObserverHUD.isClick(down: .init(x: 10, y: 10), up: .init(x: 12, y: 11)))
        XCTAssertFalse(CallObserverHUD.isClick(down: .init(x: 10, y: 10), up: .init(x: 40, y: 10)))
    }

    func test_panel_terminal_and_live_states() {
        let panel = CallObserverPanelController()
        panel.showPanel(session: session())
        panel.setTerminal(message: "Звонок завершён")
        XCTAssertEqual(panel.testHook_stateBadgeText, "Звонок завершён")
        panel.setLive()
        XCTAssertNotEqual(panel.testHook_stateBadgeText, "Звонок завершён")
        panel.close()
    }

    func test_panel_renders_interrupted_prefix() {
        let panel = CallObserverPanelController()
        panel.showPanel(session: session())
        panel.updateTranscript([
            .init(kind: .agent(text: "Полный текст", textRu: nil, utteranceTs: "u1",
                               interrupted: true, spokenText: "Полн", spokenFraction: 0.3)),
        ])
        let rendered = panel.testHook_transcriptPlainText
        XCTAssertTrue(rendered.contains("Полн"))
        XCTAssertTrue(rendered.contains("прервано"), "показать, ЧТО собеседник реально услышал")
        panel.close()
    }

    func test_panel_no_runModal_source_contract() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        for file in ["CallObserverPanelController.swift", "CallObserverHUD.swift", "main+CallObserver.swift"] {
            let text = try String(contentsOf: root.appendingPathComponent(file), encoding: .utf8)
            XCTAssertFalse(text.contains("runModal"), "\(file): runModal запрещён (Sequoia AppHang)")
        }
    }
}
```

- [ ] **Step 2: Run — verify FAIL**

- [ ] **Step 3: Implement `CallObserverHUD.swift`**

```swift
import AppKit

/// Плавающая плашка звонка (spec §3 комп. 4; паттерн LiveSubtitlesOverlay).
/// Клик (mouseUp без движения) → разворот в панель; кнопки — SF Symbols.
final class CallObserverHUD: NSObject, CallObserverHUDPresenting {
    weak var coordinator: CallObserverCoordinator?

    private var panel: NSPanel?
    private let statusLabel = NSTextField(labelWithString: "")
    private let linesLabel = NSTextField(wrappingLabelWithString: "")
    private let listenButton = NSButton()
    private let hangupButton = NSButton()
    private let closeButton = NSButton()
    private var mouseDownPoint: NSPoint?
    private var elapsedTimer: Timer?
    private var callCreatedAt: Date?

    var isHUDVisible: Bool { panel?.isVisible ?? false }

    static func isClick(down: NSPoint, up: NSPoint) -> Bool {
        hypot(up.x - down.x, up.y - down.y) < 4.0
    }

    func showHUD(session: VGSessionInfo) {
        if panel == nil { buildPanel() }
        callCreatedAt = ISO8601DateFormatter().date(from: session.createdAt)
        statusLabel.stringValue = "● \(session.callDirection) \(session.phone)"
        linesLabel.stringValue = ""
        startElapsedTimer()
        panel?.orderFrontRegardless()
    }

    func updateHUD(status: String, lastEntries: [TranscriptEntry], listenState: CallAudioPlayer.ListenState) {
        let lines = lastEntries.map { entry -> String in
            switch entry.kind {
            case .remote(let text, let tr):
                return tr.map { "Он: \(text) / \($0)" } ?? "Он: \(text)"
            case .agent(let text, let ru, _, let interrupted, let spoken, _):
                let shown = interrupted ? (spoken ?? text) + " …" : text
                return ru.map { "Агент: \(shown) / \($0)" } ?? "Агент: \(shown)"
            case .system(let msg):
                return "· \(msg)"
            }
        }
        linesLabel.stringValue = lines.joined(separator: "\n")
        listenButton.contentTintColor = (listenState == .listening) ? .systemGreen : nil
        listenButton.toolTip = listenState == .subscriberLimit
            ? "Лимит слушателей VG — попробуйте ещё раз" : "Слушать звонок"
    }

    func showLinger(message: String) {
        statusLabel.stringValue = message
        elapsedTimer?.invalidate()
    }

    func hideHUD() {
        elapsedTimer?.invalidate()
        panel?.orderOut(nil)
    }

    private func startElapsedTimer() {
        elapsedTimer?.invalidate()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self, let created = self.callCreatedAt else { return }
            let s = Int(Date().timeIntervalSince(created))
            let mmss = String(format: "%02d:%02d", s / 60, s % 60)
            var text = self.statusLabel.stringValue
            if let dotRange = text.range(of: " · ") { text = String(text[..<dotRange.lowerBound]) }
            self.statusLabel.stringValue = text + " · " + mmss
        }
    }

    private func buildPanel() {
        let p = NSPanel(contentRect: NSRect(x: 120, y: 120, width: 340, height: 96),
                        styleMask: [.nonactivatingPanel, .borderless, .utilityWindow],
                        backing: .buffered, defer: false)
        p.level = .floating
        p.isMovableByWindowBackground = true
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hidesOnDeactivate = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let content = HUDClickView()
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.72).cgColor
        content.layer?.cornerRadius = 12
        content.onClick = { [weak self] in self?.coordinator?.userExpandedHUD() }

        statusLabel.textColor = .white
        statusLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        linesLabel.textColor = NSColor.white.withAlphaComponent(0.85)
        linesLabel.font = .systemFont(ofSize: 11)
        linesLabel.maximumNumberOfLines = 3

        configure(listenButton, symbol: "speaker.wave.2", accessibility: "Слушать") { [weak self] in
            self?.coordinator?.userToggledListen()
        }
        configure(hangupButton, symbol: "phone.down.fill", accessibility: "Положить трубку") { [weak self] in
            self?.coordinator?.userRequestedHangupFromHUD()
        }
        configure(closeButton, symbol: "xmark", accessibility: "Скрыть") { [weak self] in
            self?.coordinator?.userClosedHUD()
        }

        let buttons = NSStackView(views: [listenButton, hangupButton, closeButton])
        buttons.orientation = .horizontal
        let stack = NSStackView(views: [statusLabel, linesLabel, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
        ])
        p.contentView = content
        panel = p
    }

    private func configure(_ button: NSButton, symbol: String, accessibility: String,
                           action: @escaping () -> Void) {
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: accessibility)
        button.title = ""
        button.bezelStyle = .circular
        button.setButtonType(.momentaryPushIn)
        buttonActions[ObjectIdentifier(button)] = action
        button.target = self
        button.action = #selector(buttonTapped(_:))
    }

    private var buttonActions: [ObjectIdentifier: () -> Void] = [:]

    @objc private func buttonTapped(_ sender: NSButton) {
        buttonActions[ObjectIdentifier(sender)]?()
    }

    // MARK: Test hooks
    var testHook_listenButton: NSButton { listenButton }
    var testHook_hangupButton: NSButton { hangupButton }
}

/// Клик-vs-драг: mouseUp < 4pt от mouseDown = клик (isMovableByWindowBackground
/// перехватывает драг сам, но короткий mouseDown/Up доходит).
private final class HUDClickView: NSView {
    var onClick: (() -> Void)?
    private var downPoint: NSPoint?

    override func mouseDown(with event: NSEvent) {
        downPoint = event.locationInWindow
        super.mouseDown(with: event)
    }

    override func mouseUp(with event: NSEvent) {
        if let down = downPoint,
           CallObserverHUD.isClick(down: down, up: event.locationInWindow) {
            onClick?()
        }
        downPoint = nil
        super.mouseUp(with: event)
    }
}
```

- [ ] **Step 4: Implement `CallObserverPanelController.swift`**

```swift
import AppKit

/// Полное окно звонка (spec §3 комп. 5; каркас — MeetingLivePanelController,
/// но источник данных — WS-клиент координатора, НЕ SSE). Терминал НЕ закрывает
/// окно: транскрипт — единственная копия (§3 комп. 5).
final class CallObserverPanelController: NSWindowController, CallObserverPanelPresenting {
    weak var coordinator: CallObserverCoordinator?

    private let stateBadge = NSTextField(labelWithString: "")
    private let costLabel = NSTextField(labelWithString: "—")
    private let listenButton = NSButton()
    private let hangupButton = NSButton()
    private let transcriptStack = NSStackView()
    private let scrollView = NSScrollView()
    private var hangupSheetOpen = false

    var isPanelVisible: Bool { window?.isVisible ?? false }

    convenience init() {
        let win = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 520, height: 560),
                           styleMask: [.titled, .closable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "Звонок агента"
        self.init(window: win)
        buildUI()
    }

    func showPanel(session: VGSessionInfo) {
        window?.title = "Звонок агента · \(session.phone.isEmpty ? session.id : session.phone)"
        // State-бейдж (live/terminal) задаёт ТОЛЬКО координатор — showPanel его не трогает,
        // иначе открытие панели по терминальной сессии перетёрло бы setTerminal.
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        refreshSessionPicker()
    }

    func updateTranscript(_ entries: [TranscriptEntry]) {
        transcriptStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        for entry in entries.suffix(200) {  // рендер-кап; полный буфер держит координатор
            transcriptStack.addArrangedSubview(row(for: entry))
        }
        if let doc = scrollView.documentView {
            doc.scroll(NSPoint(x: 0, y: doc.bounds.maxY))
        }
    }

    func updateStatus(status: String, muted: Bool?, held: Bool?, badge: String?) {
        var parts = [status]
        if muted == true { parts.append("mute") }
        if held == true { parts.append("hold") }
        if let badge { parts.append(badge) }
        stateBadge.stringValue = parts.joined(separator: " · ")
    }

    func updateCost(_ text: String) { costLabel.stringValue = text }

    func setTerminal(message: String) {
        stateBadge.stringValue = message
        listenButton.isEnabled = false
        hangupButton.isEnabled = false
        closeHangupSheetIfOpen()
    }

    func setLive() {
        stateBadge.stringValue = "в эфире"
        listenButton.isEnabled = true
        hangupButton.isEnabled = true
    }

    func closeHangupSheetIfOpen() {
        guard hangupSheetOpen, let window, let sheet = window.attachedSheet else { return }
        window.endSheet(sheet, returnCode: .cancel)
        hangupSheetOpen = false
    }

    func presentHangupConfirm() { onHangupTapped() }

    @objc private func onListenTapped() { coordinator?.userToggledListen() }

    @objc private func onHangupTapped() {
        guard let window, !hangupSheetOpen else { return }
        hangupSheetOpen = true
        let alert = NSAlert()
        alert.messageText = "Положить трубку?"
        alert.informativeText = "Звонок агента будет завершён."
        alert.addButton(withTitle: "Положить трубку")
        alert.addButton(withTitle: "Отмена")
        presentAlertSheet(alert, for: window) { [weak self] response in
            self?.hangupSheetOpen = false
            if response == .alertFirstButtonReturn {
                self?.coordinator?.userRequestedHangupConfirmed()
            }
        }
    }

    private func row(for entry: TranscriptEntry) -> NSView {
        let label = NSTextField(wrappingLabelWithString: "")
        label.font = .systemFont(ofSize: 12)
        switch entry.kind {
        case .remote(let text, let translation):
            label.stringValue = translation.map { "Собеседник: \(text)\n  → \($0)" } ?? "Собеседник: \(text)"
        case .agent(let text, let ru, _, let interrupted, let spoken, let fraction):
            if interrupted {
                let pct = fraction.map { Int($0 * 100) } ?? 0
                label.stringValue = "Агент: \(spoken ?? text) [прервано \(pct) %]"
                label.textColor = .secondaryLabelColor
            } else {
                label.stringValue = ru.map { "Агент: \(text)\n  → \($0)" } ?? "Агент: \(text)"
            }
        case .system(let msg):
            label.stringValue = "· \(msg)"
            label.textColor = .secondaryLabelColor
        }
        return label
    }

    private func buildUI() {
        guard let content = window?.contentView else { return }
        listenButton.image = NSImage(systemSymbolName: "speaker.wave.2", accessibilityDescription: "Слушать")
        listenButton.title = ""
        listenButton.target = self
        listenButton.action = #selector(onListenTapped)
        hangupButton.image = NSImage(systemSymbolName: "phone.down.fill", accessibilityDescription: "Положить трубку")
        hangupButton.title = ""
        hangupButton.target = self
        hangupButton.action = #selector(onHangupTapped)

        sessionPicker.target = self
        sessionPicker.action = #selector(onSessionPicked)
        sessionPicker.isHidden = true
        let header = NSStackView(views: [stateBadge, sessionPicker, NSView(), costLabel, listenButton, hangupButton])
        header.orientation = .horizontal
        header.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 4, right: 12)

        transcriptStack.orientation = .vertical
        transcriptStack.alignment = .leading
        transcriptStack.spacing = 6
        transcriptStack.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        transcriptStack.translatesAutoresizingMaskIntoConstraints = false

        scrollView.documentView = transcriptStack
        scrollView.hasVerticalScroller = true

        let root = NSStackView(views: [header, scrollView])
        root.orientation = .vertical
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scrollView.widthAnchor.constraint(equalTo: root.widthAnchor),
        ])
        window?.delegate = self
    }

    private let sessionPicker = NSPopUpButton()

    /// Пикер >1 одновременных звонков (spec §3.1); скрыт при единственной сессии.
    func refreshSessionPicker() {
        let sessions = coordinator?.observedSessions() ?? []
        sessionPicker.isHidden = sessions.count <= 1
        sessionPicker.removeAllItems()
        for (id, label) in sessions {
            sessionPicker.addItem(withTitle: label)
            sessionPicker.lastItem?.representedObject = id
        }
    }

    @objc private func onSessionPicked() {
        guard let id = sessionPicker.selectedItem?.representedObject as? String else { return }
        coordinator?.userSelectedSession(id)
    }

    // MARK: Test hooks
    var testHook_stateBadgeText: String { stateBadge.stringValue }
    var testHook_transcriptPlainText: String {
        transcriptStack.arrangedSubviews
            .compactMap { ($0 as? NSTextField)?.stringValue }
            .joined(separator: "\n")
    }
}

extension CallObserverPanelController: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) {
        coordinator?.userClosedPanel()
    }
}
```

⚠️ `transcriptStack` inside `NSScrollView` needs a width constraint to the clip view and `translatesAutoresizingMaskIntoConstraints=false` on the documentView — if rows render zero-width, mirror the scroll-view setup from `MeetingLivePanelController` (verification: `grep -n "documentView\|NSScrollView" native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift | head -8`).

- [ ] **Step 5: Settings checkboxes**

In `HistoryPanelController+LiveSubsSettings.swift`, mirror the EXISTING checkbox pattern of that section (verification: `grep -n "NSButton(checkboxWithTitle\|set_settings" native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+LiveSubsSettings.swift | head -8`) and add two checkboxes:
- «Панель звонка агента при звонке» → key `call_observer_hud_enabled`
- «Сразу включать звук звонка» → key `call_observer_autoplay_audio`

Both: read current value from the section's settings load path, write via the section's existing `set_settings` helper (off-main), and after a successful save call `(NSApp.delegate as? AgentAppDelegate)?.callObserverCoordinator?.settingsDidChange()` — no generic settings notification exists (spec §5); the coordinator also re-reads on each poll tick, so the direct call is best-effort freshness, not correctness.

- [ ] **Step 6: Run — verify PASS** (`swift test --filter CallObserverUITests`)

- [ ] **Step 7: Full build + glyph check + commit**

```bash
cd native/KrabEarAgent && swift build -c release && cd ../..
# Глиф-гейт: новые non-ASCII строки — только кириллица/типографика, уже живущие в native/
grep -o '[^\x00-\x7F]' native/KrabEarAgent/Sources/KrabEarAgent/CallObserverHUD.swift native/KrabEarAgent/Sources/KrabEarAgent/CallObserverPanelController.swift native/KrabEarAgent/Sources/KrabEarAgent/CallObserverCoordinator.swift native/KrabEarAgent/Sources/KrabEarAgent/main+CallObserver.swift | sort -u
# Ожидаемо: кириллица, «», ·, →, …, % — НИКАКИХ эмодзи. Для каждого нового символа: grep -rF '<символ>' native/ | head -1 — должен встречаться и вне новых файлов; иначе заменить.
git add native/KrabEarAgent/Sources/KrabEarAgent/CallObserverHUD.swift native/KrabEarAgent/Sources/KrabEarAgent/CallObserverPanelController.swift native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+LiveSubsSettings.swift native/KrabEarAgent/Tests/KrabEarAgentTests/CallObserverUITests.swift
git commit -m "feat(call-observer): HUD + панель (функциональный UI, SF Symbols) + чекбоксы настроек (w1 T9)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: fake VG server + e2e smoke + NOW.md

**Files:**
- Create: `scripts/fake_vg_server.py`
- Create: `scripts/e2e_call_observer_smoke.command`
- Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/CallObserverE2ETests.swift`
- Modify: `docs/NOW.md` (wave card)

**Interfaces:**
- Consumes: everything T2–T8 (integration).
- Produces: merge-gate e2e. XCTests are SKIPPED unless env `KRAB_E2E_VG_PORT` is set (unit CI stays hermetic).

- [ ] **Step 1: Create `scripts/fake_vg_server.py`**

```python
#!/usr/bin/env python3
"""Fake Voice Gateway для e2e Call Observer w1 (spec §6).

Реализует ровно потребляемое подмножество контракта VG:
GET /v1/sessions, WS /v1/sessions/<id>/stream (скриптованные события),
WS /v1/sessions/<id>/monitor/audio (metadata + μ-law синус 440Гц),
GET /v1/sessions/<id>/diagnostics, POST /v1/telephony/calls/<id>/hangup.

Таймлайн: сессия появляется через 1с после старта, события идут по сценарию,
звонок живёт до hangup или 60с. Порт — argv[1] (default 18090).
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time

from flask import Flask, jsonify
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

START_TS = time.time()
SESSION_ID = "e2e-call-1"
STATE = {"status": "running", "hangup_calls": 0, "started": START_TS + 1.0}
LOCK = threading.Lock()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _session_row() -> dict:
    with LOCK:
        return {
            "id": SESSION_ID, "status": STATE["status"], "phone": "+34600111222",
            "call_direction": "outbound", "created_at": _now_iso(),
            "updated_at": _now_iso(), "src_lang": "es", "tgt_lang": "ru",
            "source": "twilio_pstn_outbound", "call_brief": "e2e",
        }


@app.get("/v1/sessions")
def list_sessions():
    if time.time() < STATE["started"]:
        return jsonify({"ok": True, "count": 0, "items": []})
    return jsonify({"ok": True, "count": 1, "items": [_session_row()]})


@app.get(f"/v1/sessions/{SESSION_ID}/diagnostics")
def diagnostics():
    # Реальный VG мержит diag на верхний уровень: {**diag, "status": ...}.
    return jsonify({"ok": True, "status": STATE["status"], "timeline_size": 3,
                    "costs": {"total_usd": 0.07,
                              "breakdown": {"twilio": 0.05, "ai": 0.02}}})


@app.post(f"/v1/telephony/calls/{SESSION_ID}/hangup")
def hangup():
    with LOCK:
        STATE["hangup_calls"] += 1
        already = STATE["status"] in {"stopped", "failed"}
        STATE["status"] = "stopped"
    return jsonify({"ok": True, "session_id": SESSION_ID, "call_sid": "CA-e2e",
                    "status": "completed", "already_terminal": already})


@app.get("/e2e/hangup_count")
def hangup_count():
    return jsonify({"count": STATE["hangup_calls"]})


_EVENTS = [
    (0.2, "call.state", {"session_id": SESSION_ID, "status": "running"}),
    (0.2, "stt.final", {"text": "hola, quería preguntar", "engine": "e2e",
                        "confidence": 0.9, "duration_ms": 900, "language": "es"}),
    (0.2, "translation.final", {"text": "привет, хотел спросить", "source_text": "hola, quería preguntar",
                                "src_lang": "es", "tgt_lang": "ru", "provider": "e2e"}),
    (0.2, "agent.response", {"text": "Claro, dígame", "text_ru": "Конечно, слушаю",
                             "role": "assistant", "lang": "es", "utterance_ts": "u1",
                             "action": "continue", "goal_reached": False, "summary": ""}),
    (0.2, "agent.suggestion.auto_spoken", {"text": "Uno momento", "text_ru": "Минуту",
                                           "action": "continue", "digits": "",
                                           "goal_reached": False, "summary": "", "result": ""}),
    (0.2, "agent.interrupted", {"utterance_ts": "u1", "spoken_fraction": 0.4,
                                "spoken_text": "Claro, dí"}),
    (0.2, "weird.unknown_event", {"x": 1}),  # forward-compat: клиент обязан молча съесть
]


@sock.route(f"/v1/sessions/{SESSION_ID}/stream")
def stream(ws):
    for delay, etype, data in _EVENTS:
        time.sleep(delay)
        with LOCK:
            terminal = STATE["status"] in {"stopped", "failed"}
        if terminal:
            break
        ws.send(json.dumps({"type": etype, "ts": _now_iso(), "data": data}))
    # Ждём hangup (или 60с), затем терминальная цепочка.
    deadline = time.time() + 60
    while time.time() < deadline:
        with LOCK:
            if STATE["status"] in {"stopped", "failed"}:
                break
        time.sleep(0.1)
    ws.send(json.dumps({"type": "call.ended", "ts": _now_iso(),
                        "data": {"reason": "hangup", "provider": "e2e"}}))
    ws.send(json.dumps({"type": "call.closed", "ts": _now_iso(),
                        "data": {"session_id": SESSION_ID}}))
    ws.close()


def _mulaw_encode(sample: int) -> int:
    """Стандартный G.711 μ-law encode (audioop удалён из Python 3.13)."""
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_SINE_FRAME = bytes(
    _mulaw_encode(int(6000 * math.sin(2 * math.pi * 440 * i / 8000)))
    for i in range(800)
)


@sock.route(f"/v1/sessions/{SESSION_ID}/monitor/audio")
def monitor(ws):
    ws.send(json.dumps({"format": "mulaw_8k", "frame_ms": 100}))
    for _ in range(600):  # до 60с
        with LOCK:
            if STATE["status"] in {"stopped", "failed"}:
                break
        ws.send(_SINE_FRAME)
        time.sleep(0.1)
    ws.close(1000)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18090
    app.run(host="127.0.0.1", port=port, threaded=True)
```

- [ ] **Step 2: Create `CallObserverE2ETests.swift`** (integration, env-gated):

```swift
import XCTest
@testable import KrabEarAgent

/// Интеграционный прогон против scripts/fake_vg_server.py.
/// Гейт: env KRAB_E2E_VG_PORT — без него все тесты skip (юнит-CI герметичен).
final class CallObserverE2ETests: XCTestCase {
    private var baseURL: URL!

    override func setUpWithError() throws {
        guard let port = ProcessInfo.processInfo.environment["KRAB_E2E_VG_PORT"] else {
            throw XCTSkip("KRAB_E2E_VG_PORT не задан — интеграционный прогон пропущен")
        }
        baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    func test_full_watch_flow_against_fake_vg() throws {
        let fetcher = URLSessionVGSessionFetcher(baseURLProvider: { self.baseURL },
                                                tokenProvider: { "" })
        let watcher = VGSessionWatcher(fetcher: fetcher)

        final class Collector: VGSessionWatcherDelegate {
            var appeared: [VGSessionInfo] = []
            var generation: UInt64 = 0
            let appearExp = XCTestExpectation(description: "appeared")
            func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool) {
                appeared.append(s); self.generation = generation; appearExp.fulfill()
            }
            func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64) {}
            func watcherCallGone(sessionId: String, generation: UInt64) {}
            func watcherVGLost(sessionId: String, generation: UInt64) {}
            func watcherAuthRejected() {}
        }
        let collector = Collector()
        watcher.delegate = collector
        watcher.start()
        wait(for: [collector.appearExp], timeout: 15)
        watcher.stop()
        let session = collector.appeared[0]

        // События стрима: финалы + auto_spoken + interrupt + терминальная пара.
        let stream = VGCallStreamClient()
        var events: [VGCallEvent] = []
        let ended = XCTestExpectation(description: "call.ended")
        let closed = XCTestExpectation(description: "call.closed")
        let gotInterrupted = XCTestExpectation(description: "agent.interrupted доехал")
        stream.onEvent = { event, _ in
            events.append(event)
            if case .agentInterrupted = event { gotInterrupted.fulfill() }
            if case .callEnded = event { ended.fulfill() }
            if case .callClosed = event { closed.fulfill() }
        }
        stream.connect(baseURL: baseURL, sessionId: session.id,
                       generation: collector.generation, tokenProvider: { "" })

        // Аудио: метаданные + ≥5 кадров синуса.
        let player = CallAudioPlayer()
        final class CountingEngine: CallAudioEngineProtocol {
            var frames = 0
            let exp = XCTestExpectation(description: "≥5 аудио-кадров")
            func start() throws {}
            func stop() {}
            func schedule(_ samples: [Float]) {
                XCTAssertEqual(samples.count, 800)
                XCTAssertTrue(samples.contains { abs($0) > 0.05 }, "синус, не тишина")
                frames += 1
                if frames == 5 { exp.fulfill() }
            }
        }
        let engine = CountingEngine()
        player.engineFactory = { engine }
        player.startListening(baseURL: baseURL, sessionId: session.id,
                              generation: collector.generation, tokenProvider: { "" })
        wait(for: [engine.exp], timeout: 15)
        // 🔴 Весь скрипт событий обязан доехать ДО hangup — иначе fake-стрим
        // оборвётся на terminal-проверке и auto_spoken/interrupted потеряются.
        wait(for: [gotInterrupted], timeout: 20)

        // Hangup — сервер переводит сессию в stopped → терминальная цепочка стрима.
        let poster = URLSessionVGCommandPoster(tokenProvider: { "" })
        let hungUp = XCTestExpectation(description: "hangup 200")
        poster.hangup(baseURL: baseURL, sessionId: session.id) { result in
            if case .success(200) = result { hungUp.fulfill() }
        }
        wait(for: [hungUp, ended, closed], timeout: 15)

        // Контент-проверки.
        XCTAssertTrue(events.contains { if case .sttFinal(let t, _, _) = $0 { return t.contains("hola") } ; return false })
        XCTAssertTrue(events.contains { if case .agentAutoSpoken = $0 { return true } ; return false })
        XCTAssertTrue(events.contains { if case .agentInterrupted(_, _, let s) = $0 { return s == "Claro, dí" } ; return false })
        XCTAssertFalse(events.contains { if case .ignored = $0 { return true } ; return false },
                       "ignored-события не должны доходить до onEvent")

        // Cost.
        let cost = XCTestExpectation(description: "cost 0.07")
        poster.fetchCostUsd(baseURL: baseURL, sessionId: session.id) { usd in
            XCTAssertEqual(usd ?? -1, 0.07, accuracy: 0.001)
            cost.fulfill()
        }
        wait(for: [cost], timeout: 10)
        stream.disconnect()
        player.stopListening()
    }
}
```

- [ ] **Step 3: Create `scripts/e2e_call_observer_smoke.command`**

```bash
#!/bin/bash
# e2e Call Observer w1: fake VG + интеграционные XCTest.
# Bash 3.2-совместимо (macOS): без mapfile/timeout.
set -u
cd "$(dirname "$0")/.."

PORT=18090
PYTHON=".venv_krab_ear/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

"$PYTHON" scripts/fake_vg_server.py "$PORT" >/tmp/fake_vg_server.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

# Ждём готовность сервера (fail-closed: не дождались → красный выход).
READY=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:$PORT/v1/sessions" >/dev/null 2>&1; then
        READY=1; break
    fi
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    echo "FAIL: fake VG не поднялся (см. /tmp/fake_vg_server.log)" >&2
    exit 1
fi

cd native/KrabEarAgent
KRAB_E2E_VG_PORT="$PORT" swift test --filter CallObserverE2ETests
RC=$?
exit $RC
```

Run: `chmod +x scripts/e2e_call_observer_smoke.command`

- [ ] **Step 4: Run the smoke**

Run: `scripts/e2e_call_observer_smoke.command`
Expected: `Test Suite 'CallObserverE2ETests' passed`. (flask-sock is already a project dependency — rest_server uses it; if the dev venv lacks it: `pip install flask-sock` into `.venv_krab_ear`.)

- [ ] **Step 5: NOW.md card**

Add to `docs/NOW.md` a wave card: «Call Observer w1 — наблюдатель звонков VG (HUD+панель+аудио+трубка); спека 2026-08-21; shadow не нужен (view-only клиент); волна 2 (вмешательство) ждёт (a)/(e) от VG».

- [ ] **Step 6: Commit**

```bash
git add scripts/fake_vg_server.py scripts/e2e_call_observer_smoke.command native/KrabEarAgent/Tests/KrabEarAgentTests/CallObserverE2ETests.swift docs/NOW.md
git commit -m "test(call-observer): fake VG + интеграционный e2e-смок + карточка NOW (w1 T10)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Merge gate (after all tasks — coordinator runs it, not a worker)

1. Two-stage review of EVERY task (spec-compliance → code-quality) + fix-loop.
2. Fresh-context adversarial review of the WHOLE branch diff (mandatory stage).
3. `make test` (full Python suite) + `swift test` (full) + `make audit-all` + ubuntu-parity on changed Python test files.
4. `scripts/e2e_call_observer_smoke.command` — green.
5. agy/Gemini visual-polish brief (`docs/design-briefs/`) AFTER merge of functional UI; key-by-key gate of the agy diff (IPC/WS keys must not be invented).
6. Deploy: build + bundle parity (LC_UUID) via `scripts/build_and_deploy.command`; `git status` after build (Sparkle .lproj deletion trap); live check with a real VG call — cross-session coordination.
