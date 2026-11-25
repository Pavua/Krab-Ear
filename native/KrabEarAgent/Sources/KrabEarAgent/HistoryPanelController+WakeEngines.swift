import AppKit

/*
 Блок «Движки голосовой активации» в секции «Разговор с AI» (настройки).

 Два сегмента Выкл / В тени / Вживую — openWakeWord и поиск «Краб» по речи —
 и под каждым строка чисел из `wake_word_status.engines`. Источник правды о
 режиме — ответ backend (он валидирует значение и читает его на каждом чанке),
 поэтому до первого ответа сегмент без выбора: показывать дефолт как факт нельзя.
 Запись идёт патчем `set_settings` только с одним ключом — полный payload
 настроек агента эти ключи не содержит и перетереть их не может.
 */
extension HistoryPanelController {

    static func makeWakeEngineSegment(for engine: WakeEngine) -> NSSegmentedControl {
        let control = NSSegmentedControl(
            labels: WakeEngineMode.segmentTitlesRU, trackingMode: .selectOne, target: nil, action: nil)
        control.segmentStyle = .rounded
        control.selectedSegment = -1
        control.setAccessibilityLabel("Режим: \(engine.titleRU)")
        control.identifier = NSUserInterfaceItemIdentifier("wakeEngineMode.\(engine.rawValue)")
        return control
    }

    static func makeWakeEngineStatsLabel(for engine: WakeEngine) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: "Проверяю состояние…")
        label.font = KrabEarTheme.Typography.body
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.isSelectable = false
        label.identifier = NSUserInterfaceItemIdentifier("wakeEngineStats.\(engine.rawValue)")
        return label
    }

    /// Строки блока для карточки «Разговор с AI».
    @MainActor
    func makeWakeEngineRows() -> [NSView] {
        let subhead = makeSubhead("Движки голосовой активации")
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacer.setAccessibilityElement(false)
        wakeEngineRefreshButton.bezelStyle = .rounded
        wakeEngineRefreshButton.controlSize = .small
        wakeEngineRefreshButton.setAccessibilityLabel("Обновить числа срабатываний движков")
        let header = NSStackView(views: [subhead, spacer, wakeEngineRefreshButton])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = KrabEarTheme.Metrics.standard

        var rows: [NSView] = [header]
        for engine in WakeEngine.allCases {
            guard let control = wakeEngineModeControls[engine],
                  let stats = wakeEngineStatsLabels[engine] else { continue }
            rows.append(makeSettingRow(label: engine.titleRU, description: Self.wakeEngineDescription(engine), control: control))
            rows.append(stats)
        }
        return rows
    }

    /// Те же экземпляры контролов для варианта Claude Design (KrabEar_UseClaudeDesign):
    /// addArrangedSubview переносит их из скрытого основного бара.
    @MainActor
    func cdMakeWakeEngineRows() -> [NSView] {
        let refreshRow = cdMakeRow(label: "Движки голосовой активации", control: wakeEngineRefreshButton)
        var rows: [NSView] = [refreshRow]
        for engine in WakeEngine.allCases {
            guard let control = wakeEngineModeControls[engine],
                  let stats = wakeEngineStatsLabels[engine] else { continue }
            let caption = NSTextField(wrappingLabelWithString: Self.wakeEngineDescription(engine))
            caption.font = KrabEarTheme.Typography.caption
            caption.textColor = KrabEarTheme.Colors.textSecondary
            caption.isSelectable = false
            rows.append(cdMakeSeparator())
            rows.append(cdMakeRow(label: engine.titleRU, control: control))
            rows.append(caption)
            rows.append(stats)
        }
        return rows
    }

    static func wakeEngineDescription(_ engine: WakeEngine) -> String {
        let modes = "«Вживую» — будит, «В тени» — только считает срабатывания и никого не будит, «Выкл» — не слушает."
        switch engine {
        case .oww:
            return modes + " Работает, только когда включён детектор выше."
        case .stt:
            return modes + " Пока режим не «Выкл», держит в памяти отдельную копию GigaAM (около 1,1 ГБ)."
        }
    }

    // MARK: - Действия

    @objc func onWakeEngineModeChanged(_ sender: NSSegmentedControl) {
        guard let engine = wakeEngineModeControls.first(where: { $0.value === sender })?.key,
              let mode = WakeEngineMode(segmentIndex: sender.selectedSegment) else { return }
        wakeEnginePendingWrites[engine, default: 0] += 1
        wakeEngineModeWriter(engine, mode) { [weak self] ok in
            guard let self else { return }
            self.wakeEnginePendingWrites[engine] = max(0, (self.wakeEnginePendingWrites[engine] ?? 1) - 1)
            self.refreshWakeWordStatusRow(failedEngine: ok ? nil : engine)
        }
    }

    @objc func onWakeEngineRefreshClicked() {
        refreshWakeWordStatusRow()
    }

    /// Применяет ответ `wake_word_status` к сегментам и строкам чисел.
    /// `result == nil` — backend не ответил.
    @MainActor
    func applyWakeEngineStatus(_ result: [String: Any]?, failedEngine: WakeEngine? = nil) {
        let engines = result?["engines"] as? [String: Any]
        let backendNow: Double? = (result?["running"] as? Bool) == true ? result?["last_chunk_ts"] as? Double : nil
        for engine in WakeEngine.allCases {
            guard let control = wakeEngineModeControls[engine],
                  let label = wakeEngineStatsLabels[engine] else { continue }
            var line: String
            var warn = false
            if result == nil {
                line = "Нет ответа от backend — режим неизвестен"
                warn = true
                if (wakeEnginePendingWrites[engine] ?? 0) == 0 { control.selectedSegment = -1 }
            } else if let payload = engines?[engine.rawValue] as? [String: Any] {
                let report = WakeEngineReport(engine: engine, payload: payload)
                if (wakeEnginePendingWrites[engine] ?? 0) == 0 {
                    control.selectedSegment = report.mode.segmentIndex
                }
                line = WakeEngineStatsFormatter.text(engine: engine, report: report, backendNow: backendNow)
                warn = line.hasPrefix("Недоступен")
            } else {
                line = "Backend не сообщает о движках — нужен свежий backend"
                warn = true
                if (wakeEnginePendingWrites[engine] ?? 0) == 0 { control.selectedSegment = -1 }
            }
            if failedEngine == engine {
                line = "Не удалось сохранить режим, показан прежний. " + line
                warn = true
            }
            label.stringValue = line
            // Предупреждение — словом и жирным основным цветом, не оранжевым: на сером фоне
            // карточки оранжевый текст плохо читается, а смысл не должен держаться на цвете.
            label.textColor = warn ? KrabEarTheme.Colors.textPrimary : KrabEarTheme.Colors.textSecondary
            label.font = warn ? KrabEarTheme.Typography.sectionTitle : KrabEarTheme.Typography.body
            label.setAccessibilityLabel("\(engine.titleRU): \(line)")
        }
    }
}
