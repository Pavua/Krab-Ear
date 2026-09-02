import AppKit

extension HistoryPanelController {
    // MARK: - Diagnostics & Metrics handlers
    //
    // Все handlers следуют шаблону: synchronous IPC call вынесен на
    // DispatchQueue.global(qos: .userInitiated), UI update строго на main.
    // Без этого backend под нагрузкой блокирует main thread → AppHang ≥2000ms
    // (Sentry KRAB-EAR-AGENT-3, 19 events 2026-04-24). Образец паттерна — +Analytics.swift.

    @objc func onDiagnostics() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_diagnostics", params: [:]),
                  let result = response["result"] as? [String: Any] else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: не удалось получить диагностику")
                }
                return
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Диагностика"))
            }
        }
    }

    @objc func onMetrics() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_metrics_dashboard", params: [:]),
                  let result = response["result"] as? [String: Any] else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: не удалось получить метрики")
                }
                return
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Метрики"))
            }
        }
    }

    @objc func onRecordingStats() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_recording_stats", params: [:]),
                  let result = response["result"] as? [String: Any] else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: не удалось получить статистику")
                }
                return
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Статистика записей"))
            }
        }
    }

    @objc func onStorageInfo() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_storage_info", params: [:]),
                  let result = response["result"] as? [String: Any] else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: не удалось получить информацию о хранилище")
                }
                return
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Хранилище"))
            }
        }
    }

    func showDiagnosticsOutput(_ text: String) {
        diagnosticsOutputView.string = text
        diagnosticsSection?.setExpanded(true, animated: true)
        // Switch to Dictation tab if not already there
        if mainTabView.selectedTabViewItem?.identifier as? String != PanelTab.dictation.rawValue {
            mainTabView.selectTabViewItem(at: 0)
            tabSelector?.setSelected(true, forSegment: 0)
        }
    }

    nonisolated static func formatNestedResult(_ result: [String: Any], title: String) -> String {
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

    // MARK: - Profile Presets & Audio Devices handlers

    @objc func onApplyProfile() {
        let selectedTitle = profilePresetSelector.titleOfSelectedItem ?? ""
        guard !selectedTitle.isEmpty, selectedTitle != "Загрузка..." else { return }
        let presetName = (profilePresetSelector.selectedItem?.representedObject as? String) ?? selectedTitle.lowercased()
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "apply_profile_preset", params: ["profile": presetName]),
                  response["ok"] as? Bool == true else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: не удалось применить профиль '\(selectedTitle)'")
                }
                return
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.showDiagnosticsOutput("Профиль '\(selectedTitle)' применён.")
                self.syncSettingsControls()
            }
        }
    }

    func loadProfilePresets() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "list_profile_presets", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let presets = result["presets"] as? [[String: Any]] else { return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.profilePresetSelector.removeAllItems()
                for preset in presets {
                    if let name = preset["name"] as? String {
                        let label = (preset["description"] as? String) ?? name
                        self.profilePresetSelector.addItem(withTitle: label)
                        self.profilePresetSelector.lastItem?.representedObject = name
                    }
                }
            }
        }
    }

    func loadAudioDevices() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_audio_devices", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let devices = result["devices"] as? [[String: Any]] else { return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.audioDeviceSelector.target = self
                self.audioDeviceSelector.action = #selector(self.onAudioDeviceChanged)
                self.audioDeviceSelector.removeAllItems()
                self.audioDeviceSelector.addItem(withTitle: Self.defaultAudioDeviceTitle)
                for device in devices {
                    if let name = device["name"] as? String {
                        self.audioDeviceSelector.addItem(withTitle: name)
                    }
                }
                // Возвращаем пикер на сохранённое устройство: без этого он после
                // каждой перезагрузки списка показывает «По умолчанию» при живой
                // настройке, и владелец считает, что выбор потерян.
                let saved = self.settingsProvider().selectedInputDevice
                if !saved.isEmpty, self.audioDeviceSelector.itemTitles.contains(saved) {
                    self.audioDeviceSelector.selectItem(withTitle: saved)
                } else {
                    self.audioDeviceSelector.selectItem(at: 0)
                }
            }
        }
    }

    /// Заголовок первого пункта — «системное по умолчанию». Выбор этого пункта
    /// кодируется пустой строкой: ровно её `RecordingCoreService` трактует как
    /// «устройство не задано» и оставляет системный вход.
    static let defaultAudioDeviceTitle = "По умолчанию (системный)"

    /// 🔴 До 02.09.2026 пикер микрофона был украшением: заполнялся из
    /// `get_audio_devices` и не имел ни target/action, ни читателя значения.
    /// Обратная половина при этом работала — `RecordingCoreService` перед
    /// стартом записи читает `selected_input_device` и зовёт
    /// `AudioRecorder.set_device()`. То есть защита W1327 F2 ждала входа,
    /// которого никто не подавал.
    @objc func onAudioDeviceChanged() {
        guard !isSyncingSettings else { return }
        let title = audioDeviceSelector.titleOfSelectedItem ?? ""
        let value = (audioDeviceSelector.indexOfSelectedItem <= 0) ? "" : title
        applySettingsPatch(["selected_input_device": value])
    }

    @objc func onTestMicrophone() {
        micTestResultLabel.stringValue = "Тестирование..."
        micTestResultLabel.textColor = KrabEarTheme.Colors.textSecondary
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            // check_mic_noise — надмножество test_microphone: те же rms/peak ПЛЮС
            // профиль фонового шума (тип/SNR/пригодность для STT) до записи.
            guard let response = try? ipcClient.call(method: "check_mic_noise", params: ["duration_sec": 2]),
                  let result = response["result"] as? [String: Any] else {
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.micTestResultLabel.stringValue = "Ошибка теста"
                    self.micTestResultLabel.textColor = KrabEarTheme.Colors.error
                }
                return
            }
            let rms = result["rms"] as? Double ?? 0
            let peak = result["peak"] as? Double ?? 0
            let status = rms > 0.01 ? "OK" : "Тихо"
            // Многострочный отчёт о шуме (форматирование не трогает UI → можно на global).
            let noiseReport = HistoryPanelController.formatMicNoiseReport(result, rms: rms, peak: peak, status: status)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.micTestResultLabel.stringValue = String(format: "RMS: %.3f | Peak: %.3f | %@", rms, peak, status)
                self.micTestResultLabel.textColor = rms > 0.01 ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.warning
                if let noiseReport = noiseReport {
                    self.showDiagnosticsOutput(noiseReport)
                }
            }
        }
    }

    /// Собирает многострочный отчёт «Проверка микрофона» из ответа check_mic_noise.
    /// Возвращает nil, если профиль шума отсутствует (нечего показывать).
    nonisolated static func formatMicNoiseReport(
        _ result: [String: Any], rms: Double, peak: Double, status: String
    ) -> String? {
        guard let noise = result["noise"] as? [String: Any] else { return nil }

        let noiseTypeRU: [String: String] = [
            "quiet": "тихо", "office": "офис", "street": "улица",
            "music": "музыка", "crowd": "толпа",
        ]
        let freqRU: [String: String] = [
            "low_frequency": "низкочастотный", "broadband": "широкополосный",
            "high_frequency": "высокочастотный",
        ]

        let noiseType = noise["noise_type"] as? String ?? ""
        let noiseLevel = noise["noise_level_db"] as? Double ?? 0
        let snr = noise["snr_db"] as? Double ?? 0
        let freq = noise["frequency_profile"] as? String ?? ""
        let suitable = noise["suitable_for_stt"] as? Bool ?? false
        let recommendations = noise["recommendations"] as? [String] ?? []

        var lines: [String] = ["=== Проверка микрофона ==="]
        lines.append(String(format: "RMS: %.3f | Peak: %.3f | %@", rms, peak, status))
        lines.append("Тип шума: \(noiseTypeRU[noiseType] ?? noiseType)")
        lines.append(String(format: "Уровень шума: %.1f dBFS", noiseLevel))
        lines.append(String(format: "SNR: %.1f dB", snr))
        lines.append("Профиль частот: \(freqRU[freq] ?? freq)")
        lines.append("Пригодно для распознавания: \(suitable ? "Да ✓" : "Нет ✗")")
        if !recommendations.isEmpty {
            lines.append("Рекомендации:")
            for rec in recommendations {
                lines.append(" • \(rec)")
            }
        }
        return lines.joined(separator: "\n")
    }

    // MARK: - Clipboard History handlers

    @objc func onClipboardHistory() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_clipboard_history", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let items = result["items"] as? [[String: Any]] else {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Буфер обмена пуст")
                }
                return
            }
            // Форматирование можно делать на global — оно не трогает UI.
            var lines: [String] = ["=== Буфер обмена (последние \(items.count)) ==="]
            for (i, item) in items.enumerated() {
                let text = String((item["text"] as? String ?? "").prefix(80))
                let ts = item["ts"] as? String ?? ""
                lines.append("\(i + 1). [\(ts)] \(text)")
            }
            let output = lines.joined(separator: "\n")
            DispatchQueue.main.async {
                self?.showDiagnosticsOutput(output)
            }
        }
    }

    @objc func onRepasteItem() {
        let ipcClient = self.ipcClient
        let notificationService = self.notificationService
        DispatchQueue.global(qos: .userInitiated).async {
            guard let response = try? ipcClient.call(method: "get_clipboard_history", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let clipItems = result["items"] as? [[String: Any]],
                  let firstItem = clipItems.first,
                  let itemId = firstItem["history_id"] as? String else {
                DispatchQueue.main.async {
                    notificationService.notify(title: "Krab Ear", body: "Нет элементов для вставки")
                }
                return
            }
            guard let _ = try? ipcClient.call(method: "repaste_item", params: ["history_id": itemId]) else {
                DispatchQueue.main.async {
                    notificationService.notify(title: "Krab Ear", body: "Ошибка повторной вставки")
                }
                return
            }
            DispatchQueue.main.async {
                notificationService.notify(title: "Krab Ear", body: "Элемент вставлен повторно")
            }
        }
    }

    // MARK: - Diagnostics Tab (Phase B.2 F6)
    //
    // DiagnosticsTabViewController — полноценная вкладка с фильтрами severity/component
    // и NSTableView журнала ошибок. Добавляется в mainTabView следующей за «Автозвонки».

    nonisolated(unsafe) static var diagnosticsTabVCKey: UInt8 = 0

    /// Контроллер вкладки «Диагностика». Создаётся один раз в setupDiagnosticsTab().
    var diagnosticsTabVC: DiagnosticsTabViewController? {
        get { objc_getAssociatedObject(self, &HistoryPanelController.diagnosticsTabVCKey) as? DiagnosticsTabViewController }
        set { objc_setAssociatedObject(self, &HistoryPanelController.diagnosticsTabVCKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    /// Создать и встроить DiagnosticsTabViewController в NSTabView.
    /// Вызывается из setupUI() после setupCallAutomationTab().
    func setupDiagnosticsTab(contentView: NSView) {
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)
        diagnosticsTabVC = vc

        vc.view.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(vc.view)
        NSLayoutConstraint.activate([
            vc.view.topAnchor.constraint(equalTo: contentView.topAnchor),
            vc.view.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            vc.view.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            vc.view.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
        ])

        // Trigger viewDidLoad (loads view hierarchy + initial refresh)
        _ = vc.view
    }

    /// Индекс сегмента «Диагностика» в tabSelector (после «Автозвонки», index 5).
    var diagnosticsTabSegmentIndex: Int { 5 }
}
