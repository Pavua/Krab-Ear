/*
 ConversationViewController+Audio — AVAudioEngine capture / playback (Phase 1.4, wired).

 Phase 1.4: PCM16 LE mono, no codec needed.
   Uplink:   AVAudioEngine mic tap → resample to 16kHz mono Float32 →
             Float32→PCM16 LE conversion → sendAudioFrame(pcmData)
   Downlink: handleDownlinkAudio(data) → PCM16 LE → Float32 →
             AVAudioPCMBuffer → AVAudioPlayerNode.scheduleBuffer

 Voice Gateway speaks raw PCM16 LE mono (confirmed: conversation.py «Бинарные фреймы: PCM16 LE mono»).
 No Opus library is needed — format conversion is done inline.

 Downlink sample rate (downlinkSampleRate = 24000 Hz) matches VG's documented TTS output.
 AVAudioEngine's mainMixerNode auto-resamples to the hardware output rate, so a mismatch
 here only affects playback pitch — it never crashes. Adjust as the VG contract evolves.

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

    /// Downlink sample rate from the Voice Gateway (PCM16 LE mono).
    /// VERIFIED live 2026-06-20: krab_ear_pipeline engine emits conv.ready.data.sample_rate = 16000
    /// (symmetric with the 16kHz uplink). AVAudioEngine auto-resamples to hardware rate.
    /// TODO: parse conv.ready.data.sample_rate dynamically (other engines may differ, e.g. Moshi 24k).
    private static let downlinkSampleRate: Double = 16000

    private var audioHolder: AudioHolder {
        if let h = objc_getAssociatedObject(self, &ConversationViewController.audioHolderKey) as? AudioHolder {
            return h
        }
        let h = AudioHolder()
        objc_setAssociatedObject(self, &ConversationViewController.audioHolderKey, h, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return h
    }

    // MARK: - Capture (mic → uplink)

    /// Запустить захват микрофона и подключить player-node для воспроизведения downlink.
    /// Phase 1.4: один AVAudioEngine обслуживает и input-tap (uplink) и player-node (downlink).
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

            // Dispatch to main actor for PCM16 encoding and level-meter update.
            Task { @MainActor [weak self] in
                self?.processAudioSamples(samples)
            }
        }

        inputNode.installTap(onBus: 0, bufferSize: bufferSize, format: inputFormat, block: tapBlock)

        // MARK: Downlink player node (Phase 1.4)
        // Attach a player node to the SAME engine so one engine handles both
        // input-tap (uplink) and playback (downlink) without two engine starts.
        let player = AVAudioPlayerNode()
        engine.attach(player)

        guard let playbackFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: ConversationViewController.downlinkSampleRate,
            channels: 1,
            interleaved: false
        ) else {
            AgentLogger.shared.warn("[Audio] Не удалось создать playback-формат 24kHz — downlink недоступен")
            // Continue without player: uplink still works.
            audioHolder.engine = engine
            do {
                try engine.start()
                AgentLogger.shared.info("[Audio] Захват запущен (uplink only, no player)")
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
            AgentLogger.shared.info("[Audio] Захват запущен (PCM16 uplink + downlink player, 16kHz/24kHz)")
        } catch {
            AgentLogger.shared.error("[Audio] Ошибка запуска движка: \(error.localizedDescription)")
        }
    }

    /// Остановить захват микрофона и player node.
    func stopAudioCapture() {
        // Clear RT mirror before removing tap so the in-flight block exits early.
        ConversationViewController._rtSessionActive = false
        audioHolder.engine?.inputNode.removeTap(onBus: 0)
        // Stop player cleanly before engine stop to avoid scheduling on a stopped node.
        audioHolder.playerNode?.stop()
        audioHolder.playerNode = nil
        audioHolder.engine?.stop()
        audioHolder.engine = nil
        // Сбросить level-meter в idle-состояние (@MainActor — безопасно).
        resetMicLevelMeter()
        AgentLogger.shared.info("[Audio] Захват остановлен")
    }

    /// Обработать PCM-сэмплы на главном акторе.
    /// Phase 1.4: Float32 → PCM16 LE → sendAudioFrame (uplink) + level-meter.
    /// Вызывается только из Task { @MainActor } внутри installTap-блока.
    func processAudioSamples(_ samples: [Float]) {
        // Level-meter: вычислить RMS и передать в визуализатор.
        // Безопасно — вызываемся на @MainActor, без IPC (AGENT-3 чист).
        computeAndPushLevel(samples)

        // Uplink: Float32 → PCM16 LE → binary WS frame.
        // This path is @MainActor; 1280 samples (80ms) makes the loop cheap.
        guard isSessionActive, !samples.isEmpty else { return }
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            let clamped = s.isFinite ? max(-1.0, min(1.0, s)) : 0.0  // NaN/Inf-safe clamp
            let i = Int16(clamped * 32767.0)
            withUnsafeBytes(of: i.littleEndian) { pcm.append(contentsOf: $0) }
        }
        sendAudioFrame(pcm)
    }

    // MARK: - Playback (downlink → speaker)

    /// Обработать бинарный PCM16 LE фрейм от сервера.
    /// Phase 1.4: PCM16 LE → Float32 → AVAudioPCMBuffer → scheduleBuffer.
    /// Вызывается из Main Actor (из handleWSMessage) — безопасно обращаться к @MainActor свойствам.
    func handleDownlinkAudio(_ data: Data) {
        // Обновляем состояние — сервер присылает аудио только когда AI говорит.
        if conversationState != .speaking {
            conversationState = .speaking
        }

        let frameCount = data.count / 2
        guard frameCount > 0,
              let fmt = AVAudioFormat(
                  commonFormat: .pcmFormatFloat32,
                  sampleRate: ConversationViewController.downlinkSampleRate,
                  channels: 1,
                  interleaved: false
              ),
              let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(frameCount)),
              let player = audioHolder.playerNode
        else {
            AgentLogger.shared.info("[Audio] Downlink: пропуск фрейма (\(data.count) bytes) — player недоступен")
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

    // MARK: - Interrupt support (Волна 3c)

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

// MARK: - AudioHolder

private final class AudioHolder: NSObject {
    var engine: AVAudioEngine?
    var playerNode: AVAudioPlayerNode?
}
