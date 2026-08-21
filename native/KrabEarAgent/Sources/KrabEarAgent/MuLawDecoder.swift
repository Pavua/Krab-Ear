import Foundation

/// G.711 μ-law → PCM16 (монитор-аудио VG: mulaw_8k, кадры 100мс = 800 байт).
/// Таблица считается один раз при первом обращении.
enum MuLawDecoder {
    static let table: [Int16] = (0...255).map { byte in
        let u = ~UInt8(byte)
        let isNegative = (u & 0x80) != 0
        let exponent = Int((u >> 4) & 0x07)
        let mantissa = Int(u & 0x0F)
        let magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
        return Int16(clamping: isNegative ? -magnitude : magnitude)
    }

    static func decode(_ data: Data) -> [Int16] {
        data.map { table[Int($0)] }
    }

    static func decodeToFloat(_ data: Data) -> [Float] {
        data.map { Float(table[Int($0)]) / 32768.0 }
    }
}
