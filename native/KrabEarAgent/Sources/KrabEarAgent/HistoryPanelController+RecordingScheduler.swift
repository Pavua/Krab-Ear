/*
 * Запланированные записи (RecordingScheduler) — секция настроек.
 *
 * IPC-контракты (все существуют в бэкенде):
 *   - list_scheduled_recordings {} -> result.schedules, result.count
 *   - schedule_recording {start_time: String, duration_sec: Int, label: String} -> result.schedule
 *   - cancel_scheduled_recording {schedule_id: String} -> result.cancelled
 *
 * Правила AGENT-3: IPC строго в DispatchQueue.global, мутации UI — строго в DispatchQueue.main.
 */

import AppKit
import Foundation

// MARK: - Associated-object ключи

enum RecordingSchedulerAssocKeys {
    nonisolated(unsafe) static var sectionCard: UInt8 = 0
    nonisolated(unsafe) static var datePicker: UInt8 = 0
    nonisolated(unsafe) static var durationField: UInt8 = 0
    nonisolated(unsafe) static var labelField: UInt8 = 0
}

extension HistoryPanelController {

    /// Строит секцию «Запланированные записи» для Gemini-дизайна (settingsBar).
    @MainActor
    // MARK: - Helpers

    func makeSchedulerTimeField() -> NSDatePicker {
        let datePicker = NSDatePicker()
        datePicker.datePickerStyle = .textFieldAndStepper
        datePicker.datePickerElements = [.yearMonthDay, .hourMinute]
        datePicker.dateValue = Date().addingTimeInterval(3600)
        datePicker.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        return datePicker
    }
    
    func makeSchedulerDurationField() -> NSTextField {
        let durationField = NSTextField(frame: .zero)
        durationField.placeholderString = "30"
        durationField.widthAnchor.constraint(equalToConstant: 40).isActive = true
        return durationField
    }
    
    func makeSchedulerDescField() -> NSTextField {
        let labelField = NSTextField(frame: .zero)
        labelField.placeholderString = "Зум-колл..."
        labelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        return labelField
    }
    
    func makeSchedulerSubmitButton() -> ThemePrimaryButton {
        return ThemePrimaryButton(title: "Запланировать", target: self, action: #selector(onScheduleRecording(_:)))
    }

    func buildRecordingSchedulerSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "recording_scheduler",
            title: "Запланированные записи",
            isExpanded: false,
            iconSymbol: "calendar.badge.clock"
        )

        let card = ThemeCardView()

        // 1. Форма добавления

        // Date Picker
        let datePicker = makeSchedulerTimeField()
        
        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.datePicker, datePicker, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Duration Field (minutes)
        let durationField = NSTextField(frame: .zero)
        durationField.placeholderString = "30"
        durationField.font = KrabEarTheme.Typography.body
        durationField.bezelStyle = .roundedBezel
        durationField.isBordered = true
        durationField.widthAnchor.constraint(equalToConstant: 50).isActive = true
        
        let durationLabel = NSTextField(labelWithString: "мин")
        durationLabel.font = KrabEarTheme.Typography.body
        durationLabel.textColor = KrabEarTheme.Colors.textSecondary
        
        let durationStack = NSStackView(views: [durationField, durationLabel])
        durationStack.orientation = .horizontal
        durationStack.spacing = 4
        durationStack.alignment = .centerY
        
        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.durationField, durationField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Label Field
        let labelField = NSTextField(frame: .zero)
        labelField.placeholderString = "Описание (опционально)"
        labelField.font = KrabEarTheme.Typography.body
        labelField.bezelStyle = .roundedBezel
        labelField.isBordered = true
        labelField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        labelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        
        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.labelField, labelField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Submit Button
        let submitButton = makeSchedulerSubmitButton()
        submitButton.setContentHuggingPriority(.required, for: .horizontal)

        // Compositing form
        let formRow1 = makeSettingRow(label: "Начало", control: datePicker)
        let formRow2 = makeSettingRow(label: "Длительность", control: durationStack)
        let formRow3 = makeSettingRow(label: "Метка", control: labelField)
        
