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

 C3b Task 2 (спека 2026-07-16-c3b-scratchpad-panel.md): точки входа панели-
 скретчпада (QuickCapturePanelController) живут ЗДЕСЬ, не в отдельном
 main+QuickCapturePanel.swift (план предполагал такой файл — на практике
 проще встроить в уже существующий main+QuickCapture.swift, см. отчёт волны).
 Единственный владелец панели — quickCapturePanelController (main.swift),
 лениво создаётся ensureQuickCapturePanelController().
*/

import AppKit

extension AgentAppDelegate {
    static let quickCaptureCollectionName = "Быстрые заметки"

    @objc func onQuickCaptureToggle() {
        // C3a ревью F2a: debounce переиспользует ТО ЖЕ поле, что диктовка
        // (main+HotkeyRecording.swift:handleRecordToggleRequest) — оба хоткея
        // переключают состояние ОДНОГО общего recorder'а (см. F1 NOTES), так что
        // общий таймер защищает от гонки старт/стоп независимо от того, какой
        // именно "переключатель записи" сработал повторно (не только авто-repeat
        // ОДНОГО и того же хоткея — двойной тап Cmd+Shift+N сразу после Right
        // Option тоже гасится, что логично при общем recorder'е).
        let now = Date().timeIntervalSince1970
        if now - lastToggleRequestAt < toggleDebounceSec {
            logger.warn("Игнорирую повторный quick capture toggle (debounce)")
            return
        }
        lastToggleRequestAt = now

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
                    method: "start_recording",
                    params: ["source": "quick_capture"],
                    timeoutSec: 10
                )
                // callAsync возвращает полный конверт {ok, result, id} — реальные
                // поля лежат в result (сверено с IPCClient.swift:400-480 и
                // main+HotkeyRecording.swift:startRecording()).
                let result = resp["result"] as? [String: Any] ?? [:]
                // Успешный статус start_recording — "recording" (НЕ "ok"; сверено
                // с recording_core_service.py:367). C3a ревью F1: "already_recording"
                // — идемпотентный ответ recorder'а, УЖЕ занятого чужой записью
                // (например meeting, который не выставляет Swift-флаг isRecording
                // и потому не отсекается гардом выше) — это ОТКАЗ, а не успех:
                // трактовать его как успех означало бы угнать чужую запись (второе
                // нажатие шлёт stop_recording на ОБЩИЙ recorder — see
                // meeting_session_service.py:403-410 self-finalize с item_id=None).
                let status = result["status"] as? String ?? ""
                // Возвращает true только на настоящем успешном старте (не на
                // осиротевшем/отвергнутом) — только тогда имеет смысл живьём
                // проверять quick_capture_show_panel и показывать панель ниже.
                let started: Bool = await MainActor.run {
                    if status == "recording" {
                        // C3a ревью F2b: этот completion мог доехать ПОСЛЕ того,
                        // как stopQuickCapture() (двойной тап/гонка) уже сбросил
                        // quickCaptureActive и отправил свой собственный
                        // stop_recording — backend в этот момент мог быть ещё
                        // idle и потому теперь стартовал запись, которую больше
                        // некому остановить. Если состояние уже не совпадает с
                        // тем, что ожидал этот старт, — не применяем success-эффекты,
                        // а сами останавливаем осиротевшую запись fire-and-forget.
                        guard self.quickCaptureActive else {
                            // Локальный `let` вместо self.ipcClient внутри замыкания —
                            // тот же приём, что startQuickCaptureHotkeyMonitor(): IPCClient
                            // сам Sendable, а AgentAppDelegate (self) — нет.
                            let orphanIpc = self.ipcClient
                            Task.detached {
                                _ = try? await orphanIpc.callAsync(
                                    method: "stop_recording",
                                    params: ["source": "quick_capture"],
                                    timeoutSec: 120)
                            }
                            return false
                        }
                        // wake-word пауза + оверлей; streaming-paste подавлен гардом.
                        self.startRealtimeOverlayPolling()
                        BackendToast.shared.show("Быстрая заметка: запись…")
                        return true
                    } else {
                        self.quickCaptureActive = false
                        self.rebuildStatusMenu()
                        let message = status == "already_recording"
                            ? "Микрофон занят другой записью"
                            : "Не удалось начать заметку"
                        BackendToast.shared.show(message)
                        return false
                    }
                }
                if started {
                    await self.showQuickCapturePanelIfEnabled()
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
                    method: "stop_recording",
                    params: ["source": "quick_capture"],
                    timeoutSec: 120)
                let result = resp["result"] as? [String: Any] ?? [:]
                await self.handleQuickCaptureResult(result)
            } catch {
                await MainActor.run { BackendToast.shared.show("Ошибка завершения заметки") }
            }
        }
    }

    private func handleQuickCaptureResult(_ result: [String: Any]) async {
        // C3b ревью F1 (sibling-gate asymmetry): запись физически остановлена
        // независимо от исхода сохранения (дубликат/ошибка/успех) И независимо
        // от того, видима ли панель СЕЙЧАС — isRecording обязан быть источником
        // правды всегда, иначе после закрытия панели мид-записи он застревает
        // true навсегда (setRecording(true) на следующем старте молча не
        // применяется — guard в setRecording трактует состояние как уже
        // синхронизированное). Раньше здесь был `isVisible`-гейт — сам этот
        // гейт и был багом (панель, ЗАКРЫТАЯ во время записи, никогда не
        // получала свой setRecording(false)).
        quickCapturePanelController?.setRecording(false)
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
        // Свежая заметка должна появиться в списке панели, если она открыта.
        if quickCapturePanelController?.window?.isVisible == true {
            refreshQuickCapturePanelNotes()
        }
        await MainActor.run { BackendToast.shared.show("Заметка сохранена") }
    }

    /// C3a Task 3 (спека §3.3): opt-in дублирование сохранённой заметки в Apple
    /// Notes / Obsidian. Настройки читаются ЖИВЬЁМ через get_settings (НЕ кэш
    /// агента `settings`) — чекбокс в Settings должен действовать сразу после
    /// переключения, без ожидания следующего цикла обновления кэша.
    func sendQuickCaptureCopies(text: String, historyId: String) async {
        var liveSettings: [String: Any] = [:]
        if let resp = try? await ipcClient.callAsync(method: "get_settings", params: [:], timeoutSec: 10),
           let result = resp["result"] as? [String: Any] {
            liveSettings = result
        }

        if (liveSettings["quick_capture_send_to_notes"] as? Bool) == true {
            await sendQuickCaptureNoteToAppleNotes(text: text)
        }
        if (liveSettings["quick_capture_obsidian_sync"] as? Bool) == true {
            await syncQuickCaptureNoteToObsidian(text: text, historyId: historyId)
        }
    }

    /// title — первые ~60 символов ПЕРВОЙ строки текста (не всего текста целиком —
    /// длинная заметка без переводов строк не должна порождать гигантский заголовок).
    /// Приватный режим и сам осечка osascript уже гейтятся внутри
    /// handle_create_apple_note (apple_integration_service.py) — здесь только
    /// разворачиваем ok:false в toast с текстом ответа backend'а.
    private func sendQuickCaptureNoteToAppleNotes(text: String) async {
        let firstLine = text.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
            .first.map(String.init) ?? text
        let trimmedFirstLine = firstLine.trimmingCharacters(in: .whitespacesAndNewlines)
        let title = trimmedFirstLine.count > 60
            ? String(trimmedFirstLine.prefix(60)) + "…"
            : trimmedFirstLine
        guard let resp = try? await ipcClient.callAsync(
            method: "create_apple_note",
            params: ["title": title.isEmpty ? "Быстрая заметка" : title, "body": text, "folder": "Krab Ear"],
            timeoutSec: 15
        ), let result = resp["result"] as? [String: Any] else {
            await MainActor.run { BackendToast.shared.show("Не удалось сохранить заметку в Notes") }
            return
        }
        if (result["ok"] as? Bool) != true {
            // Ответ backend'а несёт user_msg (человекочитаемо, напр. privacy-гейт)
            // либо error (техническое сообщение osascript) — сверено с
            // apple_integration_service.py::handle_create_apple_note.
            let message = (result["user_msg"] as? String)
                ?? (result["error"] as? String)
                ?? "Не удалось сохранить заметку в Notes"
            await MainActor.run { BackendToast.shared.show(message) }
        }
    }

    /// Obsidian: per-item IPC-метода НЕТ — ObsidianSyncManager.sync(items, force)
    /// (backend/obsidian_sync.py) принимает СПИСОК items, поэтому форс-синк
    /// заметки собирает минимальный item-словарь {id, ts, text} из данных, уже
    /// имеющихся в этом флоу (см. NOTES отчёта Task 3 — точное имя IPC-метода:
    /// run_obsidian_sync → ObsidianSyncManager.handle_sync). Нет настроенного
    /// vault → sync() кидает RuntimeError → IPC-диспетчер отдаёт тихий
    /// invalid_request (ok:false) — здесь молча игнорируем (спека §3.3: "иначе
    /// чекбокс disabled с подсказкой"; v1 упрощение — тихий no-op, без тоста).
    private func syncQuickCaptureNoteToObsidian(text: String, historyId: String) async {
        let ts = ISO8601DateFormatter().string(from: Date())
        _ = try? await ipcClient.callAsync(
            method: "run_obsidian_sync",
            params: ["items": [["id": historyId, "ts": ts, "text": text]], "force": true],
            timeoutSec: 15
        )
    }

    // MARK: - C3b Task 2: панель-скретчпад (QuickCapturePanelController)

    /// Единственный инстанс панели: создаётся лениво, инжектится ipcClient и
    /// onToggleRecording-колбэк (тот же паттерн, что ensureMeetingPanelController
    /// в main+MeetingPanel.swift).
    func ensureQuickCapturePanelController() -> QuickCapturePanelController {
        if let existing = quickCapturePanelController { return existing }
        let c = QuickCapturePanelController()
        c.ipcClient = ipcClient
        c.onToggleRecording = { [weak self] in self?.onQuickCaptureToggle() }
        quickCapturePanelController = c
        return c
    }

    /// Вызывается ТОЛЬКО после настоящего успешного старта записи (см.
    /// onQuickCaptureToggle). Настройка читается ЖИВЬЁМ через get_settings —
    /// тот же приём, что sendQuickCaptureCopies — чекбокс «Показывать скретчпад
    /// при записи» должен действовать сразу после переключения, без ожидания
    /// следующего цикла обновления кэша.
    func showQuickCapturePanelIfEnabled() async {
        // C3b ревью F1 (sibling-gate asymmetry): уже существующая панель
        // (например открытая вручную из меню, см. onOpenQuickCapturePanel)
        // обязана узнать о реальном старте записи БЕЗУСЛОВНО — настройка
        // quick_capture_show_panel решает только "показывать ли панель
        // САМОСТОЯТЕЛЬНО", не "врать ли уже открытой панели о состоянии
        // записи". Раньше setRecording(true) был доступен ТОЛЬКО за этим
        // гейтом — при дефолтной (выключенной) настройке вручную открытая
        // панель никогда не отражала собственный успешный старт записи.
        // Nil-safe: если панель ещё не создана, optional chaining — no-op.
        quickCapturePanelController?.setRecording(true)

        guard let resp = try? await ipcClient.callAsync(method: "get_settings", params: [:], timeoutSec: 10),
              let result = resp["result"] as? [String: Any],
              (result["quick_capture_show_panel"] as? Bool) == true else { return }
        let controller = ensureQuickCapturePanelController()
        controller.setRecording(true)
        controller.show()
        refreshQuickCapturePanelNotes()
    }

    /// Обновляет список заметок панели тем же IPC-контрактом, что и подменю
    /// «Быстрые заметки» (refreshQuickNotesSubmenu, get_collection_items +
    /// suffix(7).reversed()) — продублировано минимально, сигнатура
    /// refreshQuickNotesSubmenu не меняется.
    func refreshQuickCapturePanelNotes() {
        guard let controller = quickCapturePanelController else { return }
        let ipc = self.ipcClient
        let collectionName = Self.quickCaptureCollectionName
        DispatchQueue.global(qos: .userInitiated).async {
            var items: [[String: Any]] = []
            if let resp = try? ipc.call(
                method: "get_collection_items",
                params: ["collection_name": collectionName]),
               let result = resp["result"] as? [String: Any],
               let fetched = result["items"] as? [[String: Any]] {
                items = fetched
            }
            let latest = Array(items.suffix(7).reversed())
            DispatchQueue.main.async { controller.renderNotes(latest) }
        }
    }

    /// Пункт меню-бара «Открыть скретчпад» (main+StatusMenu.swift) — показывает
    /// панель независимо от состояния записи (ручной вызов, план Task 2).
    /// Простой вариант «всегда показать» (симметрично onOpenHistory), а не
    /// toggle — план допускал оба варианта на усмотрение исполнителя.
    @objc func onOpenQuickCapturePanel() {
        let controller = ensureQuickCapturePanelController()
        controller.setRecording(quickCaptureActive)
        controller.show()
        refreshQuickCapturePanelNotes()
    }

    // MARK: - Task 2/3: глобальный хоткей (настраиваемая комбинация)

    /// Три фиксированные комбинации (спека §3.2, план Task 3) — allowlist сверен
    /// с settings_validator.py _ENUM_FIELDS["quick_capture_hotkey"]. Все три
    /// используют N = keyCode 45 (kVK_ANSI_N). cmd_shift_n — дефолт/фоллбэк для
    /// неизвестного/пустого значения.
    static func quickCaptureHotkeyCombo(for hotkeyId: String) -> (modifiers: NSEvent.ModifierFlags, keyCode: UInt16) {
        switch hotkeyId {
        case "cmd_opt_n": return ([.command, .option], 45)
        case "ctrl_shift_n": return ([.control, .shift], 45)
        default: return ([.command, .shift], 45)
        }
    }

    /// Образец — SelectionTranslator.swift:126, но с СОХРАНЕНИЕМ монитора
    /// (урок main+QuickPresets.swift: несохранённый монитор не снять в teardown).
    /// Комбинация читается ЖИВЬЁМ (get_settings, НЕ кэш `settings`) ДО регистрации
    /// монитора — чтобы пере-арм после смены в дропдауне (Task 3, см.
    /// HistoryPanelController+Settings.swift::onQuickCaptureHotkeyChanged) сразу
    /// подхватывал новое значение, а не устаревший кэш.
    func startQuickCaptureHotkeyMonitor() {
        guard quickCaptureHotkeyMonitor == nil else { return }
        let ipc = self.ipcClient
        Task.detached { [weak self] in
            var hotkeyId = "cmd_shift_n"
            if let resp = try? await ipc.callAsync(method: "get_settings", params: [:], timeoutSec: 10),
               let result = resp["result"] as? [String: Any],
               let value = result["quick_capture_hotkey"] as? String, !value.isEmpty {
                hotkeyId = value
            }
            // Rebind to a fresh `let` right before crossing the actor boundary —
            // a captured `var` (even unmutated at this point) trips the Swift 6
            // Sendable-closure-capture checker.
            let resolvedHotkeyId = hotkeyId
            await MainActor.run { [weak self] in
                self?.installQuickCaptureHotkeyMonitor(hotkeyId: resolvedHotkeyId)
            }
        }
    }

    /// Регистрирует NSEvent-монитор на main-потоке. Повторный guard-чек — защита
    /// от гонки: startQuickCaptureHotkeyMonitor() мог быть вызван дважды подряд,
    /// пока первый async-запрос настроек ещё не вернулся.
    private func installQuickCaptureHotkeyMonitor(hotkeyId: String) {
        guard quickCaptureHotkeyMonitor == nil else { return }
        let (modifiers, keyCode) = AgentAppDelegate.quickCaptureHotkeyCombo(for: hotkeyId)
        quickCaptureHotkeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            // C3a ревью F2a: OS auto-repeat (зажатая клавиша) шлёт keyDown-события
            // без реального повторного нажатия пользователем — проверяем ПЕРВОЙ,
            // до модификаторов/keyCode, чтобы не тратить на неё дальнейшую логику.
            guard !event.isARepeat else { return }
            guard let self,
                  event.modifierFlags.intersection(.deviceIndependentFlagsMask) == modifiers,
                  event.keyCode == keyCode else { return }
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
        // S34 / Fable-ревью F3: explicit user-initiated copy — не через concealed-guard.
        pasteService.putToClipboardUserInitiated(text)
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
