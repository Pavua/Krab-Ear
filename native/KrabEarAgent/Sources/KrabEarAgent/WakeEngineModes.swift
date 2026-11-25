import Foundation

/*
 Режимы двух движков голосовой активации и разбор их чисел из IPC `wake_word_status`.

 Backend (#2258) держит два движка на одном потоке микрофона: openWakeWord (`oww`)
 и поиск «Краб» по распознаванию речи (`stt`). Режим каждого — настройка
 `wake_word_oww_mode` / `wake_word_stt_mode` со значениями `off | shadow | live`;
 backend читает её на каждом чанке, поэтому переключатель действует без рестарта.

 Здесь только чистая логика (без AppKit), чтобы её можно было проверить тестом:
 соответствие сегментов значениям настройки и текст строки чисел. В строке —
 только числа и код причины, никакого распознанного текста и аудио.
 */
enum WakeEngine: String, CaseIterable {
    case oww
    case stt

    /// Ключ настройки backend (allowlist в settings_validator.py `_ENUM_FIELDS`).
    var settingKey: String {
        switch self {
        case .oww: return "wake_word_oww_mode"
        case .stt: return "wake_word_stt_mode"
        }
    }

    /// Режим по умолчанию — тот же, что `_DEFAULT_MODES` в openwakeword_adapter.py.
    var defaultMode: WakeEngineMode {
        switch self {
        case .oww: return .live
        case .stt: return .off
        }
    }

    var titleRU: String {
        switch self {
        case .oww: return "openWakeWord"
        case .stt: return "Поиск «Краб» по речи"
        }
    }
}

enum WakeEngineMode: String, CaseIterable {
    case off
    case shadow
    case live

    /// Подписи сегментов — в порядке `allCases` (индекс сегмента == индекс случая).
    static let segmentTitlesRU = ["Выкл", "В тени", "Вживую"]

    var segmentIndex: Int { Self.allCases.firstIndex(of: self) ?? 0 }

    init?(segmentIndex: Int) {
        guard Self.allCases.indices.contains(segmentIndex) else { return nil }
        self = Self.allCases[segmentIndex]
    }

    /// Неизвестное значение = безопасный дефолт движка (как делает backend).
    static func parse(_ raw: Any?, engine: WakeEngine) -> WakeEngineMode {
        guard let text = raw as? String, let mode = WakeEngineMode(rawValue: text) else {
            return engine.defaultMode
        }
        return mode
    }
}

/// Числа одного движка из `wake_word_status.engines.<engine>`.
struct WakeEngineReport: Equatable {
    var mode: WakeEngineMode
    var firesLive: Int
    var firesShadow: Int
    var lastFireTs: Double?
    /// Только `stt`: есть ли у backend распознаватель (без него режим трактуется как off).
    var available: Bool?
    /// Только `stt`: код последней причины пропуска реплики («mlx_busy»…), не текст.
    var unavailableReason: String?
    /// Только `stt`: сколько реплик пропущено из-за недоступности распознавателя.
    var spotterUnavailable: Int?
    /// Существует ли объект движка в текущей сессии слушателя (backend #2261).
    /// `nil` — старый backend поля не отдаёт: это «неизвестно», а не «выключен».
    var active: Bool?
    /// Идёт загрузка/прогрев модели (backend #2263). `nil` — старый backend.
    var loading: Bool?
    /// Код причины, по которой движок не работает при включённом режиме (#2263), без текста:
    /// oww — `not_installed` | `load_failed`; stt — `no_transcriber` | `chunk_size` | `load_failed`.
    var loadError: String?

