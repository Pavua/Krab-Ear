/*
 ConversationViewController+WebSocket — клиент URLSessionWebSocketTask.

 Протокол Voice Gateway:
 - uplink binary: PCM16 LE mono, точные 80 мс на частоте из `conv.ready`;
 - uplink control: JSON {"type":"control","action":"..."};
 - downlink binary: PCM16 LE mono на той же согласованной частоте;
 - downlink JSON: типизированные события ConversationEvent.

 URL сессии и авторизация задаются через ConversationConfig. Аудиофреймы до
 `conv.ready` не отправляются: Moshi принимает 1920@24k, legacy — 1280@16k.
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
    func startWebSocketSession(generation: UUID) {
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
        startReceiveLoop(task: task, generation: generation)
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

    private func startReceiveLoop(task: URLSessionWebSocketTask, generation: UUID) {
        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                Task { @MainActor [weak self] in
                    // UUID и идентичность задачи закрывают гонку остановки и
                    // быстрого перезапуска: старое
                    // завершение не может диспатчить событие или продолжить receive
                    // уже на сокете нового разговора.
                    guard let self,
                          self.acceptsConversationCallback(generation),
                          self.wsHolder.task === task else { return }
                    self.handleWSMessage(message, generation: generation)
                    // Планируем следующий receive — WebSocketTask не авто-повторяет.
                    self.startReceiveLoop(task: task, generation: generation)
                }

            case .failure(let error):
                Task { @MainActor [weak self] in
                    guard let self,
                          self.acceptsConversationCallback(generation),
                          self.wsHolder.task === task else { return }
                    let desc = (error as NSError).localizedDescription
                    AgentLogger.shared.info("[WS] Receive error: \(desc)")
                    self.classifyAndAnnounceWSFailure()
                    self.conversationState = .error(desc)
                    self.stopConversation()
                }
            }
        }
    }

    // MARK: - Message dispatch

    func handleWSMessage(_ message: URLSessionWebSocketTask.Message, generation: UUID) {
        guard acceptsConversationCallback(generation) else { return }
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
            // Бинарный PCM16 LE downlink передаём аудиослою для воспроизведения.
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

    /// Отправить бинарный PCM16 LE фрейм (вызывается из +Audio).
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

    /// Тестовый признак подтверждает, что изолированный режим не создал WebSocket task.
    var _testHasWebSocketTask: Bool {
        wsHolder.task != nil
    }

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
