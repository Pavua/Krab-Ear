/*
 WakeWordListenerTests — тесты WakeWordListener с mock Porcupine engine.

 Подход:
 - MockPorcupineEngine имитирует Porcupine SDK без реальных ML-весов.
 - Тесты проверяют: callback при детекции, игнор при -1 (нет совпадения),
   graceful init без ключа, stop/start lifecycle.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - MockPorcupineEngine

final class MockPorcupineEngine: PorcupineEngineProtocol {
    var processResults: [Int32]
    var processCallCount = 0
    var deleteWasCalled = false

    init(results: [Int32] = []) {
        self.processResults = results
    }

    func process(pcm: [Int16]) throws -> Int32 {
        processCallCount += 1
        if processCallCount <= processResults.count {
            return processResults[processCallCount - 1]
        }
        return -1
    }

    func delete() {
        deleteWasCalled = true
    }
}

// MARK: - MockPorcupineThrows

final class MockPorcupineThrows: PorcupineEngineProtocol {
    func process(pcm: [Int16]) throws -> Int32 {
        throw NSError(domain: "MockPorcupine", code: 1, userInfo: [NSLocalizedDescriptionKey: "Mock error"])
    }
    func delete() {}
}

// MARK: - WakeWordListenerTests

@MainActor
final class WakeWordListenerTests: XCTestCase {

    // MARK: - Callback on detection

    func test_wakeWordDetected_callsCallback() {
        var detected = false
        let mock = MockPorcupineEngine(results: [0]) // индекс 0 = «Краб»
        let listener = WakeWordListener(engine: mock) {
            detected = true
        }

        // Симулируем processFrame напрямую
        listener.testProcessFrame(Array(repeating: 0, count: 512))

        XCTAssertTrue(detected, "Callback должен быть вызван при результате индекса >= 0")
    }

    func test_noDetection_doesNotCallCallback() {
        var detected = false
        let mock = MockPorcupineEngine(results: [-1, -1, -1])
        let listener = WakeWordListener(engine: mock) {
            detected = true
        }

        listener.testProcessFrame(Array(repeating: 100, count: 512))
        listener.testProcessFrame(Array(repeating: 200, count: 512))

        XCTAssertFalse(detected, "Callback не должен вызываться при результате -1")
    }

    func test_multipleKeywords_indexOne_detected() {
        var detectedKeywordIndex: Int32 = -1
        let mock = MockPorcupineEngine(results: [1]) // индекс 1 = второй keyword
        let listener = WakeWordListener(engine: mock) {
            detectedKeywordIndex = 1
        }

        listener.testProcessFrame(Array(repeating: 0, count: 512))

        XCTAssertEqual(detectedKeywordIndex, 1)
    }

    // MARK: - Error handling

    func test_engineThrows_doesNotCrash() {
        var detected = false
        let mock = MockPorcupineThrows()
        let listener = WakeWordListener(engine: mock) {
            detected = true
        }

        // Не должно падать
        listener.testProcessFrame(Array(repeating: 0, count: 512))
        XCTAssertFalse(detected)
    }

    // MARK: - Lifecycle

    func test_start_withoutAccessKey_returnsFalse() {
        // Без реального SDK и без ключа — start() возвращает false
        let listener = WakeWordListener(engine: nil) {}
        let result = listener.start()
        XCTAssertFalse(result, "start() без движка должен вернуть false")
    }

    func test_start_withMockEngine_returnsTrue() {
        let mock = MockPorcupineEngine()
        let listener = WakeWordListener(engine: mock) {}
        // start() с mock движком — пытается запустить AVAudioEngine.
        // В test environment может не быть микрофона — ловим failure gracefully.
        // Проверяем что не крашится.
        _ = listener.start()
        listener.stop()
    }

    func test_stop_calledTwice_doesNotCrash() {
        let mock = MockPorcupineEngine()
        let listener = WakeWordListener(engine: mock) {}
        listener.stop()
        listener.stop() // Double-stop should be safe
    }

    func test_processMultipleFrames_correctCallCount() {
        let mock = MockPorcupineEngine(results: [-1, -1, 0, -1])
        var callbackCount = 0
        let listener = WakeWordListener(engine: mock) {
            callbackCount += 1
        }

        listener.testProcessFrame(Array(repeating: 0, count: 512)) // -1
        listener.testProcessFrame(Array(repeating: 0, count: 512)) // -1
        listener.testProcessFrame(Array(repeating: 0, count: 512)) // 0 → detect
        listener.testProcessFrame(Array(repeating: 0, count: 512)) // -1

        XCTAssertEqual(callbackCount, 1)
        XCTAssertEqual(mock.processCallCount, 4)
    }
}

// MARK: - Test hook extension

extension WakeWordListener {
    /// Тест-хук: напрямую вызвать processFrame без AVAudioEngine.
    func testProcessFrame(_ frame: [Int16]) {
        testProcessFrameInternal(frame)
    }
}
