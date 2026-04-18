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
    ],
    targets: [
        .executableTarget(
            name: "KrabEarAgent",
            path: "Sources/KrabEarAgent",
            // Раскомментируйте после добавления Porcupine зависимости:
            // dependencies: [
            //     .product(name: "Porcupine", package: "porcupine"),
            // ]
            swiftSettings: [
                // Закомментированный импорт Porcupine SDK не приводит к ошибкам.
                // Когда SDK добавлен — удалите эту секцию swiftSettings.
            ]
        ),
        .testTarget(
            name: "KrabEarAgentTests",
            dependencies: ["KrabEarAgent"],
            path: "Tests/KrabEarAgentTests"
        ),
    ]
)
