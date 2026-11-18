/*
 SentryConfig.swift — Sentry / GlitchTip crash telemetry initialisation.

 Полностью no-op если DSN не задан или пустая строка.
 Совместимо с self-hosted GlitchTip (Sentry-compatible protocol).

 Включение:
 1. Заведите проект на sentry.io или self-hosted GlitchTip.
 2. Сохраните DSN агента в settings через IPC-метод:
      set_settings { "sentry_dsn_agent": "https://..." }   ← только Swift-агент
      set_settings { "sentry_dsn": "https://..." }         ← только Python-backend (fallback)
    Если sentry_dsn_agent задан — агент использует его.
    Иначе fallback на sentry_dsn (backward-compat).
 3. DSN будет прочитан при следующем старте агента.
*/

import CoreFoundation
import Foundation
import os
import Sentry

@MainActor
final class SentryPrivacyLifecycle {
    private var dsn: String?
    private var lease: ConfirmedPrivacyLease?
    var isActive: Bool { dsn != nil && lease?.isValid == true }

    private let cache: SentryPrivacyCache
    private let start: (String, ConfirmedPrivacyLease, SentryPrivacyCache.Access) -> Void
    private let close: () -> Void

    init(cache: SentryPrivacyCache,
         start: @escaping (String, ConfirmedPrivacyLease, SentryPrivacyCache.Access) -> Void,
         close: @escaping () -> Void) {
        self.cache = cache
        self.start = start
        self.close = close
    }

    func reconcile(privacy: Bool?, dsn candidate: String?, lease next: ConfirmedPrivacyLease?) {
        guard privacy == false, let candidate, let next, next.isValid else {
            suspend()
            return
        }
        if lease === next && dsn == candidate { return }
        // Первый подтверждённый OFF после запуска вправе открыть прежний
        // crash cache: store сам проверяет epoch и получателя. Замена живого
        // или просроченного lease в этом процессе всегда отзывает старый кэш.
        if lease != nil { suspend() }
        guard let access = cache.open(lease: next, dsn: candidate) else { return }
        guard next.isValid else { cache.discardExpired(access); return }
        lease = next
        dsn = candidate
        start(candidate, next, access)
    }

    func finishProcess() { cache.finishProcess(lease: lease) }

    func suspend() {
        let running = dsn != nil
        dsn = nil
        lease = nil
        cache.discard { if running { close() } }
    }
}

func confirmedPrivacyFlag(_ value: Any?) -> Bool? {
    guard let flag = value as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID() else { return nil }
    return flag.boolValue
}

/// SDK создаёт задачу в init операции, а resume вызывает позже из очереди.
private final class SentryLeasedTask: URLSessionDataTask, @unchecked Sendable {
    private let task: URLSessionDataTask
    private let lease: ConfirmedPrivacyLease

    init(_ task: URLSessionDataTask, lease: ConfirmedPrivacyLease) {
        self.task = task
        self.lease = lease
        super.init()
    }

    override var originalRequest: URLRequest? { lease.isValid ? task.originalRequest : nil }
    override func resume() {
        if lease.isValid { task.resume() } else { task.cancel() }
    }
    override func cancel() { task.cancel() }
}

/// Sentry 8.58.2 создаёт HTTP-задачи через dataTask(with:completionHandler:),
/// в том числе из очереди при close(). NSURLSession не принимает новые задачи
/// после invalidateAndCancel(). Фасад оставляет SDK безопасный путь отказа.
final class SentryPrivacySession: URLSession, @unchecked Sendable {
    private let transport: OSAllocatedUnfairLock<URLSession?>
    private let lease: ConfirmedPrivacyLease
    private let expiryTask: Task<Void, Never>
    private static let rejected = URLSession(configuration: .ephemeral)
    private static let localRejection = URLRequest(url: URL(string: "about:blank")!)

