/*
 LiveSubsOverlayExitTests — мини-волна 2026-08-12 (F4/F5, спека
 docs/superpowers/specs/2026-08-12-live-subs-backpressure-design.md §3/§4):
 живой инцидент владельца — тумблер, завязанный только на isCapturing, не
 умел убрать повисший оверлей live-субтитров; окно не закрывалось само,
 даже если захват умер молча.

 Покрытие:
 1. LiveSubsToggleGate.shouldStop — все 4 комбинации isCapturing/isVisible
    (F4).
 2. LiveSubsOverlayWatchdogGate.shouldHide — граничные случаи grace-периода,
    гейт по isOverlayVisible/isCapturing (F5).
 3. Пины констант (tickIntervalSec=5, graceSec=10) — осознанные числа спеки,
    не магия.
 4. Source-контракты (тот же приём резолва пути, что
    DictationStopAutoRetryWiringSourceContractTests / MainErrorsWiringTests —
    decorative-wiring класс): AgentAppDelegate реально вызывает обе чистые
    решающие функции, а не дублирует их логику инлайн.

 Поведенческий сценарий "оверлей реально скрывается через N тиков" здесь
 намеренно НЕ дублируется через синтетический AgentAppDelegate-харнес:
 AgentAppDelegate требует NSApp/lifecycle (нетестируемо в чистом XCTest, см.
 доккомент DictationStopAutoRetryGate.swift), а SystemAudioCapture/
 LiveSubtitlesOverlay внутри него создаются лениво через associated objects
 с реальными побочными эффектами (IPC-сокет, SSE-подключение на localhost)
 — синтетический дубль воспроизвёл бы урок
 reference_dead_test_only_helper_reshape, тестируя копию, а не реальное
 решение. Тот же исход покрыт связкой gate-юнитов (реальные decision-
 функции) + source-контрактов (реальный текст wiring) + живой проверки после
 мержа (спека §6, последний пункт).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - 1. LiveSubsToggleGate (F4)

final class LiveSubsToggleGateTests: XCTestCase {

    func test_shouldStop_whenCapturingAndVisible() {
        XCTAssertTrue(LiveSubsToggleGate.shouldStop(isCapturing: true, isOverlayVisible: true))
    }

    func test_shouldStop_whenCapturingButNotVisible() {
        XCTAssertTrue(
            LiveSubsToggleGate.shouldStop(isCapturing: true, isOverlayVisible: false),
            "Идёт захват — тумблер обязан остановить его независимо от видимости окна"
        )
    }

    func test_shouldStop_whenNotCapturingButOverlayStillVisible() {
        XCTAssertTrue(
            LiveSubsToggleGate.shouldStop(isCapturing: false, isOverlayVisible: true),
            "Ровно инцидент владельца: захват умер сам, окно повисло — тумблер обязан убрать окно, а не запускать захват заново"
        )
    }

    func test_shouldStop_whenNeitherCapturingNorVisible() {
        XCTAssertFalse(
            LiveSubsToggleGate.shouldStop(isCapturing: false, isOverlayVisible: false),
            "Ничего не идёт и не висит — тумблер обязан ЗАПУСТИТЬ захват"
        )
    }
}

// MARK: - 2. LiveSubsOverlayWatchdogGate (F5)

final class LiveSubsOverlayWatchdogGateTests: XCTestCase {

    func test_shouldHide_whenOverlayHiddenAlready() {
        XCTAssertFalse(
            LiveSubsOverlayWatchdogGate.shouldHide(
                isOverlayVisible: false,
                isCapturing: false,
                secondsSinceCapturingWasTrue: 999
            ),
            "Нечего скрывать — окно уже не видно"
        )
    }

    func test_shouldHide_whenCapturingIsAlive() {
        XCTAssertFalse(
            LiveSubsOverlayWatchdogGate.shouldHide(
                isOverlayVisible: true,
                isCapturing: true,
                secondsSinceCapturingWasTrue: 999
            ),
            "Захват жив — окно не должно закрываться, даже если давно не было речи (легитимная тишина в видео)"
        )
    }

    func test_shouldHide_belowGracePeriod_staysVisible() {
        XCTAssertFalse(
            LiveSubsOverlayWatchdogGate.shouldHide(
                isOverlayVisible: true,
                isCapturing: false,
                secondsSinceCapturingWasTrue: LiveSubsOverlayWatchdogGate.graceSec - 0.01
            ),
            "Grace-период ещё не истёк — нормальное окно старта/рестарта захвата не должно закрывать окно"
        )
    }

    func test_shouldHide_atExactGracePeriod_hides() {
        XCTAssertTrue(
            LiveSubsOverlayWatchdogGate.shouldHide(
                isOverlayVisible: true,
                isCapturing: false,
                secondsSinceCapturingWasTrue: LiveSubsOverlayWatchdogGate.graceSec
            ),
            "Ровно на границе grace-периода оверлей обязан закрыться (>=, не >)"
        )
    }

    func test_shouldHide_wellPastGracePeriod_hides() {
        XCTAssertTrue(
            LiveSubsOverlayWatchdogGate.shouldHide(
                isOverlayVisible: true,
                isCapturing: false,
                secondsSinceCapturingWasTrue: LiveSubsOverlayWatchdogGate.graceSec + 120
            ),
            "Ровно инцидент владельца: захват умер молча, окно должно закрыться само"
        )
    }

    // MARK: 3. Пины констант

    func test_tickIntervalSec_isFive() {
        XCTAssertEqual(LiveSubsOverlayWatchdogGate.tickIntervalSec, 5.0)
    }

    func test_graceSec_isTen() {
        XCTAssertEqual(LiveSubsOverlayWatchdogGate.graceSec, 10.0)
    }
}

// MARK: - 4. Source contracts

final class LiveSubsOverlayExitWiringSourceContractTests: XCTestCase {

    // MARK: 4a. toggleLiveSubsCaptureFromMenu реально зовёт LiveSubsToggleGate

    func test_toggleLiveSubsCaptureFromMenu_callsToggleGate() throws {
        let src = try Self.source("main+LiveSubs.swift")
        guard
            let sigRange = src.range(of: "func toggleLiveSubsCaptureFromMenu() {"),
            let closeRange = src.range(
                of: "\n    }\n",
                range: sigRange.upperBound..<src.endIndex
            )
        else {
            return XCTFail("Не найдена toggleLiveSubsCaptureFromMenu() в main+LiveSubs.swift")
        }
        let block = src[sigRange.lowerBound..<closeRange.upperBound]
        XCTAssertTrue(
            block.contains("LiveSubsToggleGate.shouldStop("),
            "toggleLiveSubsCaptureFromMenu обязана решать через реальный гейт F4, " +
            "не дублировать условие инлайн (декоративная проводка иначе разошлась бы с тестами гейта)"
        )
        XCTAssertTrue(block.contains("stopLiveSubsCapture()"))
        XCTAssertTrue(block.contains("startLiveSubsCapture()"))
    }

    // MARK: 4b. startLiveSubsCapture взводит отметку и стартует watchdog

    func test_startLiveSubsCapture_armsWatchdog() throws {
        let src = try Self.source("main+LiveSubs.swift")
        guard
            let sigRange = src.range(of: "func startLiveSubsCapture() {"),
            let closeRange = src.range(
                of: "\n    }\n",
                range: sigRange.upperBound..<src.endIndex
            )
        else {
            return XCTFail("Не найдена startLiveSubsCapture() в main+LiveSubs.swift")
        }
        let block = src[sigRange.lowerBound..<closeRange.upperBound]
        XCTAssertTrue(
            block.contains("liveSubsCapturingLastTrueAt = Date()"),
            "startLiveSubsCapture обязана взвести отметку — иначе первый тик watchdog " +
            "увидит nil и не даст честный grace-период"
        )
        XCTAssertTrue(
            block.contains("startLiveSubsWatchdog()"),
            "startLiveSubsCapture обязана запустить watchdog-таймер (F5)"
        )
    }

    // MARK: 4c. stopLiveSubsCapture останавливает watchdog

    func test_stopLiveSubsCapture_stopsWatchdog() throws {
        let src = try Self.source("main+LiveSubs.swift")
        guard
            let sigRange = src.range(of: "func stopLiveSubsCapture() {"),
            let closeRange = src.range(
                of: "\n    }\n",
                range: sigRange.upperBound..<src.endIndex
            )
        else {
            return XCTFail("Не найдена stopLiveSubsCapture() в main+LiveSubs.swift")
        }
        let block = src[sigRange.lowerBound..<closeRange.upperBound]
        XCTAssertTrue(
            block.contains("stopLiveSubsWatchdog()"),
            "stopLiveSubsCapture обязана остановить watchdog-таймер — иначе он крутится " +
            "вечно после явной остановки (спека §4: таймер живёт ТОЛЬКО пока окно видно)"
        )
    }

    // MARK: 4d. tickLiveSubsWatchdog реально зовёт LiveSubsOverlayWatchdogGate и hide()

    func test_tickLiveSubsWatchdog_callsWatchdogGateAndHides() throws {
        let src = try Self.source("main+LiveSubs.swift")
        guard
            let sigRange = src.range(of: "func tickLiveSubsWatchdog() {"),
            let closeRange = src.range(
                of: "\n    }\n",
                range: sigRange.upperBound..<src.endIndex
            )
        else {
            return XCTFail("Не найдена tickLiveSubsWatchdog() в main+LiveSubs.swift")
        }
        let block = src[sigRange.lowerBound..<closeRange.upperBound]
        XCTAssertTrue(
            block.contains("LiveSubsOverlayWatchdogGate.shouldHide("),
            "tickLiveSubsWatchdog обязан решать через реальный гейт F5, не дублировать " +
            "условие инлайн"
        )
        XCTAssertTrue(
            block.contains("systemAudioCapture.isCapturing"),
            "Тик обязан читать ЖИВОЙ isCapturing, а не кэшированное значение — иначе " +
            "гейт по «активности распознавания» вместо «жизни захвата» (запрещено спекой)"
        )
        XCTAssertTrue(
            block.contains("liveSubsOverlay.hide()"),
            "На истёкшем grace-периоде тик обязан скрыть оверлей (спека §4: hide() + logger.info)"
        )
    }

    /// Резолвит `native/KrabEarAgent/Sources/KrabEarAgent/<name>` от тестового
    /// бандла, с fallback на #file-относительный walk-up — тот же приём, что
    /// DictationStopAutoRetryWiringSourceContractTests.source /
    /// MainHealthMonitorWiringTests.mainSwiftURL.
    private static func source(_ name: String) throws -> String {
        let bundleURL = Bundle(for: LiveSubsOverlayExitWiringSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/\(name)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #filePath)
        let fallback = fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: fallback, encoding: .utf8)
    }
}
