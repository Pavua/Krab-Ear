/*
 HistoryPanelController+KeyboardShortcuts.swift

 Глобальные keyboard shortcuts для главного окна (когда оно key window).
 Setup через NSEvent.addLocalMonitorForEvents — monitor освобождается в
 windowWillClose/deinit (см. HistoryPanelController.swift cleanup на line 468).

 Список shortcuts — синхронизировать с buildKeyboardShortcutsHelpText().
 Любое добавление новой клавиши: 2 места — switch case + help text + (опционально)
 readme в USER_MANUAL.md.

 Splited из HistoryPanelController.swift в этот extension чтобы уменьшить
 main file size (был 2534 строк → ~2440 после split).
*/

import AppKit

extension HistoryPanelController {

    /// Вызывается из applyVisualTheme. Устанавливает global key event monitor
    /// который перехватывает Cmd+1/2/3/4/F/R/D/E/I/?, Esc когда window —
    /// key window. Возвращает `nil` чтобы consume event и не пропустить дальше.
    func setupKeyboardShortcuts() {
        keyboardMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, self.window?.isKeyWindow == true else { return event }

            if event.modifierFlags.contains(.command) {
                switch event.charactersIgnoringModifiers {
                case "1":
                    self.tabSelector.selectedSegment = 0
                    self.mainTabView.selectTabViewItem(at: 0)
                    return nil
                case "2":
                    self.tabSelector.selectedSegment = 1
                    self.mainTabView.selectTabViewItem(at: 1)
                    return nil
                case "3":
                    self.tabSelector.selectedSegment = 2
                    self.mainTabView.selectTabViewItem(at: 2)
                    return nil
                case "4":
                    self.tabSelector.selectedSegment = 3
                    self.mainTabView.selectTabViewItem(at: 3)
                    return nil
                case "5":
                    self.tabSelector.selectedSegment = 4
                    self.mainTabView.selectTabViewItem(at: 4)
                    return nil
                case "6":
                    self.tabSelector.selectedSegment = 5
                    self.mainTabView.selectTabViewItem(at: 5)
                    return nil
                case "7":
                    self.tabSelector.selectedSegment = 6
                    self.mainTabView.selectTabViewItem(at: 6)
                    return nil
                case "f":
                    self.tabSelector.selectedSegment = 2
                    self.mainTabView.selectTabViewItem(at: 2)
                    self.window?.makeFirstResponder(self.searchField)
                    return nil
                case "r":
                    self.loadInitial()
                    return nil
                case "d":
                    self.onDiagnostics()
                    return nil
                case "e":
                    self.onExportSrt()
                    return nil
                case "m":
                    // ⌘M упомянут в keyboardShortcutsHelpText() как "Экспорт Markdown"
                    // но handler был отсутствовал на main — discrepancy fixed в этом PR.
                    self.onExportHistory()
                    return nil
                case "i":
                    self.onStorageInfo()
                    return nil
                case "/", "?":
                    self.showKeyboardShortcutsHelp()
                    return nil
                default:
                    break
                }
            }

            // Escape — закрыть панель
            if event.keyCode == Keycode.escape.rawValue {
                self.window?.orderOut(nil)
                return nil
            }

            return event
        }
    }

    /// Показывает NSAlert с списком всех доступных shortcuts.
    /// Также exposed для testing через `keyboardShortcutsHelpText()` pure helper.
    @MainActor
    func showKeyboardShortcutsHelp() {
        let alert = NSAlert()
        alert.messageText = "Горячие клавиши"
        alert.informativeText = HistoryPanelController.keyboardShortcutsHelpText()
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        presentAlertSheet(alert, for: self.window) { _ in }
    }

    /// Pure helper — список shortcuts как текст для alert / docs / тестов.
    /// `nonisolated static` — тестируется без instance.
    nonisolated static func keyboardShortcutsHelpText() -> String {
        return """
        ⌘1  Диктовка
        ⌘2  Live перевод
        ⌘3  История
        ⌘4  Разговор с AI
        ⌘5  Автозвонки
        ⌘6  Диагностика
        ⌘7  Архив
        ⌘F  Поиск
        ⌘R  Обновить
        ⌘D  Диагностика
        ⌘E  Экспорт SRT
        ⌘M  Экспорт Markdown
        ⌘I  Хранилище
        Esc  Закрыть
        ⌘/  Эта справка
        """
    }
}
