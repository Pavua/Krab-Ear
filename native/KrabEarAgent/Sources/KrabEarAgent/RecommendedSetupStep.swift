/*
 RecommendedSetupStep.swift

 Шаг онбординга «Рекомендованная настройка»: показывает dry_run превью
 apply_recommended_setup (10 безусловных + 3 условных настройки) перед завершением
 онбординга. «Применить» -> apply_recommended_setup{dry_run:false}. «Пропустить» ->
 ничего не меняет.

 Связи модуля:
 1) QuickStartWindowController — презентует ПОСЛЕ ModelDownloadStepController,
    ПЕРЕД WakeWordConsentStepController (main.swift, runModelDownloadStepThenComplete).
 2) IPCClient — apply_recommended_setup (строго off-main, AGENT-3).

 Спека: docs/superpowers/specs/2026-07-07-recommended-setup-design.md.
 Визуал (карточка/иконки/цвета) — docs/design-briefs/2026-07-07-recommended-setup-ui.md,
 исполняется agy отдельно (эта реализация — механика/skeleton).
*/

import AppKit
import Foundation

enum RecommendedSetupStepOutcome {
    case applied(count: Int)
    case skipped
    case fetchFailed
}

@MainActor
final class RecommendedSetupStepController: NSObject {
    private let ipcClient: IPCClient
    private let completion: (RecommendedSetupStepOutcome) -> Void

    private weak var parentWindow: NSWindow?
    private var sheetWindow: NSWindow?
    private var didComplete = false
    private var previewApplied: [[String: Any]] = []
    private var previewSkipped: [[String: Any]] = []

    private let titleLabel = NSTextField(labelWithString: "Рекомендованная настройка")
    private let summaryLabel = NSTextField(wrappingLabelWithString: "Загрузка превью...")
    private lazy var applyButton = ThemePrimaryButton(
        title: "Применить", target: self, action: #selector(onApplyTap)
    )
    private lazy var skipButton = ThemeSecondaryButton(
        title: "Пропустить", target: self, action: #selector(onSkipTap)
    )

    init(ipcClient: IPCClient, completion: @escaping (RecommendedSetupStepOutcome) -> Void) {
        self.ipcClient = ipcClient
        self.completion = completion
        super.init()
    }

    func start(over parent: NSWindow) {
        self.parentWindow = parent
        presentSheet(over: parent)
        fetchPreview()
    }

    private func presentSheet(over parent: NSWindow) {
        let sheet = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 260),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        sheet.title = "Рекомендованная настройка"
        buildUI(in: sheet)
        self.sheetWindow = sheet
        parent.beginSheet(sheet, completionHandler: nil)
    }

    private func buildUI(in window: NSWindow) {
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

        summaryLabel.font = .systemFont(ofSize: 13)
        stack.addArrangedSubview(summaryLabel)
        summaryLabel.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        stack.addArrangedSubview(NSView())

        let buttonsRow = NSStackView()
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.comfortable
        stack.addArrangedSubview(buttonsRow)
        buttonsRow.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        skipButton.applyThemeSecondary()
        buttonsRow.addArrangedSubview(skipButton)
        buttonsRow.addArrangedSubview(NSView())
        applyButton.applyThemePrimary()
        applyButton.keyEquivalent = "\r"
        applyButton.isEnabled = false  // включается после успешного fetch превью
        buttonsRow.addArrangedSubview(applyButton)
    }

    private func fetchPreview() {
        let ipc = ipcClient
        Task { [weak self] in
            do {
                let resp = try await ipc.callAsync(
                    method: "apply_recommended_setup",
                    params: ["dry_run": true],
                    timeoutSec: IPCClient.defaultTimeoutSec
                )
                let result = (resp["result"] as? [String: Any]) ?? [:]
                let applied = (result["applied"] as? [[String: Any]]) ?? []
                let skipped = (result["skipped"] as? [[String: Any]]) ?? []
                await MainActor.run { [weak self] in
                    self?.applyPreview(applied: applied, skipped: skipped)
                }
            } catch {
                NSLog("[RecommendedSetupStep] fetchPreview error: %@", error.localizedDescription)
                await MainActor.run { [weak self] in self?.showFetchFailed() }
            }
        }
    }

    @MainActor
    private func applyPreview(applied: [[String: Any]], skipped: [[String: Any]]) {
        previewApplied = applied
        previewSkipped = skipped
        summaryLabel.stringValue = "Будет включено: \(applied.count). Пропущено: \(skipped.count)."
        applyButton.isEnabled = !applied.isEmpty
    }

    @MainActor
    private func showFetchFailed() {
        summaryLabel.stringValue = "Не удалось получить превью — можно настроить позже в Настройках."
        applyButton.isEnabled = false
    }

    @objc private func onSkipTap() {
        finish(.skipped)
    }

    @objc private func onApplyTap() {
        applyButton.isEnabled = false
        skipButton.isEnabled = false
        let ipc = ipcClient
        let appliedCount = previewApplied.count
        Task { [weak self] in
            do {
                _ = try await ipc.callAsync(
                    method: "apply_recommended_setup",
                    params: ["dry_run": false],
                    timeoutSec: IPCClient.defaultTimeoutSec
                )
            } catch {
                NSLog("[RecommendedSetupStep] apply error: %@", error.localizedDescription)
            }
            await MainActor.run { [weak self] in self?.finish(.applied(count: appliedCount)) }
        }
    }

    private func finish(_ outcome: RecommendedSetupStepOutcome) {
        guard !didComplete else { return }
        didComplete = true
        if let sheet = sheetWindow, let parent = parentWindow {
            parent.endSheet(sheet)
            sheetWindow = nil
        }
        completion(outcome)
    }
}
