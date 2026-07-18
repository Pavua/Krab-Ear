/*
 QuickCaptureWiringTests — C3a source-contract тесты (anti test-validates-the-hole,
 паттерн MeetingPanelWiringTests). Грепают РЕАЛЬНЫЙ source, а не поведение
 изолированных юнитов — ловят декоративную проводку (гард определён, но реально
 не подключён) и случайное подключение заметки к paste-пайплайну (спека
 2026-07-16-c3-quick-capture-design.md §2a).
*/

import XCTest

final class QuickCaptureWiringTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        // резолв пути как в MeetingPanelWiringTests (walk-up от #file до Sources/)
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // → Tests/KrabEarAgentTests/
            .deletingLastPathComponent()   // → Tests/
            .deletingLastPathComponent()   // → корень пакета KrabEarAgent/
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_streamingPaste_guarded_by_quickCapture() throws {
        let src = try source("main+RealtimeOverlay.swift")
        // recordingDidStart обязан быть за гардом quickCaptureActive
        XCTAssertTrue(src.contains("if !quickCaptureActive"),
                      "streaming-paste должен подавляться в режиме заметки")
    }

    func test_quickCapture_never_calls_paste_pipeline() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertFalse(src.contains("handleTranscriptionResult"),
                       "заметка не должна входить в paste-пайплайн")
        XCTAssertFalse(src.contains("pasteToFrontmostApp"))
    }

    func test_dictation_guarded_against_quickCapture() throws {
        let src = try source("main+HotkeyRecording.swift")
        XCTAssertTrue(src.contains("quickCaptureActive"),
                      "Right Option обязан отвергаться при активной заметке")
    }

    func test_meeting_guarded_against_quickCapture() throws {
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("quickCaptureActive"))
    }

    func test_quickCapture_uses_overlay_polling_hooks() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertTrue(src.contains("startRealtimeOverlayPolling()"),
                      "wake-word пауза/оверлей живут в этом хуке — обязателен")
        XCTAssertTrue(src.contains("stopRealtimeOverlayPolling()"))
        XCTAssertTrue(src.contains("set_paste_status"))
        XCTAssertTrue(src.contains("add_to_collection"))
    }

    // MARK: - Task 2: хоткей + пункт меню + подменю заметок

    func test_hotkey_monitor_is_stored_and_stoppable() throws {
        let src = try source("main+QuickCapture.swift")
        // урок main+QuickPresets.swift: монитор ОБЯЗАН сохраняться
        XCTAssertTrue(src.contains("quickCaptureHotkeyMonitor"))
        XCTAssertTrue(src.contains("removeMonitor"))
    }

    func test_status_menu_has_quick_capture_items() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("onQuickCaptureToggle"))
        XCTAssertTrue(src.contains("Быстрые заметки"))
    }

    func test_menu_open_refreshes_quick_notes() throws {
        let src = try source("main+MenuBarRecap.swift")
        XCTAssertTrue(src.contains("refreshQuickNotesSubmenu"),
                      "menuWillOpen обязан обновлять подменю заметок")
    }

    // MARK: - Task 3: настройки + отправка Notes/Obsidian

    func test_sendQuickCaptureCopies_calls_create_apple_note_and_obsidian() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertTrue(src.contains("create_apple_note"),
                      "sendQuickCaptureCopies обязан звать create_apple_note при включённом чекбоксе")
        XCTAssertTrue(src.contains("run_obsidian_sync"),
                      "sendQuickCaptureCopies обязан звать run_obsidian_sync при включённом чекбоксе")
        XCTAssertTrue(src.contains("quick_capture_send_to_notes"))
        XCTAssertTrue(src.contains("quick_capture_obsidian_sync"))
    }

    func test_sendQuickCaptureCopies_reads_settings_live_not_cache() throws {
        let src = try source("main+QuickCapture.swift")
        // "живое чтение" — get_settings внутри самой функции, а не self.settings (кэш).
        guard let range = src.range(of: "func sendQuickCaptureCopies") else {
            XCTFail("sendQuickCaptureCopies not found")
            return
        }
        let body = src[range.lowerBound...]
        XCTAssertTrue(body.contains("get_settings"),
                      "sendQuickCaptureCopies обязан читать настройки живьём через get_settings")
    }

    func test_hotkey_monitor_reads_hotkey_combo_from_settings() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertTrue(src.contains("quick_capture_hotkey"))
        XCTAssertTrue(src.contains("cmd_opt_n"))
        XCTAssertTrue(src.contains("ctrl_shift_n"))
        XCTAssertTrue(src.contains("quickCaptureHotkeyCombo"))
    }

    func test_quickCaptureSection_exists_and_wired() throws {
        let src = try source("HistoryPanelController+Settings.swift")
        XCTAssertTrue(src.contains("func buildQuickCaptureSection"))
        XCTAssertTrue(src.contains("quick_capture_send_to_notes"))
        XCTAssertTrue(src.contains("quick_capture_obsidian_sync"))
        XCTAssertTrue(src.contains("quick_capture_hotkey"))
    }

    func test_quickCaptureSection_is_wired_into_settings_stack() throws {
        let src = try source("HistoryPanelController.swift")
        XCTAssertTrue(src.contains("buildQuickCaptureSection()"),
                      "buildQuickCaptureSection обязана вызываться и добавляться в settingsBar")
    }

    func test_hotkey_dropdown_change_rearms_monitor() throws {
        let src = try source("HistoryPanelController+Settings.swift")
        guard let range = src.range(of: "func onQuickCaptureHotkeyChanged") else {
            XCTFail("onQuickCaptureHotkeyChanged not found")
            return
        }
        let body = src[range.lowerBound...]
        XCTAssertTrue(body.contains("stopQuickCaptureHotkeyMonitor"))
        XCTAssertTrue(body.contains("startQuickCaptureHotkeyMonitor"))
    }

    // MARK: - C3b Task 2: панель-скретчпад — SSE + точки входа + настройка

    func test_quickCapturePanel_subscribes_to_realtime_sse() throws {
        let src = try source("QuickCapturePanelController.swift")
        XCTAssertTrue(src.contains("/v1/events?filter="),
                      "панель обязана подписываться на REST SSE-эндпоинт")
        XCTAssertTrue(src.contains("realtime.partial_transcript"))
        XCTAssertTrue(src.contains("realtime.final_transcript"))
        XCTAssertTrue(src.contains("func windowWillClose"),
                      "windowWillClose обязан существовать (уже есть с Task 1)")
    }

    func test_quickCapturePanel_show_starts_sse() throws {
        let src = try source("QuickCapturePanelController.swift")
        guard let range = src.range(of: "func show()") else {
            XCTFail("show() not found")
            return
        }
        let body = src[range.lowerBound...]
        XCTAssertTrue(body.contains("startSSE()"), "show() обязан запускать SSE-подписку")
    }

    func test_quickCapturePanel_windowWillClose_stops_sse() throws {
        let src = try source("QuickCapturePanelController.swift")
        guard let range = src.range(of: "func windowWillClose") else {
            XCTFail("windowWillClose not found")
            return
        }
        let body = src[range.lowerBound...]
        XCTAssertTrue(body.contains("stopSSE()"),
                      "закрытие панели обязано останавливать SSE-подписку")
    }

    func test_quickCapture_shows_panel_guarded_by_setting() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertTrue(src.contains("quick_capture_show_panel"),
                      "показ панели обязан читать quick_capture_show_panel живьём")
        XCTAssertTrue(src.contains("ensureQuickCapturePanelController"))
        XCTAssertTrue(src.contains("QuickCapturePanelController"))
    }

    func test_quickCapture_toggle_shows_panel_only_on_real_start() throws {
        let src = try source("main+QuickCapture.swift")
        guard let range = src.range(of: "func onQuickCaptureToggle") else {
            XCTFail("onQuickCaptureToggle not found")
            return
        }
        let body = src[range.lowerBound...]
        XCTAssertTrue(body.contains("showQuickCapturePanelIfEnabled()"),
                      "успешный старт заметки обязан пробовать показать панель")
    }

    /// C3b ревью F1 (sibling-gate asymmetry): старая версия этого теста ПИНИЛА
    /// `isVisible`-гейт на setRecording(false) как обязательное поведение —
    /// это и был живой баг (test-validates-the-hole). Реально: запись физически
    /// останавливается независимо от того, видима ли панель СЕЙЧАС — если
    /// закрыть панель мид-записи, isRecording обязан сброситься на стопе, иначе
    /// он застревает true навсегда (guard в setRecording молча блокирует
    /// следующий setRecording(true) как «уже применённое» состояние).
    func test_handleQuickCaptureResult_updates_panel_state_unconditionally() throws {
        let src = try source("main+QuickCapture.swift")
        guard let range = src.range(of: "func handleQuickCaptureResult") else {
            XCTFail("handleQuickCaptureResult not found")
            return
        }
        let body = src[range.lowerBound...]
        XCTAssertTrue(body.contains("quickCapturePanelController?.setRecording(false)"),
                      "остановка заметки обязана БЕЗУСЛОВНО (не только когда видима) снимать recording-состояние панели")
        XCTAssertTrue(body.contains("refreshQuickCapturePanelNotes"),
                      "успешное сохранение обязано обновлять список заметок панели")
    }

    /// C3b ревью F1 (сценарий 1): панель, открытая вручную из меню при
    /// ВЫКЛЮЧЕННОЙ (дефолт) настройке автопоказа, обязана узнать о реальном
    /// старте записи из своей же кнопки «Начать запись» — иначе показывает
    /// «Запись не идёт» всю запись. Гейт на quick_capture_show_panel обязан
    /// решать только «показывать ли панель САМОСТОЯТЕЛЬНО», не «синхронизировать
    /// ли состояние уже существующей».
    func test_showQuickCapturePanelIfEnabled_syncs_state_before_settings_gate() throws {
        let src = try source("main+QuickCapture.swift")
        guard let range = src.range(of: "func showQuickCapturePanelIfEnabled") else {
            XCTFail("showQuickCapturePanelIfEnabled not found")
            return
        }
        let body = src[range.lowerBound...]
        guard let guardRange = body.range(of: "guard let resp") else {
            XCTFail("settings-guard not found in showQuickCapturePanelIfEnabled")
            return
        }
        let beforeGuard = body[body.startIndex..<guardRange.lowerBound]
        XCTAssertTrue(beforeGuard.contains("quickCapturePanelController?.setRecording(true)"),
                      "уже существующая панель обязана синхронизироваться ДО гейта на настройку автопоказа")
    }

    func test_status_menu_has_open_scratchpad_item() throws {
        let src = try source("main+StatusMenu.swift")
        XCTAssertTrue(src.contains("Открыть скретчпад"))
        XCTAssertTrue(src.contains("onOpenQuickCapturePanel"))
    }

    func test_main_has_quickCapturePanelController_property() throws {
        let src = try source("main.swift")
        XCTAssertTrue(src.contains("var quickCapturePanelController: QuickCapturePanelController?"))
    }

    func test_quickCaptureSection_has_show_panel_checkbox() throws {
        let src = try source("HistoryPanelController+Settings.swift")
        XCTAssertTrue(src.contains("quick_capture_show_panel"))
        XCTAssertTrue(src.contains("quickCaptureShowPanelButton"))
        XCTAssertTrue(src.contains("onQuickCaptureShowPanelChanged"))
    }

    func test_quickCaptureSection_hydrates_show_panel_checkbox() throws {
        let src = try source("HistoryPanelController+Settings.swift")
        guard let range = src.range(of: "func refreshQuickCaptureSectionState") else {
            XCTFail("refreshQuickCaptureSectionState not found")
            return
        }
        let body = src[range.lowerBound...]
        XCTAssertTrue(body.contains("quickCaptureShowPanelButton.state"),
                      "гидратация обязана читать quick_capture_show_panel в чекбокс")
    }
}
