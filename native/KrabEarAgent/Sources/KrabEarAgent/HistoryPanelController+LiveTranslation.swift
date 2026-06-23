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
        // Сохраняем последнюю вкладку вне main thread — sync IPC read() на main вызывал AppHang
        // при каждом переключении вкладки (KRAB-EAR-AGENT-N). ui_last_tab не влияет на UI,
        // поэтому syncSettingsControls здесь не нужен.
        let ipc = ipcClient
        var tabPayload = settingsProvider().toPayload()
        tabPayload["ui_last_tab"] = tab.rawValue
        DispatchQueue.global(qos: .utility).async {
            _ = try? ipc.call(method: "set_settings", params: tabPayload)
        }
        // Обновляем индикатор STT движка при открытии вкладки «Диктовка».
        if tab == .dictation {
            fetchAndUpdateSTTEngineLabel()
        }
    }

    func startPreviewPolling() {
        stopPreviewPolling()
        previewPollTick = 0
        previewTimer = Timer.scheduledTimer(withTimeInterval: 0.9, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                self.refreshRealtimePreview()
                self.previewPollTick += 1
                // Poll Call Assist state only while a session is active — avoids
                // hammering get_call_assist_state when idle (BACKEND-T over-poll /
                // IPC rate-limit fix). callAssistActive is set on every start/stop
                // via applyCallAssistState, and the immediate refresh below + the
                // event-driven refreshes pick up an already-running session.
                if self.callAssistActive, self.previewPollTick % 3 == 0 {
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

        // Wave 59: use callAsync to avoid blocking MainActor (was sync IPC every 0.9 s).
        // The timer Task { @MainActor in ... } context is already async — awaiting here
        // yields MainActor while IPC is in-flight instead of spinning the runloop.
        let ipcClient = self.ipcClient
        Task { @MainActor in
            guard
                let response = try? await ipcClient.callAsync(
                    method: "get_recording_state", params: [:]),
                let result = response["result"] as? [String: Any]
            else {
                return
            }

            let isRecording = (result["is_recording"] as? Bool) ?? false
            let durationSec = (result["duration_sec"] as? Double) ?? 0.0
            let previewText = ((result["preview_text"] as? String) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let durationText = HistoryPanelController.formatDuration(durationSec)

            if isRecording {
                self.realtimeStatusLabel.stringValue = "Realtime: запись \(durationText)"
                self.realtimeTextView.string = previewText.isEmpty
                    ? "Слушаю... первые слова появятся через ~1-2 секунды."
                    : previewText
            } else {
                self.realtimeStatusLabel.stringValue = "Realtime: idle"
                if previewText.isEmpty {
                    self.realtimeTextView.string = "Запись не активна."
                } else {
                    self.realtimeTextView.string = previewText
                }
            }
        }
    }

    nonisolated static func formatDuration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let minutes = total / 60
        let secs = total % 60
        return String(format: "%02d:%02d", minutes, secs)
    }
}
