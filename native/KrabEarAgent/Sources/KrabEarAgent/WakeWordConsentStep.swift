/*
 WakeWordConsentStep.swift

 Отдельный шаг онбординга «Голосовой триггер» (решение 9.4 финальной спеки A1):
 wake word НЕ входит в apply_recommended_setup — always-listening микрофон должен
 быть явным, осознанным выбором пользователя, а не побочным эффектом «сделай мне хорошо».

 Связи модуля:
 1) QuickStartWindowController — презентует этот шаг ПОСЛЕ RecommendedSetupStep
    (см. main.swift, цепочка онбординга runModelDownloadStepThenComplete/
    runRecommendedSetupStepThenWakeWord/runWakeWordConsentStep).
 2) IPCClient — set_settings {wake_word_engine: "openwakeword"} НАПРЯМУЮ (строго
    off-main, AGENT-3) — НЕ через apply_recommended_setup.

 🔴 Правила: те же, что ModelDownloadStep.swift — неблокирующий sheet, IPC off-main,
 graceful skip при любой ошибке (модель может быть ещё не забутстрапена —
 bootstrap_backend.command грузит её отдельно; ошибка set_settings НЕ блокирует
 завершение онбординга).

 Спека: docs/superpowers/specs/2026-07-07-recommended-setup-design.md.
 Визуал (карточка/иконки/цвета) — docs/design-briefs/2026-07-07-recommended-setup-ui.md,
 исполняется agy отдельно (эта реализация — механика/skeleton).
*/

import AppKit
import Foundation

enum WakeWordConsentOutcome {
    case enabled
    case declined
}

@MainActor
final class WakeWordConsentStepController: NSObject {
    private let ipcClient: IPCClient
    private let completion: (WakeWordConsentOutcome) -> Void

    private weak var parentWindow: NSWindow?
    private var sheetWindow: NSWindow?
    private var didComplete = false

    private let titleLabel = NSTextField(labelWithString: "Голосовой триггер \u{2014} \u{00AB}Краб\u{00BB}")
    private let bodyLabel = NSTextField(
        wrappingLabelWithString:
            "Включить голосовой триггер? Микрофон будет постоянно слушать локально " +
            "(без отправки в сеть) в ожидании слова \u{00AB}Краб\u{00BB}. Отключить можно в любой момент в Настройках."
    )
    private lazy var enableButton = ThemePrimaryButton(
        title: "Включить", target: self, action: #selector(onEnableTap)
    )
    private lazy var declineButton = ThemeSecondaryButton(
        title: "Не сейчас", target: self, action: #selector(onDeclineTap)
    )

    init(ipcClient: IPCClient, completion: @escaping (WakeWordConsentOutcome) -> Void) {
        self.ipcClient = ipcClient
        self.completion = completion
        super.init()
    }

    func start(over parent: NSWindow) {
        self.parentWindow = parent
        presentSheet(over: parent)
    }

    private func presentSheet(over parent: NSWindow) {
        let sheet = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 190),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        sheet.title = "Голосовой триггер"
        buildUI(in: sheet)
        self.sheetWindow = sheet
        parent.beginSheet(sheet, completionHandler: nil)
    }

    private func buildUI(in window: NSWindow) {
        // Механика Auto Layout — минимальный skeleton; финальный визуал (карточка/
        // иконки/цвета) приходит из docs/design-briefs/2026-07-07-recommended-setup-ui.md
        // через agy (см. Задача 4/6 плана docs/superpowers/plans/2026-07-07-recommended-setup.md).
        let content = NSView(frame: window.contentView!.bounds)
        window.contentView = content

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = KrabEarTheme.Metrics.comfortable
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor),
        ])

        titleLabel.font = .systemFont(ofSize: 17, weight: .bold)
        stack.addArrangedSubview(titleLabel)

        bodyLabel.font = .systemFont(ofSize: 13)
        stack.addArrangedSubview(bodyLabel)
        bodyLabel.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        stack.addArrangedSubview(NSView())

        let buttonsRow = NSStackView()
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.comfortable
        stack.addArrangedSubview(buttonsRow)
        buttonsRow.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        declineButton.applyThemeSecondary()
        buttonsRow.addArrangedSubview(declineButton)
        buttonsRow.addArrangedSubview(NSView())
        enableButton.applyThemePrimary()
        enableButton.keyEquivalent = "\r"
        buttonsRow.addArrangedSubview(enableButton)
    }

    @objc private func onDeclineTap() {
        finish(.declined)
    }

    @objc private func onEnableTap() {
        enableButton.isEnabled = false
        declineButton.isEnabled = false
        let ipc = ipcClient
        Task { [weak self] in
            do {
                _ = try await ipc.callAsync(
                    method: "set_settings",
                    params: ["wake_word_engine": "openwakeword"],
                    timeoutSec: IPCClient.defaultTimeoutSec
                )
            } catch {
                // Graceful — модель может быть не забутстрапена; не блокируем онбординг.
                NSLog("[WakeWordConsentStep] set_settings error: %@", error.localizedDescription)
            }
            await MainActor.run { [weak self] in self?.finish(.enabled) }
        }
    }

    private func finish(_ outcome: WakeWordConsentOutcome) {
        guard !didComplete else { return }
        didComplete = true
        if let sheet = sheetWindow, let parent = parentWindow {
            parent.endSheet(sheet)
            sheetWindow = nil
        }
        completion(outcome)
    }
}
