/*
 SentryConfig.swift — Sentry / GlitchTip crash telemetry initialisation.

 Полностью no-op если DSN не задан или пустая строка.
 Совместимо с self-hosted GlitchTip (Sentry-compatible protocol).

 Включение:
 1. Заведите проект на sentry.io или self-hosted GlitchTip.
 2. Сохраните DSN в settings через IPC-метод set_settings { "sentry_dsn": "https://..." }.
 3. DSN будет прочитан при следующем старте агента.
*/

import Foundation
import Sentry

@MainActor
enum SentryConfig {
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
    }
}
