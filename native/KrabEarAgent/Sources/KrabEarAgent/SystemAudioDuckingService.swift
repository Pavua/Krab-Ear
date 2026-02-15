/*
 Управление приглушением системного звука во время записи Krab Ear.

 Связи модуля:
 1) main.swift: вызывает duck на старте записи и restore на остановке.
*/

import Foundation

/// Сервис временного приглушения системного вывода звука macOS.
final class SystemAudioDuckingService {
    private struct Snapshot {
        let outputMuted: Bool
        let outputVolume: Int
    }

    private let logger = AgentLogger.shared
    private var snapshot: Snapshot?
    private var isDucked = false

    /// Приглушает системный звук и запоминает исходное состояние.
    func duckForRecording(enabled: Bool, duckPercent: Int) {
        guard enabled else {
            logger.info("Приглушение системного звука отключено настройками")
            return
        }
        guard snapshot == nil else { return }

        guard let currentMuted = readBool(script: "output muted of (get volume settings)"),
              let currentVolume = readInt(script: "output volume of (get volume settings)")
        else {
            logger.warn("Не удалось получить текущее состояние системного звука")
            return
        }

        snapshot = Snapshot(outputMuted: currentMuted, outputVolume: currentVolume)
        isDucked = false

        if currentMuted {
            logger.info("Системный звук уже был muted до записи")
            return
        }

        let safePercent = max(0, min(duckPercent, 100))
        if safePercent >= 100 {
            let ok = run(script: "set volume output muted true")
            if ok {
                isDucked = true
                logger.info("Системный звук приглушен на время записи (muted)")
            } else {
                logger.warn("Не удалось приглушить системный звук")
            }
            return
        }

        let targetVolume = max(0, min(currentVolume * safePercent / 100, 100))
        let ok = run(script: "set volume output volume \(targetVolume)\nset volume output muted false")
        if ok {
            isDucked = true
            logger.info("Системный звук приглушен на время записи (volume=\(targetVolume), percent=\(safePercent))")
        } else {
            logger.warn("Не удалось приглушить системный звук")
        }
    }

    /// Восстанавливает состояние звука до записи.
    func restoreAfterRecording() {
        guard let snapshot else { return }
        defer { self.snapshot = nil }
        guard isDucked else { return }
        defer { isDucked = false }

        if snapshot.outputMuted {
            _ = run(script: "set volume output muted true")
            logger.info("Состояние звука восстановлено: осталось muted (как до записи)")
            return
        }

        let safeVolume = max(0, min(snapshot.outputVolume, 100))
        let restoreScript = "set volume output volume \(safeVolume)\nset volume output muted false"
        if run(script: restoreScript) {
            logger.info("Системный звук восстановлен после записи (volume=\(safeVolume))")
        } else {
            logger.warn("Не удалось восстановить системный звук после записи")
        }
    }

    private func run(script: String) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = script
            .split(separator: "\n")
            .flatMap { ["-e", String($0)] }

        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            logger.warn("Ошибка запуска osascript для звука: \(error.localizedDescription)")
            return false
        }
    }

    private func readInt(script: String) -> Int? {
        guard let raw = runAndCapture(script: script) else { return nil }
        return Int(raw.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private func readBool(script: String) -> Bool? {
        guard let raw = runAndCapture(script: script)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        else {
            return nil
        }
        if raw == "true" { return true }
        if raw == "false" { return false }
        return nil
    }

    private func runAndCapture(script: String) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]

        let outPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = Pipe()

        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return nil }
            let data = outPipe.fileHandleForReading.readDataToEndOfFile()
            return String(data: data, encoding: .utf8)
        } catch {
            logger.warn("Ошибка osascript capture для звука: \(error.localizedDescription)")
            return nil
        }
    }
}
