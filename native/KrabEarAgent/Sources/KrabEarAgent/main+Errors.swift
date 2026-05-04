/*
 main+Errors.swift
 AgentAppDelegate extension: Phase B.1 error bus wiring.

 Связывает:
 - ErrorActionHandler (парсит krab_error SSE, диспатчит action tap в backend)
 - ErrorToastPresenter (ToastPresenting — показывает UI toast при ошибках)
 - HealthMonitor.subscribeToProbeEvents (Task 13) — flash green на rewriter_recovered

 Lifecycle:
 - setupErrorBus(toastPresenter:) — вызывается из completeStartupAfterBackendReady()
 - tearDownErrorBus() — вызывается из applicationWillTerminate()

 Связи:
 - AgentAppDelegate: хранит errorActionHandler и sseErrorTask
 - ErrorActionHandler: парсит события + диспатчит actions
 - main+HealthMonitor.swift: healthMonitor property (для probe subscription)
*/

import AppKit
import Foundation
import ObjectiveC.runtime

// MARK: - Associated object keys

/// Уникальные ключи для хранения через objc runtime.
private nonisolated(unsafe) var errorActionHandlerKey: UInt8 = 0
private nonisolated(unsafe) var sseErrorTaskKey: UInt8 = 0

// MARK: - AgentAppDelegate + Error Bus

@MainActor
extension AgentAppDelegate {

    // MARK: - Accessors (stored via objc associated objects)

    /// Хранит ErrorActionHandler через ObjC associated objects.
    var errorActionHandler: ErrorActionHandler? {
        get {
            objc_getAssociatedObject(self, &errorActionHandlerKey) as? ErrorActionHandler
        }
        set {
            objc_setAssociatedObject(
                self,
                &errorActionHandlerKey,
                newValue,
                .OBJC_ASSOCIATION_RETAIN_NONATOMIC
            )
        }
    }

    /// Хранит Task SSE подписки на krab_error события.
    private var sseErrorTask: Task<Void, Never>? {
        get { objc_getAssociatedObject(self, &sseErrorTaskKey) as? Task<Void, Never> }
        set {
            objc_setAssociatedObject(
                self,
                &sseErrorTaskKey,
                newValue,
                .OBJC_ASSOCIATION_RETAIN_NONATOMIC
            )
        }
    }

    // MARK: - Setup

    /// Создаёт ErrorActionHandler, запускает SSE подписку на krab_error события,
    /// и (Task 13) подписывает HealthMonitor на rewriter_recovered → flashGreen.
    ///
    /// - Parameter toastPresenter: объект реализующий ToastPresenting.
    ///   В production передаётся `ErrorToastPresenter.shared`.
    func setupErrorBus(toastPresenter: any ToastPresenting) {
        let handler = ErrorActionHandler(
            ipcClient: ipcClient,
            toastPresenter: toastPresenter
        )
        self.errorActionHandler = handler
        logger.info("ErrorActionHandler инициализирован")

        // Запускаем SSE подписку на krab_error события
        startErrorBusSSEStream()

        // Task 13 wiring (HealthMonitor → rewriter_recovered → flashGreen) лежит ИСКЛЮЧИТЕЛЬНО
        // в setupHealthMonitor (main+HealthMonitor.swift) — оно выполняется первым в startup
        // последовательности. Дублировать здесь = silently no-op (если monitor не готов) +
        // double-subscribe (если оба путя выполнены) — оба сценария вредны. Single source of truth.
        logger.info("Error bus SSE stream запущен")
    }

    // MARK: - SSE stream for krab_error events

    /// Запускает SSE stream к /v1/events?filter=krab_error.
    /// События декодируются и передаются в ErrorActionHandler.handleErrorEvent.
    private func startErrorBusSSEStream(restBaseURL: String = "http://127.0.0.1:5005") {
        // Отменяем предыдущий task
        sseErrorTask?.cancel()

        guard let handler = errorActionHandler else { return }
        guard let url = URL(string: "\(restBaseURL)/v1/events?filter=krab_error") else { return }

        let sseBox = ErrorSSEBox(handler: handler)
        let task = Task.detached { [weak sseBox] in
            guard let sseBox else { return }
            await sseBox.startStreaming(url: url)
        }
        sseErrorTask = task
    }

    // MARK: - Tear down

    /// Освобождает ErrorActionHandler и останавливает SSE stream.
    func tearDownErrorBus() {
        sseErrorTask?.cancel()
        sseErrorTask = nil
        errorActionHandler = nil
        logger.info("ErrorActionHandler остановлен")
    }
}

// MARK: - ErrorSSEBox

/// URLSession-based SSE subscriber для krab_error событий.
/// Изолирован от actor для Swift 6 Sendable compliance.
private final class ErrorSSEBox: @unchecked Sendable {
    private let handler: ErrorActionHandler
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var sseDelegate: SSESessionDelegate?
    private var pendingEventType = ""

    init(handler: ErrorActionHandler) {
        self.handler = handler
    }

    deinit {
        task?.cancel()
        session?.invalidateAndCancel()
    }

    func startStreaming(url: URL) async {
        let weakSelf = self
        let delegate = SSESessionDelegate { line in
            weakSelf.handleSSELine(line)
        }
        self.sseDelegate = delegate
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        self.session = session

        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let dataTask = session.dataTask(with: request)
        self.task = dataTask
        dataTask.resume()

        await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                Task {
                    while !Task.isCancelled {
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                    continuation.resume()
                }
            }
        } onCancel: {
            weakSelf.task?.cancel()
            weakSelf.session?.invalidateAndCancel()
        }
    }

    private func handleSSELine(_ line: String) {
        if line.hasPrefix("event: ") {
            pendingEventType = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        } else if line.hasPrefix("data: ") {
            let eventType = pendingEventType
            let jsonStr = String(line.dropFirst(6))
            if eventType == "krab_error" {
                let handler = self.handler
                Task { @MainActor in
                    handler.handleRawSSEData(jsonStr)
                }
            }
            pendingEventType = ""
        } else if line.isEmpty {
            pendingEventType = ""
        }
    }
}
