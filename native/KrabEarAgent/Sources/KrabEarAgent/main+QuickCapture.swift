/*
 main+QuickCapture.swift
 C3a (спека 2026-07-16-c3-quick-capture-design.md §2-§3): быстрая голосовая
 заметка — запись БЕЗ вставки в активное окно; результат уходит в history +
 коллекцию «Быстрые заметки» (лениво создаётся при первом использовании),
 opt-in дублирование в Notes/Obsidian наполняется в Task 3.

 Переиспользует штатный record-флоу (start_recording/stop_recording +
 start/stopRealtimeOverlayPolling — там живут wake-word пауза и оверлей).
 Финальный paste НЕ вызывается — заметка никогда не входит в обычный
 paste-пайплайн диктовки (см. main+PasteHandling.swift), streaming-paste
 подавлен отдельным гардом в main+RealtimeOverlay.swift.
*/

import AppKit

extension AgentAppDelegate {
    static let quickCaptureCollectionName = "Быстрые заметки"

    @objc func onQuickCaptureToggle() {
        if quickCaptureActive { stopQuickCapture(); return }
        // Взаимное исключение: диктовка/обработка. Встреча отсекается своим
        // собственным гардом (main+MeetingPanel.swift); чужая активная запись
        // backend'а (если она всё же есть) отразится в status != "recording"
        // на старте ниже.
        if isRecording || isProcessing {
            BackendToast.shared.show("Уже идёт запись — заметка недоступна")
            return
        }
        quickCaptureActive = true
        rebuildStatusMenu()
        Task.detached { [weak self] in
            guard let self else { return }
            do {
                let resp = try await self.ipcClient.callAsync(
                    method: "start_recording", params: [:], timeoutSec: 10)
                // callAsync возвращает полный конверт {ok, result, id} — реальные
                // поля лежат в result (сверено с IPCClient.swift:400-480 и
                // main+HotkeyRecording.swift:startRecording()).
                let result = resp["result"] as? [String: Any] ?? [:]
                // Успешный статус start_recording — "recording" (НЕ "ok"; сверено
                // с recording_core_service.py:367). "already_recording" —
                // идемпотентный повтор, тоже успех.
                let status = result["status"] as? String ?? ""
                await MainActor.run {
                    if status == "recording" || status == "already_recording" {
                        // wake-word пауза + оверлей; streaming-paste подавлен гардом.
                        self.startRealtimeOverlayPolling()
                        BackendToast.shared.show("Быстрая заметка: запись…")
                    } else {
                        self.quickCaptureActive = false
                        self.rebuildStatusMenu()
                        BackendToast.shared.show("Не удалось начать заметку")
                    }
                }
            } catch {
                await MainActor.run {
                    self.quickCaptureActive = false
                    self.rebuildStatusMenu()
                    BackendToast.shared.show("Не удалось начать заметку")
                }
            }
        }
    }

    func stopQuickCapture() {
        stopRealtimeOverlayPolling()
        Task.detached { [weak self] in
            guard let self else { return }
            defer {
                Task { await MainActor.run {
                    self.quickCaptureActive = false
                    self.rebuildStatusMenu()
                } }
            }
            do {
                let resp = try await self.ipcClient.callAsync(
                    method: "stop_recording", params: [:], timeoutSec: 120)
                let result = resp["result"] as? [String: Any] ?? [:]
                await self.handleQuickCaptureResult(result)
            } catch {
                await MainActor.run { BackendToast.shared.show("Ошибка завершения заметки") }
            }
        }
    }

    private func handleQuickCaptureResult(_ result: [String: Any]) async {
        let status = result["status"] as? String ?? ""
        if result["skipped"] as? String == "duplicate" {
            await MainActor.run { BackendToast.shared.show("Заметка совпала с недавней записью — пропущена") }
            return
        }
        guard status == "ok", let historyId = result["history_id"] as? String, !historyId.isEmpty else {
            await MainActor.run { BackendToast.shared.show("Заметка не сохранилась") }
            return
        }
        // 1) коллекция (лениво создать), 2) нейтральный paste_status, 3) отправки.
        // create_collection на уже существующей коллекции кидает ValueError
        // ("Коллекция '...' уже существует", collection_manager.py:189) —
        // та же exception-ветка (invalid_request), что и «нет имени», но проверка
        // выполняется ДО записи на диск, поэтому try?-проглатывание дёшево и
        // корректно идемпотентно для фиксированного имени коллекции.
        _ = try? await ipcClient.callAsync(
            method: "create_collection",
            params: ["name": Self.quickCaptureCollectionName,
                     "description": "Быстрые голосовые заметки"], timeoutSec: 10)
        _ = try? await ipcClient.callAsync(
            method: "add_to_collection",
            params: ["collection_name": Self.quickCaptureCollectionName, "item_id": historyId],
            timeoutSec: 10)
        // Параметры сверены с _handle_set_paste_status (service.py:2459) — ключи
        // "id" и "paste_status", НЕ "item_id"/"status".
        _ = try? await ipcClient.callAsync(
            method: "set_paste_status",
            params: ["id": historyId, "paste_status": "skipped"], timeoutSec: 10)
        await sendQuickCaptureCopies(text: result["text"] as? String ?? "", historyId: historyId)
        await MainActor.run { BackendToast.shared.show("Заметка сохранена") }
    }

    /// Наполняется в Task 3 (авто-отправка в Apple Notes / Obsidian).
    func sendQuickCaptureCopies(text: String, historyId: String) async {}
}
