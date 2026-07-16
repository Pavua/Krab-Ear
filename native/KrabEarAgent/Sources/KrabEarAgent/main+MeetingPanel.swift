/*
 main+MeetingPanel — C2c Task 3: владение MeetingLivePanelController + точки входа
 (спека §2.7/§2.7a п.3).

 Единственный владелец панели — AgentAppDelegate (свойство meetingPanelController
 в main.swift, НЕ associated object — тот паттерн зарезервирован для Phase 2B
 live-subs state, здесь класс определён в основном файле). Вызывающие точки:
 меню-бар «Встреча» (main+StatusMenu.swift) и кнопка «Встреча» в topActionsRow
 истории (HistoryPanelController.swift) — обе роутят сюда, в onMeetingPanelToggle().
*/

import AppKit
import Foundation

extension AgentAppDelegate {

    /// Единый вход: сессии нет → meeting_start (backend идемпотентен:
    /// already_active/promoted) → показать панель; сессия есть → просто показать.
    @objc func onMeetingPanelToggle() {
        let controller = ensureMeetingPanelController()
        controller.show()
        controller.startUpdates()
        let client = ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "meeting_start", params: [:])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    if (result["skipped"] as? String) == "privacy_mode" {
                        controller.render(state: ["ok": true, "active": false,
                                                  "privacy_mode_active": true])
                    }
                    // успех/already_active: ближайший poll/SSE наполнит панель
                }
            } catch {
                DispatchQueue.main.async {
                    controller.showTransientError("Не удалось начать встречу: \(error.localizedDescription)")
                }
            }
        }
    }

    /// Единственный инстанс панели: создаётся лениво, инжектится ipcClient
    /// и onFinished-колбэк (финализация → отчёт).
    func ensureMeetingPanelController() -> MeetingLivePanelController {
        if let existing = meetingPanelController { return existing }
        let c = MeetingLivePanelController()
        c.ipcClient = ipcClient
        c.onFinished = { [weak self] itemID in
            self?.openMeetingReportAfterFinish(itemID: itemID)
        }
        meetingPanelController = c
        return c
    }

    /// finished → get_meeting_report → standalone-окно; без item_id — панель в idle.
    func openMeetingReportAfterFinish(itemID: String?) {
        guard let itemID, !itemID.isEmpty else {
            meetingPanelController?.resetToIdle()
            return
        }
        let client = ipcClient
        // AGENT-3: ipcClient.call строго off-main.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try client.call(
                    method: "get_meeting_report", params: ["id": itemID])
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async { [weak self] in
                    self?.meetingPanelController?.resetToIdle()
                    HistoryPanelController.presentMeetingReportStandalone(result: result)
                }
            } catch {
                DispatchQueue.main.async { [weak self] in
                    self?.meetingPanelController?.showTransientError(
                        "Отчёт не построился: \(error.localizedDescription)")
                    self?.meetingPanelController?.resetToIdle()
                }
            }
        }
    }
}
