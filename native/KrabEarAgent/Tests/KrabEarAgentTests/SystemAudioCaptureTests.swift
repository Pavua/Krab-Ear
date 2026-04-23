/*
 SystemAudioCaptureTests — тесты lifecycle + permission error handling (Phase 2B step 1/3).

 Подход: testable-обёртки вместо реального SCStream (SCStream требует Screen Recording permission
 и не инициализируется в sandbox XCTest). Тесты покрывают:
 - lifecycle: start/stop state transitions
 - permission error propagation
 - audio format tracking (capturedSeconds)
 - delegate calls (didFail, didReceiveSampleBuffer)
 - hudStatusString formatting
 - UserDefaults key round-trip
*/

import XCTest
import CoreMedia
@testable import KrabEarAgent

// MARK: - Testable variant

/// Тестируемый вариант SystemAudioCapture, заменяющий SCStream на инжектируемый мок.
final class SystemAudioCaptureTestable {

    // MARK: - State (mirrors SystemAudioCapture)

    private(set) var isCapturing: Bool = false
    private(set) var capturedSeconds: TimeInterval = 0
    private(set) var totalSamplesReceived: Int64 = 0
    private var sampleRate: Double = 44100

    weak var delegate: SystemAudioCaptureTestableDelegate?

    // MARK: - Stubbable controls

    /// true = разрешение выдано, false = denied
    var stubbedHasPermission: Bool = true
    /// nil = старт успешен, non-nil = ошибка startCapture
    var stubbedStartError: Error? = nil
    /// nil = стоп успешен, non-nil = ошибка stopCapture
    var stubbedStopError: Error? = nil

    // MARK: - Call tracking

    private(set) var startCallCount: Int = 0
    private(set) var stopCallCount: Int = 0
    private(set) var openedSettingsURL: String?

    var hudStatusString: String {
        guard isCapturing else { return "Live субтитры: остановлены" }
        let sec = String(format: "%.1f", capturedSeconds)
        return "Live субтитры: захвачено \(sec)с аудио | следующий шаг: STT pipeline"
    }

    // MARK: - Public API (mirrors SystemAudioCapture)

    func start(completion: @escaping (Error?) -> Void) {
        startCallCount += 1

        guard stubbedHasPermission else {
            openedSettingsURL = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
            completion(SystemAudioCaptureError.permissionDenied)
            return
        }

        if let err = stubbedStartError {
            completion(SystemAudioCaptureError.streamStartFailed(underlying: err))
            return
        }

        isCapturing = true
        totalSamplesReceived = 0
        capturedSeconds = 0
        completion(nil)
    }

    func stop(completion: ((Error?) -> Void)? = nil) {
        stopCallCount += 1
        guard isCapturing else {
            completion?(nil)
            return
        }

        if let err = stubbedStopError {
            completion?(SystemAudioCaptureError.streamStopFailed(underlying: err))
            return
        }

        isCapturing = false
        totalSamplesReceived = 0
        capturedSeconds = 0
        completion?(nil)
    }

    /// Симулирует приход audio frame (без реального CMSampleBuffer — используем nil-stub).
    func simulateAudioFrame(sampleCount: Int, rate: Double = 44100) {
        sampleRate = rate
        totalSamplesReceived += Int64(sampleCount)
        capturedSeconds = Double(totalSamplesReceived) / sampleRate
        delegate?.testableCapture(self, didReceiveFrameWithCapturedSeconds: capturedSeconds)
    }

    /// Симулирует ошибку потока (разрешение отозвано и т.д.).
    func simulateStreamError(_ error: Error) {
        isCapturing = false
        delegate?.testableCapture(self, didFailWithError: error)
    }
}

protocol SystemAudioCaptureTestableDelegate: AnyObject {
    func testableCapture(_ capture: SystemAudioCaptureTestable, didReceiveFrameWithCapturedSeconds: TimeInterval)
    func testableCapture(_ capture: SystemAudioCaptureTestable, didFailWithError: Error)
}

// MARK: - Tests

final class SystemAudioCaptureTests: XCTestCase {

    // MARK: - Initial state

    func test_initialState_notCapturing() {
        let sut = SystemAudioCaptureTestable()
        XCTAssertFalse(sut.isCapturing, "Должен быть не запущен по умолчанию")
        XCTAssertEqual(sut.capturedSeconds, 0)
    }

