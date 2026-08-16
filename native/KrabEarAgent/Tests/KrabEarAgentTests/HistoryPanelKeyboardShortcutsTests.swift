/*
 HistoryPanelKeyboardShortcutsTests — юнит-тесты pure helper'а
 `keyboardShortcutsHelpText()` из +KeyboardShortcuts.swift.

 Helper форматирует список shortcuts как plain text для NSAlert helper
 + docs + sanity check'ов. Изменения списка shortcuts должны обновлять
 этот test чтобы catch missing additions в help text.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelKeyboardShortcutsTests: XCTestCase {

    func test_helpText_notEmpty() {
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertFalse(s.isEmpty)
    }

    func test_helpText_containsAllTabShortcuts() {
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertTrue(s.contains("⌘1"))
        XCTAssertTrue(s.contains("⌘2"))
        XCTAssertTrue(s.contains("⌘3"))
        XCTAssertTrue(s.contains("⌘4"))
        XCTAssertTrue(s.contains("⌘5"))
        XCTAssertTrue(s.contains("⌘6"))
        XCTAssertTrue(s.contains("⌘7"))
    }

    func test_helpText_containsNavigationShortcuts() {
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertTrue(s.contains("⌘F"), "Поиск shortcut")
        XCTAssertTrue(s.contains("⌘R"), "Обновить shortcut")
    }

    func test_helpText_containsActionShortcuts() {
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertTrue(s.contains("⌘D"), "Диагностика shortcut")
        XCTAssertTrue(s.contains("⌘E"), "Экспорт SRT shortcut")
        XCTAssertTrue(s.contains("⌘M"), "Экспорт Markdown shortcut (handler added in PR #325)")
        XCTAssertTrue(s.contains("⌘I"), "Хранилище shortcut")
    }

    func test_helpText_containsEscape() {
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertTrue(s.contains("Esc"))
        XCTAssertTrue(s.contains("Закрыть"))
    }

    func test_helpText_containsHelpShortcut() {
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertTrue(s.contains("⌘/"))
        XCTAssertTrue(s.contains("справк"), "должен описывать что это help")
    }

    func test_helpText_humanReadableLabels() {
        // Проверяем что есть описательные названия табов, не просто "Tab 1" / "Tab 2".
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertTrue(s.contains("Диктовка"))
        XCTAssertTrue(s.contains("Live перевод"))
        XCTAssertTrue(s.contains("История"))
        XCTAssertTrue(s.contains("AI"))
        XCTAssertTrue(s.contains("Автозвонки"))
        XCTAssertTrue(s.contains("Диагностика"))
        XCTAssertTrue(s.contains("Архив"))
    }

    func test_helpText_validUTF8() {
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        XCTAssertNotNil(s.data(using: .utf8))
    }

    func test_helpText_lineCount() {
        // Sanity check — должно быть достаточно строк для каждого shortcut.
        let s = HistoryPanelController.keyboardShortcutsHelpText()
        let lines = s.components(separatedBy: "\n").filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        XCTAssertGreaterThanOrEqual(lines.count, 10, "Должно быть ≥10 shortcuts")
    }
}
