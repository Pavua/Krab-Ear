/*
 ConversationViewController+Audio — захват и воспроизведение PCM для «Разговора с AI».

 После `conv.ready` одна согласованная частота применяется в обе стороны:
 - uplink: микрофон → ресемплинг → точные 80-мс фреймы → PCM16 LE → WebSocket;
 - downlink: PCM16 LE → Float32 → AVAudioPlayerNode на согласованной частоте.

 Moshi использует 24 кГц и требует ровно 1920 сэмплов на фрейм; старый pipeline
 использует 16 кГц и 1280 сэмплов. До `conv.ready` захват не запускается, поэтому
 сервер никогда не получает фрейм неверной длины. Для legacy `engine.loaded` и
 `conv.ready` без `sample_rate` действует fallback 16 кГц.

 Tap AVAudioNode вызывается в real-time потоке Core Audio, а контроллер изолирован
 главным актором. Поэтому tap читает только nonisolated-зеркало активности, копирует
 сэмплы и передаёт дальнейшую обработку через Task на главный актор.
*/

@preconcurrency import AVFoundation
import Foundation

extension ConversationViewController {

    // MARK: - Хранилище аудиосостояния

    nonisolated(unsafe) private static var audioHolderKey: UInt8 = 0

    /// Зеркало `isSessionActive` для real-time tap без обращения к главному актору.
    nonisolated(unsafe) static var _rtSessionActive: Bool = false

    private var audioHolder: AudioHolder {
        if let h = objc_getAssociatedObject(self, &ConversationViewController.audioHolderKey) as? AudioHolder {
            return h
        }
        let h = AudioHolder()
        objc_setAssociatedObject(self, &ConversationViewController.audioHolderKey, h, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return h
    }

    // MARK: - Согласование контракта

    /// Сбрасывает состояние предыдущей сессии. До ready сборщик намеренно закрыт.
    func prepareAudioNegotiation() {
        ConversationViewController._rtSessionActive = false
        audioHolder.negotiationReady = false
        audioHolder.sampleRate = ConversationAudioContract.fallbackSampleRate
        audioHolder.frameAssembler = ConversationAudioFrameAssembler(
            frameLength: ConversationAudioContract.samplesPerFrame(
                sampleRate: ConversationAudioContract.fallbackSampleRate
            )
        )
    }

    /// Применяет серверную частоту, но не касается аудиоустройств; это чистая граница
    /// между парсингом протокола и AVAudioEngine, пригодная для headless-тестов.
    func configureNegotiatedAudio(sampleRate: Double?) {
        let normalized = ConversationAudioContract.normalizedSampleRate(sampleRate)
        audioHolder.sampleRate = normalized
        audioHolder.frameAssembler = ConversationAudioFrameAssembler(
            frameLength: ConversationAudioContract.samplesPerFrame(sampleRate: normalized)
        )
        audioHolder.negotiationReady = true
    }

    /// Активирует аудио после ready. Повторное идентичное событие не рвёт поток;
    /// изменение частоты приводит к контролируемой перенастройке одного engine.
    func activateNegotiatedAudio(sampleRate: Double?) {
        let normalized = ConversationAudioContract.normalizedSampleRate(sampleRate)
        let alreadyConfigured = audioHolder.negotiationReady
            && audioHolder.sampleRate == normalized
            && audioHolder.engine != nil
        guard !alreadyConfigured else { return }

        if audioHolder.engine != nil {
            stopAudioCapture()
        }
        configureNegotiatedAudio(sampleRate: normalized)
        guard isSessionActive else { return }
        startAudioCapture()
    }

    /// Единый playback-формат: downlink интерпретируется на той же частоте, что ready.
    func makeDownlinkPlaybackFormat() -> AVAudioFormat? {
        AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: audioHolder.sampleRate,
            channels: 1,
            interleaved: false
        )
    }

    /// Добавляет ресемплированный чанк и возвращает только полные 80-мс фреймы.
    /// До согласования возвращает пустой массив — это основной uplink-гейт.
    func assembleUplinkFrames(_ samples: [Float]) -> [[Float]] {
        guard audioHolder.negotiationReady else { return [] }
        return audioHolder.frameAssembler.append(samples)
    }

    // MARK: - Захват (микрофон → uplink)

