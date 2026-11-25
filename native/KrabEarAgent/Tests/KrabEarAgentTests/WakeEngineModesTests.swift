import AppKit
import XCTest
@testable import KrabEarAgent

/// Переключатели режимов двух движков голосовой активации (#2258) и строка их чисел.
final class WakeEngineModesTests: XCTestCase {

    // MARK: - Чистая логика

    func test_settingKeysAndDefaults_matchBackend() {
        XCTAssertEqual(WakeEngine.oww.settingKey, "wake_word_oww_mode")
        XCTAssertEqual(WakeEngine.stt.settingKey, "wake_word_stt_mode")
        XCTAssertEqual(WakeEngine.oww.defaultMode, .live)
        XCTAssertEqual(WakeEngine.stt.defaultMode, .off)
        XCTAssertEqual(WakeEngineMode.allCases.map(\.rawValue), ["off", "shadow", "live"])
    }

    func test_segmentIndex_roundTrip_andOutOfRange() {
        for mode in WakeEngineMode.allCases {
            XCTAssertEqual(WakeEngineMode(segmentIndex: mode.segmentIndex), mode)
        }
        XCTAssertNil(WakeEngineMode(segmentIndex: -1))
        XCTAssertNil(WakeEngineMode(segmentIndex: 3))
        XCTAssertEqual(WakeEngineMode.segmentTitlesRU.count, WakeEngineMode.allCases.count)
    }

    func test_parse_garbage_fallsBackToEngineDefault() {
        XCTAssertEqual(WakeEngineMode.parse("bogus", engine: .oww), .live)
        XCTAssertEqual(WakeEngineMode.parse(nil, engine: .stt), .off)
        XCTAssertEqual(WakeEngineMode.parse(5, engine: .stt), .off)
        XCTAssertEqual(WakeEngineMode.parse("shadow", engine: .oww), .shadow)
    }

    func test_stt_unavailableReason_isShownInsteadOfSilentZero() {
        let report = WakeEngineReport(engine: .stt, payload: [
            "mode": "shadow", "fires_live": 0, "fires_shadow": 0, "available": true,
            "unavailable_reason": "mlx_busy", "spotter": ["unavailable": 4, "utterances": 9],
        ])
        let text = WakeEngineStatsFormatter.text(engine: .stt, report: report, backendNow: nil)
        XCTAssertEqual(text, "Недоступен (mlx_busy) · пропущено реплик: 4")
    }

    func test_stt_withoutTranscriber_isUnavailable_whenNotOff() {
        let on = WakeEngineReport(engine: .stt, payload: ["mode": "live", "available": false])
        XCTAssertTrue(WakeEngineStatsFormatter.text(engine: .stt, report: on, backendNow: nil).hasPrefix("Недоступен"))
        // В «Выкл» причина недоступности не важна — показываем выключенный движок с числами.
        let off = WakeEngineReport(engine: .stt, payload: ["mode": "off", "available": false, "unavailable_reason": "x"])
        XCTAssertEqual(WakeEngineStatsFormatter.text(engine: .stt, report: off, backendNow: nil),
                       "Выключен · Срабатываний вживую: 0 · в тени: 0")
    }

    func test_counts_andAge_measuredOnBackendClockOnly() {
        let report = WakeEngineReport(engine: .oww, payload: [
            "mode": "live", "fires_live": 3, "fires_shadow": 12, "last_fire_ts": 1000.0,
        ])
        XCTAssertEqual(WakeEngineStatsFormatter.text(engine: .oww, report: report, backendNow: 1000.0 + 5 * 60),
                       "Срабатываний вживую: 3 · в тени: 12 · последнее: 5 мин назад")
        // Без «сейчас» по часам backend давность не выдумываем.
        XCTAssertEqual(WakeEngineStatsFormatter.text(engine: .oww, report: report, backendNow: nil),
                       "Срабатываний вживую: 3 · в тени: 12 · последнее: было")
        XCTAssertEqual(WakeEngineStatsFormatter.text(engine: .oww, report: report, backendNow: 999),
                       "Срабатываний вживую: 3 · в тени: 12 · последнее: было")
    }

