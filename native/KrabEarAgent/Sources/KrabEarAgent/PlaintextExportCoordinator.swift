// A5.3: один RAM coordinator для всех открытых файлов Swift и ручных backend-копий.
// Capability подтверждает контекст доверенного клиента, но не доказывает клик UI
// другому процессу того же UID. Не сериализовать контекст и не логировать ответы.
import Foundation
import CoreFoundation

enum PlaintextExportSink: String {
    case historyMarkdown = "history-md"
    case historyNDJSON = "history-ndjson"
    case historySelected = "history-selected"
    case historyActionItems = "history-action-items"
    case historyMeetingReport = "history-meeting-report"
    case historyStatsReport = "history-stats-report"
    case historyPDF = "history-pdf"
}

@MainActor
final class PlaintextExportCoordinator {
    enum BackendMethod: String {
        case exportHistoryMarkdown = "export_history"
        case runObsidianSync = "run_obsidian_sync"
    }

    enum Failure: Error, LocalizedError {
        case cancelled, closed, expired, privacy, unavailable, malformed, transport, writeFailed, partial
        var errorDescription: String? {
            switch self {
            case .cancelled: return "Сохранение открытого файла отменено."
            case .closed: return "Приложение завершает работу. Сохранение отменено."
            case .expired: return "Условия сохранения изменились. Повторите экспорт и подтвердите его заново."
            case .privacy: return "Сохранение открытых файлов недоступно в приватном режиме."
            case .unavailable: return "Не удалось проверить политику сохранения. Файл не сохранён."
            case .malformed: return "Ответ на запрос сохранения не прошёл проверку. Операция остановлена."
            case .transport: return "Результат запроса сохранения не подтверждён. Автоматического повтора не будет."
            case .writeFailed: return "Не удалось сохранить открытый файл. Проверьте выбранную папку."
            case .partial: return "Синхронизация завершена частично. Проверьте результат перед повтором."
            }
        }
    }

    /// Билет не раскрывает capability/context и не может быть сконструирован вызывающим кодом.
    final class Ticket: CustomStringConvertible, Sendable {
        fileprivate let id: UUID
        fileprivate let owner: UUID
        private let cleanup: @Sendable () -> Void
        fileprivate init(id: UUID, owner: UUID, cleanup: @escaping @Sendable () -> Void) {
            self.id = id
            self.owner = owner
            self.cleanup = cleanup
        }
        deinit { cleanup() }
        var description: String { "PlaintextExportTicket" }
    }
    final class BackendTicket: CustomStringConvertible, Sendable {
        fileprivate let id: UUID
        fileprivate let owner: UUID
        private let cleanup: @Sendable () -> Void
        fileprivate init(id: UUID, owner: UUID, cleanup: @escaping @Sendable () -> Void) {
            self.id = id
            self.owner = owner
            self.cleanup = cleanup
        }
        deinit { cleanup() }
        var description: String { "PlaintextBackendTicket" }
    }

    private struct Policy: Equatable {
        let epoch: String
        let generation: Int
        let encrypted: Bool
    }
    private struct Context {
        let version: UUID
        let session: String
        let policy: Policy
        let capability: String?
        var namespace: [String: Any] {
            ["app_session_id": session, "epoch": policy.epoch,
             "capability": capability as Any? ?? NSNull(),
             "expected_policy_generation": policy.generation]
        }
    }
    private final class Completion {
        var succeeded = false
    }
    private struct LocalOperation {
        let context: Context
        let sink: PlaintextExportSink
        let completion: Completion
    }
    private struct BackendOperation {
        let context: Context
        let method: BackendMethod
        let prerequisite: Completion?
    }

    private let ipc: IPCClient
    private let owner = UUID()
    private var session: String? = UUID().uuidString.lowercased()
    private var authorizationVersion = UUID()
    private var grant: Context?
    // Не участвует в авторизации: после ambiguous failure нужен только для quit.
    // Последний известный токен отзывает ВСЕ grants app-session на Python стороне.
    private var revokeOnlyContext: Context?
    private var pendingGrants = 0
    private var pendingRevocations: [UUID: Task<Void, Never>] = [:]
    private var cleanupWaiters: [CheckedContinuation<Void, Never>] = []
    private var locals: [UUID: LocalOperation] = [:]
    private var backends: [UUID: BackendOperation] = [:]
    private var sequence = 0
    private var seenReceipts = Set<String>()
    private var closed = false
    // MainActor реентерабелен: очередь удерживается через transport/consent await.
    private var inFlight = false
    private var waiters: [CheckedContinuation<Bool, Never>] = []

