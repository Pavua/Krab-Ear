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

    // MARK: - Task 2: глобальный хоткей Cmd+Shift+N

    /// Образец — SelectionTranslator.swift:126, но с СОХРАНЕНИЕМ монитора
    /// (урок main+QuickPresets.swift: несохранённый монитор не снять в teardown).
    func startQuickCaptureHotkeyMonitor() {
        guard quickCaptureHotkeyMonitor == nil else { return }
        quickCaptureHotkeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self,
                  event.modifierFlags.intersection(.deviceIndependentFlagsMask) == [.command, .shift],
                  event.keyCode == 45 /* kVK_ANSI_N */ else { return }
            DispatchQueue.main.async { self.onQuickCaptureToggle() }
        }
    }

    func stopQuickCaptureHotkeyMonitor() {
        if let monitor = quickCaptureHotkeyMonitor {
            NSEvent.removeMonitor(monitor)
            quickCaptureHotkeyMonitor = nil
        }
    }

    // MARK: - Task 2: подменю «Быстрые заметки»

    /// Вызывается из menuWillOpen (main+MenuBarRecap.swift) при каждом открытии
    /// status-меню. IPC строго off-main (AGENT-3); UI-обновление подменю — на main.
    func refreshQuickNotesSubmenu() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            // get_collection_items кидает RuntimeError, если коллекция ещё не
            // создана (ни одной заметки не сохранено) — try? сворачивает эту
            // ветку в пустой список, что рендерится как «Заметок пока нет»
            // тем же путём, что и privacy-режим (backend возвращает items: []).
            var items: [[String: Any]] = []
            if let resp = try? self.ipcClient.call(
                method: "get_collection_items",
                params: ["collection_name": Self.quickCaptureCollectionName]),
               // call() возвращает ПОЛНЫЙ конверт {ok, result, id} — реальные
               // поля лежат в result (тот же контракт, что callAsync, сверено
               // с IPCClient.swift:400-480).
               let result = resp["result"] as? [String: Any],
               let fetched = result["items"] as? [[String: Any]] {
                items = fetched
            }
            let latest = Array(items.suffix(7).reversed())
            DispatchQueue.main.async { self.rebuildQuickNotesSubmenu(latest) }
        }
    }

    /// Перестраивает содержимое подменю «Быстрые заметки» на main-потоке.
    /// items — уже усечённый и развёрнутый (последние первыми) список из
    /// refreshQuickNotesSubmenu; поля сверены с HistoryItem.to_dict()
    /// (models.py) — "text" (str) и "ts" (ISO-8601 UTC, timespec=seconds).
    func rebuildQuickNotesSubmenu(_ items: [[String: Any]]) {
        guard let submenu = quickNotesSubmenu else { return }
        submenu.removeAllItems()

        if items.isEmpty {
            let emptyItem = NSMenuItem(title: "Заметок пока нет", action: nil, keyEquivalent: "")
            emptyItem.isEnabled = false
            submenu.addItem(emptyItem)
        } else {
            for item in items {
                let text = (item["text"] as? String) ?? ""
                let ts = (item["ts"] as? String) ?? ""
                let noteItem = NSMenuItem(
                    title: Self.quickNoteMenuTitle(text: text, ts: ts),
                    action: #selector(onQuickNoteItemClicked(_:)),
                    keyEquivalent: ""
                )
                noteItem.target = self
                noteItem.representedObject = text
                submenu.addItem(noteItem)
            }
        }

        submenu.addItem(.separator())
        let showAllItem = NSMenuItem(
            title: "Показать все…",
            action: #selector(onOpenHistory),
            keyEquivalent: ""
        )
        showAllItem.target = self
        submenu.addItem(showAllItem)
    }

    /// Клик по пункту заметки — копирует полный текст в буфер обмена (тот же
    /// helper, что «Копировать последний»; заметка никогда не входит в
    /// paste-пайплайн активного окна).
    @objc func onQuickNoteItemClicked(_ sender: NSMenuItem) {
        guard let text = sender.representedObject as? String, !text.isEmpty else { return }
        pasteService.putToClipboard(text)
        BackendToast.shared.show("Скопировано")
    }

    /// «первые ~40 символов… · HH:mm» — разделитель U+00B7 MIDDLE DOT (глиф-гейт
    /// AGENT-J: уже используется в main+MenuBarRecap.swift).
    private static func quickNoteMenuTitle(text: String, ts: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let snippetLimit = 40
        let snippet: String
        if trimmed.isEmpty {
            snippet = "(без текста)"
        } else if trimmed.count > snippetLimit {
            snippet = String(trimmed.prefix(snippetLimit)) + "…"
        } else {
            snippet = trimmed
        }
        return "\(snippet) · \(quickNoteTimeLabel(from: ts))"
    }

    /// ts приходит как ISO-8601 UTC (HistoryItem.ts = isoformat(timespec="seconds"));
    /// парсим двумя формататерами (с/без дробных секунд — образец
    /// HistoryPanelController+RecordingChain.swift:144-147) и рендерим в local HH:mm.
    private static func quickNoteTimeLabel(from ts: String) -> String {
        let isoFrac = ISO8601DateFormatter()
        isoFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let isoNoFrac = ISO8601DateFormatter()
        isoNoFrac.formatOptions = [.withInternetDateTime]
        guard let date = isoFrac.date(from: ts) ?? isoNoFrac.date(from: ts) else {
            return "--:--"
        }
        let hhmm = DateFormatter()
        hhmm.dateFormat = "HH:mm"
        return hhmm.string(from: date)
    }
}
