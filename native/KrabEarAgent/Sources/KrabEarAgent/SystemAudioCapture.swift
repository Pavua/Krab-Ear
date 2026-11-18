/*
 SystemAudioCapture — захват системного аудио через ScreenCaptureKit (SCStream).

 Горячая клавиша: Cmd+Option+Shift+L (зарегистрирована в main+StatusMenu.swift).
 Поток:
   1. SCStreamOutput.stream(_:didOutputSampleBuffer:of:) → extractPCM()
   2. PCM буферизуется в ringBuffer (~1 с при 16 kHz)
   3. Каждые ~1 с → base64 → IPC `live_subs_ingest(audio_chunk, sample_rate, target_lang, is_final)`
   4. stop() → IPC `live_subs_stop` (flush последнего чанка)

 Упрощённый PCM path (без AVAudioConverter):
   - берём raw Int16 сэмплы напрямую из CMSampleBuffer если формат уже Int16/Float32
   - если Float32 — downconvert вручную (умножаем на 32767, clamp)
   - Resampling до 16 kHz делает Python backend через librosa

 Связи:
 - IPCClient: отправка чанков и stop-команды
 - LiveSubtitlesOverlay: запускается/останавливается вместе с захватом
*/

import Foundation
import os
@preconcurrency import ScreenCaptureKit
import CoreMedia
import AppKit

struct SystemAudioStream {
    let identity: ObjectIdentifier
    let start: @MainActor () async throws -> Void
    let stop: @MainActor () async -> Void
}

/// Привязан к одному Start. Каждый отложенный send хранит тот же epoch.
private final class LiveSubsCapturePermit: Sendable {
    let lease: ConfirmedPrivacyLease
    let epoch: String
    // В deliveryQueue попадают только ID. Отзыв освобождает PCM сразу, даже
    // когда предыдущий IPC или сама очередь ещё заблокированы.
    private let pending = OSAllocatedUnfairLock<[UUID: [Int16]]?>(initialState: [:])

    init?(lease: ConfirmedPrivacyLease?) {
        guard let lease, lease.isValid, let epoch = lease.liveSubsEpoch else { return nil }
        self.lease = lease
        self.epoch = epoch
    }

    var isValid: Bool {
        guard lease.isValid else { revoke(); return false }
        return pending.withLock { $0 != nil }
    }

    func enqueue(_ samples: [Int16]) -> UUID? {
        pending.withLock { chunks in
            guard chunks != nil, lease.isValid else { chunks = nil; return nil }
            let id = UUID()
            chunks?[id] = samples
            return id
        }
    }

    func take(_ id: UUID) -> [Int16]? {
        pending.withLock { chunks in
            guard lease.isValid else { chunks = nil; return nil }
            return chunks?.removeValue(forKey: id)
        }
    }

    func revoke() { pending.withLock { $0 = nil } }
}

@MainActor
final class SystemAudioCapture: NSObject {

    // MARK: - Configuration

    /// Целевой язык перевода, передаётся бэкенду вместе с каждым чанком.
    var targetLang: String = "ru"

    /// Показывать ли оригинал+перевод (true) или только перевод (false).
    var showOriginalAndTranslation: Bool = true

    // MARK: - State

    private(set) var isCapturing = false
    private var stream: SystemAudioStream?
    private let makeStream: @MainActor (SystemAudioCapture) async throws -> SystemAudioStream?
    private var startTask: Task<Void, Never>?
    private var stopTask: Task<Void, Never>?
    private var captureGeneration: UUID?
    private var capturePermit: LiveSubsCapturePermit?
    private nonisolated let intake = OSAllocatedUnfairLock<(ObjectIdentifier, LiveSubsCapturePermit)?>(initialState: nil)
    private let deliveryQueue: DispatchQueue
    private let deliver: @Sendable (String, [String: Any]) -> Void
    private let logger = AgentLogger.shared

    /// Флаг для однократной отправки breadcrumb при первом аудио-сэмпле.
    private var didReceiveFirstSample = false

    // MARK: - Audio buffering

    /// Накопленные Int16 сэмплы (нативная частота дискретизации SCStream)
    private var ringBuffer: [Int16] = []
    /// Примерное количество сэмплов за ~1 сек при 48 kHz
    private let chunkSamples = 48_000
    private var nativeSampleRate: Int = 48_000

    // MARK: - Flush timer

    private var flushTimer: Timer?

    // MARK: - Init