    init(ipc: IPCClient) { self.ipc = ipc }

    static func errorMessage(_ error: Error) -> String {
        (error as? Failure)?.errorDescription ?? Failure.writeFailed.errorDescription!
    }

    func prepare(sink: PlaintextExportSink, consent: () async -> Bool) async throws -> Ticket {
        try await enter()
        defer { leave() }
        let context = try await prepareContext(consent: consent)
        let id = UUID()
        locals[id] = LocalOperation(context: context, sink: sink, completion: Completion())
        return Ticket(id: id, owner: owner) { [weak self] in
            Task { @MainActor in self?.locals.removeValue(forKey: id) }
        }
    }

    func prepareBackend(method: BackendMethod, consent: () async -> Bool) async throws -> BackendTicket {
        try await enter()
        defer { leave() }
        let context = try await prepareContext(consent: consent)
        let id = UUID()
        backends[id] = BackendOperation(context: context, method: method, prerequisite: nil)
        return BackendTicket(id: id, owner: owner) { [weak self] in
            Task { @MainActor in self?.backends.removeValue(forKey: id) }
        }
    }

    /// Вторая Markdown-копия имеет собственный билет с контекстом ДО SavePanel.
    /// Она не получает разрешение от первой записи: backend проверит policy заново.
    func deriveBackendTicket(from ticket: Ticket, method: BackendMethod) throws -> BackendTicket {
        try ensureOpen()
        guard ticket.owner == owner, let local = locals[ticket.id] else { throw Failure.expired }
        let id = UUID()
        backends[id] = BackendOperation(context: local.context, method: method, prerequisite: local.completion)
        return BackendTicket(id: id, owner: owner) { [weak self] in
            Task { @MainActor in self?.backends.removeValue(forKey: id) }
        }
    }

    func discard(_ ticket: Ticket) {
        if ticket.owner == owner { locals.removeValue(forKey: ticket.id) }
    }
    func discard(_ ticket: BackendTicket) {
        if ticket.owner == owner { backends.removeValue(forKey: ticket.id) }
    }

    /// Синхронная closure — ровно одна запланированная запись, без await после validation.
    func perform(_ ticket: Ticket, write: () throws -> Void) async throws {
        try ensureOpen()
        // Удаление ДО первого await предотвращает повтор callback даже в очереди.
        guard ticket.owner == owner, let operation = locals.removeValue(forKey: ticket.id) else {
            throw Failure.expired
        }
        try await enter()
        defer { leave() }
        guard operation.context.version == authorizationVersion else { throw Failure.expired }
        guard sequence < Int.max else { invalidate(); throw Failure.expired }
        sequence += 1
        var params = operation.context.namespace
        params["operation_seq"] = sequence
        params["sink_kind"] = operation.sink.rawValue
        do {
            let response = try await request("validate_plaintext_export", params)
            try ensureOpen()
            let result = try successResult(response)
            guard epoch(result["epoch"]) == operation.context.policy.epoch,
                  integer(result["policy_generation"]) == operation.context.policy.generation,
                  integer(result["operation_seq"]) == sequence,
                  result["sink_kind"] as? String == operation.sink.rawValue,
                  let receipt = token(result["receipt"]), !seenReceipts.contains(receipt) else {
                throw Failure.malformed
            }
            seenReceipts.insert(receipt)
        } catch {
            invalidate()
            throw safeFailure(error)
        }
        do {
            try write()
            operation.completion.succeeded = true
        } catch {
            throw Failure.writeFailed
        }
    }