        let submitRow = NSStackView(views: [submitButton])
        submitRow.orientation = .horizontal
        submitRow.alignment = .trailing
        submitRow.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)
        
        let formStack = NSStackView(views: [formRow1, formRow2, formRow3, submitRow])
        formStack.orientation = .vertical
        formStack.spacing = KrabEarTheme.Metrics.tight
        formStack.alignment = .leading

        let mainRow = makeSettingRow(
            label: "Новая запись",
            description: "Назначьте время и длительность для автоматического старта записи.",
            control: formStack
        )

        // Загрузочная карточка
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary

        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.sectionCard, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        card.contentStackView.addArrangedSubview(mainRow)
        card.contentStackView.addArrangedSubview(schedulerMakeSeparator())
        card.contentStackView.addArrangedSubview(loadingLabel)

        section.contentStackView.addArrangedSubview(card)

        // Первоначальная загрузка списка
        fetchAndRebuildSchedulerCard()

        return section
    }

    // MARK: - Загрузка списка

    func fetchAndRebuildSchedulerCard() {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            let schedules: [[String: Any]]
            do {
                let resp = try ipc.call(method: "list_scheduled_recordings", params: [:])
                let result = resp["result"] as? [String: Any]
                schedules = result?["schedules"] as? [[String: Any]] ?? []
            } catch {
                schedules = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.rebuildSchedulerCard(schedules: schedules)
            }
        }
    }

    @MainActor
    private func rebuildSchedulerCard(schedules: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(self, &RecordingSchedulerAssocKeys.sectionCard) as? NSView else { return }

        let isCD = card is CDSettingsCardView
        let stack = (card as? ThemeCardView)?.contentStackView ?? (card as? CDSettingsCardView)?.contentStackView
        guard let contentStack = stack else { return }
        let arrangedViews = contentStack.arrangedSubviews
        // Сохраняем первые 2 вьюхи (форму добавления и разделитель)
        for v in arrangedViews.dropFirst(2) {
            contentStack.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        let subhead = isCD ? NSTextField(labelWithString: "ОЖИДАЮЩИЕ ЗАПИСИ") : makeSubhead("ОЖИДАЮЩИЕ ЗАПИСИ")
        if isCD {
            subhead.font = KrabEarTheme.Typography.captionMedium
            subhead.textColor = KrabEarTheme.Colors.textSecondary
            subhead.isBordered = false
            subhead.drawsBackground = false
        }
        contentStack.addArrangedSubview(subhead)

        let pendingSchedules = schedules.filter { ($0["status"] as? String) == "pending" }

        if pendingSchedules.isEmpty {
            let empty = NSTextField(labelWithString: "Нет запланированных записей")
            empty.font = KrabEarTheme.Typography.caption
            empty.textColor = KrabEarTheme.Colors.textSecondary
            contentStack.addArrangedSubview(empty)
        } else {
            // Сортировка по времени (строки ISO8601 сортируются лексикографически корректно)
            let sortedSchedules = pendingSchedules.sorted { a, b in
                let t1 = a["start_time"] as? String ?? ""
                let t2 = b["start_time"] as? String ?? ""
                return t1 < t2
            }

            let dateFormatter = ISO8601DateFormatter()
            dateFormatter.formatOptions = [.withInternetDateTime]
            
            let displayFormatter = DateFormatter()
            displayFormatter.dateStyle = .medium
            displayFormatter.timeStyle = .short

            for (index, schedule) in sortedSchedules.enumerated() {
                guard let id = schedule["id"] as? String,
                      let startTimeStr = schedule["start_time"] as? String,
                      let durationSec = schedule["duration_sec"] as? Int else { continue }
                
                let label = schedule["label"] as? String ?? ""
                
                var displayTime = startTimeStr
                if let date = dateFormatter.date(from: startTimeStr) {
                    displayTime = displayFormatter.string(from: date)
                }
                
                let durationMin = durationSec / 60
                
                if isCD && index > 0 {
                    contentStack.addArrangedSubview(cdMakeSeparator())
                }
                
                let row = makeScheduleRow(id: id, displayTime: displayTime, durationMin: durationMin, label: label, isCD: isCD)
                contentStack.addArrangedSubview(row)
            }
        }
    }

    @MainActor
    private func makeScheduleRow(id: String, displayTime: String, durationMin: Int, label: String, isCD: Bool = false) -> NSView {
        let timeLabel = NSTextField(labelWithString: "\(displayTime) (\(durationMin) мин)")
        timeLabel.font = isCD ? KrabEarTheme.Typography.body : NSFont.systemFont(ofSize: 13, weight: .medium)
        timeLabel.textColor = KrabEarTheme.Colors.textPrimary
        timeLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let descLabel = NSTextField(labelWithString: label.isEmpty ? "Без названия" : label)
        descLabel.font = KrabEarTheme.Typography.caption
        descLabel.textColor = KrabEarTheme.Colors.textSecondary
        descLabel.lineBreakMode = .byTruncatingTail
        descLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        
        let textStack = NSStackView(views: [timeLabel, descLabel])
        textStack.orientation = .vertical
        textStack.alignment = .leading
        textStack.spacing = 2
        textStack.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let cancelButton: NSButton
        if isCD {
            cancelButton = ThemeSecondaryButton(title: "Отменить", target: self, action: #selector(onCancelScheduledRecording(_:)))
            cancelButton.identifier = NSUserInterfaceItemIdentifier(id)
            cancelButton.setContentHuggingPriority(.required, for: .horizontal)
        } else {
            cancelButton = NSButton(title: "Отменить", target: self, action: #selector(onCancelScheduledRecording(_:)))
            cancelButton.bezelStyle = .inline
            cancelButton.identifier = NSUserInterfaceItemIdentifier(id)
            cancelButton.setContentHuggingPriority(.required, for: .horizontal)
        }

        let row = NSStackView(views: [textStack, cancelButton])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.standard
        row.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)
        return row
    }

    // MARK: - Обработчики действий

    @objc private func onScheduleRecording(_ sender: Any) {
        guard let datePicker = objc_getAssociatedObject(self, &RecordingSchedulerAssocKeys.datePicker) as? NSDatePicker,
              let durationField = objc_getAssociatedObject(self, &RecordingSchedulerAssocKeys.durationField) as? NSTextField,
              let labelField = objc_getAssociatedObject(self, &RecordingSchedulerAssocKeys.labelField) as? NSTextField else { return }

        let date = datePicker.dateValue
        
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let startTimeStr = formatter.string(from: date)
        
        let durationRaw = durationField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let durationMin = Int(durationRaw) ?? 30 // 30 минут по умолчанию
        let durationSec = durationMin * 60
        
        let label = labelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let resp = try ipc.call(method: "schedule_recording", params: [
                    "start_time": startTimeStr,
                    "duration_sec": durationSec,
                    "label": label
                ])
                
                if let ok = resp["ok"] as? Bool, !ok {
                    let errorMessage = (resp["error"] as? [String: Any])?["message"] as? String ?? "Неизвестная ошибка"
                    DispatchQueue.main.async {
                        BackendToast.shared.show("Ошибка планирования: \(errorMessage)", duration: 4.0)
                    }
                    return
                }
                
                DispatchQueue.main.async {
                    // Успех, сброс полей
                    durationField.stringValue = ""
                    labelField.stringValue = ""
                    datePicker.dateValue = Date().addingTimeInterval(3600)
                    BackendToast.shared.show("Запись запланирована", duration: 2.0)
                }
                self.fetchAndRebuildSchedulerCard()
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Сбой планирования записи: \(error.localizedDescription)", duration: 4.0)
                }
            }
        }
    }

    @objc private func onCancelScheduledRecording(_ sender: NSButton) {
        guard let scheduleId = sender.identifier?.rawValue, !scheduleId.isEmpty else { return }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let resp = try ipc.call(method: "cancel_scheduled_recording", params: ["schedule_id": scheduleId])
                
                // Проверяем cancelled в result или корне
                let cancelledInRoot = resp["cancelled"] as? Bool
                let resultObj = resp["result"] as? [String: Any]
                let cancelledInResult = resultObj?["cancelled"] as? Bool
                
                if let isCancelled = cancelledInRoot ?? cancelledInResult, isCancelled {
                    DispatchQueue.main.async {
                        BackendToast.shared.show("Запланированная запись отменена", duration: 2.0)
                    }
                } else {
                    let errorMessage = (resp["error"] as? [String: Any])?["message"] as? String ?? "Не удалось отменить запись"
                    DispatchQueue.main.async {
                        BackendToast.shared.show("Ошибка отмены: \(errorMessage)", duration: 4.0)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Сбой отмены записи: \(error.localizedDescription)", duration: 4.0)
                }
            }
            self.fetchAndRebuildSchedulerCard()
        }
    }

    @MainActor
    private func schedulerMakeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }

    // MARK: - CD Builders

    /// Строит секцию «Запланированные записи» в компактном стиле Claude Design.
    @MainActor
    func cdBuildRecordingSchedulerSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_recording_scheduler",
            title: "Запланированные записи",
            isExpanded: false
        )
        let card = CDSettingsCardView()
        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.sectionCard, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Поля формы с регистрацией в associated-keys для CD-варианта
        let datePicker = makeSchedulerTimeField()
        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.datePicker, datePicker, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let durationField = makeSchedulerDurationField()
        durationField.font = KrabEarTheme.Typography.body
        durationField.bezelStyle = .roundedBezel
        durationField.isBordered = true
        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.durationField, durationField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let durationLabel = NSTextField(labelWithString: "мин")
        durationLabel.font = KrabEarTheme.Typography.body
        durationLabel.textColor = KrabEarTheme.Colors.textSecondary

        let durationStack = NSStackView(views: [durationField, durationLabel])
        durationStack.orientation = .horizontal
        durationStack.spacing = 4
        durationStack.alignment = .centerY

        let descField = makeSchedulerDescField()
        descField.font = KrabEarTheme.Typography.body
        descField.bezelStyle = .roundedBezel
        descField.isBordered = true
        objc_setAssociatedObject(self, &RecordingSchedulerAssocKeys.labelField, descField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let submitButton = makeSchedulerSubmitButton()
        submitButton.setContentHuggingPriority(.required, for: .horizontal)

        let formRow1 = cdMakeRow(label: "Начало", control: datePicker)
        let formRow2 = cdMakeRow(label: "Длительность", control: durationStack)
        let formRow3 = cdMakeRow(label: "Метка", control: descField)

        let submitRow = NSStackView(views: [submitButton])
        submitRow.orientation = .horizontal
        submitRow.alignment = .trailing
        submitRow.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)

        let formStack = NSStackView(views: [
            formRow1,
            cdMakeSeparator(),
            formRow2,
            cdMakeSeparator(),
            formRow3,
            cdMakeSeparator(),
            submitRow,
        ])
        formStack.orientation = .vertical
        formStack.spacing = KrabEarTheme.Metrics.tight
        formStack.alignment = .leading

        card.contentStackView.addArrangedSubview(formStack)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())

        section.contentStackView.addArrangedSubview(card)

        // Загрузка списка запланированных записей
        fetchAndRebuildSchedulerCard()

        return section
    }
}
