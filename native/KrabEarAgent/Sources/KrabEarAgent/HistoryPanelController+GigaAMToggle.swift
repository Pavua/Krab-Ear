/*
 HistoryPanelController+GigaAMToggle.swift

 GUI toggle для GigaAM-RNNT v2 (RU специализированный STT).
 Изолированный extension чтобы не разрастать +Settings.swift.

 Backend требования (см. config.py):
 - STT_GIGAAM_ENABLED: master switch
 - STT_LANGUAGE_ROUTING_ENABLED: нужен чтобы router включил GigaAM в chain для lang=ru
 - STT_GIGAAM_TRANSPORT: "auto" / "in_process" / "subprocess" (default auto)

 Pre-flight check на venv (~/.venv_krab_ear_gigaam) — если нет, alert
 с инструкцией запустить scripts/install_gigaam_venv.command. Иначе IPC
 setting прошёл бы, но при первой диктовке backend упал бы с ImportError.
*/

import AppKit
import Foundation

extension HistoryPanelController {

    /// Default путь к venv — соответствует scripts/install_gigaam_venv.command.
    /// Должен быть в sync с STT_GIGAAM_VENV_PYTHON default из core/config.py.
    nonisolated private static var defaultGigaamVenvPython: String {
        return NSString(string: "~/.venv_krab_ear_gigaam/bin/python").expandingTildeInPath
    }

    /// Handler для `gigaamEnabledButton` checkbox.
    /// При enable: pre-flight check на venv → alert + revert если нет; иначе
    /// `set_settings stt_gigaam_enabled=true + stt_language_routing_enabled=true`.
    /// При disable: оба false (router → fallback на whisper для всех языков).
    @objc func onGigaamEnabledChanged() {
        let isOn = gigaamEnabledButton.state == .on

        if isOn && !HistoryPanelController.isGigaamVenvReady() {
            // Откатываем checkbox + показываем инструкцию.
            gigaamEnabledButton.state = .off
            let alert = NSAlert()
            alert.messageText = "GigaAM venv не найден"
            alert.informativeText = """
                Для GigaAM-RNNT v2 (RU STT) нужен изолированный venv:
                  ~/.venv_krab_ear_gigaam

                Запусти один раз:
                  bash scripts/install_gigaam_venv.command

                Скрипт создаст venv с Python 3.12 + torch 2.5.1 + gigaam
                (~3-5 минут, ~630 МБ). После — снова включи галочку.
                """
            alert.alertStyle = .informational
            alert.addButton(withTitle: "Понятно")
            presentAlertSheet(alert, for: self.window) { _ in }
            return
        }

        // OK — apply через IPC.
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let params: [String: Any] = [
                "stt_gigaam_enabled": isOn,
                "stt_language_routing_enabled": isOn,
            ]
            let _ = try? ipcClient.call(method: "set_settings", params: params)
            DispatchQueue.main.async {
                guard let self = self else { return }
                if isOn {
                    self.notificationService.notify(
                        title: "Krab Ear",
                        body: "GigaAM включён. Для коротких диктовок (<25 сек) на русском будет ~2.5× выше точность."
                    )
                } else {
                    self.notificationService.notify(
                        title: "Krab Ear",
                        body: "GigaAM выключен. STT chain снова whisper-only."
                    )
                }
            }
        }
    }

    /// Pre-flight check: существует ли Python executable в default venv path.
    /// `nonisolated static` — тестируется без instance.
    /// Проверяет что path существует И является регулярным файлом (не директорией)
    /// И executable. `isExecutableFile` returns true для directories на macOS,
    /// поэтому добавлен extra `isDirectory` guard.
    nonisolated static func isGigaamVenvReady(at path: String? = nil) -> Bool {
        let p = path ?? defaultGigaamVenvPython
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: p, isDirectory: &isDir) else {
            return false
        }
        guard !isDir.boolValue else { return false }
        return FileManager.default.isExecutableFile(atPath: p)
    }
}
