/*
 Тесты панели настроек: каталог LLM-моделей.

 Контекст (живой инцидент 2026-08-26): dropdown выбора модели показывал
 захардкоженный список, потому что backend спрашивал у LM Studio endpoint
 `/api/v1/models`, который отвечает 200 с ПУСТЫМ телом. Каталог чинится на
 стороне Python; здесь фиксируется вторая половина: рекомендованные модели
 не должны переживать своё удаление с диска.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

// MARK: - Каталог LLM-моделей (2026-08-26)

@MainActor
final class LLMModelDropdownCatalogTests: XCTestCase {
    /// 🔴 Рекомендованный список статичен: удалённые с диска модели предлагать нельзя —
    /// выбор такой строки давал молчаливый отказ рерайта.
    func test_recommended_filtered_by_actual_catalog() throws {
        let source = try String(contentsOf: Self.settingsSourceURL, encoding: .utf8)
        XCTAssertTrue(
            source.contains("recommendedRewriterModels.filter { available.contains($0) }"),
            "рекомендованные обязаны фильтроваться по фактическому каталогу"
        )
    }

    /// Fail-open: LM Studio выключен → каталог пуст → dropdown НЕ должен опустеть.
    func test_empty_catalog_keeps_recommended_list() throws {
        let source = try String(contentsOf: Self.settingsSourceURL, encoding: .utf8)
        XCTAssertTrue(
            source.contains("available.isEmpty"),
            "пустой каталог обязан оставлять рекомендованные как есть"
        )
    }

    private static var settingsSourceURL: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+Settings.swift")
    }
}
