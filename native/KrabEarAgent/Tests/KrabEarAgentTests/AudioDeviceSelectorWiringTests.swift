import XCTest
@testable import KrabEarAgent

/// Пикер микрофона обязан доносить выбор до backend'а (02.09.2026).
///
/// `audioDeviceSelector` стоял в панели, заполнялся из `get_audio_devices` —
/// и на этом всё: ни `target`/`action`, ни чтения `titleOfSelectedItem`. Выбор
/// владельца никуда не уходил, запись всегда шла с устройства по умолчанию.
///
/// Обратная половина при этом давно готова: `RecordingCoreService` перед стартом
/// записи читает `selected_input_device` из настроек и зовёт
/// `AudioRecorder.set_device()` (правка W1327 F2, помеченная как HIGH). То есть
/// защита была написана для входа, которого никто не подавал — классический
/// случай декоративной проводки, где дорогая половина работает, а дешёвая
/// отсутствует.
///
/// Тест закрывает именно дешёвую половину: контрол подключён и пишет ту самую
/// строку ключа, которую читает backend. Ключ сверяется буквально — контракт
/// между Swift и Python держится на сырой строке.
final class AudioDeviceSelectorWiringTests: XCTestCase {

    private func readSourceFile(_ relativePath: String) throws -> String {
        let bundleURL = Bundle(for: AudioDeviceSelectorWiringTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent(relativePath)
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        let repoRoot = fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(
            contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8
        )
    }

    private var sources: String {
        get throws {
            let files = [
                "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift",
                "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Diagnostics.swift",
                "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+ApplyTheme+DictationSections.swift",
            ]
            return try files.map { try readSourceFile($0) }.joined(separator: "\n")
        }
    }

    func test_selector_hasTargetAndAction() throws {
        let src = try sources
        XCTAssertTrue(
            src.contains("audioDeviceSelector.target = self"),
            "без target пикер микрофона остаётся украшением"
        )
        // Внутри замыкания селектор пишется с `self.`, вне — без; проверяем имя,
        // а не форму записи — иначе тест краснеет на стилистике, а не на дыре.
        XCTAssertTrue(
            src.contains("#selector(onAudioDeviceChanged)")
                || src.contains("#selector(self.onAudioDeviceChanged)"),
            "без action выбор устройства никуда не уходит"
        )
    }

    func test_handler_writesTheKeyBackendReads() throws {
        let src = try sources
        XCTAssertTrue(
            src.contains("func onAudioDeviceChanged()"),
            "нужен обработчик смены устройства"
        )
        XCTAssertTrue(
            src.contains("\"selected_input_device\""),
            """
            обработчик обязан писать ключ selected_input_device — именно его \
            читает RecordingCoreService перед стартом записи; любое другое имя \
            означает, что настройка снова никуда не доедет
            """
        )
    }

    func test_deviceListReload_restoresSavedSelection() throws {
        let src = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Diagnostics.swift"
        )
        XCTAssertTrue(
            src.contains("selectedInputDevice"),
            """
            после перезаполнения списка пикер обязан вернуться на сохранённое \
            устройство, иначе он показывает «По умолчанию» при живой настройке \
            и владелец считает, что выбор потерян
            """
        )
    }
}