    /// Запустить захват микрофона и подключить player-node для воспроизведения downlink.
    /// Один AVAudioEngine обслуживает input-tap и player-node на частоте из ready.
    func startAudioCapture() {
        guard audioHolder.negotiationReady else {
            AgentLogger.shared.warn("[Audio] Захват отложен до conv.ready")
            return
        }

        // Зеркало выставляется до старта engine, чтобы первый tap не потерял сессию.
        ConversationViewController._rtSessionActive = isSessionActive

        let engine      = AVAudioEngine()
        let inputNode   = engine.inputNode
        let inputFormat = inputNode.outputFormat(forBus: 0)
        let sampleRate  = audioHolder.sampleRate

        // Конвертер приводит аппаратную частоту микрофона к контракту текущего движка.
        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            AgentLogger.shared.info("[Audio] Не удалось создать uplink-формат \(Int(sampleRate)) Гц")
            return
        }

        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            AgentLogger.shared.info("[Audio] Не удалось создать ресемплер микрофона")
            return
        }

        // Размер tap задан в аппаратных сэмплах; итоговую точность обеспечивает assembler.
        let tapBufferSize = AVAudioFrameCount(max(
            1,
            Int((inputFormat.sampleRate * ConversationAudioContract.frameDurationSeconds).rounded())
        ))

        // Явный @Sendable не даёт замыканию унаследовать @MainActor от этого метода.
        let tapBlock: @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void = { [weak self] buffer, _ in
            // Здесь real-time поток: свойства главного актора читать нельзя.
            guard self != nil, ConversationViewController._rtSessionActive else { return }

            let ratio = targetFormat.sampleRate / max(inputFormat.sampleRate, 1)
            let estimatedFrames = Int(ceil(Double(buffer.frameLength) * ratio)) + 16
            guard let converted = AVAudioPCMBuffer(
                pcmFormat: targetFormat,
                frameCapacity: AVAudioFrameCount(max(1, estimatedFrames))
            ) else { return }

            var error: NSError?
            let supplier = SingleAudioBufferSupplier(buffer: buffer)
            let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
                supplier.next(status: outStatus)
            }
            let status = converter.convert(to: converted, error: &error, withInputFrom: inputBlock)
            guard status == .haveData || status == .inputRanDry else { return }

            // Копия Float-массива безопасно пересекает границу потока.
            let frameLength = Int(converted.frameLength)
            let samples: [Float]
            if let channelData = converted.floatChannelData {
                samples = Array(UnsafeBufferPointer(start: channelData[0], count: frameLength))
            } else {
                samples = []
            }

            // Сборка фреймов, WebSocket и UI принадлежат главному актору.
            Task { @MainActor [weak self] in
                self?.processAudioSamples(samples)
            }
        }

        inputNode.installTap(onBus: 0, bufferSize: tapBufferSize, format: inputFormat, block: tapBlock)

        // Тот же engine обслуживает playback, чтобы не было двух конкурирующих графов.
        let player = AVAudioPlayerNode()
        engine.attach(player)

        guard let playbackFormat = makeDownlinkPlaybackFormat() else {
            AgentLogger.shared.warn("[Audio] Не удалось создать playback-формат \(Int(sampleRate)) Гц")
            // Uplink остаётся полезен даже без устройства воспроизведения.
            audioHolder.engine = engine
            do {
                try engine.start()
                AgentLogger.shared.info("[Audio] Захват запущен только для uplink")
            } catch {
                AgentLogger.shared.error("[Audio] Ошибка запуска движка: \(error.localizedDescription)")
            }
            return
        }

        engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)
        audioHolder.playerNode = player
        audioHolder.engine = engine

        do {
            try engine.start()
            player.play()
            let frameLength = ConversationAudioContract.samplesPerFrame(sampleRate: sampleRate)
            AgentLogger.shared.info(
                "[Audio] Контракт активен: \(Int(sampleRate)) Гц, \(frameLength) сэмплов/80 мс"
            )
        } catch {
            AgentLogger.shared.error("[Audio] Ошибка запуска движка: \(error.localizedDescription)")
        }
    }

    /// Остановить захват микрофона и player node.
    func stopAudioCapture() {
        // Сначала закрываем RT-гейт, затем разбираем граф.
        ConversationViewController._rtSessionActive = false
        audioHolder.engine?.inputNode.removeTap(onBus: 0)
        audioHolder.playerNode?.stop()
        audioHolder.playerNode = nil
        audioHolder.engine?.stop()
        audioHolder.engine = nil
        audioHolder.frameAssembler.reset()
        audioHolder.negotiationReady = false
        resetMicLevelMeter()
        AgentLogger.shared.info("[Audio] Захват остановлен")
    }

    /// Обработать PCM-сэмплы на главном акторе.
    /// Float32 → точные 80-мс фреймы → PCM16 LE + level-meter.
    /// Вызывается только из Task { @MainActor } внутри installTap-блока.
    func processAudioSamples(_ samples: [Float]) {
        // Индикатор получает каждый чанк, а сеть — только полные контрактные фреймы.
        computeAndPushLevel(samples)

        guard isSessionActive, !samples.isEmpty else { return }
        for frame in assembleUplinkFrames(samples) {
            var pcm = Data(capacity: frame.count * 2)
            for sample in frame {
                // Не-числовые значения превращаем в тишину, чтобы не портить PCM.
                let clamped = sample.isFinite ? max(-1.0, min(1.0, sample)) : 0.0
                let integer = Int16(clamped * 32767.0)
                withUnsafeBytes(of: integer.littleEndian) { pcm.append(contentsOf: $0) }
            }
            sendAudioFrame(pcm)
        }
    }

    // MARK: - Воспроизведение (downlink → колонки)

    /// Обработать бинарный PCM16 LE фрейм от сервера.
    /// PCM16 LE → Float32 → AVAudioPCMBuffer на частоте из ready → scheduleBuffer.
    /// Вызывается из Main Actor (из handleWSMessage) — безопасно обращаться к @MainActor свойствам.
    func handleDownlinkAudio(_ data: Data) {
        let frameCount = data.count / 2
        guard audioHolder.negotiationReady,
              data.count.isMultiple(of: 2),
              frameCount > 0,
              let fmt = makeDownlinkPlaybackFormat(),
              let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(frameCount))
        else {
            AgentLogger.shared.info("[Audio] Downlink: фрейм до ready или некорректный PCM (\(data.count) bytes)")
            return
        }

        // Валидный бинарный фрейм означает, что ассистент действительно заговорил.
        if conversationState != .speaking {
            conversationState = .speaking
        }

        guard let player = audioHolder.playerNode else {
            AgentLogger.shared.info("[Audio] Downlink: player недоступен")
            return
        }

        buf.frameLength = AVAudioFrameCount(frameCount)
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            let i16 = raw.bindMemory(to: Int16.self)
            let out = buf.floatChannelData![0]
            for n in 0..<frameCount {
                out[n] = Float(Int16(littleEndian: i16[n])) / 32768.0
            }
        }
        player.scheduleBuffer(buf, completionHandler: nil)
    }

    // MARK: - Поддержка прерывания (Волна 3c)

    /// Сбросить уже запланированные downlink-буферы (прерывание ответа).
    /// AVAudioPlayerNode.stop() снимает все scheduled buffers; play() возвращает
    /// узел в играющее состояние для следующих буферов. Engine и захват не трогаем —
    /// сессия продолжается. Безопасно при nil (аудио не стартовало) и при
    /// не-запущенном engine (play() на attached-node у остановленного engine
    /// не вызывается — guard по isRunning).
    func flushDownlinkPlayback() {
        guard let player = audioHolder.playerNode else { return }
        player.stop()
        if audioHolder.engine?.isRunning == true {
            player.play()
        }
    }
}

// MARK: - Вспомогательные хранилища

/// AVAudioConverter запрашивает вход синхронно, но тип его блока помечен Sendable.
/// Отдельный объект сохраняет одноразовое состояние без захвата изменяемой локальной
/// переменной. `@unchecked Sendable` безопасен здесь: один экземпляр живёт внутри
/// единственного синхронного вызова `convert` и не передаётся между очередями.
private final class SingleAudioBufferSupplier: @unchecked Sendable {
    private let buffer: AVAudioPCMBuffer
    private var wasSupplied = false

    init(buffer: AVAudioPCMBuffer) {
        self.buffer = buffer
    }

    func next(status: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
        guard !wasSupplied else {
            status.pointee = .noDataNow
            return nil
        }
        wasSupplied = true
        status.pointee = .haveData
        return buffer
    }
}

private final class AudioHolder: NSObject {
    var engine: AVAudioEngine?
    var playerNode: AVAudioPlayerNode?
    var sampleRate = ConversationAudioContract.fallbackSampleRate
    var negotiationReady = false
    var frameAssembler = ConversationAudioFrameAssembler(
        frameLength: ConversationAudioContract.samplesPerFrame(
            sampleRate: ConversationAudioContract.fallbackSampleRate
        )
    )
}
