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
@preconcurrency import ScreenCaptureKit
import CoreMedia
import AppKit

@MainActor
final class SystemAudioCapture: NSObject {

    // MARK: - Configuration

    /// Целевой язык перевода, передаётся бэкенду вместе с каждым чанком.
    var targetLang: String = "ru"

    /// Показывать ли оригинал+перевод (true) или только перевод (false).
    var showOriginalAndTranslation: Bool = true

    // MARK: - State

    private(set) var isCapturing = false
    private var stream: SCStream?

    private let ipcClient: IPCClient
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

    init(ipcClient: IPCClient) {
        self.ipcClient = ipcClient
        super.init()
    }

    // MARK: - Public API

    /// Запускает захват системного аудио. Асинхронный — SCStream конфигурируется через async API.
    func start() {
        guard !isCapturing else { return }
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "capture.start_called")
        Task { @MainActor in
            await startCapture()
        }
    }

    /// Останавливает захват и отправляет flush на бэкенд.
    func stop() {
        guard isCapturing else { return }
        SentryConfig.recordBreadcrumb(category: "live_subs", message: "capture.stop_called")
        isCapturing = false
        flushTimer?.invalidate()
        flushTimer = nil

        // flush остатка буфера
        if !ringBuffer.isEmpty {
            sendChunk(isFinal: true)
        }
        let capturedStream = stream
        stream = nil
        Task {
            try? await capturedStream?.stopCapture()
        }
        logger.info("SystemAudioCapture: остановлен")

        let client = ipcClient
        DispatchQueue.global(qos: .utility).async {
            _ = try? client.call(method: "live_subs_stop", params: [:])
        }
    }

    // MARK: - Private: start pipeline

    private func startCapture() async {
        do {
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
                return
            }
            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

            let newStream = SCStream(filter: filter, configuration: config, delegate: nil)
            try newStream.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "krabear.sysaudio", qos: .userInteractive))
            SentryConfig.recordBreadcrumb(category: "live_subs", message: "stream.initialized")

            try await newStream.startCapture()
            stream = newStream
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
                    if !self.ringBuffer.isEmpty {
                        self.sendChunk(isFinal: false)
                    }
                }
            }
        } catch {
            logger.error("SystemAudioCapture: ошибка запуска — \(error.localizedDescription)")
            SentryConfig.recordBreadcrumb(
                category: "live_subs",
                message: "stream.error",
                data: ["error": error.localizedDescription]
            )
        }
    }

    // MARK: - Private: send IPC chunk

    private func sendChunk(isFinal: Bool) {
        guard !ringBuffer.isEmpty else { return }
        let samples = ringBuffer
        ringBuffer = []

        let data = samples.withUnsafeBytes { Data($0) }
        let base64 = data.base64EncodedString()
        let sr = nativeSampleRate
        let lang = targetLang
        let finalFlag = isFinal
        let client = ipcClient

        DispatchQueue.global(qos: .utility).async {
            _ = try? client.call(
                method: "live_subs_ingest",
                params: [
                    "audio_chunk": base64,
                    "sample_rate": sr,
                    "target_lang": lang,
                    "is_final": finalFlag,
                ]
            )
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
        guard let result = extractSamples(from: sampleBuffer) else { return }
        let samples = result.samples
        let sr = result.sampleRate
        Task { @MainActor in
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