    func test_ago_buckets() {
        XCTAssertEqual(WakeEngineStatsFormatter.ago(10), "меньше минуты назад")
        XCTAssertEqual(WakeEngineStatsFormatter.ago(59 * 60), "59 мин назад")
        XCTAssertEqual(WakeEngineStatsFormatter.ago(3 * 3600 + 5), "3 ч назад")
    }

    func test_report_toleratesDoublesAndNonFinite() {
        let report = WakeEngineReport(engine: .oww, payload: [
            "fires_live": 2.0, "fires_shadow": Double.nan, "last_fire_ts": Double.infinity,
        ])
        XCTAssertEqual(report.firesLive, 2)
        XCTAssertEqual(report.firesShadow, 0)
        XCTAssertNil(report.lastFireTs)
        XCTAssertEqual(report.mode, .live)
    }

    // MARK: - Панель

    @MainActor
    private func makeController() -> HistoryPanelController {
        let ipc = IPCClient(socketPath: "/nonexistent-krab-ear-test.sock")
        return HistoryPanelController(
            ipcClient: ipc,
            plaintextExportCoordinator: PlaintextExportCoordinator(ipc: ipc),
            settingsProvider: { .default },
            settingsUpdater: { _ in .default },
            onToggleRecording: {},
            onRestartAgent: {},
            onStopAgent: {},
            onPasteHistoryItem: { _ in },
            onSwapRuEsDirection: {}
        )
    }

    private func descendants(_ v: NSView) -> [NSView] {
        v.subviews + v.subviews.flatMap { descendants($0) }
    }

    @MainActor
    func test_panel_hasBothEngineSegments_inSettings_withNoSelectionBeforeStatus() {
        let controller = makeController()
        _ = controller.window
        let views = controller.mainTabView.tabViewItems.compactMap(\.view).flatMap { descendants($0) }
        for engine in WakeEngine.allCases {
            let id = "wakeEngineMode.\(engine.rawValue)"
            let segments = views.compactMap { $0 as? NSSegmentedControl }.filter { $0.identifier?.rawValue == id }
            XCTAssertEqual(segments.count, 1, "сегмент \(engine) встроен в панель ровно один раз")
            guard let control = segments.first else { continue }
            XCTAssertEqual((0..<control.segmentCount).map { control.label(forSegment: $0) }, ["Выкл", "В тени", "Вживую"])
            XCTAssertEqual(control.selectedSegment, -1, "до ответа backend режим не выдаём за факт")
            XCTAssertNotNil(control.target)
            XCTAssertNotNil(control.accessibilityLabel())
        }
        XCTAssertTrue(views.contains { $0 === controller.wakeEngineRefreshButton })
    }

    @MainActor
    func test_applyStatus_setsSegmentsAndStats_nilResultClearsSelection() {
        let controller = makeController()
        _ = controller.window
        controller.applyWakeEngineStatus([
            "running": true, "last_chunk_ts": 160.0,
            "engines": [
                "oww": ["mode": "shadow", "fires_live": 1, "fires_shadow": 7, "last_fire_ts": 100.0],
                "stt": ["mode": "live", "available": true, "unavailable_reason": "model_load_failed",
                        "spotter": ["unavailable": 2]],
            ],
        ])
        XCTAssertEqual(controller.wakeEngineModeControls[.oww]?.selectedSegment, WakeEngineMode.shadow.segmentIndex)
        XCTAssertEqual(controller.wakeEngineModeControls[.stt]?.selectedSegment, WakeEngineMode.live.segmentIndex)
        XCTAssertEqual(controller.wakeEngineStatsLabels[.oww]?.stringValue,
                       "Срабатываний вживую: 1 · в тени: 7 · последнее: 1 мин назад")
        XCTAssertEqual(controller.wakeEngineStatsLabels[.stt]?.stringValue,
                       "Недоступен (model_load_failed) · пропущено реплик: 2")

        controller.applyWakeEngineStatus(nil)
        XCTAssertEqual(controller.wakeEngineModeControls[.oww]?.selectedSegment, -1)
        XCTAssertEqual(controller.wakeEngineStatsLabels[.stt]?.stringValue, "Нет ответа от backend — режим неизвестен")
    }

