/*
 ConversationAudioContractTests — регрессионные тесты согласования аудиоформата с Voice Gateway.

 Проверяют чистую часть контракта без доступа к микрофону и колонкам:
 - 80 мс всегда превращаются ровно в 1280 сэмплов при 16 кГц и 1920 при 24 кГц;
 - сборщик корректно объединяет короткие чанки и делит длинные без потерь;
 - до `conv.ready` uplink не формируется;
 - downlink использует согласованную частоту, а старый Gateway получает fallback 16 кГц.
*/

import XCTest
@testable import KrabEarAgent

final class ConversationAudioFrameAssemblerTests: XCTestCase {

    func test_frameLength_matchesEightyMillisecondsAtSupportedRates() {
        XCTAssertEqual(ConversationAudioContract.samplesPerFrame(sampleRate: 16_000), 1_280)
        XCTAssertEqual(ConversationAudioContract.samplesPerFrame(sampleRate: 24_000), 1_920)
    }

    func test_exactBoundary_emitsOneFrameWithoutRemainder() {
        var assembler = ConversationAudioFrameAssembler(frameLength: 1_280)
        let samples = (0..<1_280).map(Float.init)

        let frames = assembler.append(samples)

        XCTAssertEqual(frames, [samples])
        XCTAssertEqual(assembler.bufferedSampleCount, 0)
    }

    func test_splitInput_isMergedIntoOneExactFrame() {
        var assembler = ConversationAudioFrameAssembler(frameLength: 1_280)
        let samples = (0..<1_280).map(Float.init)

        XCTAssertTrue(assembler.append(Array(samples[..<500])).isEmpty)
        let frames = assembler.append(Array(samples[500...]))

        XCTAssertEqual(frames, [samples])
        XCTAssertEqual(assembler.bufferedSampleCount, 0)
    }

    func test_mergedInput_isSplitIntoFramesAndKeepsTail() {
        var assembler = ConversationAudioFrameAssembler(frameLength: 1_280)
        let samples = (0..<3_000).map(Float.init)

        let firstBatch = assembler.append(samples)

        XCTAssertEqual(firstBatch.count, 2)
        XCTAssertEqual(firstBatch[0], Array(samples[0..<1_280]))
        XCTAssertEqual(firstBatch[1], Array(samples[1_280..<2_560]))
        XCTAssertEqual(assembler.bufferedSampleCount, 440)

        let tail = (3_000..<3_840).map(Float.init)
        let secondBatch = assembler.append(tail)

        XCTAssertEqual(secondBatch, [Array(samples[2_560...]) + tail])
        XCTAssertEqual(assembler.bufferedSampleCount, 0)
    }
}

@MainActor
final class ConversationAudioNegotiationTests: XCTestCase {

    private func makeVC() -> ConversationViewController {
        let vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
        return vc
    }

    func test_beforeReady_uplinkFramesAreNotProduced() {
        let vc = makeVC()
        vc.prepareAudioNegotiation()

        XCTAssertTrue(vc.assembleUplinkFrames(Array(repeating: 0.25, count: 1_280)).isEmpty)
    }

    func test_moshiReady_configuresTwentyFourKDownlinkAndFrameSize() {
        let vc = makeVC()
        vc.prepareAudioNegotiation()
        vc.handleDownlinkEvent(.engineReady(name: "moshi", elapsedSec: 0, sampleRate: 24_000))

        XCTAssertEqual(vc.makeDownlinkPlaybackFormat()?.sampleRate, 24_000)
        XCTAssertEqual(
            vc.assembleUplinkFrames(Array(repeating: 0.25, count: 1_920)).first?.count,
            1_920
        )
    }

    func test_missingReadySampleRate_usesLegacySixteenKFallback() {
        let vc = makeVC()
        vc.prepareAudioNegotiation()
        vc.handleDownlinkEvent(.engineReady(name: "legacy", elapsedSec: 0, sampleRate: nil))

        XCTAssertEqual(vc.makeDownlinkPlaybackFormat()?.sampleRate, 16_000)
        XCTAssertEqual(
            vc.assembleUplinkFrames(Array(repeating: 0.25, count: 1_280)).first?.count,
            1_280
        )
    }

    func test_moshiReadyPayload_parsesTwentyFourKSampleRate() {
        let payload = #"{"type":"conv.ready","data":{"engine":"moshi","sample_rate":24000}}"#
        let event = ConversationEvent.decode(from: Data(payload.utf8))

        guard case .engineReady(let name, _, let sampleRate) = event else {
            return XCTFail("Ожидалось типизированное событие engineReady")
        }
        XCTAssertEqual(name, "moshi")
        XCTAssertEqual(sampleRate, 24_000)
    }
}