    /// Серверные writes не используют Swift operation_seq/receipt. Их per-file gate
    /// живёт в Python и получает только четыре поля сохранённого контекста.
    func performBackend(_ ticket: BackendTicket, params: [String: Any]) async throws -> [String: Any] {
        try ensureOpen()
        guard ticket.owner == owner, let operation = backends.removeValue(forKey: ticket.id) else {
            throw Failure.expired
        }
        guard params["plaintext_export"] == nil,
              operation.prerequisite?.succeeded != false else { throw Failure.expired }
        try await enter()
        defer { leave() }
        guard operation.context.version == authorizationVersion else { throw Failure.expired }
        var requestParams = params
        requestParams["plaintext_export"] = operation.context.namespace
        if operation.method == .exportHistoryMarkdown {
            requestParams["format"] = "md"
            requestParams["save_to_file"] = true
        }
        do {
            let response = try await request(operation.method.rawValue, requestParams)
            try ensureOpen()
            return try backendResult(response, method: operation.method)
        } catch {
            invalidate()
            throw safeFailure(error)
        }
    }

    /// Закрытие синхронно отсекает очереди/билеты и очищает RAM, не ждёт transport.
    /// Возвращённую best-effort задачу lifecycle ограничивает своим quit watchdog.
    @discardableResult
    func shutdown() -> Task<Void, Never>? {
        guard !closed else { return nil }
        closed = true
        let context = revokeOnlyContext
        revokeOnlyContext = nil
        session = nil
        invalidate()
        seenReceipts.removeAll()
        let pending = waiters
        waiters.removeAll()
        for waiter in pending { waiter.resume(returning: false) }
        if let context { _ = revoke(context) }
        guard pendingGrants > 0 || !pendingRevocations.isEmpty else { return nil }
        return Task { await waitForCleanup() }
    }

    private func prepareContext(consent: () async -> Bool) async throws -> Context {
        do {
            let response = try await request("get_plaintext_export_policy", [:])
            try ensureOpen()
            let result = try successResult(response)
            guard let currentEpoch = epoch(result["epoch"]),
                  let generation = integer(result["policy_generation"]),
                  let encrypted = boolean(result["encryption_enabled"]),
                  let privacy = boolean(result["privacy_mode_enabled"]),
                  let withoutGrant = boolean(result["allowed_without_grant"]),
                  withoutGrant == (!encrypted && !privacy) else { throw Failure.malformed }
            guard !privacy else { throw Failure.privacy }
            let policy = Policy(epoch: currentEpoch, generation: generation, encrypted: encrypted)
            guard let session else { throw Failure.closed }
            if let grant, grant.policy != policy { invalidate() }
            if !encrypted { return Context(version: authorizationVersion, session: session, policy: policy, capability: nil) }
            if let grant { return grant }
            guard await consent() else { throw Failure.cancelled }
            try ensureOpen()
            return try await issueGrant(policy: policy, session: session)
        } catch {
            invalidate()
            throw safeFailure(error)
        }
    }

    private func issueGrant(policy: Policy, session: String) async throws -> Context {
        pendingGrants += 1
        defer { pendingGrants -= 1; finishCleanupIfIdle() }
        let response = try await request("grant_plaintext_export_session", [
            "app_session_id": session, "expected_epoch": policy.epoch,
            "expected_policy_generation": policy.generation,
        ])
        let issued = try successResult(response)
        guard epoch(issued["epoch"]) == policy.epoch,
              integer(issued["policy_generation"]) == policy.generation,
              let capability = token(issued["capability"]) else { throw Failure.malformed }
        let context = Context(version: authorizationVersion, session: session, policy: policy, capability: capability)
        if closed {
            // Регистрируем поздний отзыв ДО pendingGrants decrement: normal quit
            // ждёт его до ответа или до общего watchdog, а не завершает app сразу.
            _ = revoke(context)
            throw Failure.closed
        }
        revokeOnlyContext = context
        try ensureOpen()
        grant = context
        return context
    }

    private func request(_ method: String, _ params: [String: Any]) async throws -> [String: Any] {
        // Никакого recovery/restart/retry при неоднозначном результате RPC.
        do { return try await ipc.callAsync(method: method, params: params, timeoutSec: IPCClient.quickTimeoutSec) }
        catch { throw Failure.transport }
    }

    private func revoke(_ context: Context) -> Task<Void, Never> {
        let client = ipc
        let params: [String: Any] = ["app_session_id": context.session,
                                    "epoch": context.policy.epoch,
                                    "capability": context.capability as Any? ?? NSNull()]
        let id = UUID()
        let task = Task { [weak self] in
            _ = try? await client.callAsync(method: "revoke_plaintext_export_session", params: params,
                                           timeoutSec: IPCClient.quickTimeoutSec)
            self?.pendingRevocations.removeValue(forKey: id)
            self?.finishCleanupIfIdle()
        }
        pendingRevocations[id] = task
        return task
    }

