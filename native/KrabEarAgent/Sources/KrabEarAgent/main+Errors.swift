/*
 main+Errors.swift
 AgentAppDelegate extension: Phase B.1 error bus wiring.

 Транспорт (фикс 2026-07-05, сиблинг wake-word волны): IPC-поллинг
 (ErrorBusPoller), НЕ SSE. Прод = два раздельных OS-процесса — IPC-бэкенд
 (service.py) и REST-сервер (:5005), каждый со своим EventBus — событие
 krab_error, эмиттированное ErrorBus в IPC-процессе, никогда не доходило
 до SSE /v1/events REST-процесса. Старый ErrorSSEBox/startErrorBusSSEStream
 подписывался на канал, в который никто и никогда не публикует — тосты об
 ошибках были декоративны в проде. См. ErrorBusPoller.swift.

 Связывает:
 - ErrorActionHandler (декодирует KrabErrorPayload, диспатчит action tap в backend)
 - ErrorToastPresenter (ToastPresenting — показывает UI toast при ошибках)
 - HealthMonitor.subscribeToProbeEvents (Task 13) — flash green на rewriter_recovered

 Lifecycle:
 - setupErrorBus(toastPresenter:) — вызывается из completeStartupAfterBackendReady()
 - tearDownErrorBus() — вызывается из applicationWillTerminate()

 Связи:
 - AgentAppDelegate: хранит errorActionHandler и errorBusPoller
 - ErrorActionHandler: парсит события + диспатчит actions
 - ErrorBusPoller: IPC-поллинг list_recent_errors {since_seq}
 - main+HealthMonitor.swift: healthMonitor property (для probe subscription)
*/

import AppKit
import Foundation
import ObjectiveC.runtime

// MARK: - Associated object keys

/// Уникальные ключи для хранения через objc runtime.
private nonisolated(unsafe) var errorActionHandlerKey: UInt8 = 0
private nonisolated(unsafe) var errorBusPollerKey: UInt8 = 0

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

    /// Хранит ErrorBusPoller через ObjC associated objects.
    private var errorBusPoller: ErrorBusPoller? {
        get { objc_getAssociatedObject(self, &errorBusPollerKey) as? ErrorBusPoller }
        set {
            objc_setAssociatedObject(
                self,
                &errorBusPollerKey,
                newValue,
                .OBJC_ASSOCIATION_RETAIN_NONATOMIC
            )
        }
    }

    // MARK: - Setup

    /// Создаёт ErrorActionHandler + запускает IPC-поллинг krab_error событий.
    ///
    /// - Parameter toastPresenter: объект реализующий ToastPresenting. В
    ///   production вызывающая сторона передаёт свежесозданный
    ///   `ErrorToastPresenter()` (БЕЗ actionHandler — см. ниже).
    func setupErrorBus(toastPresenter: any ToastPresenting) {
        let handler = ErrorActionHandler(
            ipcClient: ipcClient,
            toastPresenter: toastPresenter
        )
        self.errorActionHandler = handler
        // Разрываем циклическую зависимость конструирования: ErrorToastPresenter
        // создаётся вызывающей стороной БЕЗ actionHandler (иначе понадобился бы
        // handler ДО его же собственного создания), теперь довязываем его
        // постфактум. actionHandler в presenter — weak var, единственное
        // использование (handleActionTap dispatch) уже толерантно к nil.
        if let presenter = toastPresenter as? ErrorToastPresenter {
            presenter.actionHandler = handler
        }
        logger.info("ErrorActionHandler инициализирован")

        let poller = ErrorBusPoller(
            ipcProvider: { [weak self] in self?.ipcClient },
            onNewErrors: { [weak self] payloads in
                // Один Task на батч (не на элемент) — последовательный await
                // сохраняет порядок тостов, если за один тик пришло несколько
                // новых ошибок сразу.
                Task { @MainActor in
                    guard let self, let handler = self.errorActionHandler else { return }
                    for payload in payloads {
                        // В отличие от старого StatusIndicatorView, это обновляет
                        // реальный NSStatusItem.button.image через AppDelegate.
                        self.applyErrorSeverityBadge(payload.severity)
                        await handler.handleErrorEvent(payload)
                    }
                }
            }
        )
        self.errorBusPoller = poller
        poller.activate()

        // Task 13 wiring (HealthMonitor → rewriter_recovered → flashGreen) лежит ИСКЛЮЧИТЕЛЬНО
        // в setupHealthMonitor (main+HealthMonitor.swift) — оно выполняется первым в startup
        // последовательности. Дублировать здесь = silently no-op (если monitor не готов) +
        // double-subscribe (если оба путя выполнены) — оба сценария вредны. Single source of truth.
        logger.info("Error bus IPC-поллинг запущен")
    }

    // MARK: - Tear down

    /// Освобождает ErrorActionHandler и останавливает IPC-поллинг.
    func tearDownErrorBus() {
        errorBusPoller?.deactivate()
        errorBusPoller = nil
        errorActionHandler = nil
        clearErrorSeverityBadge()
        logger.info("ErrorActionHandler остановлен")
    }
}
