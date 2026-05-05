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

import Foundation
import Sentry

@MainActor
enum SentryConfig {
    /// True после успешного вызова `initialize(dsn:)` с непустым DSN.
    /// Используется `recordTerminate(callsite:)` для guard-проверки.
    private(set) static var isActive: Bool = false
    /// Инициализирует Sentry SDK.
    /// - Parameters:
    ///   - dsn: DSN проекта. Nil или пустая строка — SDK не запускается (no-op).
    ///   - environment: окружение (production / staging / development).
    ///   - release: строка релиза, например "krab-ear@1.2.3". Nil = не передаётся.
    static func initialize(
        dsn: String?,
        environment: String = "production",
        release: String? = nil
    ) {
        guard let dsn, !dsn.isEmpty else {
            // DSN не задан — telemetry отключена, не логируем (normal path).
            return
        }

        SentrySDK.start { options in
            options.dsn = dsn
            options.environment = environment
            if let release {
                options.releaseName = release
            }
            // 5% traces — баланс между coverage и privacy.
            options.tracesSampleRate = 0.05
            // Не отправлять PII (IP, username, headers с токенами).
            options.sendDefaultPii = false
        }
        isActive = true
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
        release: String? = nil
    ) {
        let dsn = (settings["sentry_dsn_agent"] as? String)?.nonEmpty
            ?? (settings["sentry_dsn"] as? String)?.nonEmpty
            ?? ""
        initialize(dsn: dsn.isEmpty ? nil : dsn, environment: environment, release: release)
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
