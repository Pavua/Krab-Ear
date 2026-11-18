import Foundation

/// Сериализует мутации и чтение после записи. Более старый ответ не может
/// прийти после подтверждения более новой локальной операции.
@MainActor
final class AgentSettingsOperationQueue {
    private var tail: Task<Bool, Never>?

    @discardableResult
    func enqueue(_ operation: @escaping @MainActor () async -> Bool) -> Task<Bool, Never> {
        let previous = tail
        let task = Task {
            _ = await previous?.value
            return await operation()
        }
        tail = task
        return task
    }
}
