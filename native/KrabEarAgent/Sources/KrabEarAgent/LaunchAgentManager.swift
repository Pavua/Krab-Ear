/*
 Управление launchd автозапуском Krab Ear Agent.

 Связи модуля:
 1) PermissionWizard/main.swift: включение/выключение автозапуска.
 2) Krab Ear.app bundle: единственный канонический способ запуска.

 ВАЖНО (Wave 30-A): install() теперь использует /usr/bin/open + .app bundle,
 а не scripts/start_agent.command (deprecated legacy binary).
 Это устраняет дублирование процессов — KeepAlive=false избегает respawn-петли,
 так как .app bundle управляется через macOS Login Items механизм.
*/

import Foundation

/// Управляет launchd автозапуском нативного агента.
final class LaunchAgentManager {
    // Wave 30-A: новый канонический label для .app bundle запуска
    private let label = "com.antigravity.krab-ear"
    // Устаревший label от legacy start_agent.command — используется только при uninstall
    private let legacyLabel = "com.krabear.agent"
    private let projectRoot: String

    init(projectRoot: String) {
        self.projectRoot = projectRoot
    }

    private var plistPath: String {
        let launchAgents = NSString(string: "~/Library/LaunchAgents").expandingTildeInPath
        return (launchAgents as NSString).appendingPathComponent("\(label).plist")
    }

    // Путь к legacy plist (com.krabear.agent) для cleanup
    private var legacyPlistPath: String {
        let launchAgents = NSString(string: "~/Library/LaunchAgents").expandingTildeInPath
        return (launchAgents as NSString).appendingPathComponent("\(legacyLabel).plist")
    }

    func setAutostart(enabled: Bool) {
        if enabled {
            install()
        } else {
            uninstall()
        }
    }

    func isAutostartEnabled() -> Bool {
        // Проверяем оба plist: новый canonical и старый legacy
        return FileManager.default.fileExists(atPath: plistPath)
            || FileManager.default.fileExists(atPath: legacyPlistPath)
    }

    func install() {
        let launchAgents = NSString(string: "~/Library/LaunchAgents").expandingTildeInPath
        try? FileManager.default.createDirectory(atPath: launchAgents, withIntermediateDirectories: true)

        // Wave 30-A: удаляем legacy plist если существует (избегаем дублирования)
        if FileManager.default.fileExists(atPath: legacyPlistPath) {
            let uid = getuid()
            _ = runLaunchctl(args: ["bootout", "gui/\(uid)", legacyPlistPath])
            try? FileManager.default.removeItem(atPath: legacyPlistPath)
        }

        // Canonical путь к .app bundle
        let appBundle = (projectRoot as NSString).appendingPathComponent("Krab Ear.app")
        let logsDir = (NSString(string: "~/Library/Logs").expandingTildeInPath)
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
                <string>\(appBundle)</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <false/>
            <key>StandardOutPath</key>
            <string>\(logsDir)/krab-ear-launchd-out.log</string>
            <key>StandardErrorPath</key>
            <string>\(logsDir)/krab-ear-launchd-err.log</string>
        </dict>
        </plist>
        """

        try? plist.write(toFile: plistPath, atomically: true, encoding: .utf8)
        reloadAgent()
    }

#if DEBUG
    /// Тест-хук: возвращает сгенерированный plist XML без записи на диск.
    /// Используется в unit-тестах для проверки содержимого без FileManager side-effects.
    /// Wave 30-A: генерирует canonical plist с /usr/bin/open + .app bundle (не legacy start_agent).
    func buildPlistContent() -> String {
        let appBundle = (projectRoot as NSString).appendingPathComponent("Krab Ear.app")
        let logsDir = (NSString(string: "~/Library/Logs").expandingTildeInPath)
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
                <string>\(appBundle)</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <false/>
            <key>StandardOutPath</key>
            <string>\(logsDir)/krab-ear-launchd-out.log</string>
            <key>StandardErrorPath</key>
            <string>\(logsDir)/krab-ear-launchd-err.log</string>
        </dict>
        </plist>
        """
    }

    /// Тест-хук: возвращает вычисленный путь к plist файлу без побочных эффектов.
    var plistPathForTest: String { plistPath }

    /// Тест-хук: возвращает путь к legacy plist файлу.
    var legacyPlistPathForTest: String { legacyPlistPath }

    /// Тест-хук: возвращает label агента.
    var labelForTest: String { label }
#endif

    func uninstall() {
        let uid = getuid()
        // Удаляем canonical plist (com.antigravity.krab-ear)
        _ = runLaunchctl(args: ["bootout", "gui/\(uid)", plistPath])
        try? FileManager.default.removeItem(atPath: plistPath)
        // Wave 30-A: также удаляем legacy plist (com.krabear.agent) если остался
        if FileManager.default.fileExists(atPath: legacyPlistPath) {
            _ = runLaunchctl(args: ["bootout", "gui/\(uid)", legacyPlistPath])
            try? FileManager.default.removeItem(atPath: legacyPlistPath)
        }
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
