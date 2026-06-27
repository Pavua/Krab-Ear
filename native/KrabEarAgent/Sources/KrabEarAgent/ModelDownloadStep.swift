/*
 ModelDownloadStep.swift

 Шаг онбординга «Модель распознавания речи»: на свежей установке без кэшированной
 STT-модели предлагает скачать модель (~1.5 ГБ) с прогресс-баром.

 Связи модуля:
 1) QuickStartWindowController — презентует этот шаг как sheet перед завершением онбординга.
 2) IPCClient — get_stt_model_status / download_stt_model (строго off-main, AGENT-3).

 🔴 Правила:
   - IPC ТОЛЬКО off-main (callAsync на background) → @MainActor только для UI.
   - НИКАКОГО блокирующего модала — шаг показывается через window.beginSheet (неблокирующий).
   - Прогресс ведётся опросом get_stt_model_status каждые ~0.5 c через Task (cancel при teardown).
   - Любая сетевая ошибка → graceful «можно скачать позже в Настройках», без краша.
*/

import AppKit
import Foundation

/// Результат шага загрузки модели.
enum ModelDownloadStepOutcome {
    /// Модель уже была в кэше — шаг пропущен полностью (существующие пользователи не затронуты).
    case alreadyCached
    /// Модель успешно скачана.
    case downloaded
    /// Пользователь выбрал «Позже» (или закрыл окно) — модель можно скачать позже в Настройках.
    case skipped
}

/// Контроллер модального sheet-шага загрузки STT-модели.
/// Презентуется через `present(over:)`; завершается вызовом `completion` ровно один раз.
@MainActor
final class ModelDownloadStepController: NSObject {
    private let ipcClient: IPCClient
    private let completion: (ModelDownloadStepOutcome) -> Void

    private weak var parentWindow: NSWindow?
    private var sheetWindow: NSWindow?
    private var pollTask: Task<Void, Never>?
    private var didComplete = false

    // UI
    private let titleLabel = NSTextField(labelWithString: "Модель распознавания речи")
    private let bodyLabel = NSTextField(
        wrappingLabelWithString:
            "Для офлайн-распознавания нужна модель (~1.5 ГБ). Скачать сейчас?"
    )
    private let progressBar = NSProgressIndicator()
    private let progressLabel = NSTextField(labelWithString: "")
    private lazy var downloadButton = ThemePrimaryButton(
        title: "Скачать", target: self, action: #selector(onPrimaryTap)
    )
    private lazy var laterButton = ThemeSecondaryButton(
        title: "Позже", target: self, action: #selector(onSecondaryTap)
    )

    init(ipcClient: IPCClient, completion: @escaping (ModelDownloadStepOutcome) -> Void) {
        self.ipcClient = ipcClient
        self.completion = completion
        super.init()
    }

    // MARK: - Точка входа

    /// Проверяет статус модели off-main: если уже в кэше — мгновенно `.alreadyCached`
    /// (шаг не показывается). Иначе презентует sheet над `parent`.
    func start(over parent: NSWindow) {
        self.parentWindow = parent
        let ipc = ipcClient
        Task { [weak self] in
            let cached = await Self.fetchCached(ipc: ipc)
            guard let self else { return }
            await MainActor.run {
                if cached {
                    self.finish(.alreadyCached)
                } else {
                    self.presentSheet(over: parent)
                }
            }
        }
    }

    // MARK: - Загрузка sheet

    private func presentSheet(over parent: NSWindow) {
        let sheet = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 220),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        sheet.title = "Модель распознавания речи"
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

        bodyLabel.font = .systemFont(ofSize: 13)
        stack.addArrangedSubview(bodyLabel)
        bodyLabel.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        progressBar.isIndeterminate = false
        progressBar.minValue = 0
        progressBar.maxValue = 100
        progressBar.doubleValue = 0
        progressBar.controlSize = .regular
        progressBar.isHidden = true
        stack.addArrangedSubview(progressBar)
        progressBar.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        progressLabel.font = .systemFont(ofSize: 11)
        progressLabel.textColor = .secondaryLabelColor
        progressLabel.isHidden = true
        stack.addArrangedSubview(progressLabel)

        stack.addArrangedSubview(NSView())  // спейсер

        let buttonsRow = NSStackView()
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.comfortable
        stack.addArrangedSubview(buttonsRow)
        buttonsRow.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true

        laterButton.applyThemeSecondary()
        buttonsRow.addArrangedSubview(laterButton)

        buttonsRow.addArrangedSubview(NSView())  // спейсер

