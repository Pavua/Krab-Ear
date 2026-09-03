/*
 HistoryPanelController+STTModelMemory — управление жизнью STT-модели в памяти.

 Запрос владельца 02.09.2026: «желательно, чтобы я мог в интерфейсе выбирать
 сам, сколько будет оставаться в памяти, когда его выгрузить, а когда
 загрузить». До этого управлять было нечем: загрузка происходила лениво при
 первой диктовке (замер показал 27 секунд ожидания первых слов), выгрузки не
 существовало вовсе — память освобождал только перезапуск бэкенда.

 Секция собирает четыре рычага в одном месте:
   - устройство инференса (mps / cpu);
   - прогрев при старте бэкенда — убирает ожидание из первой диктовки;
   - срок простоя до выгрузки;
   - ручные «загрузить сейчас» / «выгрузить сейчас».

 🔴 Про срок простоя честная оговорка в самом интерфейсе: пока
 `memory_conductor_enforce_gigaam` выключен, дирижёр памяти только пишет в лог
 «would evict» и ничего не выгружает — срок остаётся рекомендацией. Поэтому
 рядом стоит переключатель принудительной выгрузки.
 */

import AppKit

private enum STTMemoryAssocKeys {
    nonisolated(unsafe) static var devicePicker: UInt8 = 0
    nonisolated(unsafe) static var warmupToggle: UInt8 = 0
    nonisolated(unsafe) static var idleStepper: UInt8 = 0
    nonisolated(unsafe) static var idleLabel: UInt8 = 0
    nonisolated(unsafe) static var enforceToggle: UInt8 = 0
    nonisolated(unsafe) static var statusLabel: UInt8 = 0
}

extension HistoryPanelController {

    private var sttDevicePicker: NSPopUpButton? {
        objc_getAssociatedObject(self, &STTMemoryAssocKeys.devicePicker) as? NSPopUpButton
    }
    private var sttIdleStepper: NSStepper? {
        objc_getAssociatedObject(self, &STTMemoryAssocKeys.idleStepper) as? NSStepper
    }
    private var sttIdleLabel: NSTextField? {
        objc_getAssociatedObject(self, &STTMemoryAssocKeys.idleLabel) as? NSTextField
    }
    private var sttMemoryStatusLabel: NSTextField? {
        objc_getAssociatedObject(self, &STTMemoryAssocKeys.statusLabel) as? NSTextField
    }

    // MARK: - Helpers

    @MainActor
    func makeSTTDevicePicker() -> NSPopUpButton {
        if let existing = sttDevicePicker { return existing }
        let picker = NSPopUpButton(frame: .zero, pullsDown: false)
        picker.addItems(withTitles: ["GPU (mps)", "Процессор (cpu)"])
        picker.target = self
        picker.action = #selector(onSTTDeviceChanged)
        objc_setAssociatedObject(self, &STTMemoryAssocKeys.devicePicker, picker, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let settings = settingsProvider()
        picker.selectItem(at: settings.sttGigaamDevice == "cpu" ? 1 : 0)
        return picker
    }

    @MainActor
    func makeSTTWarmupToggle() -> NSButton {
        if let existing = objc_getAssociatedObject(self, &STTMemoryAssocKeys.warmupToggle) as? NSButton { return existing }
        let toggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onSTTWarmupOnStartupChanged))
        toggle.state = settingsProvider().sttWarmupOnStartup ? .on : .off
        objc_setAssociatedObject(self, &STTMemoryAssocKeys.warmupToggle, toggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return toggle
    }

