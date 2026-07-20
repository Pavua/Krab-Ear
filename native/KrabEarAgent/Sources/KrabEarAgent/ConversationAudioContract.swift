/*
 ConversationAudioContract — чистая модель аудиоконтракта «Разговора с AI».

 Voice Gateway сообщает частоту PCM в `conv.ready.data.sample_rate`. Этот файл
 нормализует значение и собирает произвольные чанки после ресемплинга в точные
 80-миллисекундные фреймы. Чистая реализация не зависит от AVAudioEngine, поэтому
 граничные случаи split/merge проверяются XCTest без микрофона и колонок.
*/

import Foundation

enum ConversationAudioContract {
    static let fallbackSampleRate: Double = 16_000
    static let frameDurationSeconds: Double = 0.080

    /// Защищает AVAudioFormat и размер буфера от испорченного серверного значения.
    /// Диапазон оставлен шире текущих 16/24 кГц для совместимости с будущими движками.
    static func normalizedSampleRate(_ sampleRate: Double?) -> Double {
        guard let sampleRate,
              sampleRate.isFinite,
              sampleRate >= 8_000,
              sampleRate <= 48_000 else {
            return fallbackSampleRate
        }
        return sampleRate.rounded()
    }

    /// Количество mono-сэмплов в одном обязательном 80-миллисекундном WS-фрейме.
    static func samplesPerFrame(sampleRate: Double) -> Int {
        max(1, Int((normalizedSampleRate(sampleRate) * frameDurationSeconds).rounded()))
    }
}

struct ConversationAudioFrameAssembler {
    let frameLength: Int
    private var bufferedSamples: [Float] = []

    init(frameLength: Int) {
        self.frameLength = max(1, frameLength)
    }

    var bufferedSampleCount: Int {
        bufferedSamples.count
    }

    /// Добавляет очередной ресемплированный чанк и возвращает только полные фреймы.
    /// Неполный хвост остаётся до следующего вызова, поэтому сэмплы не теряются.
    mutating func append(_ samples: [Float]) -> [[Float]] {
        guard !samples.isEmpty else { return [] }
        bufferedSamples.append(contentsOf: samples)

        let completeFrameCount = bufferedSamples.count / frameLength
        guard completeFrameCount > 0 else { return [] }

        var frames: [[Float]] = []
        frames.reserveCapacity(completeFrameCount)
        for index in 0..<completeFrameCount {
            let start = index * frameLength
            let end = start + frameLength
            frames.append(Array(bufferedSamples[start..<end]))
        }

        bufferedSamples.removeFirst(completeFrameCount * frameLength)
        return frames
    }

    mutating func reset() {
        bufferedSamples.removeAll(keepingCapacity: true)
    }
}
