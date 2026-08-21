import Foundation

struct VGSessionInfo: Equatable {
    let id: String
    let status: String
    let phone: String
    let callDirection: String
    let createdAt: String
    let updatedAt: String
    let srcLang: String
    let tgtLang: String
    let callBrief: String
}

protocol VGSessionFetching {
    /// GET {voice_gateway_url}/v1/sessions?limit=20 — completion на любой очереди.
    func fetchSessions(completion: @escaping (Result<(statusCode: Int, body: Data), Error>) -> Void)
}

protocol VGSessionWatcherDelegate: AnyObject {
    func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool)
    func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64)
    func watcherCallGone(sessionId: String, generation: UInt64)
    func watcherVGLost(sessionId: String, generation: UInt64)
    func watcherAuthRejected()
}

/// Дискавери живых звонков VG поллингом GET /v1/sessions (spec §3.1).
/// 🔴 Сессии VG НЕ исчезают из списка (терминальные строки остаются в SQLite,
/// рестарт VG патчит их в failed) → callGone ПРЕДИКАТНЫЙ, не по отсутствию.
/// fail ≠ absent; callGone только по успешному поллу, streak 2.
/// vgLost = 3 подряд неудачи И ≥30с с последнего успеха.
final class VGSessionWatcher {
    private struct Tracked {
        var generation: UInt64
        var goneStreak: Int = 0
        var terminal: Bool = false
        var lastStatus: String = ""
    }

    weak var delegate: VGSessionWatcherDelegate?

    private let fetcher: VGSessionFetching
    private let now: () -> Date
    private let monotonic: () -> TimeInterval
    private let queue = DispatchQueue(label: "krab.vg.watcher")
    private var tracked: [String: Tracked] = [:]
    private var failedStreak = 0
    private var lastSuccessUptime: TimeInterval?
    private var authHintFired = false
    private var running = false
    private static var generationCounter: UInt64 = 0
    private static let genLock = NSLock()

    private static let liveStatuses: Set<String> = ["created", "running", "paused"]
    private static let staleCutoff: TimeInterval = 6 * 3600  // = VG stale_running_session_max_age_hours
    private static let goneStreakThreshold = 2
    private static let vgLostFailures = 3
    private static let vgLostMinSilence: TimeInterval = 30

    init(fetcher: VGSessionFetching,
         now: @escaping () -> Date = Date.init,
         monotonic: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.fetcher = fetcher
        self.now = now
        self.monotonic = monotonic
    }

    func start() {
        queue.async {
            guard !self.running else { return }
            self.running = true
            self.scheduleNextLocked(after: 0.1)
        }
    }

    func stop() { queue.async { self.running = false } }

    /// Тестовый вход: один полл без таймера.
    func pollOnce(completion: (() -> Void)? = nil) {
        fetcher.fetchSessions { [weak self] result in
            guard let self else { completion?(); return }
            self.queue.async {
                self.handleLocked(result)
                completion?()
            }
        }
    }

    private static func nextGeneration() -> UInt64 {
        genLock.lock(); defer { genLock.unlock() }
        generationCounter += 1
        return generationCounter
    }