    @MainActor
    func makeSTTIdleStepperAndLabel() -> (NSTextField, NSStepper) {
        if let label = sttIdleLabel, let stepper = sttIdleStepper { return (label, stepper) }
        let settings = settingsProvider()
        let stepper = NSStepper()
        stepper.minValue = 1; stepper.maxValue = 1440; stepper.increment = 5
        stepper.integerValue = max(1, Int((settings.gigaamIdleUnloadSec / 60.0).rounded()))
        stepper.autorepeat = true; stepper.target = self; stepper.action = #selector(onSTTIdleUnloadChanged)
        objc_setAssociatedObject(self, &STTMemoryAssocKeys.idleStepper, stepper, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let label = NSTextField(labelWithString: "\(stepper.integerValue) мин")
        label.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        label.textColor = KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(self, &STTMemoryAssocKeys.idleLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return (label, stepper)
    }

    @MainActor
    func makeSTTEnforceToggle() -> NSButton {
        if let existing = objc_getAssociatedObject(self, &STTMemoryAssocKeys.enforceToggle) as? NSButton { return existing }
        let toggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onSTTEnforceUnloadChanged))
        toggle.state = settingsProvider().memoryConductorEnforceGigaam ? .on : .off
        objc_setAssociatedObject(self, &STTMemoryAssocKeys.enforceToggle, toggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return toggle
    }

    @MainActor
    func makeSTTButtonsAndLabel() -> (ThemeSecondaryButton, ThemeSecondaryButton, NSTextField) {
        let loadBtn = ThemeSecondaryButton(title: "Загрузить сейчас", target: self, action: #selector(onSTTLoadNow))
        let unloadBtn = ThemeSecondaryButton(title: "Выгрузить сейчас", target: self, action: #selector(onSTTUnloadNow))
        let lbl = NSTextField(labelWithString: "")
        lbl.font = KrabEarTheme.Typography.caption
        lbl.textColor = KrabEarTheme.Colors.textSecondary
        lbl.lineBreakMode = .byTruncatingTail
        objc_setAssociatedObject(self, &STTMemoryAssocKeys.statusLabel, lbl, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return (loadBtn, unloadBtn, lbl)
    }

    func buildSTTModelMemorySection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "stt_model_memory",
            title: "Модель STT в памяти",
            isExpanded: false,
            iconSymbol: "memorychip"
        )
        let card = ThemeCardView()
        let settings = settingsProvider()

        // --- устройство инференса ---
        let devicePicker = makeSTTDevicePicker()
        card.contentStackView.addArrangedSubview(makeSettingRow(
            label: "Устройство распознавания",
            description: "Замер 02.09.2026 на 25-секундном аудио: GPU 1.31 с против 1.42 с на процессоре, текст одинаковый. Разница невелика — при нестабильности GPU переключение на процессор безопасно.",
            control: devicePicker
        ))

        // --- прогрев при старте ---
        let warmupToggle = makeSTTWarmupToggle()
        warmupToggle.state = settings.sttWarmupOnStartup ? .on : .off
        objc_setAssociatedObject(
            self, &STTMemoryAssocKeys.warmupToggle, warmupToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        card.contentStackView.addArrangedSubview(makeSettingRow(
            label: "Загружать модель при старте",
            description: "Без прогрева модель грузится лениво — первая диктовка ждёт её загрузку (замер: 27 секунд до первых слов). С прогревом ожидание уходит в старт бэкенда.",
            control: warmupToggle
        ))

        // --- срок простоя до выгрузки ---
        let (idleLabel, idleStepper) = makeSTTIdleStepperAndLabel()
        let idleStack = NSStackView(views: [idleLabel, idleStepper])
        idleStack.orientation = .horizontal
        idleStack.alignment = .centerY
        idleStack.spacing = KrabEarTheme.Metrics.tight
        card.contentStackView.addArrangedSubview(makeSettingRow(
            label: "Выгружать после простоя",
            description: "Сколько модель ждёт следующей диктовки, прежде чем освободить память.",
            control: idleStack
        ))

        // --- принудительная выгрузка ---
        let enforceToggle = makeSTTEnforceToggle()
        enforceToggle.state = settings.memoryConductorEnforceGigaam ? .on : .off
        objc_setAssociatedObject(
            self, &STTMemoryAssocKeys.enforceToggle, enforceToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        card.contentStackView.addArrangedSubview(makeSettingRow(
            label: "Действительно выгружать по простою",
            description: "Пока выключено, дирижёр памяти только пишет в лог «would evict» и ничего не освобождает — срок выше остаётся рекомендацией.",
            control: enforceToggle
        ))

        // --- ручные кнопки ---
        let (loadButton, unloadButton, statusLabel) = makeSTTButtonsAndLabel()
        let buttonsStack = NSStackView(views: [loadButton, unloadButton, statusLabel])
        buttonsStack.orientation = .horizontal
        buttonsStack.alignment = .centerY
        buttonsStack.spacing = KrabEarTheme.Metrics.standard
        card.contentStackView.addArrangedSubview(buttonsStack)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Обработчики

    @objc func onSTTDeviceChanged() {
        guard !isSyncingSettings, let picker = sttDevicePicker else { return }
        applySettingsPatch(["stt_gigaam_device": picker.indexOfSelectedItem == 1 ? "cpu" : "mps"])
        sttMemoryStatusLabel?.stringValue = "Применится при следующей загрузке модели"
    }

    @objc func onSTTWarmupOnStartupChanged() {
        guard !isSyncingSettings,
              let toggle = objc_getAssociatedObject(self, &STTMemoryAssocKeys.warmupToggle) as? NSButton
        else { return }
        applySettingsPatch(["stt_warmup_on_startup": toggle.state == .on])
    }

    @objc func onSTTIdleUnloadChanged() {
        guard !isSyncingSettings, let stepper = sttIdleStepper else { return }
        let minutes = max(1, stepper.integerValue)
        sttIdleLabel?.stringValue = "\(minutes) мин"
        applySettingsPatch(["gigaam_idle_unload_sec": Double(minutes) * 60.0])
    }

    @objc func onSTTEnforceUnloadChanged() {
        guard !isSyncingSettings,
              let toggle = objc_getAssociatedObject(self, &STTMemoryAssocKeys.enforceToggle) as? NSButton
        else { return }
        applySettingsPatch(["memory_conductor_enforce_gigaam": toggle.state == .on])
    }

    /// Загрузка блокирует IPC-поток бэкенда на время инференса-прогрева, поэтому
    /// строго off-main (AGENT-3: синхронный вызов на главном потоке = AppHang).
    @objc func onSTTLoadNow() {
        sttMemoryStatusLabel?.stringValue = "Загружаю…"
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let response = try? ipc.call(method: "warmup_stt", params: [:])
            let result = response?["result"] as? [String: Any]
            let loaded = (result?["loaded"] as? Bool) ?? false
            let latency = (result?["latency_ms"] as? Int) ?? 0
            let error = result?["error"] as? String
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.sttMemoryStatusLabel?.stringValue = loaded
                    ? "Загружена за \(latency) мс"
                    : "Не загрузилась: \(error ?? "нет ответа бэкенда")"
            }
        }
    }

    @objc func onSTTUnloadNow() {
        sttMemoryStatusLabel?.stringValue = "Выгружаю…"
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let response = try? ipc.call(method: "unload_stt_model", params: [:])
            let result = response?["result"] as? [String: Any]
            let unloaded = (result?["unloaded"] as? Bool) ?? false
            let error = result?["error"] as? String
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.sttMemoryStatusLabel?.stringValue = unloaded
                    ? "Выгружена — поднимется при следующей диктовке"
                    : "Не выгрузилась: \(error ?? "нет ответа бэкенда")"
            }
        }
    }

    // MARK: - CD Builders

    /// Строит секцию «Модель STT в памяти» в компактном стиле Claude Design.
    @MainActor
    func cdBuildSTTModelMemorySection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_stt_model_memory",
            title: "Модель STT в памяти",
            isExpanded: false
        )
        let card = CDSettingsCardView()
        let settings = settingsProvider()

