/*
 Расширение HistoryPanelController: Live Translation и Realtime Preview.
 Методы для пресета Live Translation, polling realtime-превью,
 форматирования длительности и переключения вкладок.
*/

import AppKit

extension HistoryPanelController {

    @objc func onEnableLiveTranslationPreset() {
        applySettingsPatch([
            "translation_mode": "auto",
            "translation_style": "chat",
            "translate_and_paste": false,
            "realtime_preview_enabled": true,
            "network_mode": "offline_default",
        ])
        showInfoAlert(
            title: "Live Translation",
            body: "Включен пресет: auto-перевод, чат-стиль, realtime preview, вставка оригинала."
        )
    }

    @objc func onTabSelectorChanged() {
        let index = tabSelector.selectedSegment
        guard index >= 0, index < mainTabView.numberOfTabViewItems else { return }
        // Disable implicit animation при переключении табов —
        // уменьшает мерцание NSVisualEffectView (Liquid Glass) карточек,
        // которые иначе re-render во время анимации.
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0
            ctx.allowsImplicitAnimation = false
            mainTabView.selectTabViewItem(at: index)
        }
    }

    func tabView(_ tabView: NSTabView, didSelect tabViewItem: NSTabViewItem?) {
        guard tabView == mainTabView else { return }
        guard !isSyncingTabs, !isSyncingSettings else { return }
        if let item = tabViewItem {
            tabSelector.selectedSegment = mainTabView.indexOfTabViewItem(item)
        }
        let raw = String(describing: tabViewItem?.identifier ?? PanelTab.history.rawValue)
        let tab = PanelTab.from(settingsValue: raw)
        applySettingsPatch(["ui_last_tab": tab.rawValue])
    }

    func startPreviewPolling() {
        stopPreviewPolling()
        previewPollTick = 0
        previewTimer = Timer.scheduledTimer(withTimeInterval: 0.9, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.refreshRealtimePreview()
                self.previewPollTick += 1
                if self.previewPollTick % 3 == 0 {
                    self.refreshCallAssistState()
                }
            }
        }
        if let previewTimer {
            RunLoop.main.add(previewTimer, forMode: .common)
        }
        refreshRealtimePreview()
        refreshCallAssistState()
    }

    func stopPreviewPolling() {
        previewTimer?.invalidate()
        previewTimer = nil
        previewPollTick = 0
    }

    func refreshRealtimePreview() {
        let settings = settingsProvider()
        guard settings.realtimePreviewEnabled else {
            realtimeStatusLabel.stringValue = "Realtime: выключен"
            return
        }

        guard
            let response = try? ipcClient.call(method: "get_recording_state", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            return
        }

        let isRecording = (result["is_recording"] as? Bool) ?? false
        let durationSec = (result["duration_sec"] as? Double) ?? 0.0
        let previewText = ((result["preview_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let durationText = HistoryPanelController.formatDuration(durationSec)

        if isRecording {
            realtimeStatusLabel.stringValue = "Realtime: запись \(durationText)"
            realtimeTextView.string = previewText.isEmpty
                ? "Слушаю... первые слова появятся через ~1-2 секунды."
                : previewText
        } else {
            realtimeStatusLabel.stringValue = "Realtime: idle"
            if previewText.isEmpty {
                realtimeTextView.string = "Запись не активна."
            } else {
                realtimeTextView.string = previewText
            }
        }
    }

    /// Pure helper — не trogает self, тестируется без instance.
    /// Форматирует секунды в "MM:SS" (clamped to ≥0).
    nonisolated static func formatDuration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let minutes = total / 60
        let secs = total % 60
        return String(format: "%02d:%02d", minutes, secs)
    }
}
