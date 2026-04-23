/*
 SingleInstanceGuardTests — тесты defensive guard против дубликатов KrabEarAgent.

 Стратегия:
 - Инъектируем мок pgrepRunner вместо реального /usr/bin/pgrep.
 - Проверяем что функция убивает чужие PID-ы и пропускает selfPid.
 - Нет реального kill() — мок runner только возвращает строки с PID-ами.
   kill() к несуществующим PID-ам на macOS возвращает ESRCH (безвредно).
*/

import XCTest
@testable import KrabEarAgent

final class SingleInstanceGuardTests: XCTestCase {

    // MARK: - No duplicates

    /// Если pgrep возвращает только текущий PID — ничего не убиваем.
    func test_noDuplicates_returnsZero() {
        let selfPid = getpid()
        let runner: ([String]) -> String = { _ in "\(selfPid)\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Не должно быть убито ни одного процесса когда только self")
    }

    /// Пустой вывод pgrep — нет KrabEarAgent-процессов вообще.
    func test_emptyPgrepOutput_returnsZero() {
        let runner: ([String]) -> String = { _ in "" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Нет процессов — нечего убивать")
    }

    // MARK: - With duplicates

    /// Один посторонний PID — возвращаем 1.
    func test_oneDuplicate_returnsOne() {
        let selfPid = getpid()
        // Используем PID 99999 — маловероятно, что он существует; kill() вернёт ESRCH
        let fakePid: Int32 = 99999
        let runner: ([String]) -> String = { _ in "\(selfPid)\n\(fakePid)\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 1, "Должен быть убит 1 дубликат")
    }

    /// Несколько посторонних PID-ов — возвращаем корректное количество.
    func test_multipleDuplicates_returnsCorrectCount() {
        let selfPid = getpid()
        let fakePids: [Int32] = [99990, 99991, 99992]
        let output = ([selfPid] + fakePids).map { "\($0)" }.joined(separator: "\n")
        let runner: ([String]) -> String = { _ in output }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, fakePids.count, "Количество убитых должно совпасть с количеством чужих PID-ов")
    }

    // MARK: - Self-exclusion

    /// Self PID никогда не включается в список для убийства.
    func test_selfPid_neverKilled() {
        let selfPid = getpid()
        // runner возвращает только selfPid несколько раз
        let runner: ([String]) -> String = { _ in "\(selfPid)\n\(selfPid)\n\(selfPid)\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Self PID не должен считаться как дубликат")
    }

    // MARK: - Robustness

    /// Вывод с пробелами и пустыми строками — парсится корректно.
    func test_whitespaceInOutput_parsedCorrectly() {
        let selfPid = getpid()
        let runner: ([String]) -> String = { _ in "  \(selfPid)  \n\n  \n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Пробелы вокруг PID не должны приводить к ложным дубликатам")
    }

    /// Мусор в выводе pgrep — не вызывает краш.
    func test_garbledOutput_doesNotCrash() {
        let runner: ([String]) -> String = { _ in "abc\nxyz\n!!!\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Нечисловые строки игнорируются")
    }

    // MARK: - pgrep arguments

    /// Функция передаёт в runner аргумент -x KrabEarAgent (exact-match по имени).
    func test_pgrepCalledWithExactMatchFlag() {
        var capturedArgs: [String] = []
        let runner: ([String]) -> String = { args in
            capturedArgs = args
            return ""
        }
        _ = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertTrue(capturedArgs.contains("-x"), "pgrep должен вызываться с флагом -x (exact match)")
        XCTAssertTrue(capturedArgs.contains("KrabEarAgent"), "pgrep должен искать процесс KrabEarAgent")
    }
}
