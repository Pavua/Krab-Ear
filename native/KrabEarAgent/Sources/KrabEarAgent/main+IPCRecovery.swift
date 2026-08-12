/*
 main+IPCRecovery.swift
 AgentAppDelegate extension: IPC call wrapper with automatic backend restart on connection errors.
*/

import Foundation

extension AgentAppDelegate {

    // MARK: - Backend recovery

    /// Обёртка над IPC-вызовом с автоматическим перезапуском backend при ошибке соединения.
    ///
    /// Если первый вызов падает с socketConnectFailed/writeFailed/readFailed,
    /// пробуем `backendSupervisor.restartIfDead()` и повторяем запрос один раз.
    func callWithRecovery(method: String, params: [String: Any] = [:]) throws -> [String: Any] {
        let startedAt = Date()
        do {
            return try ipcClient.call(method: method, params: params)
        } catch let error as IPCError where Self.isConnectionError(error) {
            logger.warn("IPC ошибка соединения (\(error.localizedDescription)), пытаюсь перезапустить backend...")
            if backendSupervisor.restartIfDead() {
                logger.info("Backend перезапущен, повторяю вызов \(method)")
                let result = try ipcClient.call(method: method, params: params)
                reportReconnect(since: startedAt)
                return result
            } else {
                logger.error("Не удалось перезапустить backend (лимит перезапусков)")
                throw error
            }
        }
    }

    /// Асинхронный вариант для @MainActor call-site: и socket I/O, и возможный
    /// restart выполняются вне главного run loop. Не использовать для R2 stop —
    /// там собственный generation-aware budget RecordingStopCoordinator.
    func callAsyncWithRecovery(
        method: String,
        params: [String: Any] = [:],
        timeoutSec: Int = IPCClient.defaultTimeoutSec
    ) async throws -> [String: Any] {
        let client = ipcClient
        let startedAt = Date()
        do {
            return try await client.callAsync(
                method: method,
                params: params,
                timeoutSec: timeoutSec
            )
        } catch let error as IPCError where Self.isConnectionError(error) {
            logger.warn(
                "Async IPC ошибка соединения " +
                "(\(error.localizedDescription)), проверяю backend..."
            )
            let supervisor = backendSupervisor
            let restarted = await Task.detached {
                supervisor.restartIfDead()
            }.value
            guard restarted else {
                logger.error(
                    "Не удалось перезапустить backend (лимит перезапусков)"
                )
                throw error
            }
            logger.info("Backend перезапущен, повторяю async вызов \(method)")
            let result = try await client.callAsync(
                method: method,
                params: params,
                timeoutSec: timeoutSec
            )
            reportReconnect(since: startedAt)
            return result
        }
    }

    /// Сообщает backend'у, что вызов пережил перезапуск, — ровно после успешного
    /// повтора (провалившийся повтор бросает раньше и ничего не репортит).
    ///
    /// Fire-and-forget: телеметрия сама по себе IPC-вызов, и ожидание её ответа
    /// задержало бы путь, который только что и так потерял время на перезапуск.
    /// `attempts: 1` — recovery-путь делает ровно один повтор.
    private func reportReconnect(since startedAt: Date) {
        let client = ipcClient
        let durationMs = Int(Date().timeIntervalSince(startedAt) * 1000)
        Task.detached {
            await IPCReconnectTelemetry.report(
                client: client,
                attempts: 1,
                durationMs: durationMs
            )
        }
    }

    static func isConnectionError(_ error: IPCError) -> Bool {
        switch error {
        case .socketCreateFailed, .socketConnectFailed, .writeFailed, .readFailed:
            return true
        case .timeout:
            // Timeout means backend is alive but slow — do NOT trigger restart.
            return false
        case .invalidResponse, .backendError:
            return false
        }
    }
}