    init(
        ipcClient: IPCClient,
        deliveryQueue: DispatchQueue = DispatchQueue(label: "krabear.sysaudio.ipc", qos: .utility),
        deliver: (@Sendable (String, [String: Any]) -> Void)? = nil,
        makeStream: (@MainActor (SystemAudioCapture) async throws -> SystemAudioStream?)? = nil
    ) {
        self.deliveryQueue = deliveryQueue
        self.deliver = deliver ?? { method, params in _ = try? ipcClient.call(method: method, params: params) }
        self.makeStream = makeStream ?? { try await $0.makeScreenCaptureStream() }
        super.init()
    }

    // MARK: - Public API

    /// Запускает захват системного аудио. Асинхронный — SCStream конфигурируется через async API.
    @discardableResult
    func start(lease: ConfirmedPrivacyLease?) -> Bool {
        guard startTask == nil, stopTask == nil, !isCapturing,
              let permit = LiveSubsCapturePermit(lease: lease) else { return false }
        let generation = UUID()
        captureGeneration = generation
        capturePermit = permit
        didReceiveFirstSample = false
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "capture.start_called")
        startTask = Task { @MainActor in
            defer {
                startTask = nil
                if !isCapturing && captureGeneration == generation {
                    captureGeneration = nil
                }
            }
            guard !Task.isCancelled else { return }
            await startCapture(generation: generation)
        }
        return true
    }

    /// Stop обычной сессии сохраняет последний чанк. Privacy сбрасывает буфер.
    func stop(flush: Bool = true) {
        intake.withLock { $0 = nil }
        if !flush { capturePermit?.revoke() }
        guard captureGeneration != nil else { return }
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "capture.stop_called")
        captureGeneration = nil
        startTask?.cancel()
        let wasCapturing = isCapturing
        isCapturing = false
        flushTimer?.invalidate()
        flushTimer = nil

        if flush && wasCapturing {
            sendChunk(isFinal: true)
        }
        ringBuffer.removeAll()

        if let capturedStream = stream {
            stream = nil
            stopTask = Task { @MainActor in
                defer { stopTask = nil }
                await capturedStream.stop()
            }
        }
        logger.info("SystemAudioCapture: остановлен")

        if wasCapturing, let permit = capturePermit {
            let deliver = deliver
            deliveryQueue.async {
                deliver("live_subs_stop", ["discard": !permit.isValid, "live_subs_policy_epoch": permit.epoch])
            }
        }
    }

    // MARK: - Private: start pipeline

    private func startCapture(generation: UUID) async {
        do {
            guard let permit = capturePermit, permit.isValid, let newStream = try await makeStream(self),
                  captureGeneration == generation, permit.isValid, !Task.isCancelled else { return }
            do {
                try await newStream.start()
            } catch {
                await Task { @MainActor in await newStream.stop() }.value
                throw error
            }
            guard captureGeneration == generation, permit.isValid, !Task.isCancelled else {
                // SDK может завершить Start после Stop. Дожидаемся закрытия
                // позднего потока, прежде чем разрешить следующий Start.
                await Task { @MainActor in await newStream.stop() }.value
                return
            }
            stream = newStream
            intake.withLock { $0 = (newStream.identity, permit) }
            isCapturing = true
            logger.info("SystemAudioCapture: захват запущен")
            SentryConfig.recordBreadcrumb(
                category: "live_subs",
                message: "stream.started",
                data: ["target_lang": targetLang]
            )

            // Flush-таймер: раз в секунду отправляем накопленный буфер
            flushTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
                guard let self else { return }
                Task { @MainActor in
                    guard self.isCapturing, self.captureGeneration == generation else { return }
                    self.sendChunk(isFinal: false)
                }
            }
        } catch {
            guard captureGeneration == generation else { return }
            logger.error("SystemAudioCapture: ошибка запуска — \(error.localizedDescription)")
            SentryConfig.recordBreadcrumb(
                category: "live_subs",
                message: "stream.error",
                data: ["error": error.localizedDescription]
            )
        }
    }

    private func makeScreenCaptureStream() async throws -> SystemAudioStream? {
        // Получаем список shareable content (нужен для SCStreamConfiguration)
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        SentryConfig.recordBreadcrumb(
            category: "live_subs",
            message: "screencapture.permission_ok",
            data: ["displays_count": content.displays.count]
        )

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        // minimumFrameInterval = высокое число → видео нам не нужно
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 5

        // Берём первый дисплей как источник (аудио от всей системы)
        guard let display = content.displays.first else {
            logger.warn("SystemAudioCapture: дисплей не найден, захват аудио невозможен")
            SentryConfig.recordBreadcrumb(
                category: "live_subs",
                message: "stream.error",
                data: ["error": "no_display_found"]
            )
            return nil
        }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let newStream = SCStream(filter: filter, configuration: config, delegate: nil)
        try newStream.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "krabear.sysaudio", qos: .userInteractive))
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "stream.initialized")

        return SystemAudioStream(
            identity: ObjectIdentifier(newStream),
            start: { try await newStream.startCapture() },
            stop: { try? await newStream.stopCapture() }
        )
    }

    // MARK: - Private: send IPC chunk

    private func sendChunk(isFinal: Bool) {
        guard !ringBuffer.isEmpty else { return }
        guard let permit = capturePermit, let id = permit.enqueue(ringBuffer) else {
            ringBuffer.removeAll()
            return
        }
        ringBuffer = []

        let sr = nativeSampleRate
        let lang = targetLang
        let deliver = deliver
        deliveryQueue.async {
            guard let samples = permit.take(id) else { return }
            let data = samples.withUnsafeBytes { Data($0) }
            let params: [String: Any] = [
                "audio_chunk": data.base64EncodedString(), "sample_rate": sr,
                "target_lang": lang, "is_final": isFinal,
                "live_subs_policy_epoch": permit.epoch,
            ]
            guard permit.isValid else { return }
            deliver("live_subs_ingest", params)
        }
    }

    // MARK: - PCM extraction from CMSampleBuffer

    /// Извлекает Int16 сэмплы из CMSampleBuffer.
    /// Поддерживает Int16 и Float32 форматы (SCStream обычно отдаёт Float32).
    /// nonisolated — чистая трансформация данных, не обращается к actor-состоянию.
    /// Возвращает (samples, detectedSampleRate).
    nonisolated func extractSamples(from sampleBuffer: CMSampleBuffer) -> (samples: [Int16], sampleRate: Int)? {
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return nil }

        let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer)
        guard let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc!)?.pointee else {
            return nil
        }

        let detectedSampleRate = Int(asbd.mSampleRate)
        let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)
        let channelCount = Int(asbd.mChannelsPerFrame)
        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0

        var rawPtr: UnsafeMutablePointer<CChar>? = nil
        var length = 0
        let status = CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &rawPtr)
        guard status == kCMBlockBufferNoErr, let ptr = rawPtr else { return nil }

        var result = [Int16]()
        result.reserveCapacity(frameCount)

        if isFloat {
            // Float32 → Int16
            let floatPtr = ptr.withMemoryRebound(to: Float32.self, capacity: frameCount * channelCount) { $0 }
            for i in 0..<frameCount {
                // Смешиваем каналы → моно
                var sum: Float32 = 0
                for ch in 0..<channelCount {
                    sum += floatPtr[i * channelCount + ch]
                }
                let mono = sum / Float32(channelCount)
                let clamped = max(-1.0, min(1.0, mono))
                result.append(Int16(clamped * 32767.0))
            }
        } else {
            // Int16 raw
            let i16Ptr = ptr.withMemoryRebound(to: Int16.self, capacity: frameCount * channelCount) { $0 }
            for i in 0..<frameCount {
                var sum: Int32 = 0
                for ch in 0..<channelCount {
                    sum += Int32(i16Ptr[i * channelCount + ch])
                }
                result.append(Int16(sum / Int32(channelCount)))
            }
        }
        return (samples: result, sampleRate: detectedSampleRate)
    }
}

