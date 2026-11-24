/*
 DictationHeroViewTests — экран «Диктовка» (редизайн 2026-11, PR 2).

 Покрытие:
   - RecordButtonView: 4 состояния → текст/иконка/VoiceOver; состояние не только цветом;
     текст на заливке контрастен (белый/чёрный по яркости заливки);
   - DictationHeroView: ширины 559/860/1280 (контент окон 640/860/1280) → кнопка ≥ 240×56
     (на узком — во всю ширину), сводка 1 колонка на узком / 2 на широком, ничего за карточкой;
     снимок без изменений не перерисовывает; озвучка один раз на переход;
   - RecentDictationsView: пусто/ошибка/записи, максимум 5 строк, длинный текст ≤ 2 строк;
   - фаза из ответа get_recording_state; формат времени истории.
 KRAB_RENDER_DIR → PNG карточки в 4 состояниях × 2 темы и экран на 860.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class DictationHeroViewTests: XCTestCase {

    private func host(_ view: NSView, width: CGFloat) -> NSView {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 900))
        root.addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: root.topAnchor),
            view.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            view.widthAnchor.constraint(equalToConstant: width),
        ])
        root.layoutSubtreeIfNeeded()
        root.layoutSubtreeIfNeeded()
        return root
    }

    private func snapshot(_ phase: DictationPhase) -> DictationHeroSnapshot {
        DictationHeroSnapshot(phase: phase, hotkey: "⌥ Right", profile: "Max", autoPaste: true, engine: "GigaAM")
    }

    // MARK: - Кнопка записи

    func test_recordButton_statesHaveWordIconAndVoiceOver() {
        let button = RecordButtonView()
        let cases: [(DictationPhase, String, String, String)] = [
            (.idle, "Начать диктовку", "Начать диктовку", "mic.fill"),
            (.recording(durationSec: 12), "Остановить · 00:12", "Остановить диктовку", "stop.fill"),
            (.finalizing, "Начать диктовку", "Начать диктовку", "mic.fill"),
            (.offline, "Начать диктовку", "Начать диктовку", "exclamationmark.triangle.fill"),
        ]
        for (phase, title, a11y, symbol) in cases {
            button.apply(phase: phase)
            XCTAssertEqual(button.titleText, title)
            XCTAssertFalse(button.titleText.isEmpty, "состояние не только цветом")
            XCTAssertEqual(button.accessibilityLabel(), a11y)
            XCTAssertEqual(RecordButtonView.symbolName(for: phase), symbol)
            XCTAssertNotNil(NSImage(systemSymbolName: symbol, accessibilityDescription: nil), symbol)
        }
        XCTAssertEqual(button.accessibilityRole(), .button)
    }

    func test_bestTextColor_picksContrastingColor() {
        XCTAssertEqual(RecordButtonView.bestTextColor(on: .black), .white)
        XCTAssertEqual(RecordButtonView.bestTextColor(on: .white), .black)
        // Красный (запись): белый проходит порог крупного текста 3:1 — оставляем привычный белый.
        XCTAssertEqual(RecordButtonView.bestTextColor(on: NSColor(srgbRed: 1, green: 0.23, blue: 0.19, alpha: 1)), .white)
        // Жёлтый акцент: белый текст был бы нечитаем.
        XCTAssertEqual(RecordButtonView.bestTextColor(on: NSColor(srgbRed: 1, green: 0.8, blue: 0, alpha: 1)), .black)
    }

    func test_recordButton_pressInvokesHandler() {
        let button = RecordButtonView()
        var pressed = 0
        button.onPress = { pressed += 1 }
        _ = button.accessibilityPerformPress()
        XCTAssertEqual(pressed, 1)
    }

    // MARK: - Карточка

    func test_hero_layoutAcrossWidths() {
        for (width, narrow) in [(CGFloat(559), true), (860, false), (1280, false)] {
            let hero = DictationHeroView()
            hero.apply(snapshot(.idle))
            let root = host(hero, width: width)
            let b = hero.recordButton.convert(hero.recordButton.bounds, to: root)
            XCTAssertGreaterThanOrEqual(b.height, 56, "высота кнопки при \(width)")
            XCTAssertGreaterThanOrEqual(b.width, narrow ? width - 32 - 1 : 240, "ширина кнопки при \(width)")
            XCTAssertLessThanOrEqual(b.maxX, width, "кнопка за карточкой при \(width)")
            XCTAssertEqual(hero.isNarrow, narrow)
            XCTAssertEqual(hero.summaryColumnCount, narrow ? 1 : 2, "колонки сводки при \(width)")
            XCTAssertFalse(root.hasAmbiguousLayout, "неоднозначная раскладка при \(width)")
        }
    }

    func test_summary_isCompact_notStretchedAcrossCard() {
        let hero = DictationHeroView()
        hero.apply(snapshot(.idle))
        let root = host(hero, width: 1280)
        guard let grid = hero.subviews.first.flatMap({ Self.find(NSGridView.self, in: $0) }) else { return XCTFail("нет сетки") }
        let f = grid.convert(grid.bounds, to: root)
        XCTAssertLessThan(f.width, 900, "сводка растянута на всю карточку: \(f.width)")
    }

    private static func find<T: NSView>(_ type: T.Type, in view: NSView) -> T? {
        if let v = view as? T { return v }
        for s in view.subviews { if let v = find(type, in: s) { return v } }
        return nil
    }

    func test_hero_statusTextPerPhase_andDiagnosticsOnlyWhenOffline() {
        let hero = DictationHeroView()
        hero.apply(snapshot(.idle))
        XCTAssertEqual(hero.statusText, "Готов · или нажмите ⌥ Right")
        XCTAssertFalse(hero.isDiagnosticsButtonVisible)
        hero.apply(snapshot(.recording(durationSec: 3)))
        XCTAssertEqual(hero.statusText, "Идёт запись")
        hero.apply(snapshot(.finalizing))
        XCTAssertEqual(hero.statusText, "Распознаю предыдущую запись…")
        hero.apply(snapshot(.offline))
        XCTAssertEqual(hero.statusText, "Нет связи с движком распознавания")
        XCTAssertTrue(hero.isDiagnosticsButtonVisible)
    }

    func test_hero_announcesOncePerTransition_notPerTick() {
        let hero = DictationHeroView()
        hero.apply(snapshot(.idle))
        hero.apply(snapshot(.recording(durationSec: 1)))
        XCTAssertEqual(hero.lastAnnouncement, "Идёт запись")
        hero.apply(snapshot(.recording(durationSec: 2)))
        XCTAssertEqual(hero.lastAnnouncement, "Идёт запись", "тик длительности не озвучивается заново")
        hero.apply(snapshot(.idle))
        XCTAssertEqual(hero.lastAnnouncement, "Запись остановлена")
    }

    func test_hero_sameSnapshotDoesNotReapply() {
        let hero = DictationHeroView()
        hero.apply(snapshot(.recording(durationSec: 5)))
        hero.recordButton.apply(phase: .idle)  // внешняя порча — повтор снимка её не должен трогать
        hero.apply(snapshot(.recording(durationSec: 5)))
        XCTAssertEqual(hero.recordButton.phase, .idle, "одинаковый снимок не перерисовывает")
    }

    func test_phaseFromRecordingState() {
        XCTAssertEqual(HistoryPanelController.dictationPhase(from: [:]), .idle)
        XCTAssertEqual(HistoryPanelController.dictationPhase(from: ["is_recording": true, "duration_sec": 7.0]),
                       .recording(durationSec: 7))
        XCTAssertEqual(HistoryPanelController.dictationPhase(from: ["is_recording": false, "is_finalizing": true]),
                       .finalizing)
    }

    // MARK: - Последние результаты

    func test_recent_emptyErrorAndItems() {
        let recent = RecentDictationsView()
        recent.apply(.empty(title: "Пока нет диктовок", hint: "Нажмите «Начать диктовку»"))
        XCTAssertEqual(recent.messageText, "Пока нет диктовок")
        XCTAssertTrue(recent.rowViews.isEmpty)

        recent.apply(.error(message: "Не удалось загрузить последние записи"))
        XCTAssertTrue(recent.hasRetryButton)
        var retried = false
        recent.onRetry = { retried = true }
        recent.perform(Selector(("onRetryPressed")))
        XCTAssertTrue(retried)

        let items = (1...7).map { RecentDictationsView.Item(id: "\($0)", time: "12:0\($0)", text: "Запись \($0)") }
        recent.apply(.items(items))
        XCTAssertEqual(recent.rowViews.count, 5, "не больше 5 строк, без вложенного скролла")
        XCTAssertFalse(recent.hasRetryButton)
    }

    func test_recent_longTextWrapsToTwoLines_withinCard() {
        let recent = RecentDictationsView()
        let long = String(repeating: "очень длинный текст диктовки ", count: 30)
        recent.apply(.items([RecentDictationsView.Item(id: "1", time: "12:41", text: long)]))
        let root = host(recent, width: 559)
        guard let row = recent.rowViews.first else { return XCTFail("нет строки") }
        let f = row.textLabel.convert(row.textLabel.bounds, to: root)
        let lineHeight = KrabEarTheme.Typography.bodyLarge.boundingRectForFont.height
        XCTAssertLessThanOrEqual(f.height, lineHeight * 2 + 6, "больше двух строк: \(f.height)")
        XCTAssertLessThanOrEqual(f.maxX, 559, "текст за карточкой")
        let copy = row.copyButton.convert(row.copyButton.bounds, to: root)
        XCTAssertGreaterThanOrEqual(copy.width, 32)
        XCTAssertEqual(row.copyButton.accessibilityLabel(), "Копировать")
    }

    func test_shortTime() {
        XCTAssertEqual(RecentDictationsView.shortTime(from: "2026-11-24T12:41:05.123"), "12:41")
        XCTAssertEqual(RecentDictationsView.shortTime(from: "2026-11-24 09:05:00"), "09:05")
        XCTAssertEqual(RecentDictationsView.shortTime(from: "вчера"), "вчера")
    }

    // MARK: - PNG

    func test_renderPNG_whenRequested() throws {
        guard let dir = ProcessInfo.processInfo.environment["KRAB_RENDER_DIR"] else {
            throw XCTSkip("KRAB_RENDER_DIR не задан")
        }
        let phases: [(String, DictationPhase)] = [
            ("idle", .idle), ("recording", .recording(durationSec: 12)), ("finalizing", .finalizing), ("offline", .offline),
        ]
        for (name, ap) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            for (pname, phase) in phases {
                let container = NSStackView()
                container.orientation = .vertical
                container.alignment = .leading
                container.spacing = 16
                container.translatesAutoresizingMaskIntoConstraints = false
                let hero = DictationHeroView()
                hero.apply(snapshot(phase))
                let recent = RecentDictationsView()
                recent.apply(.items([
                    .init(id: "1", time: "12:41", text: "Нужно купить хлеб, молоко и позвонить маме вечером после работы, а ещё записаться к врачу на четверг."),
                    .init(id: "2", time: "12:38", text: "Короткая заметка."),
                    .init(id: "3", time: "11:02", text: "Встреча с командой перенесена на пятницу, 15:00."),
                ]))
                container.addArrangedSubview(hero)
                container.addArrangedSubview(recent)
                hero.widthAnchor.constraint(equalTo: container.widthAnchor).isActive = true
                recent.widthAnchor.constraint(equalTo: container.widthAnchor).isActive = true
                let width: CGFloat = 651
                let root = NSView(frame: NSRect(x: 0, y: 0, width: width + 32, height: 560))
                root.appearance = NSAppearance(named: ap)
                root.wantsLayer = true
                root.addSubview(container)
                NSLayoutConstraint.activate([
                    container.topAnchor.constraint(equalTo: root.topAnchor, constant: 16),
                    container.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 16),
                    container.widthAnchor.constraint(equalToConstant: width),
                ])
                root.appearance?.performAsCurrentDrawingAppearance {
                    root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
                }
                root.layoutSubtreeIfNeeded()
                root.layoutSubtreeIfNeeded()
                guard let rep = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { continue }
                root.cacheDisplay(in: root.bounds, to: rep)
                let url = URL(fileURLWithPath: dir).appendingPathComponent("dictation_\(pname)_\(name).png")
                try rep.representation(using: .png, properties: [:])?.write(to: url)
            }
        }
    }
}
