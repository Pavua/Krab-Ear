/*
 WakeWordModelChoice.swift

 Выбор модели слова-пробуждения по умолчанию. Чистая логика (без UI/IPC) ради тестов.
 Раньше без явного выбора всегда слушалась встроенная `hey_jarvis`, а кастомная «Краб»
 (`krab_ru`, .onnx в wake_word_models/) никогда не использовалась.
*/

import Foundation

enum WakeWordModelChoice {
    /// Кастомная модель «Краб».
    static let krabModel = "krab_ru"

    /// Модель, которую надо сохранить по умолчанию, или nil, если менять ничего не нужно:
    /// владелец уже выбрал модель сам, либо `krab_ru` на диске нет.
    static func preferredDefault(saved: String?, available: [String]) -> String? {
        if let saved, !saved.isEmpty { return nil }
        return available.contains(krabModel) ? krabModel : nil
    }
}
