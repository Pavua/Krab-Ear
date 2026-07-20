/*
 Управление launchd автозапуском Krab Ear Agent.

 Связи модуля:
 1) PermissionWizard/main.swift: включение/выключение автозапуска.
 2) Krab Ear.app bundle: canonical autostart target (Phase C.6.2).

 Исправление первопричины Phase C.6.2:
 - label plist: com.antigravity.krab-ear (канонический bundle ID);
 - ProgramArguments: /usr/bin/open -W <bundle path> — launchd открывает .app bundle,
   а не start_agent.command → runtime/KrabEarAgent;
 - install() идемпотентно удаляет старый com.krabear.agent.plist.

 Безопасность тестов:
 - production-initializer использует пользовательский `~/Library/LaunchAgents` и
   настоящий `/bin/launchctl`;
 - designated initializer принимает отдельный каталог и runner процессов, поэтому
   unit-тесты не могут менять живое launchd-состояние.
*/

import Foundation

/// Узкая граница для компонентов, которым нужно только включать автозапуск.
/// PermissionWizard зависит от этого протокола и в тестах получает spy без I/O.
protocol AutostartManaging: AnyObject {
    func setAutostart(enabled: Bool)
}

/// Подменяемый запуск процесса: тесты записывают запрос, не создавая `Process`.
typealias LaunchAgentProcessRunner = @Sendable (
    _ executable: String,
    _ arguments: [String]
) -> Int32

/// Управляет launchd автозапуском нативного агента.
final class LaunchAgentManager: AutostartManaging, @unchecked Sendable {
    /// Канонический label совпадает с bundle ID приложения.
    private let label = "com.antigravity.krab-ear"
    /// Старый label удаляется при install() во время одноразовой миграции.
    private let legacyLabel = "com.krabear.agent"
    private let projectRoot: String
    private let launchAgentsDirectory: URL
    private let processRunner: LaunchAgentProcessRunner

    /// Production-вход сохраняет прежнюю сигнатуру для всех runtime call-sites.
    convenience init(projectRoot: String) {
        self.init(
            projectRoot: projectRoot,
            launchAgentsDirectory: FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/LaunchAgents", isDirectory: true),
            processRunner: { executable, arguments in
                Self.runSystemProcess(executable: executable, arguments: arguments)
            }
        )
    }

    /// Designated initializer изолирует файловый каталог и запуск процессов.
    /// Тесты обязаны передавать UUID-temp directory и runner-spy.
    init(
        projectRoot: String,
        launchAgentsDirectory: URL,
        processRunner: @escaping LaunchAgentProcessRunner
    ) {
        self.projectRoot = projectRoot
        self.launchAgentsDirectory = launchAgentsDirectory
        self.processRunner = processRunner
    }

    private var plistPath: String {
        launchAgentsDirectory.appendingPathComponent("\(label).plist").path
    }

    /// Путь старого plist, удаляемого при миграции.
    private var legacyPlistPath: String {
        launchAgentsDirectory.appendingPathComponent("\(legacyLabel).plist").path
    }

    /// Вычисленный путь к .app bundle.
    /// Сначала ищет bundle рядом с корнем проекта, затем использует Bundle.main.
    private var bundlePath: String {
        let candidate = (projectRoot as NSString).appendingPathComponent("Krab Ear.app")
        if FileManager.default.fileExists(atPath: candidate) {
            return candidate
        }
        // Поднимаемся из внутренних каталогов до контейнера .app.
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
        try? FileManager.default.createDirectory(
            at: launchAgentsDirectory,
            withIntermediateDirectories: true
        )

        // Phase C.6.2: идемпотентно удаляем старый com.krabear.agent.plist.
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

    /// Удаляет старый com.krabear.agent.plist при одноразовой миграции.
    private func removeLegacyPlistIfPresent() {
        let path = legacyPlistPath
        guard FileManager.default.fileExists(atPath: path) else { return }
        let uid = getuid()
        _ = runLaunchctl(args: ["bootout", "gui/\(uid)", path])
        try? FileManager.default.removeItem(atPath: path)
    }

#if DEBUG
    /// Тест-хук: возвращает сгенерированный plist XML без записи на диск.
    /// Используется в unit-тестах для проверки содержимого без файловых изменений.
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

    /// Тест-хук: возвращает вычисленный bundle path без побочных эффектов.
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
        processRunner("/bin/launchctl", args)
    }

    /// Единственная live-реализация process runner; unit-тесты её не получают.
    private static func runSystemProcess(executable: String, arguments: [String]) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus
        } catch {
            return -1
        }
    }
}
