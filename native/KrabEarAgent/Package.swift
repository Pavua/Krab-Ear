// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "KrabEarAgent",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "KrabEarAgent", targets: ["KrabEarAgent"]),
    ],
    dependencies: [
        // Porcupine Swift SDK (Picovoice) — wake word detection.
        // Requires free access key from https://console.picovoice.ai
        // Custom "Краб" keyword (.ppn) trained via Picovoice Console.
        //
        // NOTE: Раскомментируйте когда получите AccessKey и .ppn файл:
        // .package(
        //     url: "https://github.com/Picovoice/porcupine",
        //     from: "3.0.0"
        // ),

        // Sentry Cocoa SDK — crash/error telemetry.
        // No-op если DSN не задан (см. SentryConfig.swift).
        // Совместимо с self-hosted GlitchTip (Sentry-compatible protocol).
        .package(
            url: "https://github.com/getsentry/sentry-cocoa.git",
            from: "8.0.0"
        ),
        .package(
            url: "https://github.com/alta/swift-opus.git",
            from: "0.0.2"
        ),
        // Sparkle 2 — автообновления .app (spec 2026-07-05-sparkle-auto-update).
        // ДИНАМИЧЕСКИЙ framework: бинарь требует Sparkle.framework по rpath
        // @executable_path/../Frameworks (см. linkerSettings ниже) — в бандле
        // это Contents/Frameworks/, для dev-бинаря native/runtime — native/Frameworks/.
        .package(
            url: "https://github.com/sparkle-project/Sparkle",
            from: "2.6.0"
        ),
    ],
    targets: [
        .executableTarget(
            name: "KrabEarAgent",
            dependencies: [
                // Раскомментируйте после добавления Porcupine зависимости:
                // .product(name: "Porcupine", package: "porcupine"),
                .product(name: "Sentry", package: "sentry-cocoa"),
                .product(name: "Opus", package: "swift-opus"),
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            path: "Sources/KrabEarAgent",
            swiftSettings: [
                // Swift 5 language mode: codebase ещё не ready к full Swift 6
                // strict-concurrency (sending 'self' / Sendable result types
                // в десятках call-sites вокруг IPCClient + DispatchQueue closures).
                // Local Xcode 26 был permissive, но Linux CI / macos-latest runner
                // enforce'ят strict mode когда Package using tools 6.0. Migration
                // tracked: docs/superpowers/specs/2026-05-XX-swift-6-migration.md (TBD)
                .swiftLanguageMode(.v5),
            ],
            linkerSettings: [
                // Sparkle.framework ищется рядом с бандлом: Contents/Frameworks.
                // swift build дополнительно зашивает абсолютный rpath на .build —
                // dev-бинарь на машине сборки находит framework и без копии.
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"]),
            ]
        ),
        .testTarget(
            name: "KrabEarAgentTests",
            dependencies: ["KrabEarAgent"],
            path: "Tests/KrabEarAgentTests"
        ),
        // UI / E2E test suite.
        // Слой 1 (KrabEarSettingsLogicTests, KrabEarSyntheticHotkeyTests) — headless,
        //   запускается через `swift test --filter KrabEarAgentUITests`.
        // Слой 2 (KrabEarXCUIFlowTests) — полный XCUITest E2E, требует Xcode UITest
        //   host runner; тесты корректно skip при запуске через swift test.
        .testTarget(
            name: "KrabEarAgentUITests",
            dependencies: ["KrabEarAgent"],
            path: "Tests/KrabEarAgentUITests"
        ),
    ]
)
