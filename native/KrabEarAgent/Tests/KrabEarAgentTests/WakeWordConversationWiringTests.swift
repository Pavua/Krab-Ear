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

// MARK: - Wake word no-focus-steal fix (2026-07-12 live incident)
//
// triggerConversationFromWakeWord() раньше делегировал в triggerConversationStart(),
// который showPanel() + NSApp.activate(ignoringOtherApps:) + makeKeyAndOrderFront —
// детекция "Краб" воровала фокус клавиатуры из текущего приложения пользователя.
// Эти тесты грепают РЕАЛЬНЫЙ source конкретно тела каждой функции (не всего файла),
// чтобы будущий рефакторинг не мог тихо вернуть activate/makeKey в wake-word путь,
// и чтобы ручной путь (triggerConversationStart, кнопка/hotkey) не потерял активацию.
final class WakeWordNoFocusStealTests: XCTestCase {

    // MARK: 1. wake-word путь НЕ активирует приложение и НЕ ворует key window

    func test_triggerConversationFromWakeWord_does_not_activate_app() throws {
        let body = try Self.functionBody(named: "triggerConversationFromWakeWord", in: Self.voiceTabSwiftURL)
        XCTAssertFalse(body.contains("NSApp.activate"),
            "triggerConversationFromWakeWord() не должен звать NSApp.activate — " +
            "детекция wake word не должна воровать фокус клавиатуры (живой инцидент 2026-07-12)")
        XCTAssertFalse(body.contains("makeKeyAndOrderFront"),
            "triggerConversationFromWakeWord() не должен звать makeKeyAndOrderFront — " +
            "окно разговора не должно становиться key window по wake-детекции")
        XCTAssertFalse(body.contains("showPanel()"),
            "triggerConversationFromWakeWord() не должен звать showPanel() — тот сам " +
            "внутри вызывает NSApp.activate(ignoringOtherApps: true)")
    }

    // MARK: 2. wake-word путь всё ещё реально стартует голосовую сессию

    func test_triggerConversationFromWakeWord_still_starts_session() throws {
        let body = try Self.functionBody(named: "triggerConversationFromWakeWord", in: Self.voiceTabSwiftURL)
        XCTAssertTrue(body.contains("conversationVC?.startConversation()"),
            "triggerConversationFromWakeWord() обязан стартовать сессию напрямую через " +
            "conversationVC.startConversation() — сама голосовая сессия должна стартовать " +
            "как и раньше, меняется только представление окна")
    }

    // MARK: 3. Регрессионный гард: ручной путь (hotkey/кнопка) остаётся активирующим

    func test_triggerConversationStart_manual_path_still_activates() throws {
        let body = try Self.functionBody(named: "triggerConversationStart", in: Self.voiceTabSwiftURL)
        XCTAssertTrue(body.contains("NSApp.activate(ignoringOtherApps: true)"),
            "triggerConversationStart() (двойной Right Option / меню) обязан оставаться " +
            "активирующим — там пользователь сам инициировал действие явным хоткеем")
        XCTAssertTrue(body.contains("makeKeyAndOrderFront"),
            "triggerConversationStart() обязан оставаться makeKeyAndOrderFront — ручной путь не менялся")
    }

    // MARK: - Function-body extraction (brace-matched substring, не весь файл)

    /// Извлекает тело функции `func <name>(...) { ... }` через подсчёт скобок от
    /// первой `{` после сигнатуры до парной закрывающей — чтобы различать соседние
    /// функции в одном файле (triggerConversationStart vs triggerConversationFromWakeWord).
    private static func functionBody(named name: String, in url: URL) throws -> String {
        let src = try String(contentsOf: url, encoding: .utf8)
        guard let sigRange = src.range(of: "func \(name)(") else {
            XCTFail("func \(name)( не найдена в \(url.lastPathComponent)")
            return ""
        }
        guard let openBrace = src.range(of: "{", range: sigRange.upperBound..<src.endIndex) else {
            XCTFail("Открывающая { не найдена после сигнатуры \(name)")
            return ""
        }
        var depth = 0
        var idx = openBrace.lowerBound
        var closeIdx: String.Index?
        while idx < src.endIndex {
            let ch = src[idx]
            if ch == "{" { depth += 1 } else if ch == "}" {
                depth -= 1
                if depth == 0 { closeIdx = idx; break }
            }
            idx = src.index(after: idx)
        }
        guard let close = closeIdx else {
            XCTFail("Не нашли парную закрывающую } для \(name)")
            return ""
        }
        return String(src[openBrace.lowerBound...close])
    }

    private static var sourcesDir: URL {
        var url = URL(fileURLWithPath: #filePath)  // .../Tests/KrabEarAgentTests/<файл>
        url.deleteLastPathComponent()              // Tests/KrabEarAgentTests
        url.deleteLastPathComponent()              // Tests
        url.deleteLastPathComponent()              // native/KrabEarAgent
        return url.appendingPathComponent("Sources/KrabEarAgent")
    }

    private static var voiceTabSwiftURL: URL {
        sourcesDir.appendingPathComponent("HistoryPanelController+VoiceTab.swift")
    }
}

// MARK: - TTS-self-echo pause fix (T5b architectural follow-up, 2026-07-13)
//
// ConversationErrorAnnouncer озвучивает ошибки уже ПОСЛЕ stopConversation()
// (.krabConversationStopped снимает .conversation-паузу синхронно), а сам
// AVAudioPlayer.play() стартует асинхронно (round-trip synthesize_speech IPC) —
// wake word уже слушал, когда колонки только начинали проигрывать фразу, и
// триггерился на собственное эхо. Фикс: отдельная причина паузы .ttsPlayback,
// завязанная на реальное начало/конец воспроизведения, а не границы разговора.
final class WakeWordTTSPauseWiringTests: XCTestCase {

