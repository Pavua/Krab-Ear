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
    dependencies: [],
    targets: [
        .executableTarget(
            name: "KrabEarAgent",
            path: "Sources/KrabEarAgent"
        ),
    ]
)
