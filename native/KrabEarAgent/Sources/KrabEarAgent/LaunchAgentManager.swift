/*
 Управление launchd автозапуском Krab Ear Agent.

 Связи модуля:
 1) PermissionWizard/main.swift: включение/выключение автозапуска.
 2) Krab Ear.app bundle: canonical autostart target (Phase C.6.2).

 Phase C.6.2 root-cause fix:
 - Plist label: com.antigravity.krab-ear (canonical bundle ID).
 - ProgramArguments: /usr/bin/open -W <bundle path> — launchd opens the .app bundle,
   not start_agent.command → runtime/KrabEarAgent.
 - install() removes legacy com.krabear.agent.plist on first run (idempotent).
*/

import Foundation

/// Управляет launchd автозапуском нативного агента.
final class LaunchAgentManager {
    /// Canonical label matching the app's bundle ID (com.antigravity.krab-ear).
    private let label = "com.antigravity.krab-ear"
    /// Legacy label used before Phase C.6.2 — removed on install() for one-time migration.
    private let legacyLabel = "com.krabear.agent"
    private let projectRoot: String

    init(projectRoot: String) {
        self.projectRoot = projectRoot
    }

    private var plistPath: String {
        let launchAgents = NSString(string: "~/Library/LaunchAgents").expandingTildeInPath
        return (launchAgents as NSString).appendingPathComponent("\(label).plist")
    }

    /// Path of the legacy plist that must be removed during migration.
    private var legacyPlistPath: String {
        let launchAgents = NSString(string: "~/Library/LaunchAgents").expandingTildeInPath
        return (launchAgents as NSString).appendingPathComponent("\(legacyLabel).plist")
    }

    /// Resolved path to the .app bundle.
    /// Prefers the bundle adjacent to the project root; falls back to Bundle.main.
    private var bundlePath: String {
        let candidate = (projectRoot as NSString).appendingPathComponent("Krab Ear.app")
        if FileManager.default.fileExists(atPath: candidate) {
            return candidate
        }
        // Fallback: strip inner bundle paths to reach the .app container.
        var url = Bundle.main.bundleURL
        while url.pathExtension != "app" && url.path != "/" {
            url.deleteLastPathComponent()
        }
        if url.pathExtension == "app" {
            return url.path
        }
        return candidate
    }

    func setAutostart(enabled: Bool) {
        if enabled {
            install()
        } else {
            uninstall()
        }
    }

    func isAutostartEnabled() -> Bool {
        return FileManager.default.fileExists(atPath: plistPath)
    }

    func install() {
        let launchAgents = NSString(string: "~/Library/LaunchAgents").expandingTildeInPath
        try? FileManager.default.createDirectory(atPath: launchAgents, withIntermediateDirectories: true)

        // Phase C.6.2: Remove legacy com.krabear.agent.plist (idempotent).
        removeLegacyPlistIfPresent()

        let bundlePathValue = bundlePath
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>\(label)</string>
            <key>ProgramArguments</key>
            <array>
                <string>/usr/bin/open</string>
                <string>-W</string>
                <string>\(bundlePathValue)</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>\(NSString(string: "~/Library/Logs/KrabEarAgent.log").expandingTildeInPath)</string>
            <key>StandardErrorPath</key>
            <string>\(NSString(string: "~/Library/Logs/KrabEarAgent.error.log").expandingTildeInPath)</string>
        </dict>
        </plist>
        """

        try? plist.write(toFile: plistPath, atomically: true, encoding: .utf8)
        reloadAgent()
    }

    /// Removes the legacy com.krabear.agent.plist if it exists (idempotent one-time migration).
    private func removeLegacyPlistIfPresent() {
        let path = legacyPlistPath
        guard FileManager.default.fileExists(atPath: path) else { return }
        let uid = getuid()
        _ = runLaunchctl(args: ["bootout", "gui/\(uid)", path])
        try? FileManager.default.removeItem(atPath: path)
    }

#if DEBUG
    /// Тест-хук: возвращает сгенерированный plist XML без записи на диск.
    /// Используется в unit-тестах для проверки содержимого без FileManager side-effects.
    func buildPlistContent() -> String {
        let bundlePathValue = bundlePath
        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>\(label)</string>
            <key>ProgramArguments</key>
            <array>
                <string>/usr/bin/open</string>
                <string>-W</string>
                <string>\(bundlePathValue)</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>\(NSString(string: "~/Library/Logs/KrabEarAgent.log").expandingTildeInPath)</string>
            <key>StandardErrorPath</key>
            <string>\(NSString(string: "~/Library/Logs/KrabEarAgent.error.log").expandingTildeInPath)</string>
        </dict>
        </plist>
        """
    }

    /// Тест-хук: возвращает вычисленный путь к plist файлу без побочных эффектов.
    var plistPathForTest: String { plistPath }

    /// Тест-хук: возвращает label агента.
    var labelForTest: String { label }

    /// Тест-хук: возвращает legacy label для проверки миграции.
    var legacyLabelForTest: String { legacyLabel }

    /// Тест-хук: возвращает resolved bundle path без побочных эффектов.
    var bundlePathForTest: String { bundlePath }
#endif

    func uninstall() {
        let uid = getuid()
        _ = runLaunchctl(args: ["bootout", "gui/\(uid)", plistPath])
        try? FileManager.default.removeItem(atPath: plistPath)
    }

    private func reloadAgent() {
        let uid = getuid()
        _ = runLaunchctl(args: ["bootout", "gui/\(uid)", plistPath])
        _ = runLaunchctl(args: ["bootstrap", "gui/\(uid)", plistPath])
    }

    @discardableResult
    private func runLaunchctl(args: [String]) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = args
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus
        } catch {
            return -1
        }
    }
}