    private func scheduleNextLocked(after delay: TimeInterval) {
        guard running else { return }
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.running else { return }
            self.pollOnce { [weak self] in
                guard let self else { return }
                self.queue.async { self.scheduleNextLocked(after: self.currentCadenceLocked()) }
            }
        }
    }

    private func currentCadenceLocked() -> TimeInterval {
        if failedStreak > 0 { return min(60, 15 * Double(failedStreak)) }
        let hasLive = tracked.values.contains { !$0.terminal }
        return hasLive ? 2 : 3
    }

    private func handleLocked(_ result: Result<(statusCode: Int, body: Data), Error>) {
        switch result {
        case .failure:
            registerFailureLocked(authRejected: false)
        case .success(let resp) where resp.statusCode == 401 || resp.statusCode == 403:
            registerFailureLocked(authRejected: true)
        case .success(let resp) where resp.statusCode == 200:
            guard let obj = (try? JSONSerialization.jsonObject(with: resp.body)) as? [String: Any],
                  let items = obj["items"] as? [[String: Any]] else {
                registerFailureLocked(authRejected: false)
                return
            }
            handleSuccessLocked(items: items)
        case .success:
            registerFailureLocked(authRejected: false)
        }
    }

    private func registerFailureLocked(authRejected: Bool) {
        failedStreak += 1
        if authRejected && !authHintFired {
            authHintFired = true
            notify { $0.watcherAuthRejected() }
        }
        let silence = monotonic() - (lastSuccessUptime ?? monotonic())
        guard failedStreak >= Self.vgLostFailures,
              lastSuccessUptime != nil, silence >= Self.vgLostMinSilence else { return }
        for (id, entry) in tracked where !entry.terminal {
            tracked[id]?.terminal = true
            notify { $0.watcherVGLost(sessionId: id, generation: entry.generation) }
        }
    }

    private func handleSuccessLocked(items: [[String: Any]]) {
        failedStreak = 0
        authHintFired = false
        lastSuccessUptime = monotonic()

        var liveById: [String: VGSessionInfo] = [:]
        var seenIds = Set<String>()
        for raw in items {
            guard let info = Self.parse(raw) else { continue }
            seenIds.insert(info.id)
            if isLiveLocked(info) { liveById[info.id] = info }
        }

        for (id, info) in liveById {
            if var entry = tracked[id] {
                if entry.terminal {
                    // Resurrection (post-vgLost) — терминальный СТАТУС сюда не попадает,
                    // liveById уже отфильтрован предикатом.
                    let gen = Self.nextGeneration()
                    entry = Tracked(generation: gen, lastStatus: info.status)
                    tracked[id] = entry
                    notify { $0.watcherCallAppeared(info, generation: gen, resurrected: true) }
                } else {
                    entry.goneStreak = 0
                    entry.lastStatus = info.status
                    tracked[id] = entry
                    notify { $0.watcherCallUpdated(info, generation: entry.generation) }
                }
            } else {
                let gen = Self.nextGeneration()
                tracked[id] = Tracked(generation: gen, lastStatus: info.status)
                notify { $0.watcherCallAppeared(info, generation: gen, resurrected: false) }
            }
        }

        for (id, var entry) in tracked where !entry.terminal && liveById[id] == nil {
            entry.goneStreak += 1
            if entry.goneStreak >= Self.goneStreakThreshold {
                entry.terminal = true
                notify { $0.watcherCallGone(sessionId: id, generation: entry.generation) }
            }
            tracked[id] = entry
        }

        // Cleanup: терминальные записи, чьих id уже нет в ответе вовсе.
        for (id, entry) in tracked where entry.terminal && !seenIds.contains(id) {
            tracked.removeValue(forKey: id)
        }
    }

    private func isLiveLocked(_ s: VGSessionInfo) -> Bool {
        guard Self.liveStatuses.contains(s.status) else { return false }
        guard !s.phone.isEmpty || !s.callDirection.isEmpty else { return false }
        // stale-гард; непарсибельная дата — fail-open в сторону показа звонка.
        if let updated = Self.parseISO(s.updatedAt) {
            if now().timeIntervalSince(updated) > Self.staleCutoff { return false }
        }
        return true
    }

    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// VG отдаёт ISO и с долями секунды, и без — принимаем оба.
    static func parseISO(_ s: String) -> Date? {
        isoFractional.date(from: s) ?? isoPlain.date(from: s)
    }

    private static func parse(_ raw: [String: Any]) -> VGSessionInfo? {
        guard let id = raw["id"] as? String, let status = raw["status"] as? String else { return nil }
        func s(_ k: String) -> String { raw[k] as? String ?? "" }
        return VGSessionInfo(id: id, status: status, phone: s("phone"),
                             callDirection: s("call_direction"), createdAt: s("created_at"),
                             updatedAt: s("updated_at"), srcLang: s("src_lang"),
                             tgtLang: s("tgt_lang"), callBrief: s("call_brief"))
    }

    private func notify(_ block: @escaping (VGSessionWatcherDelegate) -> Void) {
        DispatchQueue.main.async { [weak self] in
            guard let d = self?.delegate else { return }
            block(d)
        }
    }
}
