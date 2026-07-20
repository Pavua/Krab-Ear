/*
 LiveSubsTests — unit tests для SystemAudioCapture и LiveSubtitlesOverlay.

 Покрытие:
 1.  PCM extraction Float32 → Int16 mono (clamp + scale)
 2.  PCM extraction Float32 stereo → mono downmix
 3.  extractSamples non-nil for valid buffer
 4.  sampleRate detection
 5.  targetLang default = "ru"
 6.  targetLang setter
 7.  isCapturing starts false
 8.  stop when not capturing is no-op
 9.  LiveSubtitlesOverlay.isVisible starts false
 10. show/hide toggles isVisible
 11. addEntry + clearAll no crash
 12. max 3 entries (rapid fire 4) — no crash
 13. showOriginalAndTranslation toggle
 14. resetPosition no crash
 15. rapid fire 20 entries — no crash
 16. restBaseURL default contains 5005
*/

import XCTest
import CoreMedia
import Foundation
@testable import KrabEarAgent

// MARK: - Helpers

/// Пустая SSE-задача не создаёт сокет: она нужна для unit-проверок состояния overlay.
private final class NoOpLiveSubtitlesSSETask: LiveSubtitlesSSETask {
    func resume() {}
    func cancel() {}
}

/// Пустая SSE-сессия сохраняет смысл show/hide, не обращаясь к backend на localhost.
private final class NoOpLiveSubtitlesSSESession: LiveSubtitlesSSESession {
    private let task = NoOpLiveSubtitlesSSETask()

    func makeLiveSubtitlesTask(with request: URLRequest) -> LiveSubtitlesSSETask {
        task
    }

    func invalidateAndCancel() {}
}

/// Создаёт минимальный CMSampleBuffer с Float32 audio данными.
private func makeSampleBuffer(
    samples: [Float32],
    channels: Int = 1,
    sampleRate: Float64 = 48000
) -> CMSampleBuffer? {
    var asbd = AudioStreamBasicDescription(
        mSampleRate: sampleRate,
        mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        mBytesPerPacket: UInt32(4 * channels),
        mFramesPerPacket: 1,
        mBytesPerFrame: UInt32(4 * channels),
        mChannelsPerFrame: UInt32(channels),
        mBitsPerChannel: 32,
        mReserved: 0
    )

    var formatDesc: CMAudioFormatDescription?
    guard CMAudioFormatDescriptionCreate(
        allocator: kCFAllocatorDefault,
        asbd: &asbd,
        layoutSize: 0,
        layout: nil,
        magicCookieSize: 0,
        magicCookie: nil,
        extensions: nil,
        formatDescriptionOut: &formatDesc
    ) == noErr, let formatDesc else { return nil }

    let byteCount = samples.count * 4
    var blockBuffer: CMBlockBuffer?
    guard CMBlockBufferCreateWithMemoryBlock(
        allocator: kCFAllocatorDefault,
        memoryBlock: nil,
        blockLength: byteCount,
        blockAllocator: kCFAllocatorDefault,
        customBlockSource: nil,
        offsetToData: 0,
        dataLength: byteCount,
        flags: 0,
        blockBufferOut: &blockBuffer
    ) == noErr, let blockBuffer else { return nil }

    CMBlockBufferAssureBlockMemory(blockBuffer)
    var dst: UnsafeMutablePointer<CChar>?
    var dstLen = 0
    CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &dstLen, dataPointerOut: &dst)
    if let dst, dstLen >= byteCount {
        samples.withUnsafeBytes { src in
            dst.withMemoryRebound(to: Float32.self, capacity: samples.count) { f32 in
                for i in 0..<samples.count { f32[i] = samples[i] }
            }
        }
    }

    var sampleBuffer: CMSampleBuffer?
    CMSampleBufferCreate(
        allocator: kCFAllocatorDefault,
        dataBuffer: blockBuffer,
        dataReady: true,
        makeDataReadyCallback: nil,
        refcon: nil,
        formatDescription: formatDesc,
        sampleCount: CMItemCount(samples.count / channels),
        sampleTimingEntryCount: 0,
        sampleTimingArray: nil,
        sampleSizeEntryCount: 0,
        sampleSizeArray: nil,
        sampleBufferOut: &sampleBuffer
    )
    return sampleBuffer
}

// MARK: - SystemAudioCaptureTests

@MainActor
final class SystemAudioCaptureTests: XCTestCase {

    func makeCapture() -> SystemAudioCapture {
        SystemAudioCapture(ipcClient: IPCClient(socketPath: "/tmp/mock-krabear-\(Int.random(in: 1000...9999)).sock"))
    }

    // MARK: 1. Float32 → Int16 mono scale + clamp

    func testExtractSamplesFloat32MonoScaleAndClamp() {
        let capture = makeCapture()
        guard let buf = makeSampleBuffer(samples: [0.5, -0.5, 1.0, -1.0], channels: 1, sampleRate: 48000) else {
            XCTFail("Could not create sample buffer"); return
        }
        let result = capture.extractSamples(from: buf)
        XCTAssertNotNil(result)
        guard let r = result else { return }
        XCTAssertEqual(r.sampleRate, 48000)
        XCTAssertEqual(r.samples.count, 4)
        XCTAssertEqual(r.samples[0], 16383, accuracy: 2)
        XCTAssertEqual(r.samples[1], -16383, accuracy: 2)
        XCTAssertEqual(r.samples[2], 32767, accuracy: 2)
        XCTAssertEqual(r.samples[3], -32767, accuracy: 2)
    }

