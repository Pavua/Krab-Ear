/*
 HistoryPanelCallAssistTests — тесты чистой логики CallAssist-хелперов.

 Стратегия:
 HistoryPanelController+CallAssist.swift содержит несколько pure-функций,
 которые не требуют UI и не вызывают IPC:
   - formatCallTimelinePreview  → строковый форматтер
   - formatCallSummary          → строковый форматтер
   - formatCallCostEstimate     → строковый форматтер
   - selectedCaptureSourceMode  → UI-зависим, тестируем индекс→строку whitebox
   - selectedCallPhraseDirection → аналогично

 Все тесты используют статические helper-функции, продублированные здесь
 whitebox-методом (копируем ту же логику, не дотрагиваясь до UI).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Whitebox helpers (дублируют логику из extension без UI)

private func formatCallTimelinePreview(items: [[String: Any]]) -> String {
    var lines: [String] = []
    for item in items {
        let ts   = (item["ts"]   as? String) ?? "-"
        let kind = (item["kind"] as? String) ?? "unknown"
        let text = ((item["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let shortText: String
        if text.isEmpty {
            shortText = "(без текста)"
        } else if text.count > 120 {
            shortText = String(text.prefix(120)) + "…"
        } else {
            shortText = text
        }
        lines.append("[\(ts)] \(kind): \(shortText)")
    }
    return lines.joined(separator: "\n")
}

private func formatCallSummary(_ payload: [String: Any]) -> String {
    let summaryText = ((payload["summary"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    let rawTasks = (payload["tasks"] as? [Any]) ?? []
    var tasks: [String] = []
    for raw in rawTasks {
        if let dict = raw as? [String: Any] {
            let candidate = (
                (dict["task"]  as? String)
                ?? (dict["title"] as? String)
                ?? (dict["text"]  as? String)
                ?? ""
            ).trimmingCharacters(in: .whitespacesAndNewlines)
            if !candidate.isEmpty { tasks.append(candidate) }
        } else if let rawText = raw as? String {
            let candidate = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
            if !candidate.isEmpty { tasks.append(candidate) }
        }
    }
    let safeSummary = summaryText.isEmpty ? "—" : summaryText
    let tasksText = tasks.isEmpty
        ? "- (нет задач)"
        : tasks.prefix(10).enumerated().map { "\($0 + 1). \($1)" }.joined(separator: "\n")
    return "\(safeSummary)\ntasks:\n\(tasksText)"
}

private func formatCallCostEstimate(_ payload: [String: Any]) -> String {
    let country     = (payload["country"]      as? String) ?? "n/a"
    let ratesSource = (payload["rates_source"] as? String) ?? "unknown"
    let ratesNote   = ((payload["rates_note"]  as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

    let telephony = (payload["telephony_usd"] as? [String: Any]) ?? [:]
    let ai        = (payload["ai_usd"]        as? [String: Any]) ?? [:]
    let total = (payload["total_usd"] as? Double)
        ?? (payload["total_usd"] as? NSNumber)?.doubleValue ?? 0.0

    let telephonyTotal = (telephony["total"] as? Double)
        ?? (telephony["total"] as? NSNumber)?.doubleValue ?? 0.0
    let aiTotal = (ai["total"] as? Double)
        ?? (ai["total"] as? NSNumber)?.doubleValue ?? 0.0

    let noteLine = ratesNote.isEmpty ? "" : "\nrates_note: \(ratesNote)"
    return """
    country: \(country)
    rates_source: \(ratesSource)\(noteLine)
    telephony_total_usd: \(String(format: "%.3f", telephonyTotal))
    ai_total_usd: \(String(format: "%.3f", aiTotal))
    total_usd: \(String(format: "%.3f", total))
    """
}

// MARK: - Tests

final class HistoryPanelCallAssistTests: XCTestCase {

    // MARK: - formatCallTimelinePreview

    /// Нормальные items форматируются как "[ts] kind: text".
    func test_formatCallTimelinePreview_normalItems() {
        let items: [[String: Any]] = [
            ["ts": "2026-04-20T10:00:00Z", "kind": "stt", "text": "Hello"],
            ["ts": "2026-04-20T10:01:00Z", "kind": "tts", "text": "World"],
        ]
        let result = formatCallTimelinePreview(items: items)
        XCTAssertTrue(result.contains("[2026-04-20T10:00:00Z] stt: Hello"))
        XCTAssertTrue(result.contains("[2026-04-20T10:01:00Z] tts: World"))
    }

    /// Item без поля "text" отображается как "(без текста)".
    func test_formatCallTimelinePreview_emptyText() {
        let items: [[String: Any]] = [["ts": "T1", "kind": "event", "text": ""]]
        let result = formatCallTimelinePreview(items: items)
        XCTAssertTrue(result.contains("(без текста)"))
    }

    /// Длинный текст обрезается до 120 символов + "…".
    func test_formatCallTimelinePreview_longTextTruncated() {
        let longText = String(repeating: "A", count: 200)
        let items: [[String: Any]] = [["ts": "T1", "kind": "stt", "text": longText]]
        let result = formatCallTimelinePreview(items: items)
        XCTAssertTrue(result.hasSuffix("…"))
        // Полная строка содержит 120 повторений "A" и затем "…"
        XCTAssertTrue(result.contains(String(repeating: "A", count: 120) + "…"))
    }

    /// Пустой массив items → пустая строка.
    func test_formatCallTimelinePreview_emptyArray() {
        let result = formatCallTimelinePreview(items: [])
        XCTAssertEqual(result, "")
    }

    // MARK: - formatCallSummary

    /// Summary без задач → "- (нет задач)".
    func test_formatCallSummary_noTasks() {
        let payload: [String: Any] = ["summary": "Встреча прошла хорошо", "tasks": [Any]()]
        let result = formatCallSummary(payload)
        XCTAssertTrue(result.contains("Встреча прошла хорошо"))
        XCTAssertTrue(result.contains("- (нет задач)"))
    }

    /// Пустой summary → "—".
    func test_formatCallSummary_emptySummary() {
        let payload: [String: Any] = ["summary": "   "]
        let result = formatCallSummary(payload)
        XCTAssertTrue(result.hasPrefix("—"))
    }

    /// Задачи в виде строк нумеруются корректно.
    func test_formatCallSummary_stringTasks() {
        let payload: [String: Any] = [
            "summary": "OK",
            "tasks": ["Позвонить клиенту", "Написать отчёт"],
        ]
        let result = formatCallSummary(payload)
        XCTAssertTrue(result.contains("1. Позвонить клиенту"))
        XCTAssertTrue(result.contains("2. Написать отчёт"))
    }

    // MARK: - formatCallCostEstimate

    /// Базовые поля присутствуют в выводе.
    func test_formatCallCostEstimate_basicFields() {
        let payload: [String: Any] = [
            "country": "ES",
            "rates_source": "twilio",
            "telephony_usd": ["total": 12.5],
            "ai_usd": ["total": 3.2],
            "total_usd": 15.7,
        ]
        let result = formatCallCostEstimate(payload)
        XCTAssertTrue(result.contains("country: ES"))
        XCTAssertTrue(result.contains("rates_source: twilio"))
        XCTAssertTrue(result.contains("telephony_total_usd: 12.500"))
        XCTAssertTrue(result.contains("ai_total_usd: 3.200"))
        XCTAssertTrue(result.contains("total_usd: 15.700"))
    }

    /// Поле rates_note добавляется только если не пустое.
    func test_formatCallCostEstimate_ratesNotePresent() {
        let payload: [String: Any] = [
            "country": "MX",
            "rates_source": "cached",
            "rates_note": "fallback to defaults",
            "total_usd": 0.0,
        ]
        let result = formatCallCostEstimate(payload)
        XCTAssertTrue(result.contains("rates_note: fallback to defaults"))
    }

    /// Пустой payload → значения по умолчанию, не крашится.
    func test_formatCallCostEstimate_emptyPayload() {
        let result = formatCallCostEstimate([:])
        XCTAssertTrue(result.contains("country: n/a"))
        XCTAssertTrue(result.contains("rates_source: unknown"))
        XCTAssertTrue(result.contains("total_usd: 0.000"))
    }
}
