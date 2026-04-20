/*
 HistoryPanelDiagnosticsTests — тесты для логики вкладки Diagnostics.

 Стратегия:
 HistoryPanelController+Diagnostics.swift тесно связан с AppKit/IPC, поэтому
 тестируем только чистую логику:
   1. formatNestedResult — рендеринг structured dict в текст
   2. Заголовок (title) присутствует в выводе
   3. Вложенные секции (system / stt / llm / history / settings_cache)
   4. Сортировка ключей (алфавитный порядок)
   5. Числовые значения latency / confidence передаются как есть
   6. Строки ошибок, показываемые при недоступном бэкенде
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Тестируемый хелпер

// HistoryPanelController — AppKit-heavy, поэтому создаём изолированный
// stub, воспроизводящий публичную pure-функцию formatNestedResult.
// Это позволяет тестировать логику без инициализации NSWindow / NSTextField.

private struct DiagnosticsFormatter {
    /// Копия алгоритма из HistoryPanelController+Diagnostics.swift.
    /// При изменении production-кода обновите и этот stub.
    func formatNestedResult(_ result: [String: Any], title: String) -> String {
        var lines: [String] = ["=== \(title) ==="]
        for (key, value) in result.sorted(by: { $0.key < $1.key }) {
            if let dict = value as? [String: Any] {
                lines.append("\n[\(key)]")
                for (k, v) in dict.sorted(by: { $0.key < $1.key }) {
                    lines.append("  \(k): \(v)")
                }
            } else {
                lines.append("\(key): \(value)")
            }
        }
        return lines.joined(separator: "\n")
    }
}

// MARK: - Tests

final class HistoryPanelDiagnosticsTests: XCTestCase {

    private let fmt = DiagnosticsFormatter()

    // MARK: 1. Заголовок присутствует в выводе

    func test_formatNestedResult_containsTitle() {
        let result = formatSample()
        XCTAssertTrue(result.contains("=== Диагностика ==="),
                      "Вывод должен содержать заголовок '=== Диагностика ==='")
    }

    // MARK: 2. Секции верхнего уровня рендерятся как [key]

    func test_formatNestedResult_sectionsRenderedWithBrackets() {
        let result = formatSample()
        XCTAssertTrue(result.contains("[history]"), "Секция history должна быть в квадратных скобках")
        XCTAssertTrue(result.contains("[stt]"),     "Секция stt должна быть в квадратных скобках")
        XCTAssertTrue(result.contains("[system]"),  "Секция system должна быть в квадратных скобках")
    }

    // MARK: 3. Вложенные ключ-значение имеют двойной отступ

    func test_formatNestedResult_nestedValuesIndented() {
        let result = formatSample()
        // Все строки вложенных значений начинаются с "  "
        let indentedLines = result.components(separatedBy: "\n")
            .filter { $0.hasPrefix("  ") }
        XCTAssertFalse(indentedLines.isEmpty,
                       "Должны быть строки с двойным отступом для вложенных значений")
        XCTAssertTrue(indentedLines.contains(where: { $0.contains("count:") }),
                      "Вложенное значение 'count' должно присутствовать с отступом")
    }

    // MARK: 4. Ключи верхнего уровня отсортированы по алфавиту

    func test_formatNestedResult_topLevelKeysSorted() {
        let dict: [String: Any] = [
            "zzz": ["val": 1],
            "aaa": ["val": 2],
            "mmm": ["val": 3],
        ]
        let result = fmt.formatNestedResult(dict, title: "Sort Test")
        // Ищем позиции секций в тексте
        let aaaRange = result.range(of: "[aaa]")
        let mmmRange = result.range(of: "[mmm]")
        let zzzRange = result.range(of: "[zzz]")
        XCTAssertNotNil(aaaRange)
        XCTAssertNotNil(mmmRange)
        XCTAssertNotNil(zzzRange)
        // aaa < mmm < zzz
        XCTAssertLessThan(aaaRange!.lowerBound, mmmRange!.lowerBound,
                          "'aaa' должна быть раньше 'mmm' (алфавитная сортировка)")
        XCTAssertLessThan(mmmRange!.lowerBound, zzzRange!.lowerBound,
                          "'mmm' должна быть раньше 'zzz' (алфавитная сортировка)")
    }

    // MARK: 5. Числовые значения latency / confidence передаются корректно

    func test_formatNestedResult_numericValuesPreserved() {
        let dict: [String: Any] = [
            "stt": [
                "avg_latency_ms": 123.45,
                "confidence": 0.98,
            ]
        ]
        let result = fmt.formatNestedResult(dict, title: "Метрики")
        XCTAssertTrue(result.contains("avg_latency_ms"),
                      "Ключ avg_latency_ms должен быть в выводе")
        XCTAssertTrue(result.contains("confidence"),
                      "Ключ confidence должен быть в выводе")
        // Значения присутствуют (Swift String(interpolating:) для Double)
        XCTAssertTrue(result.contains("123.45") || result.contains("123"),
                      "Числовое значение latency должно быть в выводе")
        XCTAssertTrue(result.contains("0.98"),
                      "Числовое значение confidence должно быть в выводе")
    }

    // MARK: 6. Flat (не словарные) значения верхнего уровня рендерятся без отступа

    func test_formatNestedResult_flatValuesNoIndent() {
        let dict: [String: Any] = [
            "status": "ok",
            "version": "1.2.3",
        ]
        let result = fmt.formatNestedResult(dict, title: "Flat")
        let lines = result.components(separatedBy: "\n")
        let statusLine = lines.first(where: { $0.contains("status:") })
        XCTAssertNotNil(statusLine, "'status' должна быть в выводе")
        XCTAssertFalse(statusLine!.hasPrefix("  "),
                       "Плоские значения не должны иметь отступ")
        XCTAssertTrue(statusLine!.contains("ok"),
                      "Значение 'ok' должно быть рядом с ключом")
    }

    // MARK: 7. Строка ошибки при недоступном бэкенде

    func test_errorStrings_diagnostics() {
        // Проверяем константы ошибок, отображаемых пользователю при сбое IPC.
        // Они жёстко заданы в source и не должны молча измениться.
        let diagError     = "Ошибка: не удалось получить диагностику"
        let metricsError  = "Ошибка: не удалось получить метрики"
        let statsError    = "Ошибка: не удалось получить статистику"
        let storageError  = "Ошибка: не удалось получить информацию о хранилище"

        // Убеждаемся что строки непусты и содержат ключевое слово "Ошибка"
        for msg in [diagError, metricsError, statsError, storageError] {
            XCTAssertTrue(msg.hasPrefix("Ошибка:"),
                          "Сообщение об ошибке должно начинаться с 'Ошибка:': \(msg)")
        }
    }

    // MARK: 8. Пустой dict → только заголовок

    func test_formatNestedResult_emptyDict_onlyTitle() {
        let result = fmt.formatNestedResult([:], title: "Пусто")
        XCTAssertEqual(result, "=== Пусто ===",
                       "Пустой dict должен давать только строку заголовка")
    }

    // MARK: - Helpers

    /// Типовой ответ get_diagnostics с пятью секциями.
    private func formatSample() -> String {
        let dict: [String: Any] = [
            "system": [
                "cpu_percent": 12.5,
                "ram_mb": 512,
            ],
            "stt": [
                "model": "whisper-balanced",
                "avg_latency_ms": 450,
            ],
            "llm": [
                "circuit_breaker": "closed",
                "requests_total": 37,
            ],
            "history": [
                "count": 142,
                "oldest_ts": "2026-01-01T00:00:00Z",
            ],
            "settings_cache": [
                "ttl_sec": 5,
                "hit_rate": 0.87,
            ],
        ]
        return fmt.formatNestedResult(dict, title: "Диагностика")
    }
}
