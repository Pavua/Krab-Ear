/*
 AnalyticsDashboardTests — юнит-тесты статических хелперов AnalyticsDashboardData.

 Стратегия: тестируем только pure static функции, которые не требуют инстанцирования
 AnalyticsDashboardViewController (невозможно в headless-тестах без UI).

 Покрываем:
   1. parse(from:) — правильный маппинг IPC dict → AnalyticsDashboardData
   2. trendEmoji / trendDescription — форматирование трендов
   3. formatHour / formatPercent / formatMB — утилиты форматирования
   4. confidenceColor — цветовая кодировка качества
   5. Edge-cases: пустой dict, нулевые значения, нестандартные NSNumber из JSON
*/

import XCTest
@testable import KrabEarAgent

final class AnalyticsDashboardTests: XCTestCase {

    // MARK: - parse(from:)

    func test_parse_fullDict_mapsAllFields() {
        let raw: [String: Any] = [
            "overview": ["total_recordings": 100, "total_hours": 2.5, "total_words": 5000, "avg_daily": 3.3],
            "today": ["recordings": 5, "duration_min": 12.5, "words": 300],
            "trends": ["confidence_trend": "improving", "pace_trend": "declining", "volume_trend": "stable"],
            "languages": ["distribution": ["ru": 0.6, "es": 0.4], "translation_rate": 0.25],
            "quality": ["avg_confidence": 0.88, "low_confidence_rate": 0.1, "llm_rewrite_rate": 0.2],
            "engagement": ["streak_days": 7, "peak_hour": 14, "most_active_day": "Monday"],
            "storage": ["history_size_mb": 1.5, "backups_count": 3, "cache_size_mb": 0.25],
            "performance": ["avg_stt_latency_ms": 320.0, "p95_latency_ms": 850.0],
        ]

        let d = AnalyticsDashboardData.parse(from: raw)

        XCTAssertEqual(d.totalRecordings, 100)
        XCTAssertEqual(d.totalHours, 2.5, accuracy: 0.001)
        XCTAssertEqual(d.totalWords, 5000)
        XCTAssertEqual(d.avgDaily, 3.3, accuracy: 0.001)

        XCTAssertEqual(d.todayRecordings, 5)
        XCTAssertEqual(d.todayDurationMin, 12.5, accuracy: 0.001)
        XCTAssertEqual(d.todayWords, 300)

        XCTAssertEqual(d.confidenceTrend, "improving")
        XCTAssertEqual(d.paceTrend, "declining")
        XCTAssertEqual(d.volumeTrend, "stable")

        XCTAssertEqual(d.translationRate, 0.25, accuracy: 0.001)
        XCTAssertEqual(d.langDistribution.count, 2)

        XCTAssertEqual(d.avgConfidence, 0.88, accuracy: 0.001)
        XCTAssertEqual(d.lowConfidenceRate, 0.1, accuracy: 0.001)
        XCTAssertEqual(d.llmRewriteRate, 0.2, accuracy: 0.001)

        XCTAssertEqual(d.streakDays, 7)
        XCTAssertEqual(d.peakHour, 14)
        XCTAssertEqual(d.mostActiveDay, "Monday")

        XCTAssertEqual(d.historySizeMB, 1.5, accuracy: 0.001)
        XCTAssertEqual(d.backupsCount, 3)
        XCTAssertEqual(d.cacheSizeMB, 0.25, accuracy: 0.001)

        XCTAssertEqual(d.avgSttLatencyMs, 320.0, accuracy: 0.001)
        XCTAssertEqual(d.p95LatencyMs, 850.0, accuracy: 0.001)
    }

    func test_parse_emptyDict_returnsDefaults() {
        let d = AnalyticsDashboardData.parse(from: [:])

        XCTAssertEqual(d.totalRecordings, 0)
        XCTAssertEqual(d.totalHours, 0.0, accuracy: 0.001)
        XCTAssertEqual(d.totalWords, 0)
        XCTAssertEqual(d.confidenceTrend, "stable")
        XCTAssertEqual(d.paceTrend, "stable")
        XCTAssertEqual(d.volumeTrend, "stable")
        XCTAssertNil(d.peakHour)
        XCTAssertEqual(d.langDistribution.count, 0)
    }

    func test_parse_NSNumber_valuesFromJSONSerialization() {
        // JSON deserialization returns NSNumber — verify coercion works
        let raw: [String: Any] = [
            "overview": [
                "total_recordings": NSNumber(value: 42),
                "total_hours": NSNumber(value: 1.75),
                "total_words": NSNumber(value: 1200),
                "avg_daily": NSNumber(value: 2.0),
            ],
            "quality": [
                "avg_confidence": NSNumber(value: 0.91),
                "low_confidence_rate": NSNumber(value: 0.05),
                "llm_rewrite_rate": NSNumber(value: 0.15),
            ],
        ]

        let d = AnalyticsDashboardData.parse(from: raw)

        XCTAssertEqual(d.totalRecordings, 42)
        XCTAssertEqual(d.totalHours, 1.75, accuracy: 0.001)
        XCTAssertEqual(d.avgConfidence, 0.91, accuracy: 0.001)
    }