        // 1. Устройство распознавания
        let devicePicker = makeSTTDevicePicker()
        let deviceRow = cdMakeRow(label: "Устройство распознавания", control: devicePicker)

        // 2. Прогрев при старте бэкенда
        let warmupToggle = makeSTTWarmupToggle()
        warmupToggle.setButtonType(.switch)
        warmupToggle.title = ""
        warmupToggle.state = settings.sttWarmupOnStartup ? .on : .off
        let warmupRow = cdMakeRow(label: "Загружать модель при старте", control: warmupToggle)

        // 3. Срок простоя до выгрузки
        let (idleLabel, idleStepper) = makeSTTIdleStepperAndLabel()
        let idleStack = NSStackView(views: [idleLabel, idleStepper])
        idleStack.orientation = .horizontal
        idleStack.alignment = .centerY
        idleStack.spacing = KrabEarTheme.Metrics.tight
        let idleRow = cdMakeRow(label: "Выгружать после простоя", control: idleStack)

        // 4. Принудительная выгрузка по простою
        let enforceToggle = makeSTTEnforceToggle()
        enforceToggle.setButtonType(.switch)
        enforceToggle.title = ""
        enforceToggle.state = settings.memoryConductorEnforceGigaam ? .on : .off
        let enforceRow = cdMakeRow(label: "Действительно выгружать по простою", control: enforceToggle)

        // 5. Кнопки ручного управления и статус
        let (loadButton, unloadButton, statusLabel) = makeSTTButtonsAndLabel()
        let buttonsStack = NSStackView(views: [loadButton, unloadButton, statusLabel])
        buttonsStack.orientation = .horizontal
        buttonsStack.alignment = .centerY
        buttonsStack.spacing = KrabEarTheme.Metrics.standard
        buttonsStack.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)

        card.contentStackView.addArrangedSubview(deviceRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(warmupRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(idleRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(enforceRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(buttonsStack)

        section.contentStackView.addArrangedSubview(card)
        return section
    }
}