    init(lease: ConfirmedPrivacyLease, ephemeralConfiguration: URLSessionConfiguration = .ephemeral,
         onExpiry: @escaping @Sendable () -> Void = {}) {
        self.lease = lease
        let transport = OSAllocatedUnfairLock<URLSession?>(initialState: URLSession(configuration: ephemeralConfiguration))
        self.transport = transport
        // Отменяет даже уже отправленные задачи, пока AppKit занят. Продление
        // разрешено только до истечения прежнего срока.
        expiryTask = Task.detached {
            while let deadline = lease.deadline {
                do { try await Task.sleep(until: deadline, clock: .continuous) }
                catch { return }
            }
            Self.cancelTransport(transport)
            onExpiry()
        }
        super.init()
    }

    override func dataTask(
        with request: URLRequest,
        completionHandler: @escaping @Sendable (Data?, URLResponse?, (any Error)?) -> Void
    ) -> URLSessionDataTask {
        transport.withLock { session in
            if let session, lease.isValid {
                return SentryLeasedTask(session.dataTask(with: request, completionHandler: completionHandler), lease: lease)
            }
            // В отказавшую задачу не передаём ни DSN, ни тело envelope.
            return Self.rejected.dataTask(with: Self.localRejection) { _, _, _ in
                completionHandler(nil, nil, URLError(.cancelled))
            }
        }
    }

    var allowsTelemetry: Bool { transport.withLock { $0 != nil && lease.isValid } }

    func revoke() {
        expiryTask.cancel()
        Self.cancelTransport(transport)
    }

    private static func cancelTransport(_ transport: OSAllocatedUnfairLock<URLSession?>) {
        let session = transport.withLock { session in
            defer { session = nil }
            return session
        }
        session?.invalidateAndCancel()
    }
}

@MainActor
enum SentryConfig {
    private static let cache = SentryPrivacyCache(
        defaults: .standard, root: URL.cachesDirectory.appendingPathComponent("KrabEarAgentSentry", isDirectory: true)
    )
    private static var transport: SentryPrivacySession?
    private static var environment = "production"
    private static var release: String?
    static let lifecycle = SentryPrivacyLifecycle(
        cache: cache, start: { startSDK(dsn: $0, lease: $1, access: $2) },
        close: {
            transport?.revoke()
            transport = nil
            SentrySDK.close()
        }
    )

    static var isActive: Bool { lifecycle.isActive }

    /// Инициализирует Sentry SDK.
    /// - Parameters:
    ///   - dsn: DSN проекта. Nil или пустая строка — SDK не запускается (no-op).
    ///   - environment: окружение (production / staging / development).
    ///   - release: строка релиза, например "krab-ear@1.2.3". Nil = не передаётся.
    ///   - settings: текущие настройки из IPC. Если ``privacy_mode_enabled`` = true,
    ///               Sentry инициализация полностью пропускается (no-op).
    static func initialize(
        dsn: String?,
        environment: String = "production",
        release: String? = nil,
        settings: [String: Any]? = nil,
        lease: ConfirmedPrivacyLease? = nil,
        owner: SentryPrivacyLifecycle? = nil
    ) {
        Self.environment = environment
        Self.release = release
        (owner ?? lifecycle).reconcile(privacy: confirmedPrivacyFlag(settings?["privacy_mode_enabled"]),
                                       dsn: dsn?.nonEmpty, lease: lease)
    }

    private static func startSDK(dsn: String, lease: ConfirmedPrivacyLease, access: SentryPrivacyCache.Access) {
        let store = cache
        let transport = SentryPrivacySession(lease: lease, onExpiry: { store.discardExpired(access) })
        Self.transport = transport

        // Читаем версию из Bundle один раз: CFBundleShortVersionString → releaseName,
        // CFBundleVersion → dist. Это гарантирует, что Sentry events всегда
        // несут корректные поля даже если caller передал release: nil.
        let bundleVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
        let bundleBuild   = Bundle.main.infoDictionary?["CFBundleVersion"] as? String
        let resolvedRelease = release ?? bundleVersion.map { "krab-ear@\($0)" }
        let resolvedDist    = bundleBuild ?? bundleVersion ?? "unknown"

        let environment = Self.environment
        SentrySDK.start { options in
            options.dsn = dsn
            options.environment = environment
            options.urlSession = transport
            options.cacheDirectoryPath = access.directory.path
            // При отзыве разрешения не ждём отправку очереди.
            options.shutdownTimeInterval = 0
            options.beforeSend = { event in
                transport.allowsTelemetry ? event : nil
            }
            if let r = resolvedRelease {
                options.releaseName = r
            }
            // dist = build number / version string; distinguishes builds within the same release.
            options.dist = resolvedDist
            // 5% traces — баланс между coverage и privacy.
            options.tracesSampleRate = 0.05
            // Не отправлять PII (IP, username, headers с токенами).
            options.sendDefaultPii = false
        }
    }

