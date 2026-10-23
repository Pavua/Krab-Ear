import Foundation

/// Владелец cursor-based догрузки; UI и transport только подают сигналы.
@MainActor
final class TranslationReplaySubscription {
    typealias Fetch = @MainActor ([String: Any]) async throws -> [String: Any]
    private let fetch: Fetch
    private let onEntries: ([[String: Any]]) -> Void
    private let onGap: (String) -> Void
    private let onClear: () -> Void
    private(set) var cursor: String?
    private var active = false
    private var dirty = false
    private var timer: Timer?
    private var readTask: Task<Void, Never>?

    init(fetch: @escaping Fetch, onEntries: @escaping ([[String: Any]]) -> Void,
         onGap: @escaping (String) -> Void, onClear: @escaping () -> Void = {}) {
        self.fetch = fetch
        self.onEntries = onEntries
        self.onGap = onGap
        self.onClear = onClear
    }

    deinit {
        readTask?.cancel()
        let timer = timer
        DispatchQueue.main.async { timer?.invalidate() }
    }

    func start(pollInterval: TimeInterval = 2) {
        guard !active else { return }
        active = true
        let interval = pollInterval.isFinite && pollInterval > 0 ? max(0.05, pollInterval) : 2
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.reconcile() }
        }
        reconcile()
    }

    func reconcile() {
        guard active else { return }
        guard readTask == nil else { dirty = true; return }
        readPage(budget: 50)
    }

    func stop() {
        active = false
        dirty = false
        timer?.invalidate()
        timer = nil
        readTask?.cancel()
        readTask = nil
    }

    private func readPage(budget: Int) {
        guard active, readTask == nil, budget > 0 else { return }
        dirty = false
        var params: [String: Any] = ["cursor_mode": true, "event_type": "live_subs.result", "limit": 200]
        if let cursor { params["cursor"] = cursor }
        let previousCursor = cursor
        let fetch = fetch
        // До await удерживаем только fetch/params, не владельца или NSView.
        readTask = Task { [weak self] in
            do {
                let response = try await fetch(params)
                guard !Task.isCancelled, let self, self.active, self.cursor == previousCursor else { return }
                self.readTask = nil
                // Privacy очищает UI даже если остальная страница повреждена.
                if response["privacy_mode"] as? Bool == true { self.onClear() }
                guard self.active else { return }
                let page = try self.validate(response, after: previousCursor)
                let epochChanged = previousCursor?.split(separator: ":").first != page.cursor.split(separator: ":").first
                // Новый backend мог пропустить privacy/purge между чтениями.
                if page.reset_reason == "clear" || (previousCursor != nil && epochChanged) { self.onClear() }
                self.cursor = page.cursor
                guard self.active, !page.privacy_mode else { return }
                if let gap = page.gap { self.onGap(gap) }
                guard self.active else { return }
                self.onEntries(page.events.compactMap { event in
                    guard event.data.redacted != true else { return nil }
                    let text = event.data.original ?? event.data.text ?? ""
                    let translation = event.data.translation ?? event.data.translated ?? ""
                    guard !text.isEmpty || !translation.isEmpty else { return nil }
                    return ["text": text, "translation": translation]
                })
                // Ограничиваем один burst размером кольца. Остаток подхватит timer.
                if self.active, page.has_more || self.dirty { self.readPage(budget: budget - 1) }
            } catch {
                guard !Task.isCancelled, let self, self.active else { return }
                self.readTask = nil
                self.dirty = false
                // Сбой IPC/схемы не двигает cursor, строки остаются; timer повторит.
            }
        }
    }

    private struct Payload: Decodable {
        let original: String?
        let text: String?
        let translation: String?
        let translated: String?
        let redacted: Bool?
    }
    private struct Event: Decodable {
        let type: String
        let seq: Int
        let data: Payload
    }
    private struct Page: Decodable {
        let events: [Event]
        let cursor: String
        let has_more: Bool
        let gap: String?
        let privacy_mode: Bool
        let reset_reason: String?
    }
    private enum InvalidPage: Error { case malformed }

    private func parseCursor(_ value: String) throws -> (epoch: String, seq: Int) {
        let parts = value.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 2, parts[0].utf8.count == 32,
              parts[0].utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
              !parts[1].isEmpty, parts[1].utf8.count <= 19,
              parts[1].utf8.allSatisfy({ (48...57).contains($0) }),
              let seq = Int(parts[1]) else { throw InvalidPage.malformed }
        return (String(parts[0]), seq)
    }

    private func validate(_ response: [String: Any], after cursor: String?) throws -> Page {
        let bytes = try JSONSerialization.data(withJSONObject: response)
        let page = try JSONDecoder().decode(Page.self, from: bytes)
        let next = try parseCursor(page.cursor)
        guard page.events.count <= 200, [nil, "retention", "reset"].contains(page.gap),
              !page.has_more || !page.events.isEmpty else { throw InvalidPage.malformed }
        if page.gap == "reset" {
            guard ["restart", "clear"].contains(page.reset_reason) else { throw InvalidPage.malformed }
        } else if page.reset_reason != nil { throw InvalidPage.malformed }
        var previous = 0
        if let cursor {
            let old = try parseCursor(cursor)
            if old.epoch == next.epoch {
                guard page.gap != "reset", next.seq >= old.seq else { throw InvalidPage.malformed }
                previous = old.seq
            } else {
                guard page.gap == "reset" else { throw InvalidPage.malformed }
            }
        } else if page.gap != nil { throw InvalidPage.malformed }
        for event in page.events {
            guard event.type == "live_subs.result", event.seq > previous, event.seq <= next.seq else {
                throw InvalidPage.malformed
            }
            previous = event.seq
        }
        if page.has_more, page.events.last?.seq != next.seq { throw InvalidPage.malformed }
        return page
    }
}
