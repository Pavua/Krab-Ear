import XCTest
@testable import KrabEarAgent

final class MuLawDecoderTests: XCTestCase {
    func test_golden_vectors() {
        XCTAssertEqual(MuLawDecoder.table[0x00], -32124)
        XCTAssertEqual(MuLawDecoder.table[0x80], 32124)
        XCTAssertEqual(MuLawDecoder.table[0xFF], 0)
        XCTAssertEqual(MuLawDecoder.table[0x7F], 0)
        XCTAssertEqual(MuLawDecoder.table[0xE0], 372)
        XCTAssertEqual(MuLawDecoder.table[0x60], -372)
    }

    func test_table_is_antisymmetric() {
        for b in 0...127 {
            XCTAssertEqual(MuLawDecoder.table[b], -MuLawDecoder.table[b | 0x80],
                           "byte \(b) vs \(b | 0x80)")
        }
    }

    func test_decode_frame_and_float_range() {
        let frame = Data([0x00, 0xFF, 0x80])
        XCTAssertEqual(MuLawDecoder.decode(frame), [-32124, 0, 32124])
        let floats = MuLawDecoder.decodeToFloat(frame)
        XCTAssertEqual(floats.count, 3)
        XCTAssertEqual(floats[1], 0.0)
        XCTAssertTrue(floats.allSatisfy { $0 >= -1.0 && $0 <= 1.0 })
    }
}
