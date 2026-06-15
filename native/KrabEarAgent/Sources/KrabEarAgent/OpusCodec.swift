import Foundation
import AVFoundation
import Opus

/// Обёртка над кодеком Opus.
///
/// Stage 1: Поддерживает только один валидный Opus-фрейм за вызов.
/// Валидные размеры фреймов: 2.5, 5, 10, 20, 40, 60 мс.
/// Для 16kHz (uplink) это 40, 80, 160, 320, 640, 960 сэмплов.
/// Внимание: размер 1280 сэмплов (80мс) для 16kHz является НЕВАЛИДНЫМ.
public final class OpusCodec {
    private let encoder: Opus.Encoder
    private let decoder: Opus.Decoder
    
    /// Инициализирует кодек.
    /// По умолчанию: encoder 16kHz mono (uplink), decoder 24kHz mono (downlink).
    public init() throws {
        guard let encodeFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false) else {
            fatalError("Failed to create encode AVAudioFormat")
        }
        
        guard let decodeFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 24000, channels: 1, interleaved: false) else {
            fatalError("Failed to create decode AVAudioFormat")
        }
        
        self.encoder = try Opus.Encoder(format: encodeFormat, application: .voip)
        self.decoder = try Opus.Decoder(format: decodeFormat, application: .voip)
    }
    
    /// Кодирует один валидный PCM-фрейм (16kHz mono) в пакет Opus.
    public func encode(_ pcm: AVAudioPCMBuffer) throws -> Data {
        // Выделяем буфер с запасом под максимальный размер Opus пакета (обычно до 4000 байт)
        var data = Data(count: 4000)
        let writtenBytes = try encoder.encode(pcm, to: &data)
        return data.prefix(writtenBytes)
    }
    
    /// Декодирует один пакет Opus в PCM-фрейм (24kHz mono).
    public func decode(_ data: Data) throws -> AVAudioPCMBuffer {
        return try decoder.decode(data)
    }
}
