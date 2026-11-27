/*
 SettingsWriteHonestyTests — регресс по находкам ChatGPT G3-memory-08
 (KEM08-01…08): панель настроек не должна врать о результате записи и не должна
 терять пользовательский ввод при ошибке.

 Две части:
 1) поведенческие тесты чистых хелперов (SettingsInputValidation.swift);
 2) source-контракты на контроллеры (паттерн QuickCaptureWiringTests): UI
    не поднимаем, поэтому проверяем РЕАЛЬНЫЙ source на возврат старых дыр.
*/

import XCTest
@testable import KrabEarAgent

final class SettingsWriteHonestyTests: XCTestCase {
    // MARK: - KEM08-08: max_tokens

    func test_maxTokens_emptyUsesDefault() {
        XCTAssertEqual(SummaryProfileInput.parseMaxTokens(""), .value(300))
        XCTAssertEqual(SummaryProfileInput.parseMaxTokens("   "), .value(300))
    }

    func test_maxTokens_validValueIsKept() {
        XCTAssertEqual(SummaryProfileInput.parseMaxTokens(" 512 "), .value(512))
        XCTAssertEqual(SummaryProfileInput.parseMaxTokens("1"), .value(1))
        XCTAssertEqual(
            SummaryProfileInput.parseMaxTokens(String(SummaryProfileInput.maxTokensUpperBound)),
            .value(SummaryProfileInput.maxTokensUpperBound)
        )
    }

    func test_maxTokens_garbageIsInvalid_notSilently300() {
        for raw in ["abc", "12abc", "3.5", "-5", "0", "99999999", "٣٠٠"] {
            XCTAssertEqual(SummaryProfileInput.parseMaxTokens(raw), .invalid, "'\(raw)' должно быть invalid")
        }
    }

    // MARK: - KEM08-04: подтверждение записи профиля

    func test_acknowledged_requiresResultProfileWithName() {
        XCTAssertTrue(SummaryProfileInput.isCreationAcknowledged(
            ["result": ["profile": ["name": "Мой", "builtin": false]]]
        ))
    }

    func test_notAcknowledged_forResultWithoutProfile() {
        XCTAssertFalse(SummaryProfileInput.isCreationAcknowledged(["result": ["ok": false]]))
        XCTAssertFalse(SummaryProfileInput.isCreationAcknowledged([:]))
        XCTAssertFalse(SummaryProfileInput.isCreationAcknowledged(["result": [:] as [String: Any]]))
        XCTAssertFalse(SummaryProfileInput.isCreationAcknowledged(["result": ["profile": "x"]]))
    }

    func test_notAcknowledged_forEmptyNameOrErrorKey() {
        XCTAssertFalse(SummaryProfileInput.isCreationAcknowledged(["result": ["profile": ["name": "  "]]]))
        XCTAssertFalse(SummaryProfileInput.isCreationAcknowledged(
            ["error": ["message": "x"], "result": ["profile": ["name": "A"]]]
        ))
    }

    // MARK: - KEM08-02/03: не стирать новый черновик

    func test_shouldClear_onlyWhenFieldUnchangedSinceSubmit() {
        XCTAssertTrue(DraftFieldClearing.shouldClear(current: "A", submitted: "A"))
        XCTAssertFalse(DraftFieldClearing.shouldClear(current: "B", submitted: "A"))
        XCTAssertFalse(DraftFieldClearing.shouldClear(current: "", submitted: "A"))
    }

    // MARK: - Source-контракты контроллеров

    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // Tests/
            .deletingLastPathComponent()   // корень пакета
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// Тело функции от `marker` до следующего `\n    @objc`/`\n    private func`/`\n    func`.
    private func body(of marker: String, in src: String) throws -> String {
        let start = try XCTUnwrap(src.range(of: marker), "нет \(marker)")
        let rest = src[start.lowerBound...]
        let stops = ["\n    @objc ", "\n    private func ", "\n    func ", "\n    // MARK:"]
        var end = rest.endIndex
        for stop in stops {
            if let r = rest.range(of: stop, range: rest.index(after: rest.startIndex)..<rest.endIndex),
               r.lowerBound < end { end = r.lowerBound }
        }
        return String(rest[..<end])
    }

    func test_quickCaptureToggles_doNotSwallowSetSettingsErrors() throws {
        let src = try source("HistoryPanelController+Settings.swift")
        for key in ["quick_capture_send_to_notes", "quick_capture_obsidian_sync",
                    "quick_capture_show_panel", "quick_capture_hotkey"] {
            XCTAssertFalse(
                src.contains("try? ipcClient.call(method: \"set_settings\", params: [\"\(key)\""),
                "KEM08-01: set_settings(\(key)) не должен вызываться через try? (ошибка проглатывается)"
            )
        }
        let persist = try body(of: "private func persistQuickCaptureSetting", in: src)
        XCTAssertTrue(persist.contains("try ipcClient.call(method: \"set_settings\""))
        XCTAssertTrue(persist.contains("refreshQuickCaptureSectionState(onReadFailure: rollback)"),
                      "при неудаче контролы обязаны вернуться к состоянию backend'а")
        XCTAssertTrue(persist.contains("BackendToast.shared.show"))
    }