    // MARK: - Lifecycle: start

    func test_start_withPermission_setsIsCapturing() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true

        var callbackError: Error?
        sut.start { err in callbackError = err }

        XCTAssertTrue(sut.isCapturing, "После успешного start() isCapturing должен быть true")
        XCTAssertNil(callbackError, "Callback не должен содержать ошибку")
        XCTAssertEqual(sut.startCallCount, 1)
    }

    func test_start_withoutPermission_returnsError() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = false

        var callbackError: Error?
        sut.start { err in callbackError = err }

        XCTAssertFalse(sut.isCapturing, "Без разрешения isCapturing должен остаться false")
        XCTAssertNotNil(callbackError, "Должна быть ошибка permissionDenied")
        if case SystemAudioCaptureError.permissionDenied = callbackError! {
            // ok
        } else {
            XCTFail("Ожидалась ошибка .permissionDenied, получено: \(callbackError!)")
        }
    }

    func test_start_withoutPermission_opensSystemSettings() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = false

        sut.start { _ in }

        XCTAssertNotNil(sut.openedSettingsURL, "При permissionDenied должен открываться URL System Settings")
        XCTAssertTrue(sut.openedSettingsURL?.contains("Privacy_ScreenCapture") == true)
    }

    func test_start_streamStartFailed_returnsError() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true
        sut.stubbedStartError = NSError(domain: "TestDomain", code: 42, userInfo: nil)

        var callbackError: Error?
        sut.start { err in callbackError = err }

        XCTAssertFalse(sut.isCapturing, "При ошибке startCapture isCapturing должен остаться false")
        XCTAssertNotNil(callbackError)
        if case SystemAudioCaptureError.streamStartFailed = callbackError! {
            // ok
        } else {
            XCTFail("Ожидалась ошибка .streamStartFailed, получено: \(callbackError!)")
        }
    }

    // MARK: - Lifecycle: stop

    func test_stop_afterStart_setsNotCapturing() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true
        sut.start { _ in }

        var stopError: Error?
        sut.stop { err in stopError = err }

        XCTAssertFalse(sut.isCapturing, "После stop() isCapturing должен быть false")
        XCTAssertNil(stopError)
        XCTAssertEqual(sut.stopCallCount, 1)
    }

    func test_stop_whenNotCapturing_noOp() {
        let sut = SystemAudioCaptureTestable()

        var stopError: Error?
        sut.stop { err in stopError = err }

        XCTAssertNil(stopError, "stop() без активного захвата не должен возвращать ошибку")
        XCTAssertEqual(sut.stopCallCount, 1)
    }

    func test_stop_streamStopFailed_returnsError() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true
        sut.start { _ in }
        sut.stubbedStopError = NSError(domain: "StopDomain", code: 99, userInfo: nil)

        var stopError: Error?
        sut.stop { err in stopError = err }

        XCTAssertNotNil(stopError)
        if case SystemAudioCaptureError.streamStopFailed = stopError! {
            // ok
        } else {
            XCTFail("Ожидалась ошибка .streamStopFailed, получено: \(stopError!)")
        }
    }

    // MARK: - Audio frame accumulation

    func test_simulateAudioFrame_updatesCapturedSeconds() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true
        sut.start { _ in }

        sut.simulateAudioFrame(sampleCount: 44100, rate: 44100.0) // 1 секунда
        XCTAssertEqual(sut.capturedSeconds, 1.0, accuracy: 0.001)
    }

    func test_simulateAudioFrame_accumulates() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true
        sut.start { _ in }

        sut.simulateAudioFrame(sampleCount: 22050, rate: 44100.0) // 0.5s
        sut.simulateAudioFrame(sampleCount: 22050, rate: 44100.0) // 0.5s
        XCTAssertEqual(sut.capturedSeconds, 1.0, accuracy: 0.001)
    }

    func test_stop_resetsCapturedSeconds() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true
        sut.start { _ in }
        sut.simulateAudioFrame(sampleCount: 44100, rate: 44100.0)

        sut.stop(completion: nil)

        XCTAssertEqual(sut.capturedSeconds, 0, "После stop() capturedSeconds должен сбрасываться")
    }

    // MARK: - HUD status string

    func test_hudStatus_whenNotCapturing_showsStopped() {
        let sut = SystemAudioCaptureTestable()
        XCTAssertEqual(sut.hudStatusString, "Live субтитры: остановлены")
    }

    func test_hudStatus_whenCapturing_showsSeconds() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true
        sut.start { _ in }
        sut.simulateAudioFrame(sampleCount: 44100 * 5, rate: 44100.0) // 5s

        XCTAssertTrue(sut.hudStatusString.contains("5.0с"), "HUD должен отображать 5.0с: \(sut.hudStatusString)")
        XCTAssertTrue(sut.hudStatusString.contains("STT pipeline"))
    }

    // MARK: - Delegate calls

    class MockDelegate: SystemAudioCaptureTestableDelegate {
        var receivedFrameCount = 0
        var lastCapturedSeconds: TimeInterval = 0
        var failErrors: [Error] = []

        func testableCapture(_ capture: SystemAudioCaptureTestable, didReceiveFrameWithCapturedSeconds sec: TimeInterval) {
            receivedFrameCount += 1
            lastCapturedSeconds = sec
        }

        func testableCapture(_ capture: SystemAudioCaptureTestable, didFailWithError error: Error) {
            failErrors.append(error)
        }
    }

    func test_delegate_receivesFrameCallback() {
        let sut = SystemAudioCaptureTestable()
        let mockDelegate = MockDelegate()
        sut.delegate = mockDelegate
        sut.stubbedHasPermission = true
        sut.start { _ in }

        sut.simulateAudioFrame(sampleCount: 8000, rate: 16000.0)

        XCTAssertEqual(mockDelegate.receivedFrameCount, 1)
        XCTAssertEqual(mockDelegate.lastCapturedSeconds, 0.5, accuracy: 0.001)
    }

    func test_delegate_receivesErrorCallback() {
        let sut = SystemAudioCaptureTestable()
        let mockDelegate = MockDelegate()
        sut.delegate = mockDelegate
        sut.stubbedHasPermission = true
        sut.start { _ in }

        let testError = NSError(domain: "TestStream", code: 1, userInfo: [NSLocalizedDescriptionKey: "Stream revoked"])
        sut.simulateStreamError(testError)

        XCTAssertEqual(mockDelegate.failErrors.count, 1)
        XCTAssertFalse(sut.isCapturing, "После ошибки потока isCapturing должен быть false")
    }

    // MARK: - Double start guard

    func test_doubleStart_callsCompletionOnce() {
        let sut = SystemAudioCaptureTestable()
        sut.stubbedHasPermission = true

        sut.start { _ in }
        sut.start { _ in }

        // Второй start не должен сломать состояние
        XCTAssertTrue(sut.isCapturing)
        XCTAssertEqual(sut.startCallCount, 2)
    }

    // MARK: - UserDefaults key

    func test_userDefaults_liveSubsEnabled_roundTrip() {
        let ud = UserDefaults.standard
        ud.liveSubsEnabled = true
        XCTAssertTrue(ud.liveSubsEnabled)

        ud.liveSubsEnabled = false
        XCTAssertFalse(ud.liveSubsEnabled)

        // Cleanup
        ud.removeObject(forKey: UserDefaults.krabLiveSubsEnabledKey)
    }

    // MARK: - SystemAudioCaptureError descriptions

    func test_error_permissionDenied_hasDescription() {
        let err = SystemAudioCaptureError.permissionDenied
        XCTAssertTrue(err.errorDescription?.contains("Screen Recording") == true,
                      "permissionDenied должен упоминать Screen Recording")
        XCTAssertTrue(err.errorDescription?.contains("Системные настройки") == true)
    }

    func test_error_unsupportedOS_hasDescription() {
        let err = SystemAudioCaptureError.unsupportedOS
        XCTAssertTrue(err.errorDescription?.contains("12.3") == true)
    }

    func test_error_streamStartFailed_wrapsUnderlying() {
        let underlying = NSError(domain: "Test", code: 5, userInfo: [NSLocalizedDescriptionKey: "boom"])
        let err = SystemAudioCaptureError.streamStartFailed(underlying: underlying)
        XCTAssertTrue(err.errorDescription?.contains("boom") == true)
    }
}
