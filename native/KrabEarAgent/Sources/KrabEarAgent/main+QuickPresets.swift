/*
 main+QuickPresets.swift
 AgentAppDelegate extension: быстрое переключение пресетов записи через menu bar и Cmd+Shift+P.
*/

import AppKit
import Foundation

struct RecordingPreset {
    let id: String
    let label: String
    let menuLabel: String
    let badge: String
}

extension AgentAppDelegate {

    static let recordingPresets: [RecordingPreset] = [
        RecordingPreset(id: "default",       label: "Default",        menuLabel: "Default (D)",        badge: "D"),
        RecordingPreset(id: "meeting",        label: "Meeting",        menuLabel: "Meeting (M)",        badge: "M"),
        RecordingPreset(id: "translation",    label: "Translation",    menuLabel: "Translation (T)",    badge: "T"),
        RecordingPreset(id: "call_recording", label: "Call Recording", menuLabel: "Call Recording (C)", badge: "C"),
    ]

    func startPresetHotkeyMonitor() {
        NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            let isCmdShiftP = event.modifierFlags.contains([.command, .shift])
                && !event.modifierFlags.contains(.option)
                && !event.modifierFlags.contains(.control)
                && event.keyCode == 35
            guard isCmdShiftP else { return }
            DispatchQueue.main.async { self?.cycleToNextPreset() }
        }
    }

    func applyRecordingPreset(_ presetId: String, source: String = "menu") {
        // Offload IPC call off the main thread to prevent >2s AppHang
        // (AGENT-3 pattern: sync callWithRecovery on main thread blocks runloop).
        // Wave 188 fix: mirror the Task.detached pattern from main+HotkeyRecording.swift.
        // UI updates (UserDefaults, refreshStatusItemTitle, rebuildStatusMenu) hop back
        // to MainActor after the IPC call completes.
        let ipc = self.ipcClient
        let log = self.logger
        Task.detached { [weak self] in
            do {
                _ = try await ipc.callAsync(method: "apply_profile_preset", params: ["profile": presetId])
                // Wave 554: bind `let self` inside MainActor.run to satisfy Swift 6 strict
                // concurrency — captured `self?` cannot be reused across concurrent contexts.
                await MainActor.run { [weak self] in
                    guard let self else { return }
                    self.userDefaults.set(presetId, forKey: "KrabEar_ActivePreset")
                    self.refreshStatusItemTitle()
                    self.rebuildStatusMenu()
                }
            } catch {
                log.error("applyRecordingPreset \(presetId) (\(source)): \(error.localizedDescription)")
            }
        }
    }

    func cycleToNextPreset() {
        let currentPreset = userDefaults.string(forKey: "KrabEar_ActivePreset") ?? "default"
        let ids = AgentAppDelegate.recordingPresets.map { $0.id }
        let currentIdx = ids.firstIndex(of: currentPreset) ?? 0
        let nextIdx = (currentIdx + 1) % ids.count
        applyRecordingPreset(ids[nextIdx], source: "hotkey")
    }

    func activePresetBadge() -> String {
        let active = userDefaults.string(forKey: "KrabEar_ActivePreset") ?? "default"
        return AgentAppDelegate.recordingPresets.first { $0.id == active }?.badge ?? "D"
    }

    func buildPresetSubmenu() -> NSMenu {
        let submenu = NSMenu()
        let active = userDefaults.string(forKey: "KrabEar_ActivePreset") ?? "default"
        for (idx, preset) in AgentAppDelegate.recordingPresets.enumerated() {
            let item = NSMenuItem(
                title: preset.menuLabel,
                action: #selector(onPresetMenuItemClicked(_:)),
                keyEquivalent: ""
            )
            item.target = self
            item.tag = idx
            item.state = preset.id == active ? .on : .off
            submenu.addItem(item)
        }
        submenu.addItem(.separator())
        let openSettingsItem = NSMenuItem(
            title: "Открыть настройки…",
            action: #selector(onOpenSettings),
            keyEquivalent: ","
        )
        openSettingsItem.target = self
        submenu.addItem(openSettingsItem)
        return submenu
    }

    func addPresetMenuEntry(to menu: NSMenu) {
        let presetItem = NSMenuItem(title: "Пресет записи", action: nil, keyEquivalent: "")
        menu.addItem(presetItem)
        menu.setSubmenu(buildPresetSubmenu(), for: presetItem)
    }

    @objc func onOpenSettings() {
        // Open main panel; user can switch to Settings tab.
        // Defined here as fallback for "Открыть настройки…" menu item from preset submenu.
        openHistoryPanel(forceMenubar: false)
    }

    @objc func onPresetMenuItemClicked(_ sender: NSMenuItem) {
        let presets = AgentAppDelegate.recordingPresets
        guard sender.tag >= 0, sender.tag < presets.count else { return }
        applyRecordingPreset(presets[sender.tag].id, source: "menu")
    }

    @objc func onCyclePreset() {
        cycleToNextPreset()
    }
}
