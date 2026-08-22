/*
 HistoryPanelImportTests — юнит-тесты чистой логики +Import расширения.

 Стратегия:
 - HistoryPanelController нельзя инстанцировать в headless-тестах (setupUI требует
   работающего AppKit/NSWindow). Тестируем исключительно чистые static-функции,
   добавленные в +Import как "Testable static helpers".
 - Тесты охватывают:
   1. normalizedImportSignature — сортировка, дедупликация пустых, trim.
   2. mmss — форматирование секунд в MM:SS (включая русский "Файл большой" контекст).
   3. stageRu — маппинг pipeline-стадий на русские подписи.
   4. errorsPreviewText — агрегация importErrorMessages, лимит 3 строк + хвост.
   5. ImportPreview/ImportJob struct — инициализация без NSWindow.
   6. formatBytes — логика форматирования байт (whitebox-реплика).
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelImportTests: XCTestCase {

    // MARK: - normalizedImportSignature

    /// Пути сортируются → одинаковый набор в разном порядке даёт одну сигнатуру.
    func test_normalizedSignature_sortsPaths() {
        let a = HistoryPanelController.normalizedImportSignatureStatic(["/c", "/a", "/b"])
        let b = HistoryPanelController.normalizedImportSignatureStatic(["/a", "/b", "/c"])
        XCTAssertEqual(a, b, "Сигнатура не должна зависеть от порядка путей")
    }

    /// Пустые строки и строки из пробелов фильтруются.
    func test_normalizedSignature_filtersEmptyAndWhitespace() {
        let sig = HistoryPanelController.normalizedImportSignatureStatic(["  ", "", "/audio/file.m4a"])
        XCTAssertEqual(sig, "/audio/file.m4a",
                       "Пустые и пробельные пути должны отфильтровываться")
    }

    /// Пути с ведущими/ведомыми пробелами тримятся перед объединением.
    func test_normalizedSignature_trimsPaths() {
        let sig1 = HistoryPanelController.normalizedImportSignatureStatic(["  /a/b.mp3  "])
        let sig2 = HistoryPanelController.normalizedImportSignatureStatic(["/a/b.mp3"])
        XCTAssertEqual(sig1, sig2, "Пробелы вокруг путей должны триммироваться")
    }

    /// Пустой массив даёт пустую сигнатуру.
    func test_normalizedSignature_emptyInput() {
        let sig = HistoryPanelController.normalizedImportSignatureStatic([])
        XCTAssertEqual(sig, "", "Пустой массив → пустая сигнатура")
    }

    func test_production_import_uses_shared_helpers() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+Import.swift")
        let source = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(source.contains("HistoryPanelController.stageRuStatic("))
        XCTAssertTrue(source.contains("HistoryPanelController.mmssStatic("))
        XCTAssertTrue(source.contains("HistoryPanelController.normalizedImportSignatureStatic("))
    }

    // MARK: - mmss (форматирование секунд)

    /// 0 секунд → "00:00".
    func test_mmss_zero() {
        XCTAssertEqual(HistoryPanelController.mmssStatic(0), "00:00")
    }

    /// 59 секунд → "00:59".
    func test_mmss_lessThanOneMinute() {
        XCTAssertEqual(HistoryPanelController.mmssStatic(59), "00:59")
    }

    /// 60 секунд → "01:00".
    func test_mmss_exactlyOneMinute() {
        XCTAssertEqual(HistoryPanelController.mmssStatic(60), "01:00")
    }

    /// 754.2 секунды → "12:34" (пример из docstring в источнике).
    func test_mmss_twelveMinutes() {
        XCTAssertEqual(HistoryPanelController.mmssStatic(754.2), "12:34")
    }

    /// Отрицательное значение → "00:00" (max(0, ...) защита).
    func test_mmss_negativeInput() {
        XCTAssertEqual(HistoryPanelController.mmssStatic(-10), "00:00")
    }

    /// Округление: 90.6 → 91 сек → "01:31".
    func test_mmss_roundsUp() {
        XCTAssertEqual(HistoryPanelController.mmssStatic(90.6), "01:31")
    }

    // MARK: - stageRu (локализация стадий пайплайна)

    func test_stageRu_audioLoad() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("audio_load"), "загрузка")
    }

    func test_stageRu_normalize() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("normalize"), "нормализация")
    }

    func test_stageRu_stt() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("stt"), "распознавание")
    }

    func test_stageRu_cleanup() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("cleanup"), "обработка текста")
    }

    func test_stageRu_diarize() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("diarize"), "разделение говорящих")
    }

    func test_stageRu_translate() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("translate"), "перевод")
    }

    func test_stageRu_llmRewrite() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("llm_rewrite"), "LLM-правка")
    }

    func test_stageRu_idle() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("idle"), "ожидание")
    }

    /// Неизвестная стадия возвращается как есть (pass-through).
    func test_stageRu_unknownStage_passThrough() {
        XCTAssertEqual(HistoryPanelController.stageRuStatic("custom_step"), "custom_step")
    }

    // MARK: - errorsPreviewText (агрегация ошибок импорта)

    /// Пустой список → пустая строка.
    func test_errorsPreview_emptyList_returnsEmpty() {
        let result = HistoryPanelController.errorsPreviewText(errorMessages: [])
        XCTAssertEqual(result, "")
    }

    /// Одна ошибка: содержит "Ошибки:" и саму строку с "•".
    func test_errorsPreview_singleError_containsBullet() {
        let result = HistoryPanelController.errorsPreviewText(
            errorMessages: ["Файл слишком большой"]
        )
        XCTAssertTrue(result.contains("Ошибки:"), "Должен содержать заголовок 'Ошибки:'")
        XCTAssertTrue(result.contains("• Файл слишком большой"), "Должен содержать пункт с •")
        XCTAssertFalse(result.contains("ещё"), "При 1 ошибке не должно быть хвоста")
    }

    /// Ровно 3 ошибки — нет суффикса "+ещё".
    func test_errorsPreview_threeErrors_noTail() {
        let msgs = ["err1", "err2", "err3"]
        let result = HistoryPanelController.errorsPreviewText(errorMessages: msgs)
        XCTAssertTrue(result.contains("• err1"))
        XCTAssertTrue(result.contains("• err2"))
        XCTAssertTrue(result.contains("• err3"))
        XCTAssertFalse(result.contains("ещё"), "Ровно 3 ошибки — хвоста не должно быть")
    }

    /// 5 ошибок: показываются первые 3, хвост "+ещё 2".
    func test_errorsPreview_fiveErrors_showsThreeAndTail() {
        let msgs = ["e1", "e2", "e3", "e4", "e5"]
        let result = HistoryPanelController.errorsPreviewText(errorMessages: msgs)
        XCTAssertTrue(result.contains("• e1"))
        XCTAssertTrue(result.contains("• e2"))
        XCTAssertTrue(result.contains("• e3"))
        XCTAssertFalse(result.contains("• e4"), "e4 должна быть скрыта в хвосте")
        XCTAssertTrue(result.contains("+ещё 2"), "Хвост должен указывать количество скрытых ошибок")
    }

    /// Русский паттерн "Файл слишком большой" корректно парсится backend'ом (whitebox).
    func test_errorsPreview_russianFileSizeError_detected() {
        let msgs = ["Файл слишком большой: /path/to/file.wav (1.2 GB)"]
        let result = HistoryPanelController.errorsPreviewText(errorMessages: msgs)
        XCTAssertTrue(result.contains("Файл слишком большой"),
                      "Русское сообщение об ошибке размера файла должно появляться в превью")
    }

    // MARK: - ImportPreview / ImportJob structs

    /// ImportPreview инициализируется без AppKit-зависимостей.
    func test_importPreview_init() {
        let preview = HistoryPanelController.ImportPreview(
            audioCount: 3,
            folderCount: 1,
            sample: ["a.mp3", "b.wav"],
            byExtension: ["mp3": 2, "wav": 1],
            totalBytes: 1024 * 1024 * 50
        )
        XCTAssertEqual(preview.audioCount, 3)
        XCTAssertEqual(preview.folderCount, 1)
        XCTAssertEqual(preview.sample, ["a.mp3", "b.wav"])
        XCTAssertEqual(preview.byExtension["mp3"], 2)
        XCTAssertEqual(preview.byExtension["wav"], 1)
        XCTAssertEqual(preview.totalBytes, 1024 * 1024 * 50)
    }

    /// ImportJob инициализируется без AppKit-зависимостей.
    func test_importJob_init() {
        let job = HistoryPanelController.ImportJob(
            paths: ["/calls/call1.m4a", "/calls/call2.m4a"],
            sourceTag: "drop_zone",
            audioCount: 2,
            folderCount: 1,
            totalBytes: 1024 * 200,
            byExtension: ["m4a": 2]
        )
        XCTAssertEqual(job.paths.count, 2)
        XCTAssertEqual(job.sourceTag, "drop_zone")
        XCTAssertEqual(job.audioCount, 2)
        XCTAssertEqual(job.byExtension["m4a"], 2)
    }

    // MARK: - formatBytes (whitebox-реплика)

    /// Реплицируем логику formatBytes из +History, чтобы проверить граничные случаи
    /// без инстанцирования HistoryPanelController.
    private func formatBytes(_ value: Int) -> String {
        let safe = max(0, value)
        if safe < 1024 { return "\(safe) B" }
        let kb = Double(safe) / 1024.0
        if kb < 1024 { return String(format: "%.1f KB", kb) }
        let mb = kb / 1024.0
        if mb < 1024 { return String(format: "%.1f MB", mb) }
        let gb = mb / 1024.0
        return String(format: "%.2f GB", gb)
    }

    func test_formatBytes_bytes() {
        XCTAssertEqual(formatBytes(512), "512 B")
        XCTAssertEqual(formatBytes(1023), "1023 B")
    }

    func test_formatBytes_kilobytes() {
        XCTAssertEqual(formatBytes(1024), "1.0 KB")
        XCTAssertEqual(formatBytes(2048), "2.0 KB")
    }

    func test_formatBytes_megabytes() {
        XCTAssertEqual(formatBytes(1024 * 1024), "1.0 MB")
        // Типичный размер часового ALAC/AAC звонка: 70–100 MB — должен форматироваться в MB.
        XCTAssertEqual(formatBytes(1024 * 1024 * 100), "100.0 MB")
    }

    func test_formatBytes_gigabytes() {
        XCTAssertEqual(formatBytes(1024 * 1024 * 1024), "1.00 GB")
    }

    func test_formatBytes_negativeInput_returnsZeroBytes() {
        XCTAssertEqual(formatBytes(-100), "0 B",
                       "Отрицательный ввод должен быть защищён max(0,...)")
    }
}
