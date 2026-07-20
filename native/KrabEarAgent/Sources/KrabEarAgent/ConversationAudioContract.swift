/*
 ConversationAudioContract — чистая модель аудиоконтракта «Разговора с AI».

 Voice Gateway сообщает частоту PCM в `conv.ready.data.sample_rate`. Этот файл
 нормализует значение, ограниченно хранит звук холодного старта, ресемплирует его
 и собирает произвольные чанки в точные 80-миллисекундные фреймы. Чистая реализация
 не зависит от AVAudioEngine, поэтому все границы проверяются без аудиоустройств.
*/

import Foundation

enum ConversationAudioContract {
    static let fallbackSampleRate: Double = 16_000
    static let frameDurationSeconds: Double = 0.080
    static let prebufferDurationSeconds: Double = 60

    static var prebufferMaxSampleCount: Int {
        Int(fallbackSampleRate * prebufferDurationSeconds)
    }

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

struct ConversationAudioPrebuffer {
    let maxSampleCount: Int
    private var bufferedSamples: [Float] = []
    private(set) var droppedSampleCount = 0

    init(maxSampleCount: Int) {
        self.maxSampleCount = max(1, maxSampleCount)
    }

    var bufferedSampleCount: Int {
        bufferedSamples.count
    }

    /// Сохраняет самые ранние сэмплы сессии: цель prebuffer — не потерять именно
    /// первую реплику во время холодной загрузки движка. После лимита новые сэмплы
    /// учитываются как отброшенные, но память больше не растёт.
    mutating func append(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        let available = max(0, maxSampleCount - bufferedSamples.count)
        let acceptedCount = min(available, samples.count)
        if acceptedCount > 0 {
            bufferedSamples.append(contentsOf: samples.prefix(acceptedCount))
        }
        droppedSampleCount += samples.count - acceptedCount
    }

    /// Передаёт владение накопленным звуком flush-пути и переоткрывает буфер.
    mutating func drain() -> [Float] {
        let drained = bufferedSamples
        bufferedSamples.removeAll(keepingCapacity: true)
        droppedSampleCount = 0
        return drained
    }

    /// Полная очистка на stop/error освобождает и зарезервированную память.
    mutating func reset() {
        bufferedSamples.removeAll(keepingCapacity: false)
        droppedSampleCount = 0
    }
}

enum ConversationAudioResampler {
    /// Линейный ресемплер нужен только для ограниченного cold-start prebuffer.
    /// Живой поток после ready продолжает использовать AVAudioConverter.
    static func resample(
        _ samples: [Float],
        sourceSampleRate: Double,
        targetSampleRate: Double
    ) -> [Float] {
        guard !samples.isEmpty else { return [] }
        let sourceRate = ConversationAudioContract.normalizedSampleRate(sourceSampleRate)
        let targetRate = ConversationAudioContract.normalizedSampleRate(targetSampleRate)
        guard sourceRate != targetRate else { return samples }

        let outputCount = max(1, Int((Double(samples.count) * targetRate / sourceRate).rounded()))
        var output = [Float](repeating: 0, count: outputCount)
        let sourceStep = sourceRate / targetRate

        for outputIndex in 0..<outputCount {
            let sourcePosition = Double(outputIndex) * sourceStep
            let lowerIndex = min(Int(sourcePosition), samples.count - 1)
            let upperIndex = min(lowerIndex + 1, samples.count - 1)
            let fraction = Float(sourcePosition - Double(lowerIndex))
            output[outputIndex] = samples[lowerIndex]
                + (samples[upperIndex] - samples[lowerIndex]) * fraction
        }
        return output
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