    func test_hotkeyMonitorRestartsOnlyAfterConfirmedWrite() throws {
        let src = try source("HistoryPanelController+Settings.swift")
        let fn = try body(of: "@objc func onQuickCaptureHotkeyChanged", in: src)
        XCTAssertTrue(fn.contains("persistQuickCaptureSetting([\"quick_capture_hotkey\": hotkeyId]"))
        // перезапуск монитора — внутри onSuccess-замыкания, а не безусловно после вызова
        XCTAssertTrue(fn.contains("{ [weak self] in\n            self?.quickCaptureHotkeySelector.tag = idx + 1\n            guard let appDelegate"),
                      "KEM08-05: stop/start монитора должны жить в onSuccess")
        let persist = try body(of: "private func persistQuickCaptureSetting", in: src)
        XCTAssertTrue(persist.contains("if saved {\n                    onSuccess?()"))
    }

    func test_snippetAdd_clearsFieldsOnlyOnSuccess_andDoesNotLogErrorText() throws {
        let src = try source("HistoryPanelController+TextSnippets.swift")
        let fn = try body(of: "private func addSnippetIPC", in: src)
        XCTAssertTrue(fn.contains("guard saved else"), "KEM08-02: при неудаче — ранний выход до очистки полей")
        let guardIdx = try XCTUnwrap(fn.range(of: "guard saved else"))
        let clearIdx = try XCTUnwrap(fn.range(of: "clearSnippetFields("))
        XCTAssertLessThan(guardIdx.lowerBound, clearIdx.lowerBound)
        XCTAssertFalse(src.contains("print(\"[Agent] add_text_snippet failed"),
                       "KEM08-07: сырую ошибку IPC в stdout не выводим")
        XCTAssertFalse(src.contains("print(\"[Agent] remove_text_snippet failed"))
    }

    func test_snippetList_errorDoesNotBecomeEmptyList() throws {
        let src = try source("HistoryPanelController+TextSnippets.swift")
        let fn = try body(of: "func fetchAndRebuildTextSnippetsList", in: src)
        XCTAssertFalse(fn.contains("snippets = []"),
                       "KEM08-06: ошибка чтения не должна превращаться в пустой список")
        XCTAssertTrue(fn.contains("loaded = nil"))
        XCTAssertTrue(fn.contains("guard let snippets = loaded else"))
    }

    func test_summaryProfileCreate_validatesAndConfirmsAndKeepsDraftOnFailure() throws {
        let src = try source("HistoryPanelController+SummaryProfiles.swift")
        let fn = try body(of: "private func createSummaryProfile", in: src)
        XCTAssertFalse(fn.contains("Int(maxTokensText) ?? 300"), "KEM08-08")
        XCTAssertTrue(fn.contains("SummaryProfileInput.parseMaxTokens"))
        XCTAssertTrue(fn.contains("SummaryProfileInput.isCreationAcknowledged(response)"), "KEM08-04")
        XCTAssertFalse(fn.contains("_ = try ipc.call(method: \"add_summary_profile\""),
                       "ответ add_summary_profile должен проверяться, а не отбрасываться")
        // перестройка карточек — только в ветке успеха (до else-ветки с ошибкой)
        let successRebuild = try XCTUnwrap(fn.range(of: "fetchAndRebuildSummaryProfilesCard(isClaudeDesign: false)"))
        let elseBranch = try XCTUnwrap(fn.range(of: "} else {\n                    // KEM08-03"))
        XCTAssertLessThan(successRebuild.lowerBound, elseBranch.lowerBound,
                          "KEM08-03: rebuild не должен идти после ошибки")
        XCTAssertFalse(fn[elseBranch.lowerBound...].contains("fetchAndRebuildSummaryProfilesCard"))
    }

    func test_summaryProfileRebuild_reusesExistingCreateForm() throws {
        let src = try source("HistoryPanelController+SummaryProfiles.swift")
        XCTAssertTrue(src.contains("existingForm ?? makeGeminiCreateForm()"),
                      "KEM08-03: форма не пересоздаётся при перестройке (Gemini)")
        XCTAssertTrue(src.contains("existingForm ?? makeCDCreateForm()"),
                      "KEM08-03: форма не пересоздаётся при перестройке (CD)")
        XCTAssertFalse(src.contains("addArrangedSubview(makeGeminiCreateForm())"))
        XCTAssertFalse(src.contains("addArrangedSubview(makeCDCreateForm())"))
    }
}
