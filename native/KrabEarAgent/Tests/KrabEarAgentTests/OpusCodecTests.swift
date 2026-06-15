import XCTest
import AVFoundation
@testable import KrabEarAgent

final class OpusCodecTests: XCTestCase {
    
    func testEncodeDecodeRoundTrip() throws {
        // Инициализируем кодек
        let codec = try OpusCodec()
        
        // Генерируем 20мс PCM буфер (320 сэмплов) @ 16kHz (Float32, Mono)
        let sampleRate: Double = 16000
        let frameCount: AVAudioFrameCount = 320 // 20мс
        guard let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false) else {
            XCTFail("Failed to create audio format")
            return
        }
        guard let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else {
            XCTFail("Failed to create pcm buffer")
            return
        }
        pcmBuffer.frameLength = frameCount
        
        // Заполняем синусоидой (440 Гц)
        let frequency: Float = 440.0
        let channelData = pcmBuffer.floatChannelData![0]
        for i in 0..<Int(frameCount) {
            let time = Float(i) / Float(sampleRate)
            channelData[i] = sin(2.0 * .pi * frequency * time)
        }
        
        // Кодируем (16kHz -> Opus)
        let encodedData = try codec.encode(pcmBuffer)
        XCTAssertFalse(encodedData.isEmpty, "Encoded Opus data is empty")
        
        // Декодируем (Opus -> 24kHz)
        let decodedPcm = try codec.decode(encodedData)
        
        // Проверяем формат и длину результата
        XCTAssertEqual(decodedPcm.format.sampleRate, 24000.0)
        XCTAssertEqual(decodedPcm.format.channelCount, 1)
        // При 20мс фрейме и 24kHz количество сэмплов должно быть 24000 * 0.02 = 480
        XCTAssertEqual(decodedPcm.frameLength, 480)
        
        // Проверяем, что есть ненулевая энергия (сигнал не пуст)
        guard let decodedData = decodedPcm.floatChannelData?[0] else {
            XCTFail("No decoded channel data")
            return
        }
        
        var energy: Float = 0
        for i in 0..<Int(decodedPcm.frameLength) {
            let sample = decodedData[i]
            energy += sample * sample
        }
        XCTAssertGreaterThan(energy, 0.0, "Decoded PCM buffer has zero energy")
    }
}