// MARK: - SCStreamOutput

extension SystemAudioCapture: SCStreamOutput {
    nonisolated func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        receiveSamples(from: ObjectIdentifier(stream)) { extractSamples(from: sampleBuffer) }
    }

    /// Отсев до копирования PCM на очереди ScreenCaptureKit и после hop на AppKit.
    @discardableResult
    nonisolated func receiveSamples(
        from streamID: ObjectIdentifier, extract: () -> (samples: [Int16], sampleRate: Int)?
    ) -> Task<Void, Never>? {
        guard let (id, permit) = intake.withLock({ $0 }), id == streamID, permit.isValid,
              let (samples, sr) = extract() else { return nil }
        return Task { @MainActor in
            guard self.isCapturing, self.capturePermit === permit, permit.isValid else { return }
            self.nativeSampleRate = sr
            self.ringBuffer.append(contentsOf: samples)
            // Первый аудио-сэмпл — однократный breadcrumb для диагностики
            if !self.didReceiveFirstSample {
                self.didReceiveFirstSample = true
                SentryConfig.recordBreadcrumb(
                    category: "live_subs",
                    message: "first_sample_received",
                    data: ["sample_rate": sr, "samples_count": samples.count]
                )
            }
            // Не ждём таймер — если накопили целый chunk, отправим сразу
            if self.ringBuffer.count >= self.chunkSamples {
                self.sendChunk(isFinal: false)
            }
        }
    }
}
