/*
 HistoryPanelController+VoiceGatewayCard.swift — подключение к Voice Gateway.

 Починка (24.11.2026): ключ и адрес шлюза попадали в разговор с AI ОДИН раз — при
 создании панели (get_voice_gateway_credential в setupConversationTab). Сохранение ключа
 в настройках не обновляло ConversationViewController.config, и разговор продолжал
 стучаться без ключа (401) до перезапуска агента. Теперь каждое сохранение адреса/ключа
 сразу применяется к конфигу разговора.

 Секрет: значение ключа не логируется, не показывается и не попадает в тексты ошибок.
 IPC — только off-main (callAsync), AGENT-3.
*/

import AppKit

extension HistoryPanelController {

    /// Карточка вверху вкладки «Разговор с AI». Строка на «Live перевод» прячется:
    /// одно место для ключа, там, где человек получает ошибку подключения.
    func setupVoiceGatewayCard() {
        let card = voiceGatewayConnectionView
        card.setURL(settingsProvider().voiceGatewayURL)
        card.onCommitURL = { [weak self] url in self?.commitVoiceGatewayURL(url) }
        card.onCommitKey = { [weak self] key in self?.commitVoiceGatewayKey(key) }
        card.onCheck = { [weak self] in self?.runVoiceGatewayCheck() }
        card.onShowDetails = { [weak self] in self?.showVoiceGatewayCheckDetails() }
        // Старая строка «Gateway URL / API key» на «Live перевод»: сохраняла ключ только по
        // Enter и сжималась на узком окне до нечитаемых подписей. TODO: удалить строку после
        // мержа #2247 (он правит её placeholder) — здесь только скрываем, без конфликта строк.
        voiceGatewayRow.isHidden = true
        refreshVoiceGatewayKeyStatus()
    }

    /// Сохраняет адрес и сразу применяет к разговору (WS-адрес + HTTP-база).
    func commitVoiceGatewayURL(_ raw: String) {
        applySettingsPatch(["voice_gateway_url": raw])
        applyVoiceGatewayConfigToConversation(url: raw, key: nil)
    }

    /// Сохраняет ключ и сразу применяет к разговору. Пустую строку сюда не передают.
    func commitVoiceGatewayKey(_ raw: String) {
        applySettingsPatch(["voice_gateway_api_key": raw])
        applyVoiceGatewayConfigToConversation(url: nil, key: raw)
        voiceGatewayConnectionView.apply(keyStatus: raw.isEmpty ? .missing : .saved)
    }

    /// Обновляет конфиг разговора. Начатый разговор продолжает со старым соединением;
    /// новый ключ/адрес используются со следующего «Начать разговор».
    func applyVoiceGatewayConfigToConversation(url: String?, key: String?) {
        guard let vc = conversationVC else { return }
        if let key { vc.config.apiKey = key }
        if let url {
            var settings = settingsProvider()
            settings.voiceGatewayURL = url
            vc.config.wsURLString = buildConversationWSURL(from: settings)
            vc.config.httpBaseURLString = url
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        }
    }

    /// Есть ли сохранённый ключ — без раскрытия значения.
    func refreshVoiceGatewayKeyStatus() {
        let ipc = ipcClient
        Task { @MainActor [weak self] in
            let response = try? await ipc.callAsync(method: "get_voice_gateway_credential", params: [:])
            let key = (response?["result"] as? [String: Any])?["voice_gateway_api_key"] as? String
            guard let self else { return }
            if let key {
                self.voiceGatewayConnectionView.apply(keyStatus: key.isEmpty ? .missing : .saved)
            }
        }
    }

    // MARK: - Проверка подключения

    /// Честная проверка: /health — только адрес; «ключ принят» — лишь после ответа маршрута,
    /// который в VG требует авторизацию (GET /v1/settings/conversation, _auth_required).
    func runVoiceGatewayCheck() {
        let card = voiceGatewayConnectionView
        guard !card.isChecking else { return }
        card.setChecking(true)
        let ipc = ipcClient
        let fieldURL = card.urlField.stringValue
        Task { @MainActor [weak self] in
            let cred = try? await ipc.callAsync(method: "get_voice_gateway_credential", params: [:])
            let credResult = cred?["result"] as? [String: Any]
            let key = (credResult?["voice_gateway_api_key"] as? String) ?? ""
            var base = fieldURL.trimmingCharacters(in: .whitespacesAndNewlines)
            if base.isEmpty { base = (credResult?["voice_gateway_url"] as? String) ?? "" }
            if base.isEmpty { base = "http://127.0.0.1:8090" }
            base = base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))

            let outcome = await HistoryPanelController.probeVoiceGateway(base: base, apiKey: key)
            guard let self else { return }
            self.lastVoiceGatewayCheckDetails = outcome.details
            card.setChecking(false)
            card.apply(check: outcome.check, hasDetails: !outcome.details.isEmpty)
        }
    }

    /// Сетевые запросы проверки. Ключ уходит только в заголовок Authorization.
    /// `configuration` подменяется в тестах (URLProtocol-заглушка); в проде — .ephemeral.
    nonisolated static func probeVoiceGateway(
        base: String, apiKey: String,
        configuration: URLSessionConfiguration = .ephemeral
    ) async -> (check: VoiceGatewayCheck, details: String) {
        var details = "Адрес: \(base)\n"
        guard let healthURL = URL(string: base + "/health"),
              let authURL = URL(string: base + "/v1/settings/conversation") else {
            return (VoiceGatewayCheck.evaluate(healthStatus: nil, healthError: "некорректный адрес",
                                               authStatus: nil, keyProvided: !apiKey.isEmpty), details)
        }
        let session = URLSession(configuration: configuration)
        var healthStatus: Int?
        var healthError: String?
        do {
            var req = URLRequest(url: healthURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
            req.httpMethod = "GET"
            let (data, resp) = try await session.data(for: req)
            healthStatus = (resp as? HTTPURLResponse)?.statusCode
            details += "GET /health → HTTP \(healthStatus ?? 0)\n\(String(data: data.prefix(4000), encoding: .utf8) ?? "")\n"
        } catch {
            healthError = (error as NSError).localizedDescription
            details += "GET /health → ошибка: \(healthError ?? "")\n"
        }
        var authStatus: Int?
        if let code = healthStatus, (200..<300).contains(code) {
            var req = URLRequest(url: authURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
            req.httpMethod = "GET"
            if !apiKey.isEmpty { req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization") }
            if let (_, resp) = try? await session.data(for: req) {
                authStatus = (resp as? HTTPURLResponse)?.statusCode
            }
            details += "GET /v1/settings/conversation (\(apiKey.isEmpty ? "без ключа" : "с ключом")) → HTTP \(authStatus.map(String.init) ?? "нет ответа")\n"
        }
        session.finishTasksAndInvalidate()
        let check = VoiceGatewayCheck.evaluate(healthStatus: healthStatus, healthError: healthError,
                                               authStatus: authStatus, keyProvided: !apiKey.isEmpty)
        return (check, details)
    }

    func showVoiceGatewayCheckDetails() {
        let alert = NSAlert()
        alert.messageText = "Подробности проверки Voice Gateway"
        alert.informativeText = lastVoiceGatewayCheckDetails
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Закрыть")
        presentAlertSheet(alert, for: window) { _ in }
    }
}
