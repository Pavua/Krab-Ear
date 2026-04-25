/*
 ConfidenceIndicator.swift
 Вспомогательная функция для отображения уверенности STT в истории транскрибаций.

 Логика выбора цвета инкапсулирована отдельно от UIViewController,
 чтобы её можно было тестировать без создания NSWindow.

 Использует только KrabEarTheme.Colors semantic tokens —
 ни одного литерального hex/rgb цвета.
*/

import AppKit

/// Возвращает semantic color из KrabEarTheme.Colors для заданного уровня уверенности STT.
///
/// Пороги:
/// - `>= 0.85` → `Colors.success` (systemGreen)  — высокая уверенность
/// - `>= 0.65` → `Colors.warning` (systemOrange) — средняя уверенность
/// - `<  0.65` → `Colors.error`   (systemRed)    — низкая уверенность
/// - `nil`     → `NSColor.clear`                 — индикатор скрыт (импорт без метаданных)
///
/// Новых цветовых литералов нет — только существующие KrabEarTheme tokens.
@MainActor
func confidenceColor(for confidence: Double?) -> NSColor {
    guard let confidence else { return .clear }
    if confidence >= 0.85 {
        return KrabEarTheme.Colors.success
    } else if confidence >= 0.65 {
        return KrabEarTheme.Colors.warning
    } else {
        return KrabEarTheme.Colors.error
    }
}
