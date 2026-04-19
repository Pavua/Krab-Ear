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
        do {
            return try ipcClient.call(method: method, params: params)
        } catch let error as IPCError where Self.isConnectionError(error) {
            logger.warn("IPC ошибка соединения (\(error.localizedDescription)), пытаюсь перезапустить backend...")
            if backendSupervisor.restartIfDead() {
                logger.info("Backend перезапущен, повторяю вызов \(method)")
                return try ipcClient.call(method: method, params: params)
            } else {
                logger.error("Не удалось перезапустить backend (лимит перезапусков)")
                throw error
            }
        }
    }

    static func isConnectionError(_ error: IPCError) -> Bool {
        switch error {
        case .socketCreateFailed, .socketConnectFailed, .writeFailed, .readFailed:
            return true
        case .invalidResponse, .backendError:
            return false
        }
    }
}