    init(engine: WakeEngine, payload: [String: Any]?) {
        let p = payload ?? [:]
        mode = WakeEngineMode.parse(p["mode"], engine: engine)
        firesLive = Self.int(p["fires_live"])
        firesShadow = Self.int(p["fires_shadow"])
        lastFireTs = Self.double(p["last_fire_ts"])
        available = p["available"] as? Bool
        let reason = (p["unavailable_reason"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        unavailableReason = (reason?.isEmpty ?? true) ? nil : reason
        let spotter = p["spotter"] as? [String: Any]
        spotterUnavailable = spotter.map { Self.int($0["unavailable"]) }
        active = p["active"] as? Bool
        loading = p["loading"] as? Bool
        let error = (p["load_error"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        loadError = (error?.isEmpty ?? true) ? nil : error
    }

    private static func int(_ value: Any?) -> Int {
        if let n = value as? Int { return n }
        if let d = value as? Double, d.isFinite { return Int(d) }
        return 0
    }

    private static func double(_ value: Any?) -> Double? {
        if let d = value as? Double, d.isFinite { return d }
        if let n = value as? Int { return Double(n) }
        return nil
    }
}

enum WakeEngineStatsFormatter {
    /// Причины, при которых движок действительно не работает. Остальные (`mlx_busy` —
    /// диктовка держит GPU, `run_failed`, `not_text`) — разовые пропуски реплик:
    /// backend хранит ПОСЛЕДНЮЮ причину и не сбрасывает её после успешного
    /// распознавания, поэтому по ней одной нельзя писать «недоступен» вместо чисел.
    static let hardUnavailableReasons: Set<String> = ["model_load_failed"]

    static let turningOnText = "Включается… (загружается модель)"
    /// Автоповторы статуса кончились, а движок так и не поднялся: вечное «Включается…»
    /// вводило бы в заблуждение (backend не отличает прогрев от упавшей загрузки).
    static let turningOnSlowText = "Включается дольше обычного — нажмите «Обновить числа»"

    /// Строка чисел движка.
    ///
    /// `backendNow` — «сейчас» по часам backend: `last_chunk_ts` из того же ответа
    /// (штампуется на каждом ненулевом чанке, пока слушатель работает). `last_fire_ts`
    /// — `time.monotonic()` процесса backend; сравнивать его с часами агента нельзя
    /// (разные процессы, на macOS разные часы ведут себя по-разному во сне), поэтому
    /// давность считаем только внутри часов backend, а без слушателя не показываем.
    ///
    /// `listening` — `running` из того же ответа: `nil` — неизвестно (старый backend).
    /// Без слушателя backend отдаёт `active == false` у обоих движков, поэтому
    /// «Включается…» показываем только при работающем слушателе.
    static func text(engine: WakeEngine, report: WakeEngineReport, backendNow: Double?,
                     listening: Bool? = nil) -> String {
        // Новый backend (#2263) прямо сообщает ошибку загрузки через load_error — она главнее.
        // `explicitState` — признак нового backend: у него сбой загрузки не выводим из
        // устаревшей unavailable_reason.
        let explicitState = report.loading != nil
        if report.mode != .off, let error = report.loadError {
            return loadErrorText(engine: engine, code: error)
        }
        if engine == .stt, report.mode != .off, report.available == false {
            return "Недоступен (распознаватель не загружен)"
        }
        // «Включается…»: идёт загрузка ИЛИ слушатель работает, а объекта движка ещё нет.
        // Второе — переход сразу после клика: движок создаётся только на следующем чанке
        // микрофона, и первый статус приходит с active=false, loading=false (ревью Codex
        // #2264). Устойчивых тупиков в этом состоянии нет — их backend #2263 отдаёт через
        // load_error (обработан выше), а у старого backend это прежняя эвристика.
        if report.mode != .off, report.loading == true || (listening == true && report.active == false) {
            return turningOnText
        }
        var parts = [
            "Срабатываний вживую: \(report.firesLive)",
            "в тени: \(report.firesShadow)",
        ]
        if let last = report.lastFireTs {
            if let now = backendNow, now >= last {
                parts.append("последнее: \(ago(now - last))")
            } else {
                parts.append("последнее: было")
            }
        }
        if engine == .stt, report.mode != .off {
            let skipped = report.spotterUnavailable ?? 0
            if skipped > 0 || report.unavailableReason != nil {
                var tail = "пропущено реплик: \(skipped)"
                if let reason = report.unavailableReason { tail += " (последняя причина: \(reason))" }
                parts.append(tail)
            }
            // Новый backend сообщает сбой загрузки через load_error (обработан выше).
            if !explicitState, let reason = report.unavailableReason, hardUnavailableReasons.contains(reason) {
                parts.insert("Недоступен (\(reason))", at: 0)
            }
        }
        if report.mode == .off {
            parts.insert("Выключен", at: 0)
        } else if listening == false {
            // Детектор выключен или прослушивание на паузе (запись, разговор, privacy mode).
            parts.insert("Сейчас не слушает", at: 0)
        }
        return parts.joined(separator: " · ")
    }

    /// Понятная причина по коду `load_error`. Неизвестный код показываем как есть —
    /// молчаливый ноль хуже непереведённого кода.
    static func loadErrorText(engine: WakeEngine, code: String) -> String {
        switch (engine, code) {
        case (.oww, "not_installed"):
            return "Недоступен (openWakeWord не установлен)"
        case (.oww, "load_failed"):
            // backend повторяет загрузку раз в 60 с — строка честно чередуется с «Включается…».
            return "Недоступен (модель не загрузилась, повтор через минуту)"
        case (.stt, "no_transcriber"):
            return "Недоступен (распознаватель не загружен)"
        case (.stt, "chunk_size"):
            return "Недоступен (неподходящий размер аудиоблока)"
        case (.stt, "load_failed"):
            return "Недоступен (модель распознавания не загрузилась)"
        default:
            return "Недоступен (\(code))"
        }
    }

    static func ago(_ seconds: Double) -> String {
        if seconds < 60 { return "меньше минуты назад" }
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "\(minutes) мин назад" }
        return "\(minutes / 60) ч назад"
    }
}