    func test_parse_langDistribution_sortedByShareDescending() {
        let raw: [String: Any] = [
            "languages": [
                "distribution": ["es": 0.2, "ru": 0.7, "en": 0.1],
                "translation_rate": 0.0,
            ]
        ]

        let d = AnalyticsDashboardData.parse(from: raw)

        XCTAssertEqual(d.langDistribution.count, 3)
        XCTAssertEqual(d.langDistribution[0].lang, "ru")
        XCTAssertGreaterThan(d.langDistribution[0].share, d.langDistribution[1].share)
        XCTAssertGreaterThan(d.langDistribution[1].share, d.langDistribution[2].share)
    }

    // MARK: - trendEmoji

    func test_trendEmoji_improving_returnsUp() {
        XCTAssertEqual(AnalyticsDashboardData.trendEmoji("improving"), "↑")
    }

    func test_trendEmoji_declining_returnsDown() {
        XCTAssertEqual(AnalyticsDashboardData.trendEmoji("declining"), "↓")
    }

    func test_trendEmoji_stable_returnsRight() {
        XCTAssertEqual(AnalyticsDashboardData.trendEmoji("stable"), "→")
    }

    func test_trendEmoji_unknown_returnsRight() {
        XCTAssertEqual(AnalyticsDashboardData.trendEmoji(""), "→")
        XCTAssertEqual(AnalyticsDashboardData.trendEmoji("unknown"), "→")
    }

    // MARK: - trendDescription

    func test_trendDescription_allCases() {
        XCTAssertEqual(AnalyticsDashboardData.trendDescription("improving"), "растёт")
        XCTAssertEqual(AnalyticsDashboardData.trendDescription("declining"), "снижается")
        XCTAssertEqual(AnalyticsDashboardData.trendDescription("stable"), "стабильно")
        XCTAssertEqual(AnalyticsDashboardData.trendDescription("other"), "стабильно")
    }

    // MARK: - formatHour

    func test_formatHour_midnight() {
        XCTAssertEqual(AnalyticsDashboardData.formatHour(0), "0:00")
    }

    func test_formatHour_noon() {
        XCTAssertEqual(AnalyticsDashboardData.formatHour(12), "12:00")
    }

    func test_formatHour_lateEvening() {
        XCTAssertEqual(AnalyticsDashboardData.formatHour(23), "23:00")
    }

    // MARK: - formatPercent

    func test_formatPercent_zero() {
        XCTAssertEqual(AnalyticsDashboardData.formatPercent(0.0), "0.0%")
    }

    func test_formatPercent_half() {
        XCTAssertEqual(AnalyticsDashboardData.formatPercent(0.5), "50.0%")
    }

    func test_formatPercent_full() {
        XCTAssertEqual(AnalyticsDashboardData.formatPercent(1.0), "100.0%")
    }

    func test_formatPercent_small() {
        XCTAssertEqual(AnalyticsDashboardData.formatPercent(0.015), "1.5%")
    }

    // MARK: - formatMB

    func test_formatMB_lessThanOneMB_showsKB() {
        let result = AnalyticsDashboardData.formatMB(0.5)
        XCTAssertTrue(result.contains("КБ"), "Должен показывать КБ: \(result)")
        XCTAssertTrue(result.contains("512"), "0.5 МБ = 512 КБ: \(result)")
    }

    func test_formatMB_exactlyOneMB() {
        let result = AnalyticsDashboardData.formatMB(1.0)
        XCTAssertTrue(result.contains("МБ"), "Должен показывать МБ: \(result)")
        XCTAssertTrue(result.hasPrefix("1.00"), "Должен быть 1.00 МБ: \(result)")
    }

    func test_formatMB_largeSizes() {
        let result = AnalyticsDashboardData.formatMB(15.75)
        XCTAssertTrue(result.contains("15.75"), "Должен форматировать 15.75 МБ: \(result)")
    }

    // MARK: - confidenceColor

    func test_confidenceColor_high_isGreen() {
        XCTAssertEqual(AnalyticsDashboardData.confidenceColor(0.9), .systemGreen)
        XCTAssertEqual(AnalyticsDashboardData.confidenceColor(0.85), .systemGreen)
    }

    func test_confidenceColor_medium_isOrange() {
        XCTAssertEqual(AnalyticsDashboardData.confidenceColor(0.75), .systemOrange)
        XCTAssertEqual(AnalyticsDashboardData.confidenceColor(0.70), .systemOrange)
    }

    func test_confidenceColor_low_isRed() {
        XCTAssertEqual(AnalyticsDashboardData.confidenceColor(0.5), .systemRed)
        XCTAssertEqual(AnalyticsDashboardData.confidenceColor(0.0), .systemRed)
    }
}