    private func waitForCleanup() async {
        if pendingGrants == 0 && pendingRevocations.isEmpty { return }
        await withCheckedContinuation { cleanupWaiters.append($0) }
    }
    private func finishCleanupIfIdle() {
        guard pendingGrants == 0 && pendingRevocations.isEmpty else { return }
        let pending = cleanupWaiters
        cleanupWaiters.removeAll()
        for continuation in pending { continuation.resume() }
    }

    private func ensureOpen() throws {
        guard !closed else { throw Failure.closed }
        guard !Task.isCancelled else { throw Failure.cancelled }
    }
    private func enter() async throws {
        try ensureOpen()
        if inFlight {
            let admitted = await withCheckedContinuation { waiters.append($0) }
            guard admitted else { throw Failure.closed }
            do { try ensureOpen() } catch { leave(); throw error }
        } else {
            inFlight = true
        }
    }
    private func leave() {
        if waiters.isEmpty { inFlight = false }
        else { waiters.removeFirst().resume(returning: true) }
    }
    private func invalidate() {
        // Изъятый из registry билет мог уже ждать FIFO. Его локальная версия
        // также отзывается: неопределённый запрос не оставляет queued старый grant.
        authorizationVersion = UUID()
        grant = nil
        locals.removeAll()
        backends.removeAll()
    }
    private func safeFailure(_ error: Error) -> Failure { error as? Failure ?? .transport }

    private func envelope(_ response: [String: Any]) throws -> [String: Any] {
        guard boolean(response["ok"]) == true,
              let id = response["id"] as? String, !id.isEmpty,
              response["error"] == nil,
              let result = response["result"] as? [String: Any] else { throw Failure.malformed }
        return result
    }
    private func successResult(_ response: [String: Any]) throws -> [String: Any] {
        let result = try envelope(response)
        guard let ok = boolean(result["ok"]) else { throw Failure.malformed }
        guard ok else { throw reason(result["reason"]) }
        guard result["reason"] == nil, result["error"] == nil else { throw Failure.malformed }
        return result
    }
    private func backendResult(_ response: [String: Any], method: BackendMethod) throws -> [String: Any] {
        let result = try envelope(response)
        if let value = result["ok"] {
            guard let ok = boolean(value) else { throw Failure.malformed }
            guard ok else { throw reason(result["reason"] ?? result["error"]) }
        }
        if result["error"] != nil { throw reason(result["reason"] ?? result["error"]) }
        switch method {
        case .exportHistoryMarkdown:
            if let value = result["reason"], !(value is NSNull) { throw reason(value) }
            guard result["content"] is String, integer(result["total_items"]) != nil,
                  let path = result["path"] as? String, !path.isEmpty else { throw Failure.writeFailed }
        case .runObsidianSync:
            guard integer(result["synced_count"]) != nil, integer(result["skipped_count"]) != nil,
                  let errors = result["errors"] as? [String],
                  result["new_files"] is [String], result["updated_files"] is [String],
                  let partial = boolean(result["partial"]) else { throw Failure.malformed }
            if partial || !errors.isEmpty { throw Failure.partial }
            if let value = result["reason"], !(value is NSNull) { throw reason(value) }
        }
        return result
    }
    private func reason(_ value: Any?) -> Failure {
        switch value as? String {
        case "privacy_mode_active": return .privacy
        case "plaintext_policy_unavailable": return .unavailable
        case "plaintext_session_expired", "plaintext_consent_required": return .expired
        default: return .unavailable
        }
    }
    private func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    private func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType)) else { return nil }
        let candidate = number.int64Value
        guard candidate >= 0, candidate <= Int.max,
              number.compare(NSNumber(value: candidate)) == .orderedSame else { return nil }
        return Int(candidate)
    }
    private func epoch(_ value: Any?) -> String? {
        guard let text = value as? String, text.utf8.count == 64,
              text.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else { return nil }
        return text
    }
    private func token(_ value: Any?) -> String? {
        guard let text = value as? String, text.utf8.count == 43,
              text.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0)
                || (97...122).contains($0) || $0 == 45 || $0 == 95 }) else { return nil }
        return text
    }
}
