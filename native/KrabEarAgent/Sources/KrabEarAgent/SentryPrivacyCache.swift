import Foundation
import CryptoKit

/// Указатель в preferences ограничивает обслуживание каталогом данного агента.
/// Право открыть его повторно подтверждается атомарной записью в самом каталоге.
final class SentryPrivacyCache: @unchecked Sendable {
    static let pointerKey = "KrabEar_SentryCacheEpoch"
    private let defaults: UserDefaults
    private let root: URL
    private let lock = NSLock()
    private var processFinished = false
    private let marker = ".privacy-binding-v1.json"

    struct Access: Sendable {
        let directory: URL
        let owner: UUID
    }

    private struct Binding: Codable {
        let version: Int
        let cacheID: UUID
        let policyEpoch: String?
        let destinationDigest: String
        let owner: UUID
    }

    init(defaults: UserDefaults, root: URL) {
        self.defaults = defaults
        self.root = root
    }

    private var previousID: UUID? {
        defaults.string(forKey: Self.pointerKey).flatMap(UUID.init(uuidString:))
    }

    private func directory(_ id: UUID) -> URL {
        root.appendingPathComponent(id.uuidString, isDirectory: true)
    }

    private func readBinding(_ id: UUID) -> Binding? {
        let url = directory(id).appendingPathComponent(marker)
        guard let file = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? file.close() }
        guard let data = try? file.read(upToCount: 4097), data.count <= 4096,
              let bound = try? JSONDecoder().decode(Binding.self, from: data),
              bound.version == 1, bound.cacheID == id else { return nil }
        return bound
    }

    func open(lease: ConfirmedPrivacyLease, dsn: String) -> Access? {
        lock.withLock {
            guard !processFinished else { return nil }
            guard lease.isValid else { discardLocked(); return nil }
            let digest = Data(SHA256.hash(data: Data(dsn.utf8))).base64EncodedString()
            let epoch = lease.liveSubsEpoch
            let id: UUID
            if let previousID, let epoch, let bound = readBinding(previousID),
               bound.policyEpoch == epoch, bound.destinationDigest == digest {
                id = previousID
            } else {
                discardLocked()
                id = UUID()
            }
            let dir = directory(id)
            let binding = Binding(version: 1, cacheID: id, policyEpoch: epoch,
                                  destinationDigest: digest, owner: UUID())
            do {
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                try JSONEncoder().encode(binding).write(to: dir.appendingPathComponent(marker), options: .atomic)
            } catch {
                discardLocked()
                try? FileManager.default.removeItem(at: dir)
                return nil
            }
            defaults.set(id.uuidString, forKey: Self.pointerKey)
            return Access(directory: dir, owner: binding.owner)
        }
    }

    func discard(afterDetaching stop: () -> Void) {
        let dir = lock.withLock { processFinished ? nil : detachLocked() }
        stop()
        if let dir { try? FileManager.default.removeItem(at: dir) }
    }

    /// Таймер прежнего SDK не должен удалить кэш уже открытого экземпляра.
    func discardExpired(_ access: Access) {
        lock.withLock {
            guard !processFinished, let id = previousID, directory(id) == access.directory,
                  readBinding(id)?.owner == access.owner else { return }
            discardLocked()
        }
    }

    /// Под тем же замком завершаем обслуживание ДО releaseFileLock агента.
    /// Таймер старого процесса больше не изменит диск при старте нового.
    func finishProcess(lease: ConfirmedPrivacyLease?) {
        lock.withLock {
            guard !processFinished else { return }
            if let lease, !lease.isValid { discardLocked() }
            processFinished = true
        }
    }

    private func discardLocked() {
        if let dir = detachLocked() { try? FileManager.default.removeItem(at: dir) }
    }

    private func detachLocked() -> URL? {
        guard defaults.object(forKey: Self.pointerKey) != nil else { return nil }
        let dir = previousID.map(directory)
        if let dir { try? FileManager.default.removeItem(at: dir.appendingPathComponent(marker)) }
        defaults.removeObject(forKey: Self.pointerKey)
        return dir
    }
}
