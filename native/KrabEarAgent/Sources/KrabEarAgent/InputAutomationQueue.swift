import Foundation

/// Одна очередь для собственного clipboard/input. Deadline включает ожидание FIFO.
enum InputAutomationFailure: String, Error { case busy, cancelled, deadlineExceeded, quarantined }

final class InputAutomationLease: @unchecked Sendable {
    let id = UUID()
    let deadline: ContinuousClock.Instant
    fileprivate init(deadline: ContinuousClock.Instant) { self.deadline = deadline }
}

final class InputAutomationQueue: @unchecked Sendable {
    static let shared = InputAutomationQueue()
    private let lock = NSLock()
    private let capacity: Int
    private var waiting: [UUID] = []
    private var active: UUID?
    private var quarantined = false
    private var epoch: UInt64 = 0
    private var clipboardEdges: [String: [Int: Int]] = [:]
    func recordClipboardWrite(board: String, before: Int, after: Int) {
        lock.withLock {
            var edges = clipboardEdges[board] ?? [:]
            if edges.count >= 64 { edges.removeAll() }
            edges[before] = after; clipboardEdges[board] = edges
        }
    }
    func ownsClipboardChanges(board: String, from: Int, to: Int) -> Bool {
        lock.withLock {
            var count = from
            for _ in 0..<65 {
                if count == to { return true }
                guard let next = clipboardEdges[board]?[count], next != count else { return false }
                count = next
            }
            return false
        }
    }
    init(capacity: Int = 8) { self.capacity = max(0, capacity) }
    var pendingCount: Int { lock.withLock { waiting.count } }
    var generation: UInt64 { lock.withLock { epoch } }
    /// Даже writer без await инвалидирует ещё не отправленный собственный input.
    func invalidatePending() { lock.withLock { epoch &+= 1 } }
    func quarantine() { lock.withLock { quarantined = true } }
    func release(_ lease: InputAutomationLease) {
        lock.withLock { if active == lease.id { active = nil } }
    }
    func owns(_ lease: InputAutomationLease) -> Bool { lock.withLock { active == lease.id && !quarantined } }
    func acquire(deadline: ContinuousClock.Instant) async -> Result<InputAutomationLease, InputAutomationFailure> {
        let lease = InputAutomationLease(deadline: deadline)
        let admission: InputAutomationFailure? = lock.withLock {
            if quarantined { return .quarantined }
            if active != nil ? waiting.count >= capacity : waiting.count > capacity { return .busy }
            waiting.append(lease.id)
            return nil
        }
        if let admission { return .failure(admission) }
        defer { lock.withLock { waiting.removeAll { $0 == lease.id } } }
        while true {
            if Task.isCancelled { return .failure(.cancelled) }
            if ContinuousClock.now >= deadline { return .failure(.deadlineExceeded) }
            let state: Int = lock.withLock {
                if quarantined { return -1 }
                if active == nil && waiting.first == lease.id {
                    waiting.removeFirst(); active = lease.id; return 1
                }
                return 0
            }
            if state < 0 { return .failure(.quarantined) }
            if state > 0 { return .success(lease) }
            do { try await Task.sleep(for: .milliseconds(10)) }
            catch { return .failure(.cancelled) }
        }
    }
}
