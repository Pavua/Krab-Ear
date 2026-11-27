/*
 SettingsInputValidation — чистая (без AppKit/IPC) валидация ввода панели
 настроек и проверка подтверждения записи бэкендом.

 Закрывает находки ChatGPT G3-memory-08 (KEM08-04, KEM08-08): раньше
 некорректный max_tokens молча превращался в 300, а любой ответ add_summary_profile
 без исключения считался «профиль создан». Логика вынесена из HistoryPanelController,
 чтобы покрываться поведенческими тестами без поднятия UI.
*/

import Foundation

enum SummaryProfileInput {
    /// Значение по умолчанию, когда поле токенов оставлено пустым.
    static let defaultMaxTokens = 300
    /// Разумный потолок на клиенте: больше — почти наверняка опечатка.
    static let maxTokensUpperBound = 32_768

    enum MaxTokens: Equatable {
        case value(Int)
        /// Поле заполнено, но это не целое число в допустимом диапазоне.
        case invalid
    }

    /// Пустое поле → значение по умолчанию (явный выбор пользователя «не менять»);
    /// непустое должно быть целым в 1...maxTokensUpperBound, иначе `.invalid`
    /// (раньше «abc» молча превращалось в 300).
    static func parseMaxTokens(_ raw: String) -> MaxTokens {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { return .value(defaultMaxTokens) }
        guard let value = Int(text), (1...maxTokensUpperBound).contains(value) else {
            return .invalid
        }
        return .value(value)
    }

    /// Ответ `add_summary_profile` считается подтверждением, только если backend
    /// вернул созданный профиль: `result.profile` — словарь с непустым `name`.
    /// Ответ вида `{"result":{"ok":false}}` или без `result` — НЕ подтверждение.
    static func isCreationAcknowledged(_ response: [String: Any]) -> Bool {
        guard response["error"] == nil,
              let result = response["result"] as? [String: Any],
              let profile = result["profile"] as? [String: Any],
              let name = profile["name"] as? String,
              !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return false }
        return true
    }
}

enum DraftFieldClearing {
    /// Поле можно очистить после успешной записи только если пользователь не
    /// успел его изменить, пока шёл IPC: иначе стёрли бы новый черновик.
    static func shouldClear(current: String, submitted: String) -> Bool {
        current == submitted
    }
}
