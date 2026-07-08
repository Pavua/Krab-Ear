/*
 ConversationViewController+WebSocket — URLSessionWebSocketTask клиент.

 Протокол (spec Section 4.1):
 - Uplink binary:   Opus PCM 16kHz 80ms фреймы (отправляется из +Audio).
 - Uplink control:  JSON {"type":"control","action":"..."}.
 - Downlink binary: Opus PCM 24kHz 80ms (воспроизводится в +Audio).
 - Downlink JSON:   события ConversationEvent (декодируются здесь).

 Реальный WS endpoint задаётся через ConversationConfig.wsURLString.
 Пока Voice Gateway (PR 1.1) не смерджен — URL является плейсхолдером,
 который пользователь может переопределить через настройки.
*/

import Foundation

extension ConversationViewController {

    // MARK: - Internal WS state (stored as associated objc objects to avoid stored properties)

    // Ключи для objc_getAssociatedObject — статические переменные типа UInt8 дают
    // стабильный UnsafeRawPointer без предупреждений об экспозиции String-буфера.
    nonisolated(unsafe) private static var wsHolderKey: UInt8 = 0

    // NSViewController не позволяет хранить свойства в extension — используем
    // вспомогательный класс-хранилище, прикреплённый через objc runtime.
    private var wsHolder: WSHolder {
        if let h = objc_getAssociatedObject(self, &ConversationViewController.wsHolderKey) as? WSHolder {
            return h
        }
        let h = WSHolder()
        objc_setAssociatedObject(self, &ConversationViewController.wsHolderKey, h, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return h
    }

    // MARK: - Session start / stop

    /// Открыть WS-соединение с Voice Gateway и начать receive-loop.
    func startWebSocketSession() {
        guard let url = URL(string: config.wsURLString) else {
            AgentLogger.shared.info("[WS] Невалидный URL: \(config.wsURLString)")
            conversationState = .error("Невалидный Gateway URL")
            return
        }

        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 30
        let session = URLSession(configuration: configuration)

        var request = URLRequest(url: url)
        if !config.apiKey.isEmpty {
            request.setValue("Bearer \(config.apiKey)", forHTTPHeaderField: "Authorization")
        }
        // Указываем движок и мозг через query params (spec-compatible).
        if var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            var items = components.queryItems ?? []
            if config.engine != "auto" { items.append(URLQueryItem(name: "engine", value: config.engine)) }
            if config.brain  != "auto" { items.append(URLQueryItem(name: "brain",  value: config.brain))  }
            if config.languageHint != "auto" { items.append(URLQueryItem(name: "lang", value: config.languageHint)) }
            // brain_mode (Волна 3b) — ВСЕГДА передаём явно, даже "auto":
            // Voice Gateway полагается на явный сигнал от клиента, не на умолчание сервера.
            items.append(URLQueryItem(name: "brain_mode", value: config.brainMode))
            if !items.isEmpty {
                components.queryItems = items
                if let newURL = components.url {
                    request.url = newURL
                }
            }
        }

        let task = session.webSocketTask(with: request)
        wsHolder.session = session
        wsHolder.task    = task
        task.resume()

        AgentLogger.shared.info("[WS] Connecting → \(url.absoluteString)")
        startReceiveLoop()
    }

    /// Закрыть соединение чисто (close frame).
    func closeWebSocket() {
        wsHolder.task?.cancel(with: .normalClosure, reason: nil)
        wsHolder.task    = nil
        wsHolder.session?.invalidateAndCancel()
        wsHolder.session = nil
        AgentLogger.shared.info("[WS] Closed")
    }

    // MARK: - Receive loop

    private func startReceiveLoop() {
        guard let task = wsHolder.task else { return }

        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.handleWSMessage(message)
                    // Планируем следующий receive — WebSocketTask не авто-повторяет.
                    self.startReceiveLoop()
                }

            case .failure(let error):
                Task { @MainActor [weak self] in
                    guard let self, self.isSessionActive else { return }
                    let desc = (error as NSError).localizedDescription
                    AgentLogger.shared.info("[WS] Receive error: \(desc)")
                    self.conversationState = .error(desc)
                    self.stopConversation()
                }
            }
        }
    }

    // MARK: - Message dispatch

    private func handleWSMessage(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            guard let data = text.data(using: .utf8),
                  let event = ConversationEvent.decode(from: data)
            else {
                AgentLogger.shared.info("[WS] Нераспознанный JSON: \(text.prefix(120))")
                return
            }
            handleDownlinkEvent(event)

        case .data(let data):
            // Бинарный Opus downlink — передаём в Audio extension для воспроизведения.
            handleDownlinkAudio(data)

        @unknown default:
            break
        }
    }

    // MARK: - Uplink helpers

    /// Отправить управляющую команду (JSON).
    func sendControlMessage(_ action: ConversationControlAction) {
        let msg = ConversationControlMessage(action: action)
        guard let data = msg.jsonData,
              let json = String(data: data, encoding: .utf8)
        else { return }

        wsHolder.task?.send(.string(json)) { error in
            if let error {
                AgentLogger.shared.info("[WS] Send control error: \(error.localizedDescription)")
            }
        }
    }

    /// Отправить бинарный Opus-фрейм (вызывается из +Audio).
    func sendAudioFrame(_ data: Data) {
        guard isSessionActive else { return }
        wsHolder.task?.send(.data(data)) { error in
            if let error {
                AgentLogger.shared.info("[WS] Send audio error: \(error.localizedDescription)")
            }
        }
    }
}

// MARK: - WSHolder (хранилище WS-объектов, обходит ограничения stored props в extension)

private final class WSHolder: NSObject {
    var task: URLSessionWebSocketTask?
    var session: URLSession?
}

// MARK: - DEBUG test hooks

#if DEBUG
extension ConversationViewController {

    /// Список JSON-строк, которые были отправлены через sendControlMessage в тестовом режиме.
    /// Используется в XCTest для проверки содержимого uplink-сообщений без реального сокета.
    nonisolated(unsafe) static var _testSentMessages: [String] = []

    /// Сброс тестового буфера между тестами.
    static func _resetTestState() {
        _testSentMessages = []
    }

    /// Собирает URLRequest, который был бы отправлен при connect(url:), без открытия сокета.
    /// Возвращает итоговый URL с query-параметрами для проверки в тестах.
    func _buildWSRequest(for urlString: String) -> URLRequest? {
        guard let url = URL(string: urlString) else { return nil }
        var request = URLRequest(url: url)
        if !config.apiKey.isEmpty {
            request.setValue("Bearer \(config.apiKey)", forHTTPHeaderField: "Authorization")
        }
        if var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            var items = components.queryItems ?? []
            if config.engine != "auto" { items.append(URLQueryItem(name: "engine", value: config.engine)) }
            if config.brain  != "auto" { items.append(URLQueryItem(name: "brain",  value: config.brain))  }
            if config.languageHint != "auto" { items.append(URLQueryItem(name: "lang", value: config.languageHint)) }
            items.append(URLQueryItem(name: "brain_mode", value: config.brainMode))
            if !items.isEmpty {
                components.queryItems = items
                if let newURL = components.url { request.url = newURL }
            }
        }
        return request
    }
}
#endif