    /// Офлайн-рендер секции «Разговор с AI» с блоком движков (только при KRAB_RENDER_DIR):
    /// визуальная проверка без запуска приложения. Попутно — подписи не обрезаны по высоте.
    @MainActor
    func test_renderVoiceAssistantSection_withEngineBlock() throws {
        let controller = makeController()
        guard let window = controller.window else { return XCTFail("нет окна") }
        controller.selectPanelTab(.dictation)
        let views = controller.mainTabView.tabViewItems.compactMap(\.view).flatMap { descendants($0) }
        guard let section = views.compactMap({ $0 as? CollapsibleSectionView })
            .first(where: { $0.sectionId == "settings_voice_assistant" }) else { return XCTFail("нет секции") }
        section.setExpanded(true, animated: false)
        controller.applyWakeEngineStatus([
            "running": true, "last_chunk_ts": 400.0,
            "engines": [
                "oww": ["mode": "live", "fires_live": 3, "fires_shadow": 0, "last_fire_ts": 100.0],
                "stt": ["mode": "shadow", "available": true, "unavailable_reason": "mlx_busy",
                        "spotter": ["unavailable": 2]],
            ],
        ])
        let dir = ProcessInfo.processInfo.environment["KRAB_RENDER_DIR"]
        for width in [860.0, 640.0] {
            window.appearance = NSAppearance(named: .aqua)
            window.setContentSize(NSSize(width: width, height: 820))
            window.contentView?.layoutSubtreeIfNeeded()
            window.contentView?.layoutSubtreeIfNeeded()
            for engine in WakeEngine.allCases {
                guard let label = controller.wakeEngineStatsLabels[engine] else { continue }
                let need = label.cell?.cellSize(forBounds: NSRect(x: 0, y: 0, width: label.bounds.width, height: 10_000)).height ?? 0
                XCTAssertGreaterThan(label.bounds.width, 100, "строка чисел \(engine) не сжата на \(width)")
                XCTAssertLessThanOrEqual(need, label.bounds.height + 1, "строка чисел \(engine) обрезана по высоте на \(width)")
            }
            guard let dir, let rep = section.bitmapImageRepForCachingDisplay(in: section.bounds) else { continue }
            section.cacheDisplay(in: section.bounds, to: rep)
            try rep.representation(using: .png, properties: [:])?
                .write(to: URL(fileURLWithPath: dir).appendingPathComponent("wake_engines_\(Int(width)).png"))
        }
    }

    @MainActor
    func test_segmentClick_writesOnlyThatKey_andPendingWriteIsNotOverwritten() {
        let controller = makeController()
        _ = controller.window
        var writes: [(WakeEngine, WakeEngineMode)] = []
        var completions: [(Bool) -> Void] = []
        controller.wakeEngineModeWriter = { engine, mode, completion in
            writes.append((engine, mode))
            completions.append(completion)
        }
        guard let stt = controller.wakeEngineModeControls[.stt] else { return XCTFail("нет сегмента stt") }
        stt.selectedSegment = WakeEngineMode.shadow.segmentIndex
        controller.onWakeEngineModeChanged(stt)
        XCTAssertEqual(writes.count, 1)
        XCTAssertEqual(writes.first?.0, .stt)
        XCTAssertEqual(writes.first?.1, .shadow)

        // Пока запись в пути, устаревший статус (ещё «off») не возвращает сегмент назад.
        controller.applyWakeEngineStatus(["engines": ["oww": ["mode": "live"], "stt": ["mode": "off"]]])
        XCTAssertEqual(stt.selectedSegment, WakeEngineMode.shadow.segmentIndex)
        XCTAssertEqual(controller.wakeEnginePendingWrites[.stt], 1)

        // Отказ записи: счётчик снят, следующий статус показывает фактический режим и предупреждение.
        completions.first?(false)
        XCTAssertEqual(controller.wakeEnginePendingWrites[.stt], 0)
        controller.applyWakeEngineStatus(["engines": ["oww": ["mode": "live"], "stt": ["mode": "off"]]],
                                         failedEngine: .stt)
        XCTAssertEqual(stt.selectedSegment, WakeEngineMode.off.segmentIndex)
        XCTAssertTrue(controller.wakeEngineStatsLabels[.stt]?.stringValue.hasPrefix("Не удалось сохранить режим") ?? false)
    }
}
