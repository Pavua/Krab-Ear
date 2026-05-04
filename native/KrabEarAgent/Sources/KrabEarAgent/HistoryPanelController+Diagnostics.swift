import AppKit

extension HistoryPanelController {
    // MARK: - Diagnostics & Metrics handlers
    //
    // Все handlers следуют шаблону: synchronous IPC call вынесен на
    // DispatchQueue.global(qos: .userInitiated), UI update строго на main.
    // Без этого backend под нагрузкой блокирует main thread → AppHang ≥2000ms
    // (Sentry KRAB-EAR-AGENT-3, 19 events 2026-04-24). Образец паттерна — +Analytics.swift.

    @objc func onDiagnostics() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
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
        nonisolated(unsafe) let ipcClient = self.ipcClient
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
        nonisolated(unsafe) let ipcClient = self.ipcClient
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
        nonisolated(unsafe) let ipcClient = self.ipcClient
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
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "apply_profile_preset", params: ["preset": presetName]),
                  let result = response["result"] as? [String: Any],
                  result["applied"] as? Bool == true else {
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
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "list_profile_presets", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let presets = result["presets"] as? [[String: Any]] else { return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.profilePresetSelector.removeAllItems()
                for preset in presets {
                    if let name = preset["name"] as? String {
                        let label = (preset["label"] as? String) ?? name
                        self.profilePresetSelector.addItem(withTitle: label)
                        self.profilePresetSelector.lastItem?.representedObject = name
                    }
                }
            }
        }
    }

    func loadAudioDevices() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "get_audio_devices", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let devices = result["devices"] as? [[String: Any]] else { return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.audioDeviceSelector.removeAllItems()
                self.audioDeviceSelector.addItem(withTitle: "По умолчанию (системный)")
                for device in devices {
                    if let name = device["name"] as? String {
                        self.audioDeviceSelector.addItem(withTitle: name)
                    }
                }
            }
        }
    }

    @objc func onTestMicrophone() {
        micTestResultLabel.stringValue = "Тестирование..."
        micTestResultLabel.textColor = KrabEarTheme.Colors.textSecondary
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let response = try? ipcClient.call(method: "test_microphone", params: ["duration_sec": 2]),
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
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.micTestResultLabel.stringValue = String(format: "RMS: %.3f | Peak: %.3f | %@", rms, peak, status)
                self.micTestResultLabel.textColor = rms > 0.01 ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.warning
            }
        }
    }

    // MARK: - Clipboard History handlers

    @objc func onClipboardHistory() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
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
        nonisolated(unsafe) let ipcClient = self.ipcClient
        nonisolated(unsafe) let notificationService = self.notificationService
        DispatchQueue.global(qos: .userInitiated).async {
            guard let response = try? ipcClient.call(method: "get_clipboard_history", params: [:]),
                  let result = response["result"] as? [String: Any],
                  let clipItems = result["items"] as? [[String: Any]],
                  let firstItem = clipItems.first,
                  let itemId = firstItem["id"] as? String else {
                DispatchQueue.main.async {
                    notificationService.notify(title: "Krab Ear", body: "Нет элементов для вставки")
                }
                return
            }
            guard let _ = try? ipcClient.call(method: "repaste_item", params: ["id": itemId]) else {
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
