/*
 DictationPasteTargetTests.swift
 Выбор приложения-получателя для автовставки диктовки (живой инцидент 2026-08-03).

 Что чинится
 -----------
 Лог агента 2026-08-03 01:52:51 → 01:54:08:
     Запомнен target app на старте записи: com.anthropic.claudefordesktop
     Попытка вставки: bundle=com.apple.Safari, pid=805, ok=false, reason=no_editable_focus
 Диктовка длиной 39 с финализировалась 38 с. За это окно владелец переключился в
 Safari — и `resolvePreferredPasteTargetApp()` отдал frontmost, потому что тот
 стоял ПЕРВЫМ в приоритете, а запомненная цель — лишь третьим запасным путём.
 `lastExternalApp` спасти не мог: `handleWorkspaceActivatedApp` перезаписывает
 его на КАЖДУЮ активацию внешнего приложения, то есть к моменту вставки он тоже
 стал Safari. Единственным носителем правильной цели остаётся `recordingTargetApp`.

 Сообщение об ошибке (`no_editable_focus`) описывало ПОСЛЕДНЕЕ звено — в Safari
 действительно не было текстового поля. Корень же на звено выше: выбран не тот
 адресат. Тесты здесь пришпиливают именно выбор адресата.

 Решение владельца (2026-08-03): вставка идёт туда, где диктовка НАЧАЛАСЬ.
 Запомненная цель выигрывает всегда; frontmost — запасной путь на случай, когда
 запомненного приложения уже нет.

 Стратегия тестирования
 ----------------------
 `AgentAppDelegate` не инстанцируется в XCTest без полного NSApplication runloop
 (см. шапку PasteHandlingTests.swift). Поэтому политика выбора вынесена в чистую
 функцию над БУЛЕВЫМИ ФАКТАМИ, свободную от AppKit — тесты гоняют РЕАЛЬНУЮ
 production-логику, а не её копию-реплику. Сопоставление выбора с конкретными
 `NSRunningApplication` остаётся в делегате и накрыто source-контрактом ниже.
*/

import XCTest
@testable import KrabEarAgent

final class DictationPasteTargetTests: XCTestCase {

    // MARK: - Политика выбора (чистая логика)

    /// Регрессия инцидента 2026-08-03: запомненная цель жива, но активным стало
    /// ДРУГОЕ приложение. До фикса выигрывал frontmost — диктовка уходила чужому.
    func test_captured_target_wins_over_changed_frontmost() {
        let choice = DictationPasteTargetPolicy.choose(
            capturedIsAlive: true,
            frontmostIsExternal: true,
            lastExternalIsAlive: true
        )
        XCTAssertEqual(
            choice, .captured,
            "Диктовка обязана прийти туда, где началась, даже если за время " +
            "финализации активным стало другое приложение (инцидент 2026-08-03)."
        )
    }

    /// Запомненное приложение закрыли за время финализации — вставлять некуда,
    /// падаем на текущее активное, иначе текст просто потеряется.
    func test_dead_captured_target_falls_back_to_frontmost() {
        let choice = DictationPasteTargetPolicy.choose(
            capturedIsAlive: false,
            frontmostIsExternal: true,
            lastExternalIsAlive: true
        )
        XCTAssertEqual(choice, .frontmost)
    }

    /// Frontmost — сам Krab Ear (панель истории поверх всего): вставлять в себя
    /// бессмысленно, берём последнее внешнее приложение.
    func test_self_frontmost_falls_back_to_last_external() {
        let choice = DictationPasteTargetPolicy.choose(
            capturedIsAlive: false,
            frontmostIsExternal: false,
            lastExternalIsAlive: true
        )
        XCTAssertEqual(choice, .lastExternal)
    }

    /// Ни одного кандидата — честный отказ, а не вставка вслепую.
    func test_no_candidates_yields_none() {
        let choice = DictationPasteTargetPolicy.choose(
            capturedIsAlive: false,
            frontmostIsExternal: false,
            lastExternalIsAlive: false
        )
        XCTAssertEqual(choice, .none)
    }

    /// Запомненная цель жива, а frontmost — сам агент. Запомненная всё равно
    /// первая: проверяем, что приоритет не завязан на внешность frontmost'а.
    func test_captured_wins_even_when_frontmost_is_self() {
        let choice = DictationPasteTargetPolicy.choose(
            capturedIsAlive: true,
            frontmostIsExternal: false,
            lastExternalIsAlive: false
        )
        XCTAssertEqual(choice, .captured)
    }

    // MARK: - Source-контракт проводки
    //
    // Класс «test-validates-the-hole»: чистая политика может быть безупречной и
    // при этом не вызываться из продакшена (ср. мёртвый setupErrorBus). Тесты
    // ниже грепают РЕАЛЬНЫЙ исходник.

    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// Цель обязана захватываться в момент готовности текста, а не читаться из
    /// `recordingTargetApp` в момент вставки: terminal cleanup обнуляет поле, а
    /// `performAutoPaste` может выполниться СИЛЬНО позже (async `ensureHistoryItem`
    /// при отсутствии history_id, таймаут QuickEdit).
    func test_transcription_result_captures_paste_target() throws {
        let src = try source("main+PasteHandling.swift")
        XCTAssertTrue(
            src.contains("dictationPasteTarget ="),
            "handleTranscriptionResult обязан захватить цель вставки до асинхронных ветвей"
        )
    }

    /// Автовставка диктовки обязана ходить через политику, а не через общий
    /// frontmost-first резолвер.
    func test_autopaste_resolves_through_dictation_policy() throws {
        let src = try source("main+PasteHandling.swift")
        XCTAssertTrue(
            src.contains("resolveDictationPasteTargetApp()"),
            "performAutoPaste обязан выбирать цель через политику диктовки"
        )
    }

    /// Быстрая вставка из истории/панели — ДРУГАЯ семантика: пользователь прямо
    /// сейчас указал, куда вставить. Она обязана остаться на frontmost-first
    /// резолвере. Это тот же урок, что S34: одна общая точка не годится, когда у
    /// вызывающих сторон разные роли.
    func test_snapshot_paste_keeps_frontmost_first_resolver() throws {
        let src = try source("main+PasteHandling.swift")
        guard let snapshotRange = src.range(of: "func pasteSnapshotText") else {
            return XCTFail("pasteSnapshotText не найдена — тест устарел")
        }
        let snapshotBody = String(src[snapshotRange.lowerBound...].prefix(1400))
        XCTAssertTrue(
            snapshotBody.contains("resolvePreferredPasteTargetApp()"),
            "Быстрая вставка обязана целиться в текущее активное приложение"
        )
        XCTAssertFalse(
            snapshotBody.contains("resolveDictationPasteTargetApp()"),
            "Политика диктовки не должна перехватывать явную пользовательскую вставку"
        )
    }
}
