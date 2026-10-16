import Darwin
import Foundation

/// Только диагностические коды: пользовательские данные/вывод команды не логируются.
enum DuckingFailure: String, Equatable, Sendable {
    case busy, cancelled, deadlineExceeded, commandFailed, invalidSnapshot
    case launchFailed, outputLimit, cleanupUnconfirmed
}

enum AudioDuckingCommandResult: Equatable, Sendable {
    case success(String)
    case failure(DuckingFailure, cleanupConfirmed: Bool)
}

protocol AudioDuckingCommandRunning: Sendable {
    func run(script: String, deadline: ContinuousClock.Instant) -> AudioDuckingCommandResult
}

/// Единственный владелец waitpid. PID остаётся unreaped до решения kill/reap,
/// поэтому timeout не может послать сигнал следующему процессу с переиспользованным PID.
struct SystemAudioDuckingCommandRunner: AudioDuckingCommandRunning {
    let executable: String
    let arguments: @Sendable (String) -> [String]
    let outputLimit: Int

    init(
        executable: String = "/usr/bin/osascript",
        arguments: @escaping @Sendable (String) -> [String] = { script in
            script.split(separator: "\n").flatMap { ["-e", String($0)] }
        },
        outputLimit: Int = 4096
    ) {
        self.executable = executable
        self.arguments = arguments
        self.outputLimit = max(1, outputLimit)
    }

    func run(script: String, deadline: ContinuousClock.Instant) -> AudioDuckingCommandResult {
        guard ContinuousClock.now < deadline else { return .failure(.deadlineExceeded, cleanupConfirmed: true) }
        var descriptors: [Int32] = [0, 0]
        guard pipe(&descriptors) == 0 else { return .failure(.launchFailed, cleanupConfirmed: true) }
        defer { close(descriptors[0]); close(descriptors[1]) }
        guard fcntl(descriptors[0], F_SETFL, O_NONBLOCK) == 0,
              fcntl(descriptors[0], F_SETFD, FD_CLOEXEC) == 0,
              fcntl(descriptors[1], F_SETFD, FD_CLOEXEC) == 0 else {
            return .failure(.launchFailed, cleanupConfirmed: true)
        }
        var actions: posix_spawn_file_actions_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else {
            return .failure(.launchFailed, cleanupConfirmed: true)
        }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawn_file_actions_adddup2(&actions, descriptors[1], STDOUT_FILENO) == 0,
              posix_spawn_file_actions_addclose(&actions, descriptors[0]) == 0,
              posix_spawn_file_actions_addclose(&actions, descriptors[1]) == 0,
              posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0) == 0,
              posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0) == 0 else {
            return .failure(.launchFailed, cleanupConfirmed: true)
        }
        let argv = ([executable] + arguments(script)).map { strdup($0) } + [nil]
        let env = ProcessInfo.processInfo.environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { argv.forEach { free($0) }; env.forEach { free($0) } }
        var pid: pid_t = 0
        guard ContinuousClock.now < deadline else { return .failure(.deadlineExceeded, cleanupConfirmed: true) }
        let spawned = argv.withUnsafeBufferPointer { args in
            env.withUnsafeBufferPointer { environment in
                posix_spawn(&pid, executable, &actions, nil,
                            UnsafeMutablePointer(mutating: args.baseAddress!),
                            UnsafeMutablePointer(mutating: environment.baseAddress!))
            }
        }
        guard spawned == 0 else { return .failure(.launchFailed, cleanupConfirmed: true) }
        var output = Data()
        var status: Int32 = 0
        var failure: DuckingFailure?
        while true {
            // Даже непрерывный writer не может удерживать drain дольше deadline.
            var buffer = [UInt8](repeating: 0, count: 1024)
            while ContinuousClock.now < deadline {
                let count = Darwin.read(descriptors[0], &buffer, buffer.count)
                if count > 0 {
                    if output.count + count > outputLimit { failure = .outputLimit; break }
                    output.append(contentsOf: buffer.prefix(count))
                } else if count < 0 && errno == EINTR {
                    continue
                } else if count < 0 && errno != EAGAIN && errno != EWOULDBLOCK {
                    failure = .commandFailed; break
                } else { break }
            }
            if failure != nil { break }
            let waited = waitpid(pid, &status, WNOHANG)
            if waited == pid {
                // Последние байты могли появиться между read(EAGAIN) и exit.
                // Child уже reaped: при ошибке drain больше нельзя посылать ему сигнал.
                while ContinuousClock.now < deadline {
                    let count = Darwin.read(descriptors[0], &buffer, buffer.count)
                    if count > 0 {
                        guard output.count + count <= outputLimit else {
                            return .failure(.outputLimit, cleanupConfirmed: true)
                        }
                        output.append(contentsOf: buffer.prefix(count))
                    } else if count < 0 && errno == EINTR { continue }
                    else { break }
                }
                guard ContinuousClock.now < deadline else {
                    return .failure(.deadlineExceeded, cleanupConfirmed: true)
                }
                return status == 0 ? .success(String(decoding: output, as: UTF8.self))
                    : .failure(.commandFailed, cleanupConfirmed: true)
            }
            if waited < 0 && errno != EINTR {
                return .failure(.cleanupUnconfirmed, cleanupConfirmed: false)
            }
            if ContinuousClock.now >= deadline { failure = .deadlineExceeded; break }
            Thread.sleep(forTimeInterval: 0.005)
        }
        // waitpid ещё не вернул pid: он не может быть переиспользован другим child.
        _ = kill(pid, SIGKILL)
        let cleanupDeadline = ContinuousClock.now + .milliseconds(500)
        repeat {
            let waited = waitpid(pid, &status, WNOHANG)
            if waited == pid { return .failure(failure ?? .commandFailed, cleanupConfirmed: true) }
            if waited < 0 && errno != EINTR { return .failure(.cleanupUnconfirmed, cleanupConfirmed: false) }
            Thread.sleep(forTimeInterval: 0.005)
        } while ContinuousClock.now < cleanupDeadline
        // Дальнейшие команды сервис запретит. Единственный оставшийся waitpid
        // завершит reap вне MainActor, даже если kernel задержал SIGKILL.
        let unreapedPID = pid
        DispatchQueue.global(qos: .utility).async {
            var finalStatus: Int32 = 0
            while waitpid(unreapedPID, &finalStatus, 0) < 0 && errno == EINTR {}
        }
        return .failure(.cleanupUnconfirmed, cleanupConfirmed: false)
    }
}
