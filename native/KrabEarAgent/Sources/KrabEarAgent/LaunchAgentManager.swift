/*
 Управление launchd автозапуском Krab Ear Agent.

 Связи модуля:
 1) PermissionWizard/main.swift: включение/выключение автозапуска.
 2) scripts/start_agent.command: целевая команда launchd.
*/

import Foundation

/// Управляет launchd автозапуском нативного агента.
final class LaunchAgentManager {
    private let label = "com.krabear.agent"
    private let projectRoot: String

    init(projectRoot: String) {
        self.projectRoot = projectRoot
    }

    private var plistPath: String {
        let launchAgents = NSString(string: "~/Library/LaunchAgents").expandingTildeInPath
        return (launchAgents as NSString).appendingPathComponent("\(label).plist")
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

        let startScript = (projectRoot as NSString).appendingPathComponent("scripts/start_agent.command")
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>\(label)</string>
            <key>ProgramArguments</key>
            <array>
                <string>/bin/zsh</string>
                <string>\(startScript)</string>
                <string>--launched-by-launchd</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>WorkingDirectory</key>
            <string>\(projectRoot)</string>
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
