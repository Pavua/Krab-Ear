/*
 main+Errors.swift
 AgentAppDelegate extension: Phase B.1 error bus wiring.

 Связывает:
 - ErrorActionHandler (парсит krab_error SSE, диспатчит action tap в backend)
 - ToastPresenting (реализуется Task 11 ErrorToastPresenter)

 Task 13 добавит реальную SSE подписку и вызов setupErrorBus() из
 completeStartupAfterBackendReady(). Здесь только определение метода
 и associated object хранилище.

 Связи:
 - AgentAppDelegate: хранит errorActionHandler
 - ErrorActionHandler: парсит события + диспатчит actions
 - main+HealthMonitor.swift: аналогичный паттерн для Phase A
*/

import AppKit
import Foundation
import ObjectiveC.runtime

// MARK: - Associated object key

/// Уникальный ключ для хранения ErrorActionHandler через objc runtime.
/// Используем pointer identity — не shared mutable state.
private nonisolated(unsafe) var errorActionHandlerKey: UInt8 = 0

// MARK: - AgentAppDelegate + Error Bus

@MainActor
extension AgentAppDelegate {

    // MARK: - Accessor (stored via objc associated objects)

    /// Хранит ErrorActionHandler через ObjC associated objects.
    /// Nil до вызова setupErrorBus(toastPresenter:).
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

    // MARK: - Setup

    /// Создаёт ErrorActionHandler и привязывает его к IPC клиенту.
    ///
    /// - Parameter toastPresenter: объект, реализующий ToastPresenting (Task 11).
    ///   В production передаётся `ErrorToastPresenter.shared`.
    ///
    /// Task 13 вызывает этот метод из `completeStartupAfterBackendReady()` и
    /// добавляет реальную SSE подписку через `startErrorBusSSEStream()`.
    func setupErrorBus(toastPresenter: any ToastPresenting) {
        let handler = ErrorActionHandler(
            ipcClient: ipcClient,
            toastPresenter: toastPresenter
        )
        self.errorActionHandler = handler
        logger.info("ErrorActionHandler инициализирован")
        // SSE subscription wired in Task 13 (main+Errors.swift will be extended).
    }

    // MARK: - Tear down

    /// Освобождает ErrorActionHandler при завершении приложения.
    /// Вызывается из applicationWillTerminate (Task 13 добавит остановку SSE stream).
    func tearDownErrorBus() {
        errorActionHandler = nil
        logger.info("ErrorActionHandler остановлен")
    }
}