        downloadButton.applyThemePrimary()
        downloadButton.keyEquivalent = "\r"
        buttonsRow.addArrangedSubview(downloadButton)
    }

    // MARK: - Действия кнопок

    /// Primary: «Скачать» в обычном состоянии, «Повторить» в состоянии ошибки.
    @objc private func onPrimaryTap() {
        startDownload()
    }

    /// Secondary: всегда «Позже» — продолжаем мастер без загрузки.
    @objc private func onSecondaryTap() {
        finish(.skipped)
    }

    private func startDownload() {
        downloadButton.isHidden = true
        progressBar.isHidden = false
        progressLabel.isHidden = false
        progressBar.doubleValue = 0
        progressLabel.stringValue = "Подготовка загрузки..."
        bodyLabel.stringValue = "Загрузка модели распознавания речи. Не закрывайте окно."

        let ipc = ipcClient
        // Off-main запуск загрузки, затем опрос статуса.
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            // 1) Запускаем загрузку (off-main внутри callAsync).
            let started = await Self.startDownloadIPC(ipc: ipc)
            guard let self else { return }
            if !started {
                await MainActor.run { self.showError() }
                return
            }
            // 2) Опрос каждые ~0.5 c до done/error.
            await self.pollLoop(ipc: ipc)
        }
    }

    /// Цикл опроса статуса. Хопает на @MainActor только для обновления UI.
    private func pollLoop(ipc: IPCClient) async {
        while !Task.isCancelled {
            let snap = await Self.fetchStatus(ipc: ipc)
            if Task.isCancelled { return }
            await MainActor.run { [weak self] in
                self?.applyStatus(snap)
            }
            if snap == nil {
                // Сетевая/IPC-ошибка опроса — показываем graceful-ошибку.
                await MainActor.run { [weak self] in self?.showError() }
                return
            }
            if let snap, snap.status == "done" || snap.cached {
                await MainActor.run { [weak self] in self?.finish(.downloaded) }
                return
            }
            if let snap, snap.status == "error" {
                await MainActor.run { [weak self] in self?.showError(message: snap.errorMsg) }
                return
            }
            try? await Task.sleep(nanoseconds: 500_000_000)  // ~0.5 c
        }
    }

    @MainActor
    private func applyStatus(_ snap: ModelStatusSnapshot?) {
        guard let snap else { return }
        let pct = max(0, min(100, snap.pct))
        progressBar.doubleValue = pct
        if snap.total > 0 {
            let dl = Self.formatMB(snap.downloaded)
            let total = Self.formatMB(snap.total)
            progressLabel.stringValue = "\(Int(pct))% — \(dl) / \(total) МБ"
        } else {
            progressLabel.stringValue = "\(Int(pct))%"
        }
    }

    @MainActor
    private func showError(message: String? = nil) {
        progressBar.isHidden = true
        progressLabel.isHidden = true
        bodyLabel.stringValue =
            "Нет подключения или ошибка загрузки — можно скачать позже в Настройках."
        if let message, !message.isEmpty {
            progressLabel.isHidden = false
            progressLabel.stringValue = message
        }
        downloadButton.isHidden = false
        downloadButton.title = "Повторить"
    }

    // MARK: - Завершение

    private func finish(_ outcome: ModelDownloadStepOutcome) {
        guard !didComplete else { return }
        didComplete = true
        pollTask?.cancel()
        pollTask = nil
        if let sheet = sheetWindow, let parent = parentWindow {
            parent.endSheet(sheet)
            sheetWindow = nil
        }
        completion(outcome)
    }

    // MARK: - IPC (строго off-main внутри callAsync)

    /// Снимок статуса для UI; nil-результат fetch = ошибка опроса.
    private struct ModelStatusSnapshot {
        let cached: Bool
        let status: String
        let pct: Double
        let downloaded: Int
        let total: Int
        let errorMsg: String
    }

    private static func resultDict(_ resp: [String: Any]) -> [String: Any] {
        return (resp["result"] as? [String: Any]) ?? [:]
    }

    /// Быстрая проверка «модель уже в кэше» при входе в шаг.
    private static func fetchCached(ipc: IPCClient) async -> Bool {
        do {
            let resp = try await ipc.callAsync(
                method: "get_stt_model_status",
                params: [:],
                timeoutSec: IPCClient.quickTimeoutSec
            )
            return (resultDict(resp)["cached"] as? Bool) ?? false
        } catch {
            // Не удалось спросить backend — не блокируем онбординг, показываем шаг.
            NSLog("[ModelDownloadStep] fetchCached error: %@", error.localizedDescription)
            return false
        }
    }

    /// Запуск загрузки. Возвращает true если backend принял команду.
    private static func startDownloadIPC(ipc: IPCClient) async -> Bool {
        do {
            _ = try await ipc.callAsync(
                method: "download_stt_model",
                params: [:],
                timeoutSec: IPCClient.defaultTimeoutSec
            )
            return true
        } catch {
            NSLog("[ModelDownloadStep] download_stt_model error: %@", error.localizedDescription)
            return false
        }
    }

    /// Один опрос статуса; nil = IPC/сетевая ошибка.
    private static func fetchStatus(ipc: IPCClient) async -> ModelStatusSnapshot? {
        do {
            let resp = try await ipc.callAsync(
                method: "get_stt_model_status",
                params: [:],
                timeoutSec: IPCClient.quickTimeoutSec
            )
            let r = resultDict(resp)
            let pct = (r["pct"] as? Double) ?? ((r["pct"] as? NSNumber)?.doubleValue ?? 0)
            return ModelStatusSnapshot(
                cached: (r["cached"] as? Bool) ?? false,
                status: (r["status"] as? String) ?? "idle",
                pct: pct,
                downloaded: (r["downloaded"] as? Int) ?? ((r["downloaded"] as? NSNumber)?.intValue ?? 0),
                total: (r["total"] as? Int) ?? ((r["total"] as? NSNumber)?.intValue ?? 0),
                errorMsg: (r["error_msg"] as? String) ?? ""
            )
        } catch {
            NSLog("[ModelDownloadStep] get_stt_model_status error: %@", error.localizedDescription)
            return nil
        }
    }

    private static func formatMB(_ bytes: Int) -> String {
        let mb = Double(bytes) / (1024.0 * 1024.0)
        return String(format: "%.0f", mb)
    }
}
