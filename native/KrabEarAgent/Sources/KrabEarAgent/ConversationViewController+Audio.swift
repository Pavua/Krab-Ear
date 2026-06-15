/*
 ConversationViewController+Audio — AVAudioEngine capture / playback stubs.

 Phase 1.3 scope: установка движка и тапов, реальный стриминг — Phase 1.4.

 Uplink:   AVAudioEngine mic tap → PCM 16kHz → Opus (stub) → sendAudioFrame()
 Downlink: handleDownlinkAudio() → Opus → PCM 24kHz (stub) → AVAudioPlayerNode

 Opus-кодек будет подключён в Phase 1.4 (PR 1.4).
 Пока что: PCM-захват настроен, encode/decode — заглушки.

 Swift 6 concurrency note:
 AVAudioNode tap block is called on the Core Audio real-time thread — NOT on the
 main actor. Accessing @MainActor-isolated properties directly from inside the
 block triggers _swift_task_checkIsolatedSwift → EXC_BREAKPOINT.
 Fix: use a nonisolated(unsafe) atomic mirror of isSessionActive for the RT guard,
 then dispatch any main-actor work via Task { @MainActor in … }.
*/

@preconcurrency import AVFoundation
import Foundation

extension ConversationViewController {

    // MARK: - Audio holder (same pattern as WSHolder)

    nonisolated(unsafe) private static var audioHolderKey: UInt8 = 0

    /// Thread-safe mirror of `isSessionActive` for use inside the Core Audio
    /// real-time tap block. Must be updated in lockstep with `isSessionActive`.
    nonisolated(unsafe) static var _rtSessionActive: Bool = false

    private var audioHolder: AudioHolder {
        if let h = objc_getAssociatedObject(self, &ConversationViewController.audioHolderKey) as? AudioHolder {
            return h
        }
        let h = AudioHolder()
        objc_setAssociatedObject(self, &ConversationViewController.audioHolderKey, h, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return h
    }

    // MARK: - Capture (mic → uplink)

    /// Запустить захват микрофона.
    /// Реальный Opus-encode добавляется в PR 1.4; пока PCM-буфер захватывается, но не отправляется.
    func startAudioCapture() {
        // Mirror session state for RT-thread access before starting engine.
        ConversationViewController._rtSessionActive = isSessionActive

        let engine      = AVAudioEngine()
        let inputNode   = engine.inputNode
        let inputFormat = inputNode.outputFormat(forBus: 0)

        // Конвертер в 16kHz mono для uplink.
        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: 16000,
            channels: 1,
            interleaved: false
        ) else {
            AgentLogger.shared.info("[Audio] Не удалось создать 16kHz формат")
            return
        }

        let converter = AVAudioConverter(from: inputFormat, to: targetFormat)

        // 80ms буфер = 16000 * 0.08 = 1280 сэмплов.
        let bufferSize: AVAudioFrameCount = 1280

        // Swift 6: AVAudioNodeTapBlock runs on the Core Audio real-time thread.
        // Without an explicit `@Sendable` annotation the compiler infers the closure as
        // `@MainActor`-isolated (because `startAudioCapture` is `@MainActor`), which makes
        // the Swift 6 runtime assert `_swift_task_checkIsolatedSwift` trap with
        // EXC_BREAKPOINT when the block fires from `RealtimeMessenger.mServiceQueue`.
        // Marking the block `@Sendable` breaks that inference and satisfies Swift 6.
        let tapBlock: @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void = { [weak self] buffer, _ in
            // ⚠️ Core Audio real-time thread — do NOT access @MainActor properties here.
            // Use the nonisolated mirror _rtSessionActive instead of self.isSessionActive.
            guard self != nil, ConversationViewController._rtSessionActive else { return }
            guard let converted = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: bufferSize) else { return }

            var error: NSError?
            // AVAudioConverterInputBlock — not an inout parameter; pass directly.
            let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
                outStatus.pointee = .haveData
                return buffer
            }
            let status = converter?.convert(to: converted, error: &error, withInputFrom: inputBlock)
            guard status == .haveData || status == .inputRanDry else { return }

            // Extract raw samples on RT thread (safe: Float array copy, no actor crossing).
            let frameLength = Int(converted.frameLength)
            let samples: [Float]
            if let channelData = converted.floatChannelData {
                samples = Array(UnsafeBufferPointer(start: channelData[0], count: frameLength))
            } else {
                samples = []
            }

            // Dispatch to main actor for any state mutations / Phase 1.4 encoding.
            Task { @MainActor [weak self] in
                self?.processAudioSamples(samples)
            }
        }

        inputNode.installTap(onBus: 0, bufferSize: bufferSize, format: inputFormat, block: tapBlock)

        audioHolder.engine = engine

        do {
            try engine.start()
            AgentLogger.shared.info("[Audio] Захват запущен (16kHz mono stub)")
        } catch {
            AgentLogger.shared.info("[Audio] Ошибка запуска движка: \(error.localizedDescription)")
        }
    }

    /// Остановить захват микрофона.
    func stopAudioCapture() {
        // Clear RT mirror before removing tap so the in-flight block exits early.
        ConversationViewController._rtSessionActive = false
        audioHolder.engine?.inputNode.removeTap(onBus: 0)
        audioHolder.engine?.stop()
        audioHolder.engine = nil
        audioHolder.playerNode = nil
        // Сбросить level-meter в idle-состояние (@MainActor — безопасно).
        resetMicLevelMeter()
        AgentLogger.shared.info("[Audio] Захват остановлен")
    }

    /// Обработать PCM-сэмплы на главном акторе.
    /// Phase 1.3: заглушка. Phase 1.4: Opus-encode → sendAudioFrame(opusData).
    /// Вызывается только из Task { @MainActor } внутри installTap-блока.
    func processAudioSamples(_ samples: [Float]) {
        // Stub: в Phase 1.4 здесь будет Opus-encode → sendAudioFrame(opusData).
        _ = samples // encoder placeholder — consume to silence warning

        // Level-meter: вычислить RMS и передать в визуализатор.
        // Безопасно — вызываемся на @MainActor, без IPC (AGENT-3 чист).
        computeAndPushLevel(samples)
    }

    // MARK: - Playback (downlink → speaker)

    /// Обработать бинарный Opus-фрейм от сервера.
    /// Phase 1.3: stub — логируем размер, без реального воспроизведения.
    /// Phase 1.4: Opus-decode → AVAudioPCMBuffer → scheduleBuffer.
    /// Вызывается из Main Actor (из handleWSMessage) — безопасно обращаться к @MainActor свойствам.
    func handleDownlinkAudio(_ data: Data) {
        // Обновляем состояние — сервер присылает аудио только когда AI говорит.
        if conversationState != .speaking {
            conversationState = .speaking
        }
        AgentLogger.shared.info("[Audio] Downlink frame: \(data.count) bytes (Opus decode stub)")

        // Stub: в Phase 1.4 здесь будет:
        // 1. Opus decode → PCM 24kHz
        // 2. audioHolder.playerNode?.scheduleBuffer(pcmBuffer)
    }
}

// MARK: - AudioHolder

private final class AudioHolder: NSObject {
    var engine: AVAudioEngine?
    var playerNode: AVAudioPlayerNode?
}
