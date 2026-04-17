/*
 ConversationViewController+Audio — AVAudioEngine capture / playback stubs.

 Phase 1.3 scope: установка движка и тапов, реальный стриминг — Phase 1.4.

 Uplink:   AVAudioEngine mic tap → PCM 16kHz → Opus (stub) → sendAudioFrame()
 Downlink: handleDownlinkAudio() → Opus → PCM 24kHz (stub) → AVAudioPlayerNode

 Opus-кодек будет подключён в Phase 1.4 (PR 1.4).
 Пока что: PCM-захват настроен, encode/decode — заглушки.
*/

@preconcurrency import AVFoundation
import Foundation

extension ConversationViewController {

    // MARK: - Audio holder (same pattern as WSHolder)

    nonisolated(unsafe) private static var audioHolderKey: UInt8 = 0

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

        inputNode.installTap(onBus: 0, bufferSize: bufferSize, format: inputFormat) { [weak self] buffer, _ in
            guard let self, self.isSessionActive else { return }
            guard let converted = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: bufferSize) else { return }

            var error: NSError?
            // AVAudioConverterInputBlock — not an inout parameter; pass directly.
            let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
                outStatus.pointee = .haveData
                return buffer
            }
            let status = converter?.convert(to: converted, error: &error, withInputFrom: inputBlock)
            guard status == .haveData || status == .inputRanDry else { return }

            // Stub: в Phase 1.4 здесь будет Opus-encode → sendAudioFrame(opusData).
            _ = converted // encoder placeholder — consume to silence warning
        }

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
        audioHolder.engine?.inputNode.removeTap(onBus: 0)
        audioHolder.engine?.stop()
        audioHolder.engine = nil
        audioHolder.playerNode = nil
        AgentLogger.shared.info("[Audio] Захват остановлен")
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
