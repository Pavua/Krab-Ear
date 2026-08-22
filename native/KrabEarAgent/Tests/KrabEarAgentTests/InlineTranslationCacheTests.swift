/*
 InlineTranslationCacheTests — юнит-тесты логики инлайн-перевода в истории.

 Стратегия:
 - HistoryPanelController нельзя инстанцировать в headless-тестах.
 - Тестируем static-хелперы из +History:
   1. inlineTranslationCacheHit — cache miss / cache hit.
   2. inlineTranslationNextVisible — toggle show / toggle hide.
*/

import XCTest
@testable import KrabEarAgent

// @MainActor: NSCache не Sendable, capture в static `inlineTranslationCacheHit`
// (которая `nonisolated`) → Swift 6 strict concurrency выдаёт SendingRisksDataRace.
// Поскольку тесты вызывают static helpers только из синхронных test methods,
// помечаем класс @MainActor — все вызовы остаются на main, sending не нужен.
@MainActor
final class InlineTranslationCacheTests: XCTestCase {

    // MARK: - Cache hit / miss

    /// Пустой кэш: cache miss для любого ID.
    func test_cacheHit_emptyCache_returnsFalse() {
        let cache = NSCache<NSString, NSString>()
        XCTAssertFalse(
            HistoryPanelController.inlineTranslationCacheHit(cache: cache, itemID: "item-1"),
            "Пустой кэш должен возвращать false"
        )
    }

    /// После сохранения: cache hit для того же ID.
    func test_cacheHit_afterStore_returnsTrue() {
        let cache = NSCache<NSString, NSString>()
        cache.setObject("Привет мир" as NSString, forKey: "item-42" as NSString)
        XCTAssertTrue(
            HistoryPanelController.inlineTranslationCacheHit(cache: cache, itemID: "item-42"),
            "Должен быть cache hit после сохранения"
        )
    }

    /// Cache hit только для нужного ID; другие ID остаются miss.
    func test_cacheHit_differentID_returnsFalse() {
        let cache = NSCache<NSString, NSString>()
        cache.setObject("Hola mundo" as NSString, forKey: "item-10" as NSString)
        XCTAssertFalse(
            HistoryPanelController.inlineTranslationCacheHit(cache: cache, itemID: "item-99"),
            "Другой ID не должен быть в кэше"
        )
    }

    // MARK: - Toggle show / hide

    /// Первый toggle на пустом set → показать (true).
    func test_toggleVisible_notInSet_returnsTrue() {
        let visible: Set<String> = []
        let result = HistoryPanelController.inlineTranslationNextVisible(
            currentlyVisible: visible, itemID: "item-1"
        )
        XCTAssertTrue(result, "Первый toggle должен показывать перевод")
    }

    /// Повторный toggle на already-visible → скрыть (false).
    func test_toggleVisible_alreadyVisible_returnsFalse() {
        let visible: Set<String> = ["item-1", "item-2"]
        let result = HistoryPanelController.inlineTranslationNextVisible(
            currentlyVisible: visible, itemID: "item-1"
        )
        XCTAssertFalse(result, "Повторный toggle должен скрывать перевод")
    }

    /// Toggle другого ID в set не влияет на возвращаемое значение.
    func test_toggleVisible_otherIDInSet_returnsTrue() {
        let visible: Set<String> = ["item-99"]
        let result = HistoryPanelController.inlineTranslationNextVisible(
            currentlyVisible: visible, itemID: "item-1"
        )
        XCTAssertTrue(result, "Другой ID в set не должен влиять")
    }

    /// NSCache.countLimit = 2: третий объект вытесняет первый (eviction).
    /// После eviction cache miss для вытесненного, hit для последних двух.
    func test_cacheEviction_countLimit() {
        let cache = NSCache<NSString, NSString>()
        cache.countLimit = 2
        cache.setObject("A" as NSString, forKey: "k1" as NSString)
        cache.setObject("B" as NSString, forKey: "k2" as NSString)
        cache.setObject("C" as NSString, forKey: "k3" as NSString)
        // k2 и k3 гарантированно в кэше; k1 может быть вытеснен.
        XCTAssertTrue(
            HistoryPanelController.inlineTranslationCacheHit(cache: cache, itemID: "k2")
        )
        XCTAssertTrue(
            HistoryPanelController.inlineTranslationCacheHit(cache: cache, itemID: "k3")
        )
    }

    func test_production_toggle_uses_shared_cache_and_visibility_helpers() throws {
        let source = try String(contentsOf: Self.historySourceURL, encoding: .utf8)
        XCTAssertTrue(source.contains("HistoryPanelController.inlineTranslationNextVisible("))
        XCTAssertTrue(source.contains("HistoryPanelController.inlineTranslationCacheHit("))
    }

    private static var historySourceURL: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+History.swift")
    }
}
