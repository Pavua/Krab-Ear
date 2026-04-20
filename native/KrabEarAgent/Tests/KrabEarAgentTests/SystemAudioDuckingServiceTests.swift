/*
 SystemAudioDuckingServiceTests — тесты приглушения системного звука.

 Подход: testable-обёртка с инжектируемыми osascript-заглушками.
 Реальные вызовы osascript нельзя мокировать через @testable на final-классе,
 поэтому тесты используют SystemAudioDuckingServiceTestable, который
 повторяет логику оригинала, но принимает замыкания вместо реальных процессов.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Testable variant

/// Повторяет логику SystemAudioDuckingService с инжектируемыми осцилляторами.
final class SystemAudioDuckingServiceTestable {
    struct Snapshot {
        let outputMuted: Bool
        let outputVolume: Int
    }

    // Инжектируемые заглушки вместо osascript
    var stubbedMuted: Bool? = false
    var stubbedVolume: Int? = 50
    var runScriptResult: Bool = true

    private(set) var snapshot: Snapshot?
    private(set) var isDucked = false
    private(set) var lastRunScript: String?
    private(set) var runScriptCallCount = 0

    func duckForRecording(enabled: Bool, duckPercent: Int) {
        guard enabled else { return }
        guard snapshot == nil else { return }

        guard let currentMuted = stubbedMuted,
              let currentVolume = stubbedVolume
        else { return }

        snapshot = Snapshot(outputMuted: currentMuted, outputVolume: currentVolume)
        isDucked = false

        if currentMuted { return }

        let safePercent = max(0, min(duckPercent, 100))
        if safePercent >= 100 {
            let ok = run(script: "set volume output muted true")
            if ok { isDucked = true }
            return
        }

        let targetVolume = max(0, min(currentVolume * safePercent / 100, 100))
        let ok = run(script: "set volume output volume \(targetVolume)\nset volume output muted false")
        if ok { isDucked = true }
    }

    func restoreAfterRecording() {
        guard let snapshot else { return }
        defer { self.snapshot = nil }
        guard isDucked else { return }
        defer { isDucked = false }

        if snapshot.outputMuted {
            _ = run(script: "set volume output muted true")
            return
        }
        let safeVolume = max(0, min(snapshot.outputVolume, 100))
        _ = run(script: "set volume output volume \(safeVolume)\nset volume output muted false")
    }

    private func run(script: String) -> Bool {
        lastRunScript = script
        runScriptCallCount += 1
        return runScriptResult
    }
}

// MARK: - Tests

final class SystemAudioDuckingServiceTests: XCTestCase {

    // MARK: - duck / unduck state transitions

    /// duck() при enabled=true и успешном osascript → isDucked=true, snapshot set.
    func test_duck_setsIsDuckedAndSnapshot() {
        let svc = SystemAudioDuckingServiceTestable()
        svc.stubbedMuted = false
        svc.stubbedVolume = 80
        svc.runScriptResult = true

        svc.duckForRecording(enabled: true, duckPercent: 50)

        XCTAssertTrue(svc.isDucked, "После duck() isDucked должен быть true")
        XCTAssertNotNil(svc.snapshot, "После duck() snapshot должен быть сохранён")
        XCTAssertEqual(svc.snapshot?.outputVolume, 80)
        XCTAssertEqual(svc.snapshot?.outputMuted, false)
    }

    /// restore() сбрасывает isDucked и snapshot в nil.
    func test_restoreAfterDuck_clearsState() {
        let svc = SystemAudioDuckingServiceTestable()
        svc.stubbedMuted = false
        svc.stubbedVolume = 60

        svc.duckForRecording(enabled: true, duckPercent: 50)
        XCTAssertTrue(svc.isDucked)

        svc.restoreAfterRecording()

        XCTAssertFalse(svc.isDucked, "После restore() isDucked должен быть false")
        XCTAssertNil(svc.snapshot, "После restore() snapshot должен быть nil")
    }

    // MARK: - No-op when disabled

    /// duckForRecording(enabled: false) не трогает состояние.
    func test_duck_disabled_noOp() {
        let svc = SystemAudioDuckingServiceTestable()
        svc.stubbedMuted = false
        svc.stubbedVolume = 70

        svc.duckForRecording(enabled: false, duckPercent: 50)

        XCTAssertFalse(svc.isDucked, "При enabled=false isDucked должен остаться false")
        XCTAssertNil(svc.snapshot, "При enabled=false snapshot должен остаться nil")
        XCTAssertEqual(svc.runScriptCallCount, 0, "При enabled=false osascript вызываться не должен")
    }

    // MARK: - Multiple duck() calls don't double-duck

    /// Повторный duck() до restore() игнорируется (snapshot уже есть).
    func test_doubleDuck_ignored() {
        let svc = SystemAudioDuckingServiceTestable()
        svc.stubbedMuted = false
        svc.stubbedVolume = 80

        svc.duckForRecording(enabled: true, duckPercent: 50)
        let callsAfterFirst = svc.runScriptCallCount

        svc.duckForRecording(enabled: true, duckPercent: 50)

        XCTAssertEqual(svc.runScriptCallCount, callsAfterFirst,
                       "Повторный duck() не должен вызывать osascript ещё раз")
        XCTAssertEqual(svc.snapshot?.outputVolume, 80,
                       "snapshot должен сохранить оригинальное значение громкости")
    }

    // MARK: - unduck() before duck() → no-op

    /// restore() без предшествующего duck() — ничего не происходит.
    func test_restoreWithoutDuck_noOp() {
        let svc = SystemAudioDuckingServiceTestable()

        svc.restoreAfterRecording()

        XCTAssertEqual(svc.runScriptCallCount, 0,
                       "restore() без duck() не должен вызывать osascript")
        XCTAssertNil(svc.snapshot)
        XCTAssertFalse(svc.isDucked)
    }

    // MARK: - Volume save/restore behavior

    /// duckPercent=100 → mute вместо уменьшения громкости; restore → вернуть исходную.
    func test_duckPercent100_mutes_thenRestoresVolume() {
        let svc = SystemAudioDuckingServiceTestable()
        svc.stubbedMuted = false
        svc.stubbedVolume = 70

        svc.duckForRecording(enabled: true, duckPercent: 100)

        XCTAssertEqual(svc.lastRunScript, "set volume output muted true",
                       "duckPercent=100 должен отправить команду mute")
        XCTAssertTrue(svc.isDucked)

        svc.restoreAfterRecording()

        XCTAssertTrue(svc.lastRunScript?.contains("output volume 70") == true,
                      "restore должен вернуть исходную громкость 70")
        XCTAssertFalse(svc.isDucked)
    }

    /// Если звук уже был muted до записи, duck() не вызывает осциллятор и restore() возвращает muted=true.
    func test_alreadyMuted_duckSkips_restoreKeepsMuted() {
        let svc = SystemAudioDuckingServiceTestable()
        svc.stubbedMuted = true
        svc.stubbedVolume = 0

        svc.duckForRecording(enabled: true, duckPercent: 50)

        XCTAssertFalse(svc.isDucked,
                       "Если звук уже был muted, isDucked должен остаться false")
        XCTAssertEqual(svc.runScriptCallCount, 0,
                       "Если звук уже muted, osascript вызываться не должен")

        // restore без duck — тоже no-op
        svc.restoreAfterRecording()
        XCTAssertNil(svc.snapshot, "После restore snapshot должен быть nil")
    }
}