    // MARK: 2. Float32 stereo → mono downmix

    func testExtractSamplesFloat32StereoDownmix() {
        let capture = makeCapture()
        // L=0.4, R=0.8 → mono = 0.6
        guard let buf = makeSampleBuffer(samples: [0.4, 0.8], channels: 2, sampleRate: 44100) else {
            XCTFail("Could not create stereo sample buffer"); return
        }
        let result = capture.extractSamples(from: buf)
        XCTAssertNotNil(result)
        guard let r = result else { return }
        XCTAssertEqual(r.sampleRate, 44100)
        XCTAssertEqual(r.samples.count, 1)
        // (0.4 + 0.8) / 2 = 0.6 → ~19660
        XCTAssertEqual(r.samples[0], 19660, accuracy: 5)
    }

    // MARK: 3. extractSamples non-nil for valid buffer

    func testExtractSamplesNotNilForValidBuffer() {
        let capture = makeCapture()
        guard let buf = makeSampleBuffer(samples: [0.1, 0.2, 0.3], channels: 1) else {
            XCTFail("Could not create buffer"); return
        }
        XCTAssertNotNil(capture.extractSamples(from: buf))
    }

    // MARK: 4. sampleRate detection

    func testExtractSampleRateDetected() {
        let capture = makeCapture()
        guard let buf = makeSampleBuffer(samples: [0.0, 0.0], channels: 1, sampleRate: 16000) else {
            XCTFail(); return
        }
        XCTAssertEqual(capture.extractSamples(from: buf)?.sampleRate, 16000)
    }

    // MARK: 5. targetLang default

    func testTargetLangDefault() {
        XCTAssertEqual(makeCapture().targetLang, "ru")
    }

    // MARK: 6. targetLang setter

    func testTargetLangSetter() {
        let capture = makeCapture()
        capture.targetLang = "es"
        XCTAssertEqual(capture.targetLang, "es")
    }

    // MARK: 7. isCapturing starts false

    func testIsCapturingStartsFalse() {
        XCTAssertFalse(makeCapture().isCapturing)
    }

    // MARK: 8. stop when not capturing is no-op

    func testStopWhenNotCapturingNoOp() {
        let capture = makeCapture()
        capture.stop()
        XCTAssertFalse(capture.isCapturing)
    }
}

// MARK: - LiveSubtitlesOverlayTests

@MainActor
final class LiveSubtitlesOverlayTests: XCTestCase {

    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "LiveSubsOverlayTests")

    override func tearDown() async throws {
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    private func makeOverlay() -> LiveSubtitlesOverlay {
        LiveSubtitlesOverlay(userDefaults: defaultsDomain.defaults)
    }

    // MARK: 9. isVisible starts false

    func testIsVisibleStartsFalse() {
        XCTAssertFalse(makeOverlay().isVisible)
    }

    // MARK: 10. show/hide toggles isVisible

    func testShowHideTogglesIsVisible() {
        // Production-фабрика создаёт URLSession; unit-тест подменяет её no-op сессией.
        let overlay = LiveSubtitlesOverlay(
            sseSessionFactory: { _ in NoOpLiveSubtitlesSSESession() },
            userDefaults: defaultsDomain.defaults
        )
        overlay.show()
        XCTAssertTrue(overlay.isVisible)
        overlay.hide()
        XCTAssertFalse(overlay.isVisible)
    }

    // MARK: 11. addEntry + clearAll no crash

    func testAddEntryAndClearAllNoCrash() {
        let overlay = makeOverlay()
        overlay.addEntry(original: "Hello", translation: "Привет")
        overlay.clearAll()
        XCTAssertTrue(true)
    }

    // MARK: 12. max 3 entries — 4th evicts 1st, no crash

    func testMaxThreeEntriesNoCrash() {
        let overlay = makeOverlay()
        for i in 1...4 {
            overlay.addEntry(original: "Orig \(i)", translation: "Trans \(i)")
        }
        overlay.clearAll()
        XCTAssertTrue(true)
    }

    // MARK: 13. showOriginalAndTranslation toggle

    func testShowOriginalToggle() {
        let overlay = makeOverlay()
        overlay.showOriginalAndTranslation = true
        XCTAssertTrue(overlay.showOriginalAndTranslation)
        overlay.showOriginalAndTranslation = false
        XCTAssertFalse(overlay.showOriginalAndTranslation)
    }

    // MARK: 14. resetPosition no crash

    func testResetPositionNoCrash() {
        makeOverlay().resetPosition()
        XCTAssertTrue(true)
    }

    // MARK: 15. rapid fire 20 entries — no crash

    func testRapidFireEntriesNoCrash() {
        let overlay = makeOverlay()
        for i in 0..<20 {
            overlay.addEntry(original: "Original \(i)", translation: "Перевод \(i)")
        }
        overlay.clearAll()
        XCTAssertTrue(true)
    }

    // MARK: 16. restBaseURL default contains 5005

    func testRestBaseURLDefault() {
        XCTAssertTrue(makeOverlay().restBaseURL.contains("5005"))
    }
}