    /// Инициализирует Sentry SDK из словаря IPC-настроек.
    ///
    /// Приоритет DSN:
    ///   1. `sentry_dsn_agent` — специальный ключ для Swift-агента (krab-ear-agent project).
    ///   2. `sentry_dsn`       — общий ключ (krab-ear-backend project); backward-compat fallback.
    ///
    /// Разделение позволяет направлять Swift-крэши в отдельный Sentry-проект
    /// (`krab-ear-agent`), а Python-крэши — в `krab-ear-backend`.
    ///
    /// - Parameters:
    ///   - settings: словарь, возвращённый IPC-методом `get_settings`.
    ///   - environment: окружение (production / staging / development).
    ///   - release: строка релиза. Nil = не передаётся.
    static func initializeFromSettings(
        _ settings: [String: Any],
        environment: String = "production",
        release: String? = nil,
        fallbackDSN: String? = nil,
        lease: ConfirmedPrivacyLease? = nil,
        owner: SentryPrivacyLifecycle? = nil
    ) {
        let dsn = [settings["sentry_dsn_agent"] as? String, settings["sentry_dsn"] as? String, fallbackDSN]
            .compactMap { $0?.nonEmpty }
            .first { $0 != "REDACTED" }
        initialize(dsn: dsn, environment: environment, release: release, settings: settings, lease: lease, owner: owner)
    }

    // MARK: - Terminate breadcrumb

    /// Записывает Sentry breadcrumb перед завершением процесса.
    ///
    /// Вызывать непосредственно перед каждым `NSApp.terminate(nil)`.
    /// No-op если Sentry SDK не был инициализирован (DSN отсутствует).
    ///
    /// - Parameter callsite: имя callsite, например `"onQuit"`, `"stopAgent"`.
    static func recordTerminate(callsite: String) {
        // No-op если DSN не задан и SDK не был инициализирован.
        guard isActive else { return }

        SentrySDK.addBreadcrumb({
            let crumb = Breadcrumb()
            crumb.category = "lifecycle"
            crumb.level = .info
            crumb.message = "NSApp.terminate from \(callsite)"
            crumb.data = ["callsite": callsite]
            return crumb
        }())

        // Flush breadcrumb so it's guaranteed to be attached to the next event
        // (important when the process exits immediately after).
        SentrySDK.flush(timeout: 0.5)
    }

    // MARK: - General breadcrumb

    /// Записывает Sentry breadcrumb с произвольной категорией и данными.
    ///
    /// No-op если Sentry SDK не был инициализирован (DSN отсутствует).
    ///
    /// - Parameters:
    ///   - category: категория breadcrumb, например `"live_subs"`, `"lifecycle"`.
    ///   - message: краткое описание события.
    ///   - data: дополнительные структурированные данные (опционально).
    static func recordBreadcrumb(category: String, message: String, data: [String: Any] = [:]) {
        guard isActive else { return }

        SentrySDK.addBreadcrumb({
            let crumb = Breadcrumb()
            crumb.category = category
            crumb.message = message
            crumb.data = data.isEmpty ? nil : data
            crumb.level = .info
            return crumb
        }())
    }
}

// MARK: - Helpers

private extension String {
    /// Возвращает self если непустая строка, иначе nil.
    var nonEmpty: String? { isEmpty ? nil : self }
}
