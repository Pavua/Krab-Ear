/*
 ConversationViewController+BrainMode — тоггл brain_mode (Волна 3b).

 Персистентность выбора между запусками (UserDefaults) + действие «Сделать дефолтом»
 (PUT /v1/settings/conversation на Voice Gateway). Сегментный контрол строится в
 +UI.swift; здесь — только поведение (обновление config, персистентность, сеть).

 Значения сегментов (индекс → brainMode): 0 = "fast", 1 = "krab", 2 = "auto".
 Контракт с Voice Gateway: docs/design-briefs/2026-07-08-vg-conversation-brain-mode.md
*/

import Foundation

private let kBrainModeUserDefaultsKey = "KrabEar_ConversationBrainMode"

extension ConversationViewController {

    /// Сегменты тоггла в порядке индекса: 0=fast, 1=krab, 2=auto. Единственный источник
    /// правды для маппинга индекс↔значение — используется и здесь, и в viewDidLoad()
    /// (ConversationViewController.swift) при восстановлении сохранённого выбора.
    /// nonisolated: используется из nonisolated savedBrainMode/saveBrainMode ниже —
    /// без этого Swift 6 strict concurrency отказывается компилировать этот доступ.
    nonisolated static let brainModeSegmentValues = ["fast", "krab", "auto"]

    // MARK: - UserDefaults persistence (static — доступно до создания VC)

    /// Последний сохранённый выбор пользователя. "auto" если ключ не задан или
    /// содержит неизвестное значение.
    nonisolated static var savedBrainMode: String {
        let raw = UserDefaults.standard.string(forKey: kBrainModeUserDefaultsKey) ?? "auto"
        return brainModeSegmentValues.contains(raw) ? raw : "auto"
    }

    /// Сохранить выбор пользователя.
    nonisolated static func saveBrainMode(_ mode: String) {
        UserDefaults.standard.set(mode, forKey: kBrainModeUserDefaultsKey)
    }

    // MARK: - Segment action (target/action wiring — в +UI.swift buildUI())

    @objc func onBrainModeSegmentChanged() {
        let idx = brainModeControl.selectedSegment
        let values = ConversationViewController.brainModeSegmentValues
        let mode = (idx >= 0 && idx < values.count) ? values[idx] : "auto"
        config.brainMode = mode
        ConversationViewController.saveBrainMode(mode)
    }

    // MARK: - "Сделать дефолтом" (PUT /v1/settings/conversation)

    @objc func onSetBrainModeDefaultTapped() {
        guard let request = _buildSetDefaultRequest() else {
            showBrainModeHint("✗ Неверный адрес Voice Gateway")
            return
        }
        Task { [weak self] in
            guard let self else { return }
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let ok = (response as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
                await MainActor.run {
                    self.showBrainModeHint(ok ? "✓ Сохранено как дефолт" : "✗ Ошибка сохранения")
                }
            } catch {
                await MainActor.run {
                    self.showBrainModeHint("✗ \(error.localizedDescription)")
                }
            }
        }
    }

    private func showBrainModeHint(_ text: String) {
        brainModeHintLabel.stringValue = text
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
            guard let self, self.brainModeHintLabel.stringValue == text else { return }
            self.brainModeHintLabel.stringValue = ""
        }
    }

    // MARK: - Request builder (shared by production + tests)

    /// Строит PUT-запрос к Voice Gateway settings API. Используется и продакшен-кодом
    /// (onSetBrainModeDefaultTapped), и тестами напрямую — НЕ debug-only, не гейтить
    /// #if DEBUG.
    func _buildSetDefaultRequest() -> URLRequest? {
        let base = config.httpBaseURLString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !base.isEmpty, let url = URL(string: base + "/v1/settings/conversation") else {
            return nil
        }
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["brain_mode": config.brainMode])
        return request
    }
}
