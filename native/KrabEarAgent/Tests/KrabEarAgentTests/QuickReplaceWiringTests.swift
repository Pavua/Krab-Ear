/*
 QuickReplaceWiringTests — source-contract тесты S64 re-paste (спека
 2026-07-19-s64-quickreplace-repaste-design.md). Грепают реальный source
 (паттерн QuickCaptureWiringTests) — ловят декоративную проводку.
*/

import XCTest

final class QuickReplaceWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// S64 re-paste: успешная замена обязана класть исправленный ПОЛНЫЙ текст
    /// (new_text из ответа backend) в буфер обмена.
    func test_success_branch_copies_new_text_to_clipboard() throws {
        let src = try source("main+QuickReplace.swift")
        XCTAssertTrue(src.contains("putToClipboard"),
                      "успешная замена обязана копировать new_text в буфер")
        XCTAssertTrue(src.contains("new_text"),
                      "текст для буфера берётся из поля new_text ответа IPC")
    }

    /// Тост обязан подсказывать пользователю про вставку.
    func test_toast_mentions_clipboard_hint() throws {
        let src = try source("main+QuickReplace.swift")
        XCTAssertTrue(src.contains("Скопировано"),
                      "тост обязан сообщать про копирование в буфер")
    }

    /// AGENT-3: sync callWithRecovery на main thread из completion алерта —
    /// AppHang-класс на деградированном пути (рестарт backend внутри recovery).
    /// Обязан быть заменён паттерном Wave 188 (Task.detached + callAsync).
    func test_ipc_call_is_off_main() throws {
        let src = try source("main+QuickReplace.swift")
        XCTAssertTrue(src.contains("Task.detached"),
                      "IPC обязан уходить off-main (паттерн Wave 188)")
        XCTAssertTrue(src.contains("callAsync"))
        // Греп по сигнатуре ВЫЗОВА «callWithRecovery(», не по голой подстроке —
        // объясняющий комментарий в source честно упоминает удалённый sync-путь
        // по имени, и подстрочный греп реддил бы корректную реализацию
        // (урок кодекса: source-inspection тесты матчат вызов, не прозу).
        XCTAssertFalse(src.contains("callWithRecovery("),
                       "sync call site обязан быть удалён из файла целиком (AGENT-3)")
    }
}
