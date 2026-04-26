/*
 Расширение HistoryPanelController: обработчики Call Assist.
 Все методы, связанные с управлением звонковыми сессиями, быстрыми фразами,
 timeline, summary и диагностикой звонка.
*/

import AppKit

extension HistoryPanelController {

    // MARK: - Start / Stop

    @objc func onStartCallAssist() {
        let settings = settingsProvider()
        let captureMode = selectedCaptureSourceMode()
        let notifyMode = callNotifyButton.state == .on ? "auto_on" : "auto_off"
        let translationMode = settings.translationMode == "off" ? "auto_to_ru" : settings.translationMode
        let autoSummaryOn = callAutoSummaryButton.state == .on
        let notifyOn = callNotifyButton.state == .on

        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "start_call_assist",
                    params: [
                        "capture_source_mode": captureMode,
                        "notify_mode": notifyMode,
                        "translation_mode": translationMode,
                        "tts_mode": "hybrid",
                        "auto_summary": autoSummaryOn,
                    ]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Call Assist", body: "Не удалось запустить звонковую сессию.")
                }
                return
            }

            DispatchQueue.main.async {
                guard let self = self else { return }
                self.applySettingsPatch([
                    "capture_source_mode": captureMode,
                    "call_notify_default": notifyOn,
                    "call_auto_summary": autoSummaryOn,
                ])
                self.applyCallAssistState(result)
            }
        }
    }

    @objc func onStopCallAssist() {
        let autoSummaryOn = callAutoSummaryButton.state == .on
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "stop_call_assist",
                    params: [
                        "auto_summary": autoSummaryOn,
                        "summary_max_items": 60,
                    ]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Call Assist", body: "Не удалось остановить звонковую сессию.")
                }
                return
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.applyCallAssistState(result)
                if let summaryStatus = result["summary_status"] as? String {
                    if summaryStatus == "ok", let summary = result["summary"] as? [String: Any] {
                        self.appendCallAssistOutput(title: "Summary звонка", body: HistoryPanelController.formatCallSummary(summary))
                        if let historyId = result["summary_history_id"] as? String, !historyId.isEmpty {
                            self.appendCallAssistOutput(title: "Summary сохранён", body: "Добавлено в историю. id: \(historyId)")
                        }
                    } else if summaryStatus == "degraded" {
                        let errorText = (result["summary_error"] as? String) ?? "unknown"
                        self.appendCallAssistOutput(title: "Summary звонка", body: "Не удалось получить summary: \(errorText)")
                    }
                }
            }
        }
    }

    // MARK: - Phrase Library

    @objc func onLoadCallPhraseLibrary() {
        let pair = selectedCallPhraseDirection()
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "list_call_assist_quick_phrases",
                    params: [
                        "source_lang": pair.sourceLang,
                        "target_lang": pair.targetLang,
                        "category": "all",
                        "limit": 60,
                    ]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Библиотека фраз", body: "Не удалось получить список быстрых фраз.")
                }
                return
            }
            let items = (result["items"] as? [[String: Any]]) ?? []
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.callPhrasePresets = items
                self.callPhrasePresetSelector.removeAllItems()
                if items.isEmpty {
                    self.callPhrasePresetSelector.addItem(withTitle: "— фразы не найдены —")
                    self.appendCallAssistOutput(title: "Библиотека фраз", body: "Список пуст.")
                    return
                }
                for item in items {
                    let text = (item["source_text"] as? String) ?? ""
                    let category = (item["category"] as? String) ?? "base"
                    self.callPhrasePresetSelector.addItem(withTitle: "[\(category)] \(text)")
                }
                self.callPhrasePresetSelector.selectItem(at: 0)
                self.onCallPhrasePresetSelected()
                self.appendCallAssistOutput(title: "Библиотека фраз", body: "Загружено фраз: \(items.count)")
            }
        }
    }

    @objc func onCallPhrasePresetSelected() {
        let idx = callPhrasePresetSelector.indexOfSelectedItem
        guard idx >= 0, idx < callPhrasePresets.count else { return }
        let item = callPhrasePresets[idx]
        let text = ((item["source_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            callPhraseInputField.stringValue = text
        }
    }

    @objc func onCallPhraseDirectionChanged() {
        onLoadCallPhraseLibrary()
    }

    @objc func onSendCallPhrase() {
        let text = callPhraseInputField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            showInfoAlert(title: "Call Assist", body: "Введите фразу для отправки.")
            return
        }
        let pair = selectedCallPhraseDirection()
        let params: [String: Any] = [
            "text": text,
            "source_lang": pair.sourceLang,
            "target_lang": pair.targetLang,
            "voice": "default",
            "style": "chat",
        ]
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(method: "call_assist_quick_phrase", params: params),
                let result = response["result"] as? [String: Any],
                let quick = result["quick_phrase"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Quick Phrase", body: "Ошибка отправки фразы в Gateway.")
                }
                return
            }

            let translated = (quick["translated_text"] as? String) ?? ""
            let audioURL = (quick["audio_url"] as? String) ?? "-"
            let cacheHit = (quick["cache_hit"] as? Bool) ?? false
            DispatchQueue.main.async {
                self?.appendCallAssistOutput(
                    title: "Quick Phrase",
                    body: """
                    \(pair.sourceLang) -> \(pair.targetLang)
                    source: \(text)
                    translated: \(translated)
                    audio: \(audioURL)
                    cache_hit: \(cacheHit)
                    """
                )
            }
        }
    }

    // MARK: - Summary / Diagnostics / Cost

    @objc func onFetchCallSummary() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "call_assist_summary",
                    params: ["max_items": 40]
                ),
                let result = response["result"] as? [String: Any],
                let summaryPayload = result["summary"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Summary", body: "Не удалось получить summary звонка.")
                }
                return
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.appendCallAssistOutput(title: "Summary", body: HistoryPanelController.formatCallSummary(summaryPayload))
            }
        }
    }

    @objc func onFetchCallDiagnostics() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "call_assist_diagnostics",
                    params: ["include_why": true]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Diagnostics", body: "Не удалось получить diagnostics.")
                }
                return
            }
            let diagnostics = (result["diagnostics"] as? [String: Any]) ?? [:]
            let whyPayload = (result["why"] as? [String: Any]) ?? [:]
            let counters = (diagnostics["counters"] as? [String: Any]) ?? [:]
            let pipeline = (diagnostics["pipeline"] as? [String: Any]) ?? [:]
            let why = (whyPayload["why"] as? [String: Any]) ?? [:]
            let whyCode = (why["code"] as? String) ?? "-"
            let whyMessage = (why["message"] as? String) ?? "-"
            // Собираем тело сообщения на background — на main отправляем только строку
            // (избегаем capture'а [String: Any] в actor-isolated closure → Swift 6 data race).
            let body = """
            translation_partial: \(counters["translation_partial"] ?? 0)
            tts_ready: \(counters["tts_ready"] ?? 0)
            cache_hits: \(pipeline["cache_hits"] ?? 0)
            cache_misses: \(pipeline["cache_misses"] ?? 0)
            fallback: \(pipeline["last_fallback"] ?? "-")
            why: \(whyCode) — \(whyMessage)
            """
            DispatchQueue.main.async {
                self?.appendCallAssistOutput(title: "Diagnostics", body: body)
            }
        }
    }

    @objc func onEstimateCallCost() {
        let countryField = NSTextField(frame: NSRect(x: 0, y: 0, width: 90, height: 24))
        countryField.applyThemeInput()
        countryField.stringValue = "ES"
        countryField.placeholderString = "ISO2"

        let inboundField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        inboundField.applyThemeInput()
        inboundField.stringValue = "200"
        let landlineField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        landlineField.applyThemeInput()
        landlineField.stringValue = "100"
        let mobileField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        mobileField.applyThemeInput()
        mobileField.stringValue = "100"
        let mediaField = NSTextField(frame: NSRect(x: 0, y: 0, width: 120, height: 24))
        mediaField.applyThemeInput()
        mediaField.stringValue = "400"

        let livePricingButton = NSButton(checkboxWithTitle: "Live pricing (Twilio API)", target: nil, action: nil)
        livePricingButton.applyThemeCheckbox()
        livePricingButton.state = .on

        let grid = NSGridView(views: [
            [NSTextField(labelWithString: "Страна:"), countryField],
            [NSTextField(labelWithString: "Inbound (мин):"), inboundField],
            [NSTextField(labelWithString: "Outbound landline (мин):"), landlineField],
            [NSTextField(labelWithString: "Outbound mobile (мин):"), mobileField],
            [NSTextField(labelWithString: "Media stream (мин):"), mediaField],
            [NSTextField(labelWithString: ""), livePricingButton],
        ])
        grid.rowSpacing = 6
        grid.columnSpacing = 10

        let alert = NSAlert()
        alert.messageText = "Оценка стоимости звонков"
        alert.informativeText = "Введите месячный микс минут. В live-режиме Gateway запросит Twilio Pricing API."
        alert.accessoryView = grid
        alert.addButton(withTitle: "Рассчитать")
        alert.addButton(withTitle: "Отмена")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let country = countryField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        let inbound = Double(inboundField.stringValue) ?? 200
        let landline = Double(landlineField.stringValue) ?? 100
        let mobile = Double(mobileField.stringValue) ?? 100
        let media = Double(mediaField.stringValue) ?? 400
        let useLivePricing = livePricingButton.state == .on

        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "call_assist_cost_estimate",
                    params: [
                        "country": country,
                        "minutes_inbound": inbound,
                        "minutes_outbound_landline": landline,
                        "minutes_outbound_mobile": mobile,
                        "minutes_media_stream": media,
                        "use_live_pricing": useLivePricing,
                    ]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Оценка стоимости", body: "Не удалось получить расчёт от Gateway.")
                }
                return
            }

            DispatchQueue.main.async {
                guard let self = self else { return }
                let report = HistoryPanelController.formatCallCostEstimate(result)
                self.appendCallAssistOutput(title: "Оценка стоимости", body: report)
                self.showInfoAlert(title: "Оценка стоимости", body: report)
            }
        }
    }

    // MARK: - Timeline

    @objc func onFetchCallTimeline() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "call_assist_timeline",
                    params: ["limit": 50]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Timeline", body: "Не удалось получить timeline.")
                }
                return
            }

            let items = (result["items"] as? [[String: Any]]) ?? []
            if items.isEmpty {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Timeline", body: "Пока событий нет.")
                }
                return
            }
            var summaryText = ""
            if
                let summaryResponse = try? ipcClient.call(
                    method: "call_assist_timeline_summary",
                    params: ["limit": 200, "max_tasks": 5]
                ),
                let summaryResult = summaryResponse["result"] as? [String: Any]
            {
                let summary = ((summaryResult["summary"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if !summary.isEmpty {
                    summaryText = "summary: \(summary)\n\n"
                }
            }
            var statsText = ""
            if
                let statsResponse = try? ipcClient.call(
                    method: "call_assist_timeline_stats",
                    params: ["limit": 200]
                ),
                let statsResult = statsResponse["result"] as? [String: Any],
                let stats = statsResult["stats"] as? [String: Any]
            {
                let count = (stats["count"] as? Int) ?? 0
                let chars = (stats["text_chars"] as? Int) ?? 0
                var kindsChunk = ""
                if let byKind = stats["by_kind"] as? [String: Any], !byKind.isEmpty {
                    let pairs = byKind.keys.sorted().map { key -> String in
                        let value = byKind[key] ?? 0
                        return "\(key)=\(value)"
                    }
                    kindsChunk = pairs.joined(separator: ", ")
                }
                statsText = "stats: count=\(count), text_chars=\(chars)\nby_kind: \(kindsChunk)\n\n"
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                let preview = HistoryPanelController.formatCallTimelinePreview(items: Array(items.prefix(12)))
                self.appendCallAssistOutput(
                    title: "Timeline",
                    body: "Событий: \(items.count)\n\(summaryText)\(statsText)\(preview)"
                )
            }
        }
    }

    @objc func onExportCallTimeline() {
        let formatAlert = NSAlert()
        formatAlert.messageText = "Экспорт Timeline"
        formatAlert.informativeText = "Выберите формат выгрузки текущей звонковой сессии."
        let formatSelector = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 220, height: 24), pullsDown: false)
        formatSelector.addItems(withTitles: ["Markdown (.md)", "NDJSON (.ndjson)"])
        formatSelector.selectItem(at: 0)
        formatAlert.accessoryView = formatSelector
        formatAlert.addButton(withTitle: "Экспорт")
        formatAlert.addButton(withTitle: "Отмена")
        guard formatAlert.runModal() == .alertFirstButtonReturn else { return }

        let selected = formatSelector.indexOfSelectedItem
        let exportFormat = selected == 1 ? "ndjson" : "md"
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "call_assist_timeline_export",
                    params: [
                        "format": exportFormat,
                        "limit": 400,
                    ]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Timeline export", body: "Не удалось выгрузить timeline.")
                }
                return
            }
            let content = (result["content"] as? String) ?? ""
            if content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Timeline export", body: "Timeline пуст, экспортировать нечего.")
                }
                return
            }
            // NSSavePanel + file write — должны идти на main (NSSavePanel требует main).
            DispatchQueue.main.async {
                guard let self = self else { return }
                let savePanel = NSSavePanel()
                let formatter = DateFormatter()
                formatter.dateFormat = "yyyyMMdd_HHmmss"
                let suffix = exportFormat == "ndjson" ? "ndjson" : "md"
                savePanel.nameFieldStringValue = "krab_call_timeline_\(formatter.string(from: Date())).\(suffix)"
                savePanel.canCreateDirectories = true
                if savePanel.runModal() != .OK {
                    return
                }
                guard let url = savePanel.url else { return }
                do {
                    try content.write(to: url, atomically: true, encoding: .utf8)
                    self.appendCallAssistOutput(title: "Timeline export", body: "Сохранено: \(url.path)")
                } catch {
                    self.appendCallAssistOutput(title: "Timeline export", body: "Ошибка записи файла: \(error.localizedDescription)")
                }
            }
        }
    }

    @objc func onClearCallTimeline() {
        let keepLast = selectedCallTimelineKeepLast()
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "call_assist_timeline_clear",
                    params: ["keep_last": keepLast]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(title: "Timeline clear", body: "Не удалось очистить timeline.")
                }
                return
            }
            let before = (result["before"] as? Int) ?? -1
            let after = (result["after"] as? Int) ?? -1
            DispatchQueue.main.async {
                self?.appendCallAssistOutput(
                    title: "Timeline clear",
                    body: "Очистка завершена. keep_last=\(keepLast), before=\(before), after=\(after)"
                )
            }
        }
    }

    @objc func onSaveCallTimelineToHistory() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "call_assist_timeline_to_history",
                    params: [
                        "format": "md",
                        "limit": 500,
                    ]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.appendCallAssistOutput(
                        title: "Timeline -> история",
                        body: "Не удалось сохранить timeline в историю."
                    )
                }
                return
            }
            let historyId = (result["history_id"] as? String) ?? "-"
            let chars = (result["chars"] as? Int) ?? 0
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.appendCallAssistOutput(
                    title: "Timeline -> история",
                    body: "Сохранено в историю. id=\(historyId), chars=\(chars)"
                )
                self.loadInitial()
            }
        }
    }

    // MARK: - Helpers

    func selectedCallTimelineKeepLast() -> Int {
        let raw = callTimelineKeepLastSelector.titleOfSelectedItem ?? "keep 1"
        let digits = raw.filter(\.isNumber)
        return Int(digits) ?? 1
    }

    nonisolated static func formatCallTimelinePreview(items: [[String: Any]]) -> String {
        var lines: [String] = []
        for item in items {
            let ts = (item["ts"] as? String) ?? "-"
            let kind = (item["kind"] as? String) ?? "unknown"
            let text = ((item["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let shortText: String
            if text.isEmpty {
                shortText = "(без текста)"
            } else if text.count > 120 {
                shortText = String(text.prefix(120)) + "…"
            } else {
                shortText = text
            }
            lines.append("[\(ts)] \(kind): \(shortText)")
        }
        return lines.joined(separator: "\n")
    }

    nonisolated static func formatCallSummary(_ payload: [String: Any]) -> String {
        let summaryText = ((payload["summary"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let rawTasks = (payload["tasks"] as? [Any]) ?? []
        var tasks: [String] = []
        for raw in rawTasks {
            if let dict = raw as? [String: Any] {
                let candidate = (
                    (dict["task"] as? String)
                    ?? (dict["title"] as? String)
                    ?? (dict["text"] as? String)
                    ?? ""
                ).trimmingCharacters(in: .whitespacesAndNewlines)
                if !candidate.isEmpty {
                    tasks.append(candidate)
                }
            } else if let rawText = raw as? String {
                let candidate = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !candidate.isEmpty {
                    tasks.append(candidate)
                }
            }
        }
        let safeSummary = summaryText.isEmpty ? "—" : summaryText
        let tasksText = tasks.isEmpty ? "- (нет задач)" : tasks.prefix(10).enumerated().map { "\($0 + 1). \($1)" }.joined(separator: "\n")
        return """
        \(safeSummary)
        tasks:
        \(tasksText)
        """
    }

    nonisolated static func formatCallCostEstimate(_ payload: [String: Any]) -> String {
        let country = (payload["country"] as? String) ?? "n/a"
        let ratesSource = (payload["rates_source"] as? String) ?? "unknown"
        let ratesNote = ((payload["rates_note"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        let telephony = (payload["telephony_usd"] as? [String: Any]) ?? [:]
        let ai = (payload["ai_usd"] as? [String: Any]) ?? [:]
        let total = (payload["total_usd"] as? Double)
            ?? (payload["total_usd"] as? NSNumber)?.doubleValue
            ?? 0.0

        let telephonyTotal = (telephony["total"] as? Double)
            ?? (telephony["total"] as? NSNumber)?.doubleValue
            ?? 0.0
        let aiTotal = (ai["total"] as? Double)
            ?? (ai["total"] as? NSNumber)?.doubleValue
            ?? 0.0

        let noteLine = ratesNote.isEmpty ? "" : "\nrates_note: \(ratesNote)"
        return """
        country: \(country)
        rates_source: \(ratesSource)\(noteLine)
        telephony_total_usd: \(String(format: "%.3f", telephonyTotal))
        ai_total_usd: \(String(format: "%.3f", aiTotal))
        total_usd: \(String(format: "%.3f", total))
        """
    }

    // MARK: - Capture Source

    func selectedCaptureSourceMode() -> String {
        switch captureSourceSelector.indexOfSelectedItem {
        case 1:
            return "system_audio"
        case 2:
            return "mic_plus_system"
        default:
            return "mic"
        }
    }

    func selectCaptureSourceMode(_ mode: String) {
        switch mode {
        case "system_audio":
            captureSourceSelector.selectItem(at: 1)
        case "mic_plus_system":
            captureSourceSelector.selectItem(at: 2)
        default:
            captureSourceSelector.selectItem(at: 0)
        }
    }

    func selectedCallPhraseDirection() -> (sourceLang: String, targetLang: String) {
        switch callPhraseDirectionSelector.indexOfSelectedItem {
        case 1:
            return ("es", "ru")
        case 2:
            return ("auto", "ru")
        default:
            return ("ru", "es")
        }
    }

    // MARK: - Output / State

    func appendCallAssistOutput(title: String, body: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        let chunk = "[\(ts)] \(title)\n\(body)\n\n"
        let existing = callAssistOutputView.string
        let combined = chunk + existing
        callAssistOutputView.string = String(combined.prefix(6000))
    }

    func applyCallAssistState(_ state: [String: Any]) {
        let active = (state["active"] as? Bool) ?? false
        let status = ((state["status"] as? String) ?? (active ? "running" : "idle")).lowercased()
        let gatewayStatus = (state["gateway_status"] as? String) ?? ""
        let gatewayError = (state["gateway_error"] as? String) ?? ""
        let sessionId = (state["session_id"] as? String) ?? ""

        var chunks: [String] = []
        chunks.append("Call Assist: \(status)")
        if !sessionId.isEmpty {
            chunks.append("id \(sessionId)")
        }
        if !gatewayStatus.isEmpty {
            chunks.append("GW \(gatewayStatus)")
        }
        if !gatewayError.isEmpty {
            chunks.append("err \(gatewayError)")
        }
        callAssistStatusLabel.stringValue = chunks.joined(separator: " • ")
        callAssistStartButton.isEnabled = !active
        callAssistStopButton.isEnabled = active || status == "running"
    }

    func refreshCallAssistState(silentOnError: Bool = true) {
        guard
            let response = try? ipcClient.call(method: "get_call_assist_state", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            if !silentOnError {
                callAssistStatusLabel.stringValue = "Call Assist: backend недоступен"
            }
            return
        }
        applyCallAssistState(result)
    }

    func refreshCaptureSourceHint() {
        guard
            let response = try? ipcClient.call(method: "list_audio_inputs", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            captureSourceSelector.toolTip = "Список входных устройств недоступен."
            return
        }
        let count = (result["count"] as? Int) ?? 0
        let items = (result["items"] as? [[String: Any]]) ?? []
        let defaultName = items.first(where: { ($0["is_default"] as? Bool) == true })?["name"] as? String
        if let defaultName, !defaultName.isEmpty {
            captureSourceSelector.toolTip = "Доступно входов: \(count). По умолчанию: \(defaultName)"
        } else {
            captureSourceSelector.toolTip = "Доступно входов: \(count)"
        }
    }
}