    // MARK: 1. Причина паузы существует в enum

    func test_ttsPlayback_pauseReason_exists() throws {
        let src = try String(contentsOf: Self.wakeWordPollerSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains("case ttsPlayback"),
            "WakeWordPauseReason обязан содержать case ttsPlayback")
    }

    // MARK: 2. setupWakeWordTTSPlaybackObservers реально вызывается (не только определён)

    func test_setupWakeWordTTSPlaybackObservers_is_actually_called() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        let callSites = src.components(separatedBy: "setupWakeWordTTSPlaybackObservers()").count - 1
        XCTAssertGreaterThanOrEqual(callSites, 2,
            "setupWakeWordTTSPlaybackObservers() должен быть и определён, и вызван в main.swift")
    }

    // MARK: 3. Обе подписки живы и дёргают правильные методы

    func test_ttsPlaybackStarted_pausesPoller() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains(".krabTTSPlaybackStarted"),
                      "подписка на .krabTTSPlaybackStarted обязана существовать")
        XCTAssertTrue(src.contains("pause(.ttsPlayback)"),
                      "обработчик started обязан вызывать pause(.ttsPlayback)")
    }

    func test_ttsPlaybackFinished_resumesPoller() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains(".krabTTSPlaybackFinished"),
                      "подписка на .krabTTSPlaybackFinished обязана существовать")
        XCTAssertTrue(src.contains("resume(.ttsPlayback)"),
                      "обработчик finished обязан вызывать resume(.ttsPlayback)")
    }

    // MARK: 4. ConversationErrorAnnouncer реально постит обе нотификации вокруг playWav

    func test_errorAnnouncer_postsBothNotifications() throws {
        let body = try Self.functionBody(named: "playWav", in: Self.errorAnnouncerSwiftURL)
        XCTAssertTrue(body.contains("post(name: .krabTTSPlaybackStarted"),
                      "playWav обязан постить .krabTTSPlaybackStarted перед/при старте воспроизведения")
        XCTAssertTrue(body.contains("post(name: .krabTTSPlaybackFinished"),
                      "playWav обязан явно закрывать TTS-паузу при перекрытии предыдущего плеера " +
                      "или неудачном play() — иначе пауза может зависнуть")
    }

    // MARK: 5. Завершение реально отслеживается через делегата, а не completionHandler:nil

    func test_errorAnnouncer_tracksPlaybackCompletion_viaDelegate() throws {
        let src = try String(contentsOf: Self.errorAnnouncerSwiftURL, encoding: .utf8)
        XCTAssertTrue(src.contains("AVAudioPlayerDelegate"),
            "воспроизведение обязано отслеживать реальное завершение через AVAudioPlayerDelegate " +
            "(didFinishPlaying), а не оставаться fire-and-forget")
        XCTAssertTrue(src.contains("audioPlayerDidFinishPlaying"),
            "делегат обязан реализовывать audioPlayerDidFinishPlaying и постить .krabTTSPlaybackFinished")
    }

    // MARK: - Source URLs

    private static var sourcesDir: URL {
        var url = URL(fileURLWithPath: #filePath)
        url.deleteLastPathComponent()
        url.deleteLastPathComponent()
        url.deleteLastPathComponent()
        return url.appendingPathComponent("Sources/KrabEarAgent")
    }

    private static var mainSwiftURL: URL { sourcesDir.appendingPathComponent("main.swift") }
    private static var wakeWordPollerSwiftURL: URL { sourcesDir.appendingPathComponent("WakeWordPoller.swift") }
    private static var errorAnnouncerSwiftURL: URL { sourcesDir.appendingPathComponent("ConversationErrorAnnouncer.swift") }

    /// Извлекает тело функции по имени (тот же паттерн, что WakeWordNoFocusStealTests).
    private static func functionBody(named name: String, in url: URL) throws -> String {
        let src = try String(contentsOf: url, encoding: .utf8)
        guard let sigRange = src.range(of: "func \(name)(") else {
            XCTFail("func \(name)( не найдена в \(url.lastPathComponent)")
            return ""
        }
        guard let openBrace = src.range(of: "{", range: sigRange.upperBound..<src.endIndex) else {
            XCTFail("Открывающая { не найдена после сигнатуры \(name)")
            return ""
        }
        var depth = 0
        var idx = openBrace.lowerBound
        var closeIdx: String.Index?
        while idx < src.endIndex {
            let ch = src[idx]
            if ch == "{" { depth += 1 } else if ch == "}" {
                depth -= 1
                if depth == 0 { closeIdx = idx; break }
            }
            idx = src.index(after: idx)
        }
        guard let close = closeIdx else {
            XCTFail("Не нашли парную закрывающую } для \(name)")
            return ""
        }
        return String(src[openBrace.lowerBound...close])
    }
}
