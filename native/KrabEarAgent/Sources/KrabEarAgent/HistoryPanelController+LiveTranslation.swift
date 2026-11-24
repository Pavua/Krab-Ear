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

    /// Единственный путь смены раздела. Источник правды — mainTabView; делегат
    /// tabView(_:didSelect:) пинует размер, зеркалит выбор в каркас и сохраняет ui_last_tab.
    func selectPanelTab(_ tab: PanelTab) {
        let index = mainTabView.indexOfTabViewItem(withIdentifier: tab.rawValue)
        guard index != NSNotFound else { return }
        // Без неявной анимации: иначе мерцают NSVisualEffectView карточек.
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0
            ctx.allowsImplicitAnimation = false
            mainTabView.selectTabViewItem(at: index)
        }
    }

    /// Зеркалит выбор mainTabView в боковую панель и заголовок шапки.
    /// Чистое представление (без IPC и сохранения) — безопасно звать под isSyncingTabs.
    func syncTabSelectorFromTabView() {
        let raw = String(describing: mainTabView.selectedTabViewItem?.identifier ?? PanelTab.history.rawValue)
        let tab = PanelTab.from(settingsValue: raw)
        panelShell.sidebar.select(tab)
        panelShell.header.title = tab.title
    }

    func tabView(_ tabView: NSTabView, didSelect tabViewItem: NSTabViewItem?) {
        guard tabView == mainTabView else { return }
        // Таб 0 (Диктовка) — единственный видимый в момент, когда mainTabView
        // впервые получает реальный размер, поэтому его content view "бесплатно"
        // наследует корректную высоту. Для ЛЮБОГО другого таба, ставшего
        // selected программно (клик tabSelector ИЛИ silent-restore ui_last_tab в
        // syncSettingsControls), NSTabView (.noTabsNoBorder + Auto-Layout-
        // управляемые дочерние view) не гарантированно подгоняет ВЫСОТУ —
        // диагностика подтвердила frame.height == 0 при полностью корректно
        // построенном содержимом внутри. Пинуем size вручную вместо доверия
        // внутренней магии NSTabView; деактивируем предыдущую пару перед новой
        // активацией — иначе стейл-constraint на уже отсоединённом view кидает
        // NSGenericException 'no common ancestor' (KRAB-EAR-AGENT-2 класс).
        NSLayoutConstraint.deactivate(activeTabSizeConstraints)
        activeTabSizeConstraints.removeAll()
        if let contentView = tabViewItem?.view {
            let widthC = contentView.widthAnchor.constraint(equalTo: mainTabView.widthAnchor)
            let heightC = contentView.heightAnchor.constraint(equalTo: mainTabView.heightAnchor)
            widthC.isActive = true
            heightC.isActive = true
            activeTabSizeConstraints = [widthC, heightC]
            contentView.needsLayout = true
            contentView.layoutSubtreeIfNeeded()
        }
        // Зеркалим всегда (и при тихом восстановлении ui_last_tab): иначе подсветка
        // раздела расходится с показанным содержимым. Сохранение — только по действию пользователя.
        syncTabSelectorFromTabView()
        guard !isSyncingTabs, !isSyncingSettings else { return }
        let raw = String(describing: tabViewItem?.identifier ?? PanelTab.history.rawValue)
        let tab = PanelTab.from(settingsValue: raw)
        // Сохраняем последнюю вкладку вне main thread — sync IPC read() на main вызывал AppHang
        // при каждом переключении вкладки (KRAB-EAR-AGENT-N). ui_last_tab не влияет на UI,
        // поэтому syncSettingsControls здесь не нужен.
        let ipc = ipcClient
        let tabPayload = ["ui_last_tab": tab.rawValue]
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

    /// Видна ли сейчас вкладка «Диктовка» (кнопке записи нужно состояние).
    var isDictationTabVisible: Bool {
        guard window?.isVisible == true else { return false }
        return (mainTabView.selectedTabViewItem?.identifier as? String) == PanelTab.dictation.rawValue
    }

    /// Снимок для карточки «Диктовка»: фаза записи + текущие настройки.
    func makeDictationHeroSnapshot() -> DictationHeroSnapshot {
        let settings = settingsProvider()
        return DictationHeroSnapshot(
            phase: dictationPhase,
            hotkey: currentHotkeyLabel(),
            profile: settings.qualityProfile.capitalized,
            autoPaste: settings.autoPaste,
            engine: sttEngineLabel.stringValue
        )
    }

    /// Обновляет карточку «Диктовка»; без изменений снимка ничего не перерисовывается.
    func refreshDictationHero() {
        dictationHero.apply(makeDictationHeroSnapshot())
    }

    /// Фаза записи из ответа get_recording_state (логика записи не меняется — только вид).
    nonisolated static func dictationPhase(from result: [String: Any]) -> DictationPhase {
        let isRecording = (result["is_recording"] as? Bool) ?? false
        let isFinalizing = (result["is_finalizing"] as? Bool) ?? false
        if isRecording { return .recording(durationSec: (result["duration_sec"] as? Double) ?? 0) }
        return isFinalizing ? .finalizing : .idle
    }

    func refreshRealtimePreview() {
        let settings = settingsProvider()
        let previewEnabled = settings.realtimePreviewEnabled
        if !previewEnabled {
            realtimeStatusLabel.stringValue = "Realtime: выключен"
        }
        // Один и тот же опрос кормит и превью, и кнопку записи. Когда превью выключено и
        // «Диктовка» не видна — не опрашиваем вовсе (без новой нагрузки на IPC, урок BACKEND-T).
        let heroVisible = isDictationTabVisible
        guard previewEnabled || heroVisible else { return }

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
                // Нет ответа: после двух подряд — «Нет связи» на кнопке записи.
                self.dictationStateFailures += 1
                if self.dictationStateFailures >= 2 {
                    self.dictationPhase = .offline
                    self.refreshDictationHero()
                }
                return
            }
            self.dictationStateFailures = 0
            self.dictationPhase = HistoryPanelController.dictationPhase(from: result)
            self.refreshDictationHero()
            guard previewEnabled else { return }

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
