/*
 PasteProfileCache.swift
 Extracted nonisolated value type for testability.
 Reader-writer cache с DispatchQueue concurrent + barrier writes.

 Используется из main+PasteAppMemory.swift как AgentAppDelegate.pasteProfileCache.
*/

import Foundation

/// Thread-safe reader-writer cache: bundleId → paste profile string.
/// Реализует классический паттерн concurrent-read / barrier-write через DispatchQueue.
public final class PasteProfileCache {
    private var storage: [String: String] = [:]
    private let queue = DispatchQueue(
        label: "com.krabear.pasteProfileCache.testable", attributes: .concurrent)

    public init() {}

    /// Возвращает профиль для bundleId или nil при cache-miss. Thread-safe.
    public func get(_ bundleId: String) -> String? {
        return queue.sync { storage[bundleId] }
    }

    /// Записывает профиль для bundleId. Barrier write. Thread-safe.
    public func set(_ profile: String, for bundleId: String) {
        queue.async(flags: .barrier) {
            self.storage[bundleId] = profile
        }
    }

    /// Очищает весь кеш. Barrier write. Thread-safe.
    public func clear() {
        queue.async(flags: .barrier) {
            self.storage.removeAll()
        }
    }

    /// Synchronous snapshot count (for testing assertions after barrier flushes).
    public var countSync: Int {
        return queue.sync { storage.count }
    }
}
