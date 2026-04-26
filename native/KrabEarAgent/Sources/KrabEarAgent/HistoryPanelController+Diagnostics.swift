import AppKit

extension HistoryPanelController {
    // MARK: - Diagnostics & Metrics handlers

    @objc func onDiagnostics() {
        guard let response = try? ipcClient.call(method: "get_diagnostics", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить диагностику")
            return
        }
        showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Диагностика"))
    }

    @objc func onMetrics() {
        guard let response = try? ipcClient.call(method: "get_metrics_dashboard", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить метрики")
            return
        }
        showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Метрики"))
    }

    @objc func onRecordingStats() {
        guard let response = try? ipcClient.call(method: "get_recording_stats", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить статистику")
            return
        }
        showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Статистика записей"))
    }

    @objc func onStorageInfo() {
        guard let response = try? ipcClient.call(method: "get_storage_info", params: [:]),
              let result = response["result"] as? [String: Any] else {
            showDiagnosticsOutput("Ошибка: не удалось получить информацию о хранилище")
            return
        }
        showDiagnosticsOutput(HistoryPanelController.formatNestedResult(result, title: "Хранилище"))
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

    /// Pure helper — форматирует вложенный dict в plain-text для diagnostics output.
    /// `nonisolated static`: не trogает self, тестируем без instance.
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
        guard let response = try? ipcClient.call(method: "apply_profile_preset", params: ["preset": presetName]),
              let result = response["result"] as? [String: Any],
              result["applied"] as? Bool == true else {
            showDiagnosticsOutput("Ошибка: не удалось применить профиль '\(selectedTitle)'")
            return
        }
        showDiagnosticsOutput("Профиль '\(selectedTitle)' применён.")
        syncSettingsControls()
    }

    func loadProfilePresets() {
        guard let response = try? ipcClient.call(method: "list_profile_presets", params: [:]),
              let result = response["result"] as? [String: Any],
              let presets = result["presets"] as? [[String: Any]] else { return }
        profilePresetSelector.removeAllItems()
        for preset in presets {
            if let name = preset["name"] as? String {
                let label = (preset["label"] as? String) ?? name
                profilePresetSelector.addItem(withTitle: label)
                profilePresetSelector.lastItem?.representedObject = name
            }
        }
    }

    func loadAudioDevices() {
        guard let response = try? ipcClient.call(method: "get_audio_devices", params: [:]),
              let result = response["result"] as? [String: Any],
              let devices = result["devices"] as? [[String: Any]] else { return }
        audioDeviceSelector.removeAllItems()
        audioDeviceSelector.addItem(withTitle: "По умолчанию (системный)")
        for device in devices {
            if let name = device["name"] as? String {
                audioDeviceSelector.addItem(withTitle: name)
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
        guard let response = try? ipcClient.call(method: "get_clipboard_history", params: [:]),
              let result = response["result"] as? [String: Any],
              let items = result["items"] as? [[String: Any]] else {
            showDiagnosticsOutput("Буфер обмена пуст")
            return
        }
        var lines: [String] = ["=== Буфер обмена (последние \(items.count)) ==="]
        for (i, item) in items.enumerated() {
            let text = String((item["text"] as? String ?? "").prefix(80))
            let ts = item["ts"] as? String ?? ""
            lines.append("\(i + 1). [\(ts)] \(text)")
        }
        showDiagnosticsOutput(lines.joined(separator: "\n"))
    }

    @objc func onRepasteItem() {
        guard let response = try? ipcClient.call(method: "get_clipboard_history", params: [:]),
              let result = response["result"] as? [String: Any],
              let clipItems = result["items"] as? [[String: Any]],
              let firstItem = clipItems.first,
              let itemId = firstItem["id"] as? String else {
            notificationService.notify(title: "Krab Ear", body: "Нет элементов для вставки")
            return
        }
        guard let _ = try? ipcClient.call(method: "repaste_item", params: ["id": itemId]) else {
            notificationService.notify(title: "Krab Ear", body: "Ошибка повторной вставки")
            return
        }
        notificationService.notify(title: "Krab Ear", body: "Элемент вставлен повторно")
    }
}
