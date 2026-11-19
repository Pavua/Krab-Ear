import Foundation
import os

/// Один непрерывный интервал подтверждённого OFF. ContinuousClock учитывает сон.
final class ConfirmedPrivacyLease: Sendable {
    let liveSubsEpoch: String?
    private let expiry: OSAllocatedUnfairLock<ContinuousClock.Instant?>

    init(until deadline: ContinuousClock.Instant, liveSubsEpoch: String? = nil) {
        expiry = OSAllocatedUnfairLock(initialState: deadline)
        self.liveSubsEpoch = liveSubsEpoch.flatMap { $0.isEmpty ? nil : $0 }
    }

    var deadline: ContinuousClock.Instant? {
        expiry.withLock { value in
            if let end = value, .now < end { return end }
            value = nil
            return nil
        }
    }

    var isValid: Bool { deadline != nil }

    func renew(until deadline: ContinuousClock.Instant) -> Bool {
        expiry.withLock { value in
            let now = ContinuousClock.now
            guard let end = value, now < end, now < deadline else {
                value = nil
                return false
            }
            value = deadline
            return true
        }
    }

    func revoke() { expiry.withLock { $0 = nil } }
}
