import Foundation

/// Запись экспортов пользовательских данных с правами 0600.
///
/// `String.write(to:atomically:)` создаёт файл по umask (обычно 0644), и расшифровка
/// становится читаемой другими локальными пользователями. Здесь временный файл
/// создаётся СРАЗУ с 0600 (`O_EXCL|O_NOFOLLOW`), дописывается, `fsync`, затем
/// атомарный `rename` поверх цели. Окна «сначала 0644, потом chmod» нет.
enum PrivateFileWriter {
    static func write(_ text: String, to url: URL) throws {
        try write(Data(text.utf8), to: url)
    }

    static func write(_ data: Data, to url: URL) throws {
        let directory = url.deletingLastPathComponent()
        // Короткое имя, не зависящее от целевого: цель у предела 255 байт иначе даёт ENAMETOOLONG.
        let temp = directory.appendingPathComponent(".krab-export-\(UUID().uuidString)")
        let fd = open(temp.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw posixError() }
        var needsCleanup = true
        defer {
            if needsCleanup { unlink(temp.path) }
        }
        do {
            try data.withUnsafeBytes { raw in
                var offset = 0
                while offset < raw.count {
                    let n = Darwin.write(fd, raw.baseAddress! + offset, raw.count - offset)
                    if n < 0 {
                        if errno == EINTR { continue }
                        throw posixError()
                    }
                    offset += n
                }
            }
            if fsync(fd) != 0 { throw posixError() }
        } catch {
            close(fd)
            throw error
        }
        guard close(fd) == 0 else { throw posixError() }
        guard rename(temp.path, url.path) == 0 else { throw posixError() }
        needsCleanup = false
    }

    private static func posixError() -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
}
